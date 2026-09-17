"""Asyncio client for logos-json-rpc-bridge over WebSocket."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed as WsConnectionClosed
from websockets.exceptions import InvalidHandshake, InvalidMessage, InvalidStatus, InvalidURI

from . import _protocol as proto
from ._transport import HeadersLike, build_connect_plan
from .codec import decode_bytes_tags, dumps, encode_args, loads, rejection_from_result
from .errors import (
    DROPPED_UPGRADE_HINTS,
    HTTP_STATUS_HINTS,
    BridgeError,
    ClientTimeout,
    ConnectError,
    ConnectionClosed,
    DiscoveryPending,
    RequestTooLarge,
    RpcError,
    SubscriptionOverflow,
    SubscriptionTerminated,
    warn,
)
from .models import Event, ModuleInfo
from .subscription import AsyncSubscription, SubscribeRequest, _Stream, _SubEntry

if TYPE_CHECKING:
    from .dynamic import DynamicModule

logger = logging.getLogger("logos_bridge")

_NEW: Final = "new"
_CONNECTING: Final = "connecting"
_OPEN: Final = "open"
_CLOSING: Final = "closing"
_CLOSED: Final = "closed"


def _check_timeout(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number, inf or None; got {value!r}")


def _resolve_timeout(timeout: float | None, default: float | None) -> float | None:
    """``None`` means the client default; ``math.inf`` means no deadline."""
    if timeout is None:
        timeout = default
    _check_timeout("timeout", timeout)
    if timeout is None or math.isinf(timeout):
        return None
    return float(timeout)


@dataclass(frozen=True)
class _CloseInfo:
    code: int
    reason: str
    initiated_by: str
    server_error: RpcError | None = None

    def exception(self) -> ConnectionClosed:
        return ConnectionClosed(
            self.code, self.reason, initiated_by=self.initiated_by, server_error=self.server_error
        )

    @classmethod
    def from_ws(cls, exc: WsConnectionClosed | None, server_error: RpcError | None) -> _CloseInfo:
        if exc is None:
            return cls(1006, "", "transport", server_error)
        rcvd, sent = exc.rcvd, exc.sent
        if rcvd is not None:
            by_server = sent is None or bool(exc.rcvd_then_sent)
            return cls(rcvd.code, rcvd.reason, "server" if by_server else "client", server_error)
        if sent is not None:
            # e.g. 1011 "keepalive ping timeout": we failed the connection ourselves.
            return cls(sent.code, sent.reason, "client", server_error)
        return cls(1006, "", "transport", server_error)


class _Pending:
    __slots__ = ("future", "op", "module", "method", "timer")

    def __init__(
        self, future: asyncio.Future[Any], op: str, module: str | None, method: str | None
    ) -> None:
        self.future = future
        self.op = op
        self.module = module
        self.method = method
        self.timer: asyncio.TimerHandle | None = None

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


def _connect_error(url: str, exc: BaseException) -> ConnectError:
    if isinstance(exc, InvalidStatus):
        status = exc.response.status_code
        hint = HTTP_STATUS_HINTS.get(status)
        return ConnectError(
            f"the upgrade was answered with HTTP {status}", url=url, status=status,
            hints=(hint,) if hint else (),
        )
    if isinstance(exc, (InvalidMessage, WsConnectionClosed, EOFError)):
        # A refused upgrade is dropped without any HTTP answer.
        return ConnectError("the connection was dropped during the WebSocket upgrade", url=url,
                            hints=DROPPED_UPGRADE_HINTS)
    if isinstance(exc, InvalidURI):
        return ConnectError(f"invalid URI: {exc}", url=url)
    if isinstance(exc, InvalidHandshake):
        return ConnectError(f"WebSocket handshake failed: {exc}", url=url,
                            hints=("is this a logos-json-rpc-bridge endpoint (ws://<host>:<port>/ws)?",))
    if isinstance(exc, ConnectionRefusedError):
        return ConnectError("connection refused", url=url, hints=(
            "nothing listens there: is json_rpc_bridge loaded and started "
            "(json_rpc_bridge.start(config)), and is http.port the port in the URL?",
        ))
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ConnectError("no WebSocket handshake within open_timeout", url=url, hints=(
            "the bridge's service thread may be stalled, or the address is filtered",
        ))
    if isinstance(exc, OSError):
        return ConnectError(f"{type(exc).__name__}: {exc}", url=url)
    return ConnectError(f"{type(exc).__name__}: {exc}", url=url)


class AsyncBridgeClient:
    """A JSON-RPC 2.0 client for one bridge WebSocket connection.

    Use it with ``async with`` (or :meth:`connect` / :meth:`aclose`). One reader task
    owns the socket; it never blocks, so unconsumed events accumulate in memory rather
    than tripping the bridge's slow-reader close (1008). The client is bound to the event
    loop it connected on and does not reconnect: after a disconnect, create a new one.

    Timeouts: ``call_timeout`` is the default for :meth:`call` (``None``: wait for the
    bridge, which has its own ``call_timeout_ms``); ``op_timeout`` for everything else.
    A per-call ``timeout=math.inf`` disables the deadline.
    """

    def __init__(
        self,
        url: str = proto.DEFAULT_URL,
        *,
        open_timeout: float | None = 10.0,
        call_timeout: float | None = None,
        op_timeout: float | None = 30.0,
        close_timeout: float | None = 5.0,
        ping_interval: float | None = 20.0,
        ping_timeout: float | None = 20.0,
        max_message_size: int | None = 64 * 2**20,
        max_request_size: int | None = 2**20,
        host_header: str | None = None,
        subprotocol: str = proto.SUBPROTOCOL,
        extra_headers: HeadersLike | None = None,
    ) -> None:
        for name, value in (
            ("open_timeout", open_timeout),
            ("call_timeout", call_timeout),
            ("op_timeout", op_timeout),
            ("close_timeout", close_timeout),
            ("ping_interval", ping_interval),
            ("ping_timeout", ping_timeout),
        ):
            _check_timeout(name, value)
        for name, size in (("max_message_size", max_message_size), ("max_request_size", max_request_size)):
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        self._url = url
        self._plan = build_connect_plan(
            url,
            subprotocol=subprotocol,
            open_timeout=open_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            close_timeout=close_timeout,
            max_message_size=max_message_size,
            host_header=host_header,
            extra_headers=extra_headers,
        )
        self._subprotocol = subprotocol
        self._call_timeout = call_timeout
        self._op_timeout = op_timeout
        self._max_request_size = max_request_size

        self._state: str = _NEW
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._subs: dict[str, _SubEntry] = {}
        self._sub_prefix = f"s{secrets.token_hex(4)}-"
        self._sub_counter = itertools.count(1)
        self._background: set[asyncio.Future[Any]] = set()
        self._closed_event = asyncio.Event()
        self._close_info: _CloseInfo | None = None
        self._unattached_server_error: RpcError | None = None

        #: The last error the bridge sent with ``id: null`` (it could not tell which request failed).
        self.last_uncorrelated_error: RpcError | None = None
        #: Responses that arrived after their request timed out or was cancelled.
        self.late_responses = 0
        #: Frames that could not be interpreted (binary, malformed, unknown).
        self.ignored_frames = 0
        #: Events for subscription ids this client no longer routes.
        self.unrouted_events = 0

    def __repr__(self) -> str:
        return f"<AsyncBridgeClient {self._url} {self._state}>"

    # ------------------------------------------------------------------ state

    @property
    def url(self) -> str:
        return self._url

    @property
    def state(self) -> str:
        """``new``, ``connecting``, ``open``, ``closing`` or ``closed``."""
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == _OPEN

    @property
    def closed(self) -> bool:
        return self._state == _CLOSED

    @property
    def close_exception(self) -> ConnectionClosed | None:
        """Why the connection ended (a fresh exception), or ``None`` while it is open."""
        return self._close_info.exception() if self._close_info is not None else None

    @property
    def subprotocol(self) -> str | None:
        """The subprotocol the bridge selected."""
        return self._ws.subprotocol if self._ws is not None else None

    @property
    def in_flight(self) -> int:
        """Requests sent and not yet answered, timed out or cancelled."""
        return len(self._pending)

    @property
    def active_subscriptions(self) -> int:
        """Bridge subscription ids this client currently routes."""
        return sum(1 for e in self._subs.values() if e.state in ("pending", "active"))

    def _check_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("AsyncBridgeClient must be used from a running event loop") from None
        if self._loop is not None and loop is not self._loop:
            raise RuntimeError(
                "AsyncBridgeClient is bound to the event loop it connected on; "
                "use logos_bridge.BridgeClient to drive it from other threads"
            )

    def _require_open(self) -> ClientConnection:
        self._check_loop()
        if self._state == _OPEN and self._ws is not None:
            return self._ws
        if self._close_info is not None:
            raise self._close_info.exception()
        if self._state in (_CLOSING, _CLOSED):
            raise ConnectionClosed(1000, initiated_by="client", hint="the client was closed")
        raise RuntimeError("not connected: call connect() or use 'async with'")

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> AsyncBridgeClient:
        if self._state == _NEW:
            await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def connect(self) -> AsyncBridgeClient:
        """Open the WebSocket. Raises :class:`~logos_bridge.errors.ConnectError` with hints."""
        self._check_loop()
        if self._state == _OPEN:
            return self
        if self._state != _NEW:
            raise RuntimeError(f"cannot connect a client that is {self._state}; create a new client")
        self._state = _CONNECTING
        loop = asyncio.get_running_loop()
        self._loop = loop
        try:
            ws = await ws_connect(self._plan.uri, **self._plan.kwargs)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            self._mark_closed()
            raise
        except BaseException as exc:
            self._mark_closed()
            raise _connect_error(self._url, exc) from exc
        if self._state != _CONNECTING:  # aclose() ran meanwhile
            await ws.close()
            self._mark_closed()
            raise ConnectError("the client was closed while connecting", url=self._url)
        self._ws = ws
        self._state = _OPEN
        if ws.subprotocol != self._subprotocol:
            logger.warning("bridge at %s selected subprotocol %r, not %r", self._url, ws.subprotocol,
                           self._subprotocol)
        self._reader = loop.create_task(self._read_loop(ws), name=f"logos-bridge reader {self._url}")
        return self

    def _mark_closed(self) -> None:
        self._state = _CLOSED
        self._closed_event.set()

    async def aclose(self) -> None:
        """Close the connection (code 1000) and wait for the reader. Idempotent.

        Pending calls and open subscriptions end with :class:`~logos_bridge.errors.ConnectionClosed`.
        """
        if self._state == _NEW:
            self._mark_closed()
            return
        self._check_loop()
        if self._state == _CONNECTING:
            self._state = _CLOSING
            await self._closed_event.wait()
            return
        if self._state == _OPEN and self._ws is not None:
            self._state = _CLOSING
            try:
                await self._ws.close()
            except Exception as exc:  # pragma: no cover - close() is not expected to raise
                logger.debug("close failed: %s", exc)
                self._ws.transport.abort()
        if self._reader is not None:
            await asyncio.wait({self._reader})
        if self._background:
            await asyncio.wait(set(self._background))

    async def wait_closed(self) -> None:
        """Return once the connection has ended, for any reason (see :attr:`close_exception`)."""
        self._check_loop()
        await self._closed_event.wait()

    # ---------------------------------------------------------------- reader

    async def _read_loop(self, ws: ClientConnection) -> None:
        closed: WsConnectionClosed | None = None
        try:
            while True:
                message = await ws.recv()
                try:
                    self._on_message(message)
                except Exception:  # a bug here must not kill the only reader
                    self.ignored_frames += 1
                    logger.exception("logos-bridge: failed to process a frame")
        except WsConnectionClosed as exc:
            closed = exc
        except asyncio.CancelledError:
            ws.transport.abort()
            self._on_disconnect(None, cancelled=True)
            raise
        except Exception:
            logger.exception("logos-bridge: reader failed")
            ws.transport.abort()
        self._on_disconnect(closed)

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            self.ignored_frames += 1
            logger.warning("logos-bridge: ignoring a binary frame from %s", self._url)
            return
        if not message:
            # The bridge's answer to a body made only of notifications; this client sends none.
            return
        try:
            decoded = loads(message)
        except ValueError:
            self.ignored_frames += 1
            logger.warning("logos-bridge: ignoring an unparseable frame: %.200r", message)
            return
        for item in decoded if isinstance(decoded, list) else (decoded,):
            self._dispatch(proto.classify(item))

    def _dispatch(self, frame: proto.Frame) -> None:
        kind = frame.kind
        if kind is proto.FrameKind.RESULT or kind is proto.FrameKind.ERROR:
            pending = self._pending.pop(frame.id, None) if proto.is_json_int(frame.id) else None
            if pending is None:
                self.late_responses += 1
                logger.debug("logos-bridge: dropping a response for id %r", frame.id)
                return
            pending.cancel_timer()
            if pending.future.done():
                return
            if kind is proto.FrameKind.RESULT:
                pending.future.set_result(frame.result)
            else:
                pending.future.set_exception(RpcError.from_error(
                    frame.error, request_id=frame.id, op=pending.op,
                    module=pending.module, method=pending.method,
                ))
        elif kind is proto.FrameKind.EVENT:
            self._route_event(frame.params)
        elif kind is proto.FrameKind.TERMINATED:
            self._route_termination(frame.params)
        elif kind is proto.FrameKind.UNCORRELATED_ERROR:
            error = RpcError.from_error(frame.error)
            self.last_uncorrelated_error = error
            self._unattached_server_error = error
            warn(f"the bridge sent an error it could not attribute to a request: {error}")
        else:
            self.ignored_frames += 1
            logger.debug("logos-bridge: ignoring %s frame (%s)", kind.value, frame.problem or frame.method)

    def _route_event(self, params: dict[str, Any]) -> None:
        sid = params.get("subscription")
        entry = self._subs.get(sid) if isinstance(sid, str) else None
        if entry is None:
            self.unrouted_events += 1
            return
        if entry.state not in ("pending", "active"):
            return
        stream = entry.stream
        if stream.ended:
            return
        if stream.full():
            assert stream.max_pending is not None
            max_pending = stream.max_pending
            ids = [e.sid for e in stream.entries]
            stream.end(lambda: SubscriptionOverflow(max_pending, ids))
            self._release_stream(stream)
            return
        generation = params.get("generation")
        ts = params.get("ts")
        module = params.get("module")
        event = params.get("event")
        stream.push(Event(
            subscription=entry.sid,
            module=module if isinstance(module, str) else entry.module,
            event=event if isinstance(event, str) else entry.event,
            data=params.get("data"),
            generation=generation if proto.is_json_int(generation) else 0,
            ts=ts if proto.is_json_int(ts) else 0,
        ))

    def _route_termination(self, params: dict[str, Any]) -> None:
        raw_sid = params.get("subscription")
        entry = self._subs.get(raw_sid) if isinstance(raw_sid, str) else None
        if entry is None:
            return
        sid = entry.sid
        del self._subs[entry.sid]
        was_zombie = entry.state == "zombie"
        entry.state = "terminated"
        if was_zombie:
            return
        reason = params.get("reason")
        module, event = entry.module, entry.event
        reason_text = reason if isinstance(reason, str) else "unknown"
        entry.stream.end(lambda: SubscriptionTerminated(sid, module, event, reason_text, raw=params))
        self._release_stream(entry.stream)

    def _detach(self, stream: _Stream) -> list[str]:
        """Stop routing an ended stream's ids; returns the acknowledged ones to unsubscribe."""
        live: list[str] = []
        for entry in stream.entries:
            if entry.state == "active":
                entry.state = "released"
                self._subs.pop(entry.sid, None)
                live.append(entry.sid)
            elif entry.state == "pending":
                entry.state = "doomed"  # _subscribe() unsubscribes it once acked
        return live

    def _release_stream(self, stream: _Stream) -> None:
        for sid in self._detach(stream):
            self._spawn_unsubscribe(sid)

    def _on_disconnect(self, exc: WsConnectionClosed | None, *, cancelled: bool = False) -> None:
        if self._close_info is not None:
            return
        server_error, self._unattached_server_error = self._unattached_server_error, None
        info = _CloseInfo.from_ws(exc, server_error)
        if cancelled and exc is None:
            info = _CloseInfo(1006, "reader cancelled", "client", server_error)
        self._close_info = info
        self._state = _CLOSED
        pending, self._pending = self._pending, {}
        for p in pending.values():
            p.cancel_timer()
            if not p.future.done():
                p.future.set_exception(info.exception())
        subs, self._subs = self._subs, {}
        for entry in subs.values():
            if entry.state in ("pending", "active", "doomed"):
                entry.state = "released"
            entry.stream.end(info.exception)
        self._closed_event.set()
        logger.debug("logos-bridge: %s", info.exception())

    # --------------------------------------------------------------- requests

    def _track(self, fut: asyncio.Future[Any]) -> None:
        self._background.add(fut)
        fut.add_done_callback(self._untrack)

    def _untrack(self, fut: asyncio.Future[Any]) -> None:
        self._background.discard(fut)
        if not fut.cancelled():
            exc = fut.exception()
            if exc is not None and not isinstance(exc, (BridgeError, WsConnectionClosed)):
                logger.debug("logos-bridge: background task failed: %r", exc)

    def _expire(self, req_id: int, timeout: float) -> None:
        pending = self._pending.pop(req_id, None)
        if pending is None or pending.future.done():
            return
        pending.timer = None
        pending.future.set_exception(ClientTimeout(
            timeout, op=pending.op, module=pending.module, method=pending.method, request_id=req_id
        ))

    async def _send_request(
        self,
        op: str,
        params: Any,
        *,
        timeout: float | None,
        module: str | None = None,
        method: str | None = None,
    ) -> Any:
        ws = self._require_open()
        loop = asyncio.get_running_loop()
        req_id = next(self._ids)
        frame = dumps(proto.make_request(req_id, op, params))
        if self._max_request_size is not None and len(frame) > self._max_request_size:
            raise RequestTooLarge(len(frame), self._max_request_size)
        pending = _Pending(loop.create_future(), op, module, method)
        self._pending[req_id] = pending
        if timeout is not None:
            pending.timer = loop.call_later(timeout, self._expire, req_id, timeout)
        try:
            # The send runs as a task so the deadline also covers a send stuck on a full socket.
            send = loop.create_task(ws.send(frame, text=True))
            self._track(send)
            await asyncio.wait((send, pending.future), return_when=asyncio.FIRST_COMPLETED)
            if send.done() and not send.cancelled():
                failure = send.exception()
                if failure is not None and not isinstance(failure, WsConnectionClosed):
                    raise failure
                # On ConnectionClosed the reader resolves the future.
            return await pending.future
        finally:
            pending.cancel_timer()
            if self._pending.get(req_id) is pending:
                del self._pending[req_id]
            if not pending.future.done():
                pending.future.cancel()

    def _spawn_unsubscribe(self, sid: str) -> None:
        if self._state != _OPEN or self._loop is None:
            return
        self._track(self._loop.create_task(self._unsubscribe_quietly(sid)))

    async def _unsubscribe_quietly(self, sid: str) -> None:
        try:
            await self._send_request(
                proto.OP_UNSUBSCRIBE, {"subscription": sid},
                timeout=_resolve_timeout(None, self._op_timeout),
            )
        except BridgeError as exc:
            logger.debug("logos-bridge: best-effort unsubscribe of %s failed: %s", sid, exc)
        finally:
            entry = self._subs.get(sid)
            if entry is not None and entry.state == "zombie":
                del self._subs[sid]

    # ----------------------------------------------------------- public ops

    async def request(self, op: str, params: Any = None, *, timeout: float | None = None) -> Any:
        """Send a raw bridge operation and return its ``result``.

        ``rpc.subscribe``/``rpc.unsubscribe`` are refused: use :meth:`subscribe`.
        """
        if op in proto.STREAM_OPS:
            raise ValueError(f"{op} needs subscription routing; use subscribe()/subscribe_many()")
        return await self._send_request(op, params, timeout=_resolve_timeout(timeout, self._op_timeout))

    async def call(
        self,
        module: str,
        method: str,
        *args: Any,
        timeout: float | None = None,
        decode_bytes: bool = True,
        detect_rejection: bool = True,
    ) -> Any:
        """Call ``module.method(*args)`` through ``rpc.call``.

        ``bytes`` arguments travel as ``{"_bytes": ...}``; with ``decode_bytes`` such tags in
        the result come back as ``bytes``. A provider refusal raises
        :class:`~logos_bridge.errors.ProviderRejection` unless ``detect_rejection`` is false.
        An application failure (``{"success": false, ...}``) is returned, not raised; see
        :func:`~logos_bridge.codec.unwrap_result`.
        """
        if not isinstance(module, str) or not module or not isinstance(method, str) or not method:
            raise ValueError("module and method must be non-empty strings")
        params = {"module": module, "method": method, "params": encode_args(args)}
        result = await self._send_request(
            proto.OP_CALL, params, timeout=_resolve_timeout(timeout, self._call_timeout),
            module=module, method=method,
        )
        if detect_rejection:
            rejection = rejection_from_result(result, module=module, method=method)
            if rejection is not None:
                raise rejection
        return decode_bytes_tags(result) if decode_bytes else result

    async def call_encoded(
        self, module: str, method: str, params: list[Any], *, timeout: float | None = None
    ) -> Any:
        """``rpc.call`` with ``params`` sent exactly as given (already wire JSON).

        The result comes back untouched: no bytes decoding, no rejection check.
        This is what typed clients use after encoding against a contract.
        """
        if not isinstance(module, str) or not module or not isinstance(method, str) or not method:
            raise ValueError("module and method must be non-empty strings")
        if not isinstance(params, list):
            raise TypeError("params must be a list of JSON values")
        return await self._send_request(
            proto.OP_CALL, {"module": module, "method": method, "params": params},
            timeout=_resolve_timeout(timeout, self._call_timeout), module=module, method=method,
        )

    def subscribe(
        self, module: str, event: str, *, timeout: float | None = None, max_pending: int | None = None
    ) -> SubscribeRequest:
        """Subscribe to one event. ``await`` the result, or use it with ``async with``."""
        return SubscribeRequest(self, [(module, event)], timeout=timeout, max_pending=max_pending)

    def subscribe_many(
        self,
        module: str,
        events: Iterable[str],
        *,
        timeout: float | None = None,
        max_pending: int | None = None,
    ) -> SubscribeRequest:
        """Subscribe to several events of one module as ONE stream, in arrival order."""
        if isinstance(events, str):
            raise TypeError("events must be an iterable of event names, not a string")
        names = list(events)
        if not names:
            raise ValueError("subscribe_many() needs at least one event")
        if len(set(names)) != len(names):
            raise ValueError("subscribe_many() got a duplicate event name")
        return SubscribeRequest(self, [(module, e) for e in names], timeout=timeout, max_pending=max_pending)

    async def ping(self) -> float:
        """``rpc.ping``; returns the round-trip time in seconds."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await self._send_request(proto.OP_PING, None, timeout=_resolve_timeout(None, self._op_timeout))
        if result != "pong":
            raise BridgeError(f"unexpected rpc.ping answer: {result!r}")
        return loop.time() - started

    async def list_modules(self) -> list[ModuleInfo]:
        """``rpc.list_modules``: every exposed module's view."""
        result = await self._send_request(
            proto.OP_LIST_MODULES, None, timeout=_resolve_timeout(None, self._op_timeout)
        )
        if not isinstance(result, list):
            raise BridgeError(f"unexpected rpc.list_modules answer: {result!r:.200}")
        return [ModuleInfo.from_json(item) for item in result]

    async def schema(self, module: str) -> ModuleInfo:
        """``rpc.schema``. An unknown or unexposed module raises :class:`~logos_bridge.errors.MethodNotFound`."""
        result = await self._send_request(
            proto.OP_SCHEMA, {"module": module}, timeout=_resolve_timeout(None, self._op_timeout),
        )
        return ModuleInfo.from_json(result)

    async def wait_for_module(self, module: str, timeout: float | None = 10.0) -> ModuleInfo:
        """``rpc.schema`` until the module's ``interface_status`` is no longer ``pending``.

        Raises :class:`~logos_bridge.errors.DiscoveryPending` if it still is after
        ``timeout`` seconds (``None``/``inf``: wait for ever). Bridges without
        ``lidl()`` discovery have no status and return at once.
        """
        _check_timeout("timeout", timeout)
        loop = asyncio.get_running_loop()
        limit = None if timeout is None or math.isinf(timeout) else float(timeout)
        deadline = None if limit is None else loop.time() + limit
        delay = 0.05
        while True:
            info = await self.schema(module)
            if not info.pending:
                return info
            now = loop.time()
            if deadline is not None and now >= deadline:
                raise DiscoveryPending(module, limit or 0.0)
            pause = delay if deadline is None else min(delay, deadline - now)
            await asyncio.sleep(pause)
            delay = min(delay * 2, 0.5)

    async def module(self, name: str, *, require_typed: bool = False,
                     discovery_wait: float | None = 10.0) -> DynamicModule:
        """A :class:`~logos_bridge.dynamic.DynamicModule` for ``name``.

        Typed when the bridge serves a valid contract; otherwise names-only with a
        :class:`~logos_bridge.errors.BridgeWarning`, or
        :class:`~logos_bridge.errors.UntypedModule` when ``require_typed``.
        ``pending`` is waited out for up to ``discovery_wait`` seconds.
        """
        from .dynamic import DynamicModule

        info = await self.wait_for_module(name, discovery_wait)
        return DynamicModule.from_module_info(self, info, require_typed=require_typed)

    # ---------------------------------------------------------- subscriptions

    def _new_sub_id(self) -> str:
        return f"{self._sub_prefix}{next(self._sub_counter)}"

    async def _subscribe(
        self, targets: Sequence[tuple[str, str]], timeout: float | None, max_pending: int | None
    ) -> AsyncSubscription:
        self._require_open()
        loop = asyncio.get_running_loop()
        limit = _resolve_timeout(timeout, self._op_timeout)
        deadline = None if limit is None else loop.time() + limit
        stream = _Stream(max_pending)
        for module, event in targets:
            # Registered before the request goes out: events may precede the ack.
            entry = _SubEntry(self._new_sub_id(), module, event, stream)
            self._subs[entry.sid] = entry
            stream.entries.append(entry)
        current: _SubEntry | None = None
        try:
            for entry in stream.entries:
                if entry.state == "doomed":  # the stream ended before this one was sent
                    entry.state = "released"
                    self._subs.pop(entry.sid, None)
                if entry.state != "pending":
                    continue
                current = entry
                ack = await self._send_request(
                    proto.OP_SUBSCRIBE,
                    {"subscription": entry.sid, "module": entry.module, "event": entry.event},
                    timeout=None if deadline is None else max(0.0, deadline - loop.time()),
                    module=entry.module,
                )
                current = None
                entry.ack = ack if isinstance(ack, dict) else {"result": ack}
                if entry.state == "doomed":
                    entry.state = "released"
                    self._subs.pop(entry.sid, None)
                    self._spawn_unsubscribe(entry.sid)
                elif entry.state == "pending":
                    entry.state = "active"
        except BaseException as exc:
            self._abort_subscribe(stream, current, exc)
            raise
        return AsyncSubscription(self, stream)

    def _abort_subscribe(self, stream: _Stream, current: _SubEntry | None, exc: BaseException) -> None:
        stream.end(None)  # never handed to the caller
        for entry in stream.entries:
            if entry is current and entry.state in ("pending", "doomed"):
                if isinstance(exc, (ClientTimeout, asyncio.CancelledError)):
                    # The bridge may have registered it: keep routing to drop its events.
                    entry.state = "zombie"
                    self._spawn_unsubscribe(entry.sid)
                    if self._state != _OPEN:
                        self._subs.pop(entry.sid, None)
                else:
                    # An error ack (or nothing sent): the id is discarded for good.
                    entry.state = "failed"
                    self._subs.pop(entry.sid, None)
            elif entry.state == "active":
                entry.state = "released"
                self._subs.pop(entry.sid, None)
                self._spawn_unsubscribe(entry.sid)
            elif entry.state in ("pending", "doomed"):
                entry.state = "failed"
                self._subs.pop(entry.sid, None)

    async def _close_stream(self, stream: _Stream) -> None:
        self._check_loop()
        if stream.ended:
            return
        stream.end(None)
        timeout = _resolve_timeout(None, self._op_timeout)
        results = await asyncio.gather(
            *(self._send_request(proto.OP_UNSUBSCRIBE, {"subscription": sid}, timeout=timeout)
              for sid in self._detach(stream)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, ConnectionClosed):
                raise result

    def _close_stream_nowait(self, stream: _Stream) -> None:
        if not stream.ended:
            stream.end(None)
            self._release_stream(stream)
