"""A protocol-faithful fake of logos-json-rpc-bridge (WebSocket + minimal HTTP).

It mirrors ``json_rpc_bridge_impl.cpp``, ``rpc_dispatcher.h``, ``ws_server.cpp``,
``discovery.h`` and ``error_map.h``: the same ops, result shapes, error codes and
constant messages, null-id errors for unparseable input, the batch cap, the
duplicate-id ``"active"`` ack, acknowledged unknown unsubscribes, and the close codes
1003/1008/1009 (1006 on stop). Frames are serialised with sorted keys, as the
bridge's nlohmann::json does. Upgrades the bridge would refuse are dropped without
an HTTP answer. The HTTP listener runs on its own port (``http_url``).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import itertools
import logging
import tempfile
import time
import urllib.parse
from pathlib import Path
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from .. import _protocol as proto
from ..codec import encode_value, loads
from ..digest import canonical_json, contract_sha256, interface_sha256
from ..lidl import IDENTITY_METHODS, Interface

logger = logging.getLogger("logos_bridge.testing")
_ws_logger = logging.getLogger("logos_bridge.testing.websockets")

_DEFAULT: Any = object()
BUILTIN_METHODS: Final = ("lidl", "name", "version")
_LOOPBACK_HOSTS: Final = ("127.0.0.1", "localhost", "[::1]")
_HTML: Final = b"<!DOCTYPE html><html><body><h1>%d</h1></body></html>"
# ws_server.cpp: how long a connection lingers after an answer that left a body unread.
_LINGER_SECONDS: Final = 5.0
_BODY_METHODS: Final = ("POST", "PUT", "PATCH")


def sha256_text(text: str) -> str:
    return contract_sha256(text)


def _parse_contract(text: str, cli: str | None, name: str) -> Interface:
    from ..lidl_sources import LidlCli

    with tempfile.TemporaryDirectory(prefix="fake-bridge-") as tmp:
        path = Path(tmp) / f"{name}.lidl"
        path.write_text(text, encoding="utf-8", newline="")
        return Interface.from_json(LidlCli(cli).json(path, identity=True))


# -- Scripted outcomes -------------------------------------------------------


@dataclass(frozen=True)
class FakeError:
    """Answer with a JSON-RPC error. ``message``/``data`` default to the bridge's own."""

    code: int
    message: str | None = None
    data: Any = _DEFAULT

    def to_error(self) -> dict[str, Any]:
        spec = proto.ERROR_TABLE.get(self.code)
        message = self.message if self.message is not None else (spec.message if spec else "error")
        error: dict[str, Any] = {"code": self.code, "message": message}
        if self.data is _DEFAULT:
            if spec is not None:
                error["data"] = spec.data()
        elif self.data is not None:
            error["data"] = self.data
        return error

    @classmethod
    def upstream(cls, call_error_code: str) -> FakeError:
        """The bridge's answer for an upstream ``logos::CallError`` code (``timeout``, ...)."""
        return cls(proto.map_call_error(call_error_code).code)


@dataclass(frozen=True)
class Reject:
    """Answer with a provider refusal *result* (``{"code", "message", "origin"}``)."""

    code: str
    message: str
    origin: str

    def to_result(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "origin": self.origin}


@dataclass(frozen=True)
class Wire:
    """Answer with ``value`` exactly as given: JSON the fake does not re-encode."""

    value: Any


class _NoResponseType:
    _instance: _NoResponseType | None = None

    def __new__(cls) -> _NoResponseType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NoResponse"


NoResponse: Final = _NoResponseType()
"""Never answer (the request stays pending, as a lost upstream reply would)."""


@dataclass(frozen=True)
class Delay:
    """Answer with ``then`` (any outcome, or a handler) after ``seconds``."""

    seconds: float
    then: Any = None


_OUTCOME_TYPES: Final = (FakeError, Reject, Delay, Wire, _NoResponseType)


# -- Recording ---------------------------------------------------------------


@dataclass(frozen=True)
class RecordedRequest:
    """One JSON-RPC request object the fake handled."""

    connection: int | None
    transport: str
    raw: Any
    text: str

    @property
    def method(self) -> str | None:
        method = self.raw.get("method") if isinstance(self.raw, dict) else None
        return method if isinstance(method, str) else None

    @property
    def id(self) -> Any:
        return self.raw.get("id") if isinstance(self.raw, dict) else None

    @property
    def has_id(self) -> bool:
        return isinstance(self.raw, dict) and "id" in self.raw

    @property
    def params(self) -> Any:
        return self.raw.get("params") if isinstance(self.raw, dict) else None


