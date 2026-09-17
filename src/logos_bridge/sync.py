"""Blocking mirror of :class:`~logos_bridge.client.AsyncBridgeClient`.

Every call runs on a :class:`~logos_bridge.portal.BlockingPortal`. Clients may share
one portal (``portal=``); a client that created its own portal stops it on close.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any

from . import _protocol as proto
from ._transport import HeadersLike
from .client import AsyncBridgeClient
from .errors import ConnectionClosed, PortalStopped, RpcError
from .models import Event, ModuleInfo
from .portal import BlockingPortal, PumpHandle
from .subscription import AsyncSubscription

if TYPE_CHECKING:
    from .dynamic import BlockingDynamicModule


async def _open_subscription(
    client: AsyncBridgeClient,
    module: str,
    events: list[str] | None,
    event: str | None,
    timeout: float | None,
    max_pending: int | None,
) -> AsyncSubscription:
    if events is not None:
        return await client.subscribe_many(module, events, timeout=timeout, max_pending=max_pending)
    assert event is not None
    return await client.subscribe(module, event, timeout=timeout, max_pending=max_pending)


class Subscription:
    """Blocking view of an :class:`~logos_bridge.subscription.AsyncSubscription`.

    Iterating blocks for the next event; queued events are delivered before the
    terminal exception, and iteration stops after :meth:`unsubscribe`.
    """

    def __init__(self, aio: AsyncSubscription, portal: BlockingPortal) -> None:
        self._aio = aio
        self._portal = portal

    def __repr__(self) -> str:
        return f"<Subscription {self._aio!r}>"

    @property
    def aio(self) -> AsyncSubscription:
        return self._aio

    @property
    def ids(self) -> tuple[str, ...]:
        return self._aio.ids

    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        return self._aio.targets

    @property
    def module(self) -> str:
        return self._aio.module

    @property
    def events(self) -> tuple[str, ...]:
        return self._aio.events

    @property
    def acks(self) -> tuple[Mapping[str, Any], ...]:
        return self._aio.acks

    @property
    def pending(self) -> int:
        return self._aio.pending

    @property
    def high_water(self) -> int:
        return self._aio.high_water

    @property
    def received(self) -> int:
        return self._aio.received

    @property
    def max_pending(self) -> int | None:
        return self._aio.max_pending

    @property
    def ended(self) -> bool:
        return self._aio.ended

    def __iter__(self) -> Iterator[Event]:
        return self

    def __next__(self) -> Event:
        try:
            return self._portal.call(self._aio.__anext__)
        except StopAsyncIteration:
            raise StopIteration from None

    def get(self, timeout: float | None = None) -> Event:
        """The next event; :class:`~logos_bridge.errors.ClientTimeout` after ``timeout`` seconds."""
        return self._portal.call(self._aio.get, timeout)

    def unsubscribe(self) -> None:
        """Stop the stream and wait for the bridge's acknowledgement. Idempotent."""
        self._portal.call(self._aio.unsubscribe)

    def cancel(self) -> None:
        """Stop the stream without waiting; safe from any thread, including callbacks."""
        try:
            self._portal.run_sync_soon(self._aio.cancel)
        except PortalStopped:
            pass

    def close(self) -> None:
        """Unsubscribe, ignoring bridge errors."""
        if not self._portal.stopped:
            self._portal.call(self._aio.aclose)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class BridgeClient:
    """Blocking client for logos-json-rpc-bridge.

    ``with BridgeClient() as bridge:`` connects and closes. ``.aio`` is the underlying
    async client and ``.portal`` the loop it runs on. Close order: event callbacks are
    cancelled first, then the connection is closed, then an owned portal is stopped.
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
        portal: BlockingPortal | None = None,
    ) -> None:
        self._aio = AsyncBridgeClient(
            url,
            open_timeout=open_timeout,
            call_timeout=call_timeout,
            op_timeout=op_timeout,
            close_timeout=close_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            max_message_size=max_message_size,
            max_request_size=max_request_size,
            host_header=host_header,
            subprotocol=subprotocol,
            extra_headers=extra_headers,
        )
        self._owns_portal = portal is None
        self._portal = portal if portal is not None else BlockingPortal(name="logos-bridge")
        self._lock = threading.Lock()
        self._handles: list[PumpHandle[Event]] = []
        self._closed = False

    def __repr__(self) -> str:
        return f"<BridgeClient {self._aio.url} {self._aio.state}>"

    @property
    def aio(self) -> AsyncBridgeClient:
        return self._aio

    @property
    def portal(self) -> BlockingPortal:
        return self._portal

    @property
    def url(self) -> str:
        return self._aio.url

    @property
    def connected(self) -> bool:
        return self._aio.connected

    @property
    def closed(self) -> bool:
        return self._aio.closed

    @property
    def close_exception(self) -> ConnectionClosed | None:
        return self._aio.close_exception

    @property
    def last_uncorrelated_error(self) -> RpcError | None:
        return self._aio.last_uncorrelated_error

    def connect(self) -> BridgeClient:
        self._portal.call(self._aio.connect)
        return self

    def close(self) -> None:
        """Cancel event callbacks, close the connection, stop an owned portal. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles, self._handles = self._handles, []
        try:
            for handle in handles:
                handle.cancel()
            if not self._portal.stopped:
                self._portal.call(self._aio.aclose)
        finally:
            if self._owns_portal:
                self._portal.stop()

    def __enter__(self) -> BridgeClient:
        if self._aio.state == "new":
            self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def wait_closed(self, timeout: float | None = None) -> None:
        """Block until the connection ends; :class:`~logos_bridge.errors.ClientTimeout` after ``timeout``."""
        self._portal.call_with_timeout(timeout, self._aio.wait_closed)

    def request(self, op: str, params: Any = None, *, timeout: float | None = None) -> Any:
        return self._portal.call(self._aio.request, op, params, timeout=timeout)

    def call(
        self,
        module: str,
        method: str,
        *args: Any,
        timeout: float | None = None,
        decode_bytes: bool = True,
        detect_rejection: bool = True,
    ) -> Any:
        return self._portal.call(
            self._aio.call, module, method, *args,
            timeout=timeout, decode_bytes=decode_bytes, detect_rejection=detect_rejection,
        )

    def subscribe(
        self, module: str, event: str, *, timeout: float | None = None, max_pending: int | None = None
    ) -> Subscription:
        aio = self._portal.call(_open_subscription, self._aio, module, None, event, timeout, max_pending)
        return Subscription(aio, self._portal)

    def subscribe_many(
        self,
        module: str,
        events: Iterable[str],
        *,
        timeout: float | None = None,
        max_pending: int | None = None,
    ) -> Subscription:
        if isinstance(events, str):
            raise TypeError("events must be an iterable of event names, not a string")
        aio = self._portal.call(_open_subscription, self._aio, module, list(events), None, timeout, max_pending)
        return Subscription(aio, self._portal)

    def ping(self) -> float:
        return self._portal.call(self._aio.ping)

    def list_modules(self) -> list[ModuleInfo]:
        return self._portal.call(self._aio.list_modules)

    def schema(self, module: str) -> ModuleInfo:
        return self._portal.call(self._aio.schema, module)

    def wait_for_module(self, module: str, timeout: float | None = 10.0) -> ModuleInfo:
        return self._portal.call(self._aio.wait_for_module, module, timeout)

    def module(self, name: str, *, require_typed: bool = False,
               discovery_wait: float | None = 10.0) -> BlockingDynamicModule:
        """The blocking :class:`~logos_bridge.dynamic.BlockingDynamicModule` for ``name``."""
        from .dynamic import BlockingDynamicModule

        aio = self._portal.call(self._aio.module, name, require_typed=require_typed,
                                discovery_wait=discovery_wait)
        return BlockingDynamicModule(self, aio)

    def on_event(
        self,
        module: str,
        event: str,
        callback: Callable[[Event], object],
        *,
        error_callback: Callable[[BaseException], object] | None = None,
        timeout: float | None = None,
        max_pending: int | None = None,
    ) -> PumpHandle[Event]:
        """Subscribe and run ``callback(event)`` on the portal's dispatch thread.

        Callbacks may call this client (``bridge.call(...)``). Exceptions from ``callback``,
        and the one that ends the stream (termination, disconnect), go to
        ``error_callback`` or the log. ``handle.cancel()`` guarantees no further callbacks
        and unsubscribes, including when called from inside a callback.
        """
        aio = self._portal.call(_open_subscription, self._aio, module, None, event, timeout, max_pending)
        handle = self._portal.pump(aio, callback, error_callback=error_callback, on_cancel=aio.cancel)
        with self._lock:
            if self._closed:
                handle.cancel()
            else:
                self._handles = [h for h in self._handles if not h.cancelled]
                self._handles.append(handle)
        return handle