def _header_lookup(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    lowered = name.lower()
    return [v for k, v in headers if k.lower() == lowered]


@dataclass(frozen=True)
class _Framing:
    """What a request's headers say about its body (ws_server.cpp's ``RequestFraming``)."""

    transfer_encoding: bool
    length: str | None  # repeated Content-Length headers joined with ", ", as lws does
    body_method: bool

    @classmethod
    def of(cls, method: str, headers: Sequence[tuple[str, str]]) -> _Framing:
        return cls(
            transfer_encoding=any(_header_lookup(headers, "Transfer-Encoding")),
            length=", ".join(_header_lookup(headers, "Content-Length")) or None,
            body_method=method in _BODY_METHODS,
        )

    @property
    def body_follows(self) -> bool:
        """Whether a body follows the headers (``bodyFollows``)."""
        if self.transfer_encoding:
            return True
        if self.length is not None:
            return self.length.strip("0") != ""
        return self.body_method

    @property
    def usable_length(self) -> int | None:
        """The one body the bridge reads: a plain Content-Length, without Transfer-Encoding."""
        if self.transfer_encoding or self.length is None or not (self.length.isascii() and self.length.isdigit()):
            return None
        return int(self.length)


def _asks_to_close(headers: Sequence[tuple[str, str]]) -> bool:
    return any(token.strip().lower() == "close"
               for value in _header_lookup(headers, "Connection") for token in value.split(","))


@dataclass(frozen=True)
class Handshake:
    """One WebSocket upgrade request, and whether the fake accepted it."""

    path: str
    headers: tuple[tuple[str, str], ...]
    remote: tuple[Any, ...] | None
    accepted: bool
    refusal: str | None
    connection: int | None

    def header(self, name: str) -> str | None:
        values = _header_lookup(self.headers, name)
        return values[0] if values else None

    def header_all(self, name: str) -> list[str]:
        return _header_lookup(self.headers, name)


@dataclass(frozen=True)
class HttpExchange:
    """One HTTP request to the fake's HTTP listener and the status it got."""

    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    status: int | None

    def header(self, name: str) -> str | None:
        values = _header_lookup(self.headers, name)
        return values[0] if values else None

    def header_all(self, name: str) -> list[str]:
        return _header_lookup(self.headers, name)


@dataclass(frozen=True)
class FakeSubscription:
    connection: int
    subscription: Any
    module: str
    event: str


# -- Contexts handed to handlers ---------------------------------------------


@dataclass
class CallContext:
    """What an ``on_call`` handler receives."""

    bridge: FakeBridge
    module: str
    method: str
    params: list[Any]
    raw_params: Any
    request_id: Any
    transport: str
    connection: FakeConnection | None

    def emit(self, event: str, *data: Any, module: str | None = None) -> int:
        """Emit ``event`` from this module (or ``module``) to every subscriber."""
        return self.bridge.emit(module or self.module, event, *data)


@dataclass
class SubscribeContext:
    """What an ``on_subscribe`` handler receives. ``emit`` targets the subscribed event."""

    bridge: FakeBridge
    module: str
    event: str
    subscription: Any
    request_id: Any
    connection: FakeConnection
    defer_emits: bool
    deferred: list[tuple[Any, ...]] = field(default_factory=list)

    def emit(self, *data: Any) -> int:
        """Emit the subscribed event; unless ``emit_events_before_subscribe_ack`` is set,
        the events are sent right after the ack."""
        if self.defer_emits:
            self.deferred.append(data)
            return 0
        return self.bridge.emit(self.module, self.event, *data)


# -- Modules and connections -------------------------------------------------


def _method_table(methods: Iterable[Any] | Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    if isinstance(methods, Mapping):
        return {str(k): list(v) for k, v in methods.items()}
    table: dict[str, list[str]] = {}
    for item in methods:
        if isinstance(item, str):
            table[item] = []
        else:
            name, params = item
            table[str(name)] = list(params)
    return table


_INCONSISTENT_ERROR: Final = "the contract does not match the running module (see cross_check)"
STATUSES: Final = (None, "pending", "ok", "untyped", "invalid")


@dataclass
class FakeModule:
    """A scripted exposed module: the bridge's view of it.

    ``status`` ``None`` imitates a bridge without ``lidl()`` discovery (the old view,
    without ``interface_*`` keys); ``pending``/``ok``/``untyped``/``invalid`` give the
    full view. A typed module (``ok`` with a ``contract``) dispatches by its
    declarations: only ``exposure`` is callable (by default every declared method and
    event; the built-ins always), and by-name parameters follow the declarations.
    ``exposure`` stands in for the bridge's policy.
    """

    name: str
    methods: dict[str, list[str]]
    events: list[str]
    resolved: bool = True
    events_declared: bool = True
    interface: dict[str, Any] | None = None
    status: str | None = None
    exposure: dict[str, list[str]] | None = None
    interface_error: str | None = None
    contract_sha256: str | None = None
    cross_check: dict[str, Any] | None = None
    stale: bool = False
    lidl_text: str | None = None
    contract: Interface | None = None

    @property
    def typed(self) -> bool:
        return self.status == "ok" and self.contract is not None

    @property
    def discovered(self) -> bool:
        """The bridge has a live report (``resolved`` in the view)."""
        return self.resolved and self.status != "pending"

    def _declared(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        assert self.contract is not None
        return self.contract.method_names, self.contract.event_names

    def exposed(self) -> dict[str, list[str]]:
        if not self.discovered:
            return {"methods": [], "events": []}
        if self.exposure is not None:
            methods = list(self.exposure.get("methods", []))
            if self.typed:
                declared, _ = self._declared()
                methods += [b for b in IDENTITY_METHODS if b in declared and b not in methods]
            return {"methods": methods, "events": list(self.exposure.get("events", []))}
        if self.typed:
            declared_methods, declared_events = self._declared()
            return {"methods": list(declared_methods), "events": list(declared_events)}
        return {"methods": list(self.methods), "events": list(self.events)}

    def method_permitted(self, method: str) -> bool:
        if self.typed:
            return method in self.exposed()["methods"]
        if self.status is not None and method in BUILTIN_METHODS and method in self.methods:
            return True  # built-ins bypass policy, not existence
        if self.exposure is not None:
            return method in self.exposure.get("methods", [])
        # discovery.h: an unresolved or empty interface does not refuse.
        if not self.discovered or not self.methods:
            return True
        return method in self.methods

    def event_permitted(self, event: str) -> bool:
        if self.typed:
            return event in self.exposed()["events"]
        if self.exposure is not None:
            return event in self.exposure.get("events", [])
        if not self.discovered or not self.events_declared:
            return True
        return event in self.events

    def to_positional(self, method: str, by_name: Mapping[str, Any]) -> tuple[list[Any] | None, str]:
        if self.typed:
            assert self.contract is not None
            declared = self.contract.method(method)
            if declared is None:
                return None, method
            names = [p.name for p in declared.params]
            for key in sorted(by_name):  # nlohmann iterates object keys sorted
                if key not in names:
                    return None, key
            ordered: list[Any] = []
            for param in declared.params:
                if param.name in by_name:
                    ordered.append(by_name[param.name])
                elif param.optional:
                    ordered.append(None)
                else:
                    return None, param.name
            return ordered, ""
        order = self.methods.get(method) if self.discovered else None
        if order is None:
            return None, method
        for key in sorted(by_name):
            if key not in order:
                return None, key
        return [by_name.get(n) for n in order], ""

    def _policy_allows(self, name: str, kind: str) -> bool:
        if self.exposure is None and not self.typed:
            return True
        if kind == "methods" and name in BUILTIN_METHODS:
            return True
        if self.typed:
            declared_methods, declared_events = self._declared()
            if name not in (declared_methods if kind == "methods" else declared_events):
                return True  # an undeclared live name: the policy is not modelled
        return name in self.exposed()[kind]

    def describe(self, *, full: bool = True) -> dict[str, Any]:
        """``rpc.schema`` (``full``) or ``rpc.list_modules`` view."""
        discovered = self.discovered
        view: dict[str, Any] = {
            "module": self.name,
            "resolved": discovered,
            "events_declared": self.events_declared and discovered,
            "methods": [m for m in self.methods if self._policy_allows(m, "methods")] if discovered else [],
            "events": [e for e in self.events if self._policy_allows(e, "events")] if discovered else [],
            "source": "lidl" if self.typed else "getPluginInterface",
            "authoritative": False,
        }
        if self.status is None:
            return view
        view.update({
            "interface_status": self.status,
            "stale": self.stale,
            "exposure": self.exposed(),
            "interface_sha256": interface_sha256(self.interface) if self.typed else None,
            "contract_sha256": self.contract_sha256,
            "cross_check": self.cross_check,
        })
        if self.typed and full:
            view["interface"] = self.interface
        if self.status == "invalid":
            view["interface_error"] = self.interface_error or _INCONSISTENT_ERROR
        return view

    def served_identity(self) -> tuple[Any, ...]:
        """What a bridge's revalidation compares: a change means a different build."""
        return (self.status, self.describe().get("interface_sha256"), self.contract_sha256,
                tuple(self.methods), tuple(self.events))

    def builtin_answer(self, method: str) -> Any:
        """``name``/``version``/``lidl`` as a generated provider answers them."""
        assert self.contract is not None
        if method == "name":
            return self.contract.name
        if method == "version":
            return self.contract.version or "1.0.0"
        if self.lidl_text is None:
            return FakeError(-32603)
        return self.lidl_text


@dataclass
class _Subscriber:
    subscription: Any
    module: str
    event: str


class FakeConnection:
    """One WebSocket connection to the fake."""

    def __init__(self, cid: int, ws: ServerConnection) -> None:
        self.id = cid
        self.ws = ws
        remote = ws.remote_address
        self.remote: tuple[Any, ...] = tuple(remote) if remote else ()
        self.open = True
        self.subs: dict[str, tuple[str, str]] = {}  # claimed ids (the bridge's Conn::subs)
        self.subscribers: dict[str, _Subscriber] = {}  # ids that receive events
        self.outbound: deque[str | bytes] = deque()
        self.wakeup = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason = ""
        self.frames_sent = 0
        self.closed: ConnectionClosed | None = None
        self.writer: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"<FakeConnection {self.id} {'open' if self.open else 'closed'}>"

    @property
    def peer(self) -> str:
        return str(self.remote[0]) if self.remote else ""

    @property
    def subprotocol(self) -> str | None:
        return self.ws.subprotocol

    @property
    def request_headers(self) -> tuple[tuple[str, str], ...]:
        request = self.ws.request
        return tuple(request.headers.raw_items()) if request is not None else ()


class _Batch:
    """Collects a body's responses and emits them once all are in (json_rpc_bridge_impl.cpp Batch)."""

    def __init__(self, sink: Callable[[str], None], expected: int, single: bool, rest_shape: bool = False) -> None:
        self._sink = sink
        self._slots: list[dict[str, Any] | None] = [None] * expected
        self._remaining = expected
        self._single = single
        self._rest_shape = rest_shape

    def fill(self, slot: int, response: dict[str, Any]) -> None:
        if self._slots[slot] is not None:
            return
        self._slots[slot] = response
        self._remaining -= 1
        if self._remaining == 0:
            self._flush()

    def _flush(self) -> None:
        out = [s for s in self._slots if s is not None]
        if not out:
            self._sink("")
            return
        if self._rest_shape:
            shaped: dict[str, Any] = {}
            if "error" in out[0]:
                shaped["error"] = out[0]["error"]
            elif "result" in out[0]:
                shaped["result"] = out[0]["result"]
            self._sink(_text(shaped))
            return
        self._sink(_text(out[0] if self._single and len(out) == 1 else out))


def _text(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


def _error(req_id: Any, error: Mapping[str, Any]) -> dict[str, Any]:
    return proto.make_error(req_id, error)


def _invalid_request(message: str) -> dict[str, Any]:
    return proto.ERROR_TABLE[-32600].to_error(message)


def _invalid_params(message: str) -> dict[str, Any]:
    return proto.ERROR_TABLE[-32602].to_error(message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# -- The fake ----------------------------------------------------------------


class FakeBridge:
    """An asyncio fake of the bridge. Use ``async with FakeBridge() as fake:``.

    Script modules with :meth:`module`, answers with :meth:`on_call` /
    :meth:`on_subscribe`, and push traffic with :meth:`emit`, :meth:`terminate` and
    :meth:`send_raw`. Everything it handled is recorded in :attr:`requests`,
    :attr:`handshakes`, :attr:`http_requests` and :attr:`connections`.

    ``host_port`` is the port the Host check expects (default: the listening port), to
    imitate a bridge reached through a tunnel. ``poison_failed_subscribe_ids``
    reproduces bridges that keep a failed subscription id claimed, so a retry with the
    same id is acked ``"active"`` and never delivers. ``emit_events_before_subscribe_ack``
    sends events emitted from ``on_subscribe`` before the ack.

    The HTTP listener follows ws_server.cpp's framing rules: every method but ``POST`` is
    served as ``GET``; a ``POST`` needs a plain ``Content-Length`` and no
    ``Transfer-Encoding`` (411 otherwise); and an answer that leaves a declared body unread
    (a refusal, or a ``GET`` with a body) says ``Connection: close``, then drops what the
    client still sends for up to 5 s before closing. Two differences are known: the refusal
    pages' markup, and pipelined requests, which the fake answers in order and the bridge
    (libwebsockets 4.3.5) does not answer.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        http: bool = True,
        http_port: int = 0,
        host_port: int | None = None,
        subprotocol: str = proto.SUBPROTOCOL,
        allowed_origins: Sequence[str] = (),
        auth_mode: str = "none",
        max_connections: int = 128,
        max_connections_per_peer: int = 8,
        max_body_bytes: int = 1 << 20,
        max_frame_bytes: int = 1 << 20,
        max_batch: int = 32,
        max_subscriptions: int = 256,
        max_queued_frames: int = 256,
        poison_failed_subscribe_ids: bool = False,
        emit_events_before_subscribe_ack: bool = False,
        close_timeout: float = 5.0,
        ping_interval: float | None = None,
        ping_timeout: float | None = None,
    ) -> None:
        if auth_mode not in ("none", "bearer"):
            raise ValueError("auth_mode must be 'none' or 'bearer'")
        self._host = host
        self._requested_port = port
        self._serve_http = http
        self._requested_http_port = http_port
        self.host_port = host_port
        self.subprotocol = subprotocol
        self.allowed_origins = tuple(allowed_origins)
        self.auth_mode = auth_mode
        self.max_connections = max_connections
        self.max_connections_per_peer = max_connections_per_peer
        self.max_body_bytes = max_body_bytes
        self.max_frame_bytes = max_frame_bytes
        self.max_batch = max_batch
        self.max_subscriptions = max_subscriptions
        self.max_queued_frames = max_queued_frames
        self.poison_failed_subscribe_ids = poison_failed_subscribe_ids
        self.emit_events_before_subscribe_ack = emit_events_before_subscribe_ack
        self._close_timeout = close_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

        self.port = 0
        self.http_port = 0
        self.modules: dict[str, FakeModule] = {}
        self.requests: list[RecordedRequest] = []
        self.handshakes: list[Handshake] = []
        self.http_requests: list[HttpExchange] = []
        self.connections: list[FakeConnection] = []
        self.handler_errors: list[BaseException] = []

        self._call_handlers: dict[tuple[str, str], Any] = {}
        self._subscribe_handlers: dict[tuple[str, str], Any] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self._accepted: dict[ServerConnection, FakeConnection] = {}
        self._ids = itertools.count(1)
        self._waiters: list[asyncio.Future[None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._http_writers: set[asyncio.StreamWriter] = set()
        self._ws_server: Server | None = None
        self._http_server: asyncio.Server | None = None
        self._started_at = time.monotonic()
        self._draining = False
        self._frozen = False
        self._stopped = False

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    @property
    def http_url(self) -> str:
        if not self._serve_http:
            raise RuntimeError("this FakeBridge was created with http=False")
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def frozen(self) -> bool:
        return self._frozen

    async def __aenter__(self) -> FakeBridge:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> FakeBridge:
        self._started_at = time.monotonic()
        self._ws_server = await serve(
            self._ws_handler,
            self._host,
            self._requested_port,
            process_request=self._process_request,
            subprotocols=[Subprotocol(self.subprotocol)],
            select_subprotocol=self._select_subprotocol,
            compression=None,
            server_header=None,
            open_timeout=10,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=self._close_timeout,
            max_size=None,
            max_queue=None,
            logger=_ws_logger,
        )
        self.port = int(next(iter(self._ws_server.sockets)).getsockname()[1])
        if self._serve_http:
            self._http_server = await asyncio.start_server(self._http_handler, self._host, self._requested_http_port)
            self.http_port = int(self._http_server.sockets[0].getsockname()[1])
        return self

    async def stop(self) -> None:
        """Drop every connection without a close frame (1006) and stop listening."""
        if self._stopped:
            return
        self._stopped = True
        self.abort_connections()
        if self._ws_server is not None:
            self._ws_server.close(close_connections=False)
            await self._ws_server.wait_closed()
        if self._http_server is not None:
            self._http_server.close()
            for writer in list(self._http_writers):
                writer.transport.abort()
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._http_server is not None:
            await self._http_server.wait_closed()
        self._notify()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _notify(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _wait_until(self, probe: Callable[[], Any], timeout: float, what: Callable[[], str]) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = probe()
            if found is not None:
                return found
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"FakeBridge: timed out after {timeout:g}s waiting for {what()}")
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            await asyncio.wait({waiter}, timeout=remaining)
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    # -------------------------------------------------------------- scripting

    def module(
        self,
        name: str,
        methods: Iterable[Any] | Mapping[str, Sequence[str]] | None = None,
        events: Iterable[str] | None = None,
        *,
        resolved: bool = True,
        events_declared: bool = True,
        interface: Interface | Mapping[str, Any] | None = None,
        status: Any = _DEFAULT,
        exposure: Mapping[str, Sequence[str]] | None = None,
        lidl_text: str | None = None,
        interface_error: str | None = None,
        contract_sha256: Any = _DEFAULT,
        cross_check: Any = _DEFAULT,
        stale: bool = False,
        terminate_on_change: bool = True,
        lidl_cli: str | None = None,
    ) -> FakeModule:
        """Expose (or reload) a module.

        ``methods`` entries are names or ``(name, [param names])``; with a contract
        they default to its declarations, as do ``events``. ``interface`` is an
        :class:`~logos_bridge.lidl.Interface`, a JSON AST or a shape document
        (``INTERFACE`` of a generated module); ``lidl_text`` is what ``lidl()``
        answers (parsed with the ``lidl`` CLI when there is no ``interface``, and
        written with :meth:`Interface.to_lidl` when there is no text). ``status``
        defaults to ``ok`` with a contract. ``lidl``/``name``/``version`` answer from
        the contract unless scripted.

        Reloading a module whose served view changed terminates its subscriptions
        with ``provider_changed``, as the bridge's revalidation does; pass
        ``terminate_on_change=False`` for a reload the bridge does not notice.
        """
        contract: Interface | None = None
        if interface is not None:
            contract = Interface.coerce(interface).with_identity()
        elif lidl_text is not None:
            contract = _parse_contract(lidl_text, lidl_cli, name)
        if lidl_text is None and contract is not None:
            with contextlib.suppress(ValueError):
                lidl_text = contract.to_lidl()
        if status is _DEFAULT:
            status = "ok" if contract is not None else None
        if status not in STATUSES:
            raise ValueError(f"unknown interface status {status!r}")
        if methods is None:
            live_methods = {m.name: [p.name for p in m.params] for m in contract.methods} if contract else {}
        else:
            live_methods = _method_table(methods)
        live_events = list(events) if events is not None else list(contract.event_names if contract else ())
        if contract_sha256 is _DEFAULT:
            contract_sha256 = (sha256_text(lidl_text) if lidl_text is not None and status in ("ok", "invalid")
                               and contract is not None else None)
        if cross_check is _DEFAULT:
            cross_check = {"state": "consistent", "findings": []} if status == "ok" else None
        module = FakeModule(
            name=name,
            methods=live_methods,
            events=live_events,
            resolved=resolved,
            events_declared=events_declared,
            interface=contract.to_json() if contract is not None else None,
            status=status,
            exposure={k: list(v) for k, v in exposure.items()} if exposure is not None else None,
            interface_error=interface_error,
            contract_sha256=contract_sha256,
            cross_check=cross_check,
            stale=stale,
            lidl_text=lidl_text,
            contract=contract,
        )
        previous = self.modules.get(name)
        self.modules[name] = module
        if previous is not None and terminate_on_change and previous.served_identity() != module.served_identity():
            self.terminate(name, reason=proto.REASON_PROVIDER_CHANGED)
        return module

    def mark_stale(self, name: str, stale: bool = True) -> None:
        """Flag a module's view as stale (the bridge noticed a change and has not rediscovered yet)."""
        self.modules[name].stale = stale

    def remove_module(self, name: str) -> None:
        self.modules.pop(name, None)

    def on_call(self, module: str, method: str, handler: Any) -> None:
        """Script ``module.method``: a value, :class:`FakeError`, :class:`Reject`,
        :class:`Wire`, :data:`NoResponse`, :class:`Delay`, or a (sync or async)
        callable taking a :class:`CallContext` and returning one of those."""
        self._call_handlers[(module, method)] = handler

    def on_subscribe(self, module: str, event: str, handler: Any) -> None:
        """Script subscriptions to ``module.event``: ``None`` (ack), :class:`FakeError`,
        :data:`NoResponse`, :class:`Delay`, or a callable taking a :class:`SubscribeContext`."""
        self._subscribe_handlers[(module, event)] = handler

    def set_draining(self, draining: bool = True) -> None:
        """Answer every body with the id:null "shutting down" error; healthz says draining."""
        self._draining = draining

    def freeze(self) -> None:
        """Stop reading and writing on open connections (a stalled service thread)."""
        self._frozen = True
        for conn in self._open_connections():
            conn.ws.transport.pause_reading()

    def unfreeze(self) -> None:
        self._frozen = False
        for conn in self._open_connections():
            with contextlib.suppress(Exception):
                conn.ws.transport.resume_reading()
            conn.wakeup.set()

    # ---------------------------------------------------------------- traffic

    def _open_connections(self) -> list[FakeConnection]:
        return [c for c in self._accepted.values() if c.open and c.writer is not None]

    def generation(self, module: str, event: str) -> int:
        return self._generations.get((module, event), 1)

    def emit(self, module: str, event: str, *data: Any, generation: int | None = None) -> int:
        """Send ``rpc.event`` to every subscriber of ``module.event``; returns how many were queued."""
        return self.emit_json(module, event, [encode_value(d) for d in data], generation=generation)

    def emit_json(self, module: str, event: str, payload: list[Any], *, generation: int | None = None) -> int:
        """:meth:`emit` with ``payload`` sent exactly as given (already JSON)."""
        gen = self.generation(module, event) if generation is None else generation
        sent = 0
        for conn in self._open_connections():
            for sub in list(conn.subscribers.values()):
                if sub.module != module or sub.event != event:
                    continue
                frame = proto.make_notification(proto.NOTIFY_EVENT, {
                    "subscription": sub.subscription,
                    "module": module,
                    "event": event,
                    "data": payload,
                    "generation": gen,
                    "ts": int(time.time() * 1000),
                })
                if self._enqueue(conn, _text(frame)):
                    sent += 1
        return sent

    def terminate(self, module: str, event: str | None = None, *, reason: str = proto.REASON_PROVIDER_UNAVAILABLE) -> int:
        """Terminate subscriptions to ``module`` (or one event of it), as a provider loss does."""
        sent = 0
        bumped: set[tuple[str, str]] = set()
        for conn in self._open_connections():
            for key, sub in list(conn.subscribers.items()):
                if sub.module != module or (event is not None and sub.event != event):
                    continue
                del conn.subscribers[key]
                conn.subs.pop(key, None)
                bumped.add((sub.module, sub.event))
                frame = proto.make_notification(proto.NOTIFY_TERMINATED, {
                    "subscription": sub.subscription,
                    "module": sub.module,
                    "event": sub.event,
                    "reason": reason,
                })
                if self._enqueue(conn, _text(frame)):
                    sent += 1
        for pair in bumped:
            self._generations[pair] = self.generation(*pair) + 1
        self._notify()
        return sent

    def send_raw(self, frame: str | bytes | Mapping[str, Any] | list[Any], *, connection: int | None = None) -> int:
        """Queue a raw frame (text, binary, or JSON) to one or every connection."""
        payload: str | bytes = frame if isinstance(frame, (str, bytes)) else _text(frame)
        sent = 0
        for conn in self._open_connections():
            if connection is not None and conn.id != connection:
                continue
            if self._enqueue(conn, payload):
                sent += 1
        return sent

    async def close_connections(self, code: int = 1000, reason: str = "") -> None:
        """Close every connection with a close frame and wait for them to end."""
        conns = self._open_connections()
        for conn in conns:
            self._request_close(conn, code, reason)
        await self._wait_until(
            lambda: True if all(not c.open for c in conns) else None,
            self._close_timeout + 5.0,
            lambda: "connections to close",
        )

    def abort_connections(self) -> None:
        """Drop every connection without a close frame (the client sees 1006)."""
        for conn in list(self._accepted.values()):
            conn.ws.transport.abort()

    def subscriptions(self, module: str | None = None, event: str | None = None) -> list[FakeSubscription]:
        """Live subscribers (ids that receive events)."""
        return [
            FakeSubscription(conn.id, sub.subscription, sub.module, sub.event)
            for conn in self._open_connections()
            for sub in conn.subscribers.values()
            if (module is None or sub.module == module) and (event is None or sub.event == event)
        ]

    async def wait_for_request(
        self,
        method: str | None = None,
        *,
        predicate: Callable[[RecordedRequest], bool] | None = None,
        count: int = 1,
        timeout: float = 5.0,
    ) -> RecordedRequest:
        """The ``count``-th recorded request matching ``method``/``predicate``."""

        def probe() -> RecordedRequest | None:
            matches = [r for r in self.requests
                       if (method is None or r.method == method) and (predicate is None or predicate(r))]
            return matches[count - 1] if len(matches) >= count else None

        found: RecordedRequest = await self._wait_until(
            probe, timeout, lambda: f"request #{count} {method or ''} (seen: {[r.method for r in self.requests]})"
        )
        return found

    async def wait_for_subscription(
        self, module: str, event: str, *, count: int = 1, timeout: float = 5.0
    ) -> FakeSubscription:
        """Wait until ``count`` subscribers of ``module.event`` exist; returns the ``count``-th."""

        def probe() -> FakeSubscription | None:
            subs = self.subscriptions(module, event)
            return subs[count - 1] if len(subs) >= count else None

        found: FakeSubscription = await self._wait_until(
            probe, timeout, lambda: f"{count} subscription(s) to {module}.{event}"
        )
        return found

    # ------------------------------------------------------------- websocket

    def _host_allowed(self, values: Sequence[str], port: int) -> bool:
        if len(values) != 1:
            return False
        host = values[0]
        expected = str(self.host_port if self.host_port is not None else port)
        return host in _LOOPBACK_HOSTS or any(host == f"{h}:{expected}" for h in _LOOPBACK_HOSTS)

    def _origin_allowed(self, values: Sequence[str]) -> bool:
        return all(v in self.allowed_origins for v in values)

    def _upgrade_refusal(self, connection: ServerConnection, request: Request) -> str | None:
        headers = request.headers
        if not self._host_allowed(headers.get_all("Host"), self.port):
            return "host"
        origins = headers.get_all("Origin")
        if not self._origin_allowed(origins):
            return "origin"
        if self.auth_mode == "bearer" and not origins:
            return "bearer"
        live = [c for c in self._accepted.values() if c.open]
        if len(live) >= self.max_connections:
            return "max_connections"
        remote = connection.remote_address
        peer = str(remote[0]) if remote else ""
        if sum(1 for c in live if c.peer == peer) >= self.max_connections_per_peer:
            return "per_peer"
        requested = [p.strip() for v in headers.get_all("Sec-WebSocket-Protocol") for p in v.split(",")]
        requested = [p for p in requested if p]
        if requested and self.subprotocol not in requested:
            return "subprotocol"
        return None

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self._prune()
        refusal = self._upgrade_refusal(connection, request)
        conn: FakeConnection | None = None
        if refusal is None:
            conn = FakeConnection(next(self._ids), connection)
            self._accepted[connection] = conn
        remote = connection.remote_address
        self.handshakes.append(Handshake(
            path=request.path,
            headers=tuple(request.headers.raw_items()),
            remote=tuple(remote) if remote else None,
            accepted=refusal is None,
            refusal=refusal,
            connection=conn.id if conn else None,
        ))
        self._notify()
        if refusal is not None:
            # lws refuses in FILTER_PROTOCOL_CONNECTION by dropping the socket: no HTTP answer.
            connection.transport.abort()
            return connection.respond(403, "")
        return None

    def _prune(self) -> None:
        for ws, conn in list(self._accepted.items()):
            if not conn.open or (conn.writer is None and ws.transport.is_closing()):
                del self._accepted[ws]

    def _select_subprotocol(
        self, connection: ServerConnection, subprotocols: Sequence[Subprotocol]
    ) -> Subprotocol | None:
        return Subprotocol(self.subprotocol) if self.subprotocol in subprotocols else None

    async def _ws_handler(self, ws: ServerConnection) -> None:
        conn = self._accepted.get(ws)
        if conn is None:  # pragma: no cover - accepted without process_request
            conn = FakeConnection(next(self._ids), ws)
            self._accepted[ws] = conn
        self.connections.append(conn)
        conn.writer = asyncio.create_task(self._writer(conn), name=f"fake-bridge writer {conn.id}")
        if self._frozen:
            ws.transport.pause_reading()
        self._notify()
        try:
            while True:
                try:
                    message = await ws.recv()
                except ConnectionClosed as exc:
                    conn.closed = exc
                    break
                if conn.close_code is not None:
                    continue
                if isinstance(message, bytes):
                    self._request_close(conn, 1003)
                    continue
                if len(message.encode("utf-8")) > self.max_frame_bytes:
                    self._request_close(conn, 1009)
                    continue
                self._on_body(message, functools.partial(self._reply, conn), "ws", conn)
        finally:
            conn.open = False
            conn.subscribers.clear()
            conn.subs.clear()
            conn.wakeup.set()
            writer = conn.writer
            if writer is not None and not writer.done():
                done, _ = await asyncio.wait({writer}, timeout=self._close_timeout + 1.0)
                if not done:
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
            self._accepted.pop(ws, None)
            self._notify()

    def _reply(self, conn: FakeConnection, text: str) -> None:
        self._enqueue(conn, text)

    def _enqueue(self, conn: FakeConnection, frame: str | bytes) -> bool:
        if not conn.open or conn.close_code is not None:
            return False
        if len(conn.outbound) >= self.max_queued_frames:
            # ws_server.cpp: one producer feeds many readers, so a slow reader is closed.
            self._request_close(conn, 1008)
            return False
        conn.outbound.append(frame)
        conn.wakeup.set()
        return True

    def _request_close(self, conn: FakeConnection, code: int, reason: str = "") -> None:
        if conn.close_code is None:
            conn.close_code = code
            conn.close_reason = reason
            conn.wakeup.set()

    async def _writer(self, conn: FakeConnection) -> None:
        ws = conn.ws
        try:
            while conn.open:
                await conn.wakeup.wait()
                conn.wakeup.clear()
                while conn.open and not self._frozen:
                    if conn.close_code is not None:
                        conn.outbound.clear()  # the close pre-empts queued frames, as in lws
                        await ws.close(conn.close_code, conn.close_reason)
                        return
                    if not conn.outbound:
                        break
                    frame = conn.outbound.popleft()
                    await ws.send(frame)
                    conn.frames_sent += 1
        except ConnectionClosed:
            return

    # ------------------------------------------------------------- dispatch

    def _record(self, raw: Any, text: str, transport: str, conn: FakeConnection | None) -> None:
        self.requests.append(RecordedRequest(conn.id if conn else None, transport, raw, text))
        self._notify()

    def _on_body(
        self,
        body: str,
        sink: Callable[[str], None],
        transport: str,
        conn: FakeConnection | None,
        route: str = "",
    ) -> None:
        shutting_down = proto.ERROR_TABLE[-32006]
        if self._draining:
            sink(_text(_error(None, shutting_down.to_error())))
            return
        if route.startswith("/modules/"):
            self._on_rest(route[len("/modules/"):], body, sink, conn)
            return
        try:
            parsed = loads(body)
        except ValueError:
            self._record(None, body, transport, conn)
            sink(_text(_error(None, proto.ERROR_TABLE[-32700].to_error())))
            return
        if isinstance(parsed, list):
            single = False
            if not parsed:
                self._record(parsed, body, transport, conn)
                sink(_text(_error(None, _invalid_request("empty batch"))))
                return
            items = parsed
        else:
            single = True
            items = [parsed]
        if len(items) > self.max_batch:
            for item in items:
                self._record(item, body, transport, conn)
            sink(_text(_error(None, proto.ERROR_TABLE[-32029].to_error("batch too large"))))
            return
        batch = _Batch(sink, len(items), single)
        for slot, item in enumerate(items):
            self._record(item, body, transport, conn)
            self._handle_one(item, batch, slot, transport, conn)

    def _on_rest(self, rest: str, body: str, sink: Callable[[str], None], conn: FakeConnection | None) -> None:
        slash = rest.find("/")
        if slash <= 0 or slash + 1 >= len(rest):
            sink(_text(_error(None, proto.not_found())))
            return
        inner: Any = {}
        if body:
            try:
                inner = loads(body)
            except ValueError:
                sink(_text(_error(None, proto.ERROR_TABLE[-32700].to_error())))
                return
        request = proto.make_request(1, proto.OP_CALL, {
            "module": rest[:slash], "method": rest[slash + 1:], "params": inner,
        })
        self._record(request, body, "http", conn)
        self._handle_one(request, _Batch(sink, 1, True, rest_shape=True), 0, "http", conn)

    def _handle_one(
        self, raw: Any, batch: _Batch, slot: int, transport: str, conn: FakeConnection | None
    ) -> None:
        if not isinstance(raw, dict):
            batch.fill(slot, _error(None, _invalid_request("request must be an object")))
            return
        if raw.get("jsonrpc") != proto.JSONRPC_VERSION:
            batch.fill(slot, _error(None, _invalid_request('jsonrpc must be "2.0"')))
            return
        method = raw.get("method")
        if not isinstance(method, str):
            batch.fill(slot, _error(None, _invalid_request("method must be a string")))
            return
        notification = "id" not in raw
        req_id = None if notification else raw["id"]
        if not notification and not (req_id is None or isinstance(req_id, str) or _is_number(req_id)):
            batch.fill(slot, _error(None, _invalid_request("id must be a string, number or null")))
            return
        params = raw["params"] if "params" in raw else {}
        if not isinstance(params, (dict, list)):
            batch.fill(slot, _error(None, _invalid_params("params must be an object or array")))
            return
        if notification and method == proto.OP_CALL:
            batch.fill(slot, _error(None, _invalid_request(
                "notifications are not accepted for rpc.call: every module call has a reply")))
            return

        if method == proto.OP_PING:
            batch.fill(slot, proto.make_result(req_id, "pong"))
        elif method == proto.OP_LIST_MODULES:
            batch.fill(slot, proto.make_result(req_id, [m.describe(full=False) for m in self.modules.values()]))
        elif method == proto.OP_SCHEMA:
            name = params.get("module") if isinstance(params, dict) else None
            if not isinstance(name, str):
                batch.fill(slot, _error(req_id, _invalid_params('rpc.schema requires a string "module"')))
                return
            module = self.modules.get(name)
            batch.fill(slot, proto.make_result(req_id, module.describe()) if module
                       else _error(req_id, proto.not_found()))
        elif method == proto.OP_CALL:
            self._call(req_id, params, batch, slot, transport, conn)
        elif method == proto.OP_SUBSCRIBE:
            self._subscribe(req_id, params, batch, slot, conn)
        elif method == proto.OP_UNSUBSCRIBE:
            self._unsubscribe(req_id, params, batch, slot, conn)
        elif method == proto.OP_CANCEL:
            batch.fill(slot, proto.make_result(req_id, {"cancelled": False, "reason": "not_supported_upstream"}))
        else:
            batch.fill(slot, _error(req_id, proto.not_found()))

    def _call(
        self, req_id: Any, params: Any, batch: _Batch, slot: int, transport: str, conn: FakeConnection | None
    ) -> None:
        if not isinstance(params, dict):
            batch.fill(slot, _error(req_id, _invalid_params(
                'rpc.call params must be an object with "module" and "method"')))
            return
        module_name, method = params.get("module"), params.get("method")
        if not isinstance(module_name, str) or not isinstance(method, str):
            batch.fill(slot, _error(req_id, _invalid_params('rpc.call requires string "module" and "method"')))
            return
        if not module_name or not method:
            batch.fill(slot, _error(req_id, _invalid_params('"module" and "method" must be non-empty')))
            return
        args = params["params"] if "params" in params else []
        if args is None:
            args = []
        if not isinstance(args, (dict, list)):
            batch.fill(slot, _error(req_id, _invalid_params("rpc.call inner params must be an object or array")))
            return
        module = self.modules.get(module_name)
        if module is None or not module.method_permitted(method):
            batch.fill(slot, _error(req_id, proto.not_found()))
            return
        positional: list[Any]
        if isinstance(args, dict):
            converted, bad_path = module.to_positional(method, args)
            if converted is None:
                batch.fill(slot, _error(req_id, proto.invalid_params_error("schema-mismatch", bad_path)))
                return
            positional = converted
        else:
            positional = list(args)
        ctx = CallContext(self, module_name, method, positional, args, req_id, transport, conn)
        handler = self._call_handlers.get((module_name, method), _DEFAULT)
        if handler is _DEFAULT and module.typed and method in BUILTIN_METHODS:
            handler = module.builtin_answer(method)
        if handler is _DEFAULT:
            logger.warning("FakeBridge: unscripted call %s.%s answers -32603", module_name, method)
            handler = FakeError(-32603)

        def respond(value: Any) -> None:
            batch.fill(slot, proto.make_result(req_id, value))

        def fail(error: dict[str, Any]) -> None:
            batch.fill(slot, _error(req_id, error))

        self._run_outcome(handler, ctx, respond, fail)

    def _subscribe_target(self, params: Any, require_event: bool) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(params, dict):
            return None, "params must be an object"
        sid = params.get("subscription")
        if "subscription" not in params or not (isinstance(sid, str) or _is_number(sid)):
            return None, '"subscription" (a caller-assigned id) is required'
        if require_event:
            module, event = params.get("module"), params.get("event")
            if not isinstance(module, str) or not isinstance(event, str):
                return None, 'rpc.subscribe requires string "module" and "event"'
            if not module or not event:
                return None, '"module" and "event" must be non-empty'
        return params, ""

    def _subscribe(self, req_id: Any, params: Any, batch: _Batch, slot: int, conn: FakeConnection | None) -> None:
        target, problem = self._subscribe_target(params, True)
        if target is None:
            batch.fill(slot, _error(req_id, _invalid_params(problem)))
            return
        sid = target["subscription"]
        module_name: str = target["module"]
        event: str = target["event"]
        module = self.modules.get(module_name)
        if module is None or not module.event_permitted(event):
            batch.fill(slot, _error(req_id, proto.not_found()))
            return
        if conn is None:  # over HTTP the bridge registers on a connection that is about to go away
            batch.fill(slot, proto.make_result(req_id, {
                "subscription": sid, "operation": "subscribe", "module": module_name,
                "event": event, "state": "registered"}))
            return
        key = _text(sid)
        if len(conn.subs) >= self.max_subscriptions:
            batch.fill(slot, _error(req_id, proto.ERROR_TABLE[-32029].to_error("too many subscriptions")))
            return
        if key in conn.subs:
            batch.fill(slot, proto.make_result(req_id, {
                "subscription": sid, "operation": "subscribe", "module": module_name,
                "event": event, "state": "active"}))
            return
        conn.subs[key] = (module_name, event)
        conn.subscribers[key] = _Subscriber(sid, module_name, event)
        self._generations.setdefault((module_name, event), 1)
        self._notify()

        defer = not self.emit_events_before_subscribe_ack
        ctx = SubscribeContext(self, module_name, event, sid, req_id, conn, defer)
        handler = self._subscribe_handlers.get((module_name, event))

        def ack(_value: Any) -> None:
            batch.fill(slot, proto.make_result(req_id, {
                "subscription": sid, "operation": "subscribe", "module": module_name,
                "event": event, "state": "registered"}))
            for data in ctx.deferred:
                self.emit(module_name, event, *data)
            ctx.deferred.clear()

        def respond(value: Any) -> None:
            if defer:
                ack(value)
            else:
                self._spawn(self._ack_next_turn(ack, value))

        def fail(error: dict[str, Any]) -> None:
            conn.subscribers.pop(key, None)
            if not self.poison_failed_subscribe_ids:
                conn.subs.pop(key, None)
            batch.fill(slot, _error(req_id, error))

        self._run_outcome(handler, ctx, respond, fail)

    async def _ack_next_turn(self, ack: Callable[[Any], None], value: Any) -> None:
        await asyncio.sleep(0)
        ack(value)

    def _unsubscribe(self, req_id: Any, params: Any, batch: _Batch, slot: int, conn: FakeConnection | None) -> None:
        target, problem = self._subscribe_target(params, False)
        if target is None:
            batch.fill(slot, _error(req_id, _invalid_params(problem)))
            return
        sid = target["subscription"]
        if conn is not None:
            key = _text(sid)
            conn.subs.pop(key, None)
            conn.subscribers.pop(key, None)
            self._notify()
        # Idempotent: an unknown id is acknowledged too.
        batch.fill(slot, proto.make_result(req_id, {"subscription": sid, "operation": "unsubscribe"}))

    # -------------------------------------------------------------- outcomes

    def _invoke(self, handler: Any, ctx: Any) -> Any:
        if callable(handler) and not isinstance(handler, (type, *_OUTCOME_TYPES)):
            return handler(ctx)
        return handler

    def _handler_failed(self, exc: BaseException, ctx: Any) -> None:
        self.handler_errors.append(exc)
        logger.error("FakeBridge: handler for %s failed", ctx, exc_info=exc)

    def _run_outcome(
        self,
        handler: Any,
        ctx: Any,
        respond: Callable[[Any], None],
        fail: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            value = self._invoke(handler, ctx)
        except Exception as exc:
            self._handler_failed(exc, ctx)
            fail(proto.ERROR_TABLE[-32603].to_error())
            return
        if inspect.isawaitable(value) or isinstance(value, Delay):
            self._spawn(self._finish_outcome(value, ctx, respond, fail))
            return
        self._apply(value, ctx, respond, fail)

    async def _finish_outcome(
        self,
        value: Any,
        ctx: Any,
        respond: Callable[[Any], None],
        fail: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            while True:
                if inspect.isawaitable(value):
                    value = await value
                elif isinstance(value, Delay):
                    await asyncio.sleep(value.seconds)
                    value = self._invoke(value.then, ctx)
                else:
                    break
        except Exception as exc:
            self._handler_failed(exc, ctx)
            fail(proto.ERROR_TABLE[-32603].to_error())
            return
        self._apply(value, ctx, respond, fail)

    def _apply(
        self,
        value: Any,
        ctx: Any,
        respond: Callable[[Any], None],
        fail: Callable[[dict[str, Any]], None],
    ) -> None:
        if value is NoResponse:
            return
        if isinstance(value, FakeError):
            fail(value.to_error())
            return
        if isinstance(value, Reject):
            respond(value.to_result())
            return
        if isinstance(value, Wire):
            respond(value.value)
            return
        try:
            encoded = encode_value(value)
        except (TypeError, ValueError) as exc:
            self._handler_failed(exc, ctx)
            fail(proto.ERROR_TABLE[-32603].to_error())
            return
        respond(encoded)

    # ------------------------------------------------------------------ http

    def _get(self, path: str) -> tuple[int, str]:
        if path == "/healthz":
            return 200, _text({
                "status": "draining" if self._draining else "ok",
                "uptime_seconds": int(time.monotonic() - self._started_at),
                "protocol": "json-rpc-2.0",
            })
        if path == "/modules":
            return 200, _text([m.describe(full=False) for m in self.modules.values()])
        if path.startswith("/modules/"):
            module = self.modules.get(path[len("/modules/"):])
            if module is not None:
                return 200, _text(module.describe())
        return 404, _text(proto.not_found())

    async def _http_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._http_writers.add(writer)
        try:
            while not self._stopped:
                keep_alive = await self._serve_http_request(reader, writer)
                if not keep_alive:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            self._http_writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve_http_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        line = await reader.readline()
        if not line:
            return False
        parts = line.decode("latin-1").rstrip("\r\n").split(" ")
        if len(parts) != 3:
            return False
        method, target, _version = parts
        headers: list[tuple[str, str]] = []
        while True:
            raw = await reader.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            name, _, value = raw.decode("latin-1").partition(":")
            headers.append((name.strip(), value.strip()))
        path = urllib.parse.urlsplit(target).path or "/"
        framing = _Framing.of(method, headers)
        # As in ws_server.cpp, everything but an accepted POST is answered without reading a
        # body, and an answer that leaves one unread closes the connection.
        body = b""
        unread = framing.body_follows
        refusal = self._http_refusal(method, headers, framing)
        if refusal is not None:
            status, content_type, payload = refusal, "text/html", _HTML % refusal
        elif method != "POST":  # ws_server.cpp treats every non-POST as GET
            status, text = self._get(path)
            content_type, payload = "application/json", text.encode("utf-8")
        else:
            length = framing.usable_length or 0
            if length > self.max_body_bytes:
                # ws_server.cpp refuses an over-cap body by dropping the connection.
                self._record_http(method, path, headers, b"", None)
                return False
            body = await reader.readexactly(length) if length else b""
            status, content_type, unread = 200, "application/json", False
            payload = await self._post_http(path, body)
        self._record_http(method, path, headers, body, status)
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}", f"Content-Type: {content_type}",
                f"Content-Length: {len(payload)}"]
        if unread:
            head.append("Connection: close")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()
        if unread:
            await self._linger(reader, writer)
            return False
        # A client's own Connection: close gets no header back, as from lws.
        return not _asks_to_close(headers)

    def _http_refusal(self, method: str, headers: Sequence[tuple[str, str]], framing: _Framing) -> int | None:
        if not self._host_allowed(_header_lookup(headers, "Host"), self.http_port) or not self._origin_allowed(
            _header_lookup(headers, "Origin")
        ):
            return 403
        if self.auth_mode == "bearer":
            return 401
        if method != "POST":
            return None
        content_type = (_header_lookup(headers, "Content-Type") or [""])[0]
        if not content_type.startswith("application/json"):
            return 415
        return 411 if framing.usable_length is None else None

    def _record_http(self, method: str, path: str, headers: Sequence[tuple[str, str]], body: bytes,
                     status: int | None) -> None:
        self.http_requests.append(HttpExchange(method, path, tuple(headers), body, status))
        self._notify()

    async def _linger(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Half-close, then drop what the client still sends until it closes (at most 5 s)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LINGER_SECONDS
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            if writer.can_write_eof():
                writer.write_eof()
            while (left := deadline - loop.time()) > 0:
                if not await asyncio.wait_for(reader.read(1 << 16), left):
                    break

    async def _post_http(self, path: str, body: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()

        def sink(text: str) -> None:
            if not answer.done():
                answer.set_result(text)

        try:
            text_body = body.decode("utf-8")
        except UnicodeDecodeError:
            text_body = "\U0000fffe"  # unparseable, like invalid UTF-8 is for nlohmann
        self._on_body(text_body, sink, "http", None, route=path if path.startswith("/modules/") else "")
        return (await answer).encode("utf-8")


_REASONS: Final = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 411: "Length Required",
                   415: "Unsupported Media Type"}
