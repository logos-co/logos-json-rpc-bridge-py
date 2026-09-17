"""Event streams: one ordered queue per ``subscribe()``/``subscribe_many()``.

Queues are unbounded on purpose. The bridge closes a connection (1008) once its
outbound queue overflows, so a client that stopped reading to apply backpressure
would be disconnected. The reader therefore always enqueues; ``max_pending`` turns
unbounded growth into a :class:`~logos_bridge.errors.SubscriptionOverflow` instead.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final

from .errors import BridgeError, ClientTimeout, SubscriptionClosed, warn
from .models import Event

if TYPE_CHECKING:
    from .client import AsyncBridgeClient

logger = logging.getLogger("logos_bridge")

PENDING_WARNING_THRESHOLD: Final = 10_000


class _SubEntry:
    """One bridge subscription id.

    States: ``pending`` (subscribe sent, no ack), ``active``, ``doomed`` (stream
    ended before the ack), ``zombie`` (ack timed out, unsubscribe in flight),
    ``failed``, ``released``, ``terminated``.
    """

    __slots__ = ("sid", "module", "event", "stream", "state", "ack")

    def __init__(self, sid: str, module: str, event: str, stream: _Stream) -> None:
        self.sid = sid
        self.module = module
        self.event = event
        self.stream = stream
        self.state = "pending"
        self.ack: Mapping[str, Any] | None = None


def _release(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


class _Stream:
    def __init__(self, max_pending: int | None) -> None:
        self.items: collections.deque[Event] = collections.deque()
        self.entries: list[_SubEntry] = []
        self.max_pending = max_pending
        self.high_water = 0
        self.received = 0
        self.ended = False
        self._warned = False
        self._end_factory: Callable[[], BaseException] | None = None
        self._waiters: list[asyncio.Future[None]] = []

    def full(self) -> bool:
        return self.max_pending is not None and len(self.items) >= self.max_pending

    def push(self, event: Event) -> None:
        self.items.append(event)
        self.received += 1
        depth = len(self.items)
        if depth > self.high_water:
            self.high_water = depth
        if depth >= PENDING_WARNING_THRESHOLD and not self._warned:
            self._warned = True
            ids = ", ".join(e.sid for e in self.entries)
            warn(
                f"{depth} unconsumed events queued on subscription {ids}; the queue is unbounded "
                "(pass max_pending= to bound it)"
            )
        self._wake()

    def end(self, factory: Callable[[], BaseException] | None) -> None:
        """Stop accepting events. Queued events stay readable, then ``factory()`` is raised
        (``None``: the client closed it, and iteration simply ends)."""
        if self.ended:
            return
        self.ended = True
        self._end_factory = factory
        self._wake()

    def terminal_exception(self) -> BaseException:
        if self._end_factory is None:
            return SubscriptionClosed("the subscription was closed by this client")
        return self._end_factory()

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            _release(waiter)

    async def get(self, timeout: float | None) -> Event:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if self.items:
                return self.items.popleft()
            if self.ended:
                raise self.terminal_exception()
            if deadline is not None and loop.time() >= deadline:
                assert timeout is not None
                raise ClientTimeout(timeout, what="waiting for an event")
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            handle = loop.call_at(deadline, _release, waiter) if deadline is not None else None
            try:
                await waiter
            finally:
                if handle is not None:
                    handle.cancel()
                if waiter in self._waiters:
                    self._waiters.remove(waiter)


class AsyncSubscription:
    """An ordered stream of :class:`~logos_bridge.models.Event`.

    Iterate it (``async for``), or call :meth:`get`. After the bridge terminates the
    subscription or the connection closes, queued events are still delivered, then
    the cause is raised (:class:`~logos_bridge.errors.SubscriptionTerminated`,
    :class:`~logos_bridge.errors.ConnectionClosed`,
    :class:`~logos_bridge.errors.SubscriptionOverflow`). After :meth:`unsubscribe`,
    iteration ends once the queue is drained.
    """

    def __init__(self, client: AsyncBridgeClient, stream: _Stream) -> None:
        self._client = client
        self._stream = stream

    def __repr__(self) -> str:
        state = "ended" if self._stream.ended else "open"
        return f"<AsyncSubscription {', '.join(self.ids)} {state} pending={self.pending}>"

    @property
    def ids(self) -> tuple[str, ...]:
        """The bridge subscription ids, one per event."""
        return tuple(e.sid for e in self._stream.entries)

    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        return tuple((e.module, e.event) for e in self._stream.entries)

    @property
    def module(self) -> str:
        return self._stream.entries[0].module

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(e.event for e in self._stream.entries)

    @property
    def acks(self) -> tuple[Mapping[str, Any], ...]:
        """The ``rpc.subscribe`` results (``state`` is ``"registered"``, or ``"active"`` for a duplicate)."""
        return tuple(e.ack for e in self._stream.entries if e.ack is not None)

    @property
    def pending(self) -> int:
        """Events received and not yet consumed."""
        return len(self._stream.items)

    @property
    def high_water(self) -> int:
        """The largest ``pending`` seen."""
        return self._stream.high_water

    @property
    def received(self) -> int:
        return self._stream.received

    @property
    def max_pending(self) -> int | None:
        return self._stream.max_pending

    @property
    def ended(self) -> bool:
        """No further events will be queued."""
        return self._stream.ended

    def __aiter__(self) -> AsyncSubscription:
        return self

    async def __anext__(self) -> Event:
        self._client._check_loop()
        try:
            return await self._stream.get(None)
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def get(self, timeout: float | None = None) -> Event:
        """The next event. Raises :class:`~logos_bridge.errors.ClientTimeout` after ``timeout``,
        and :class:`~logos_bridge.errors.SubscriptionClosed` once unsubscribed and drained."""
        self._client._check_loop()
        return await self._stream.get(timeout)

    async def unsubscribe(self) -> None:
        """Stop the stream and wait for the bridge to acknowledge. Idempotent.

        A closed connection is not an error here: its subscriptions are gone already.
        """
        await self._client._close_stream(self._stream)

    def cancel(self) -> None:
        """Stop the stream now; the unsubscribe is sent in the background. Loop thread only."""
        self._client._close_stream_nowait(self._stream)

    async def aclose(self) -> None:
        """Like :meth:`unsubscribe`, but never raises for bridge errors."""
        try:
            await self.unsubscribe()
        except BridgeError as exc:
            logger.debug("unsubscribe of %s failed: %s", self.ids, exc)

    async def __aenter__(self) -> AsyncSubscription:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class SubscribeRequest:
    """What ``subscribe()`` returns: ``await`` it, or use it with ``async with``.

    ``async with`` unsubscribes on exit.
    """

    def __init__(
        self,
        client: AsyncBridgeClient,
        targets: Sequence[tuple[str, str]],
        *,
        timeout: float | None,
        max_pending: int | None,
    ) -> None:
        if max_pending is not None and (
            not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 1
        ):
            raise ValueError("max_pending must be a positive integer or None")
        for module, event in targets:
            if not isinstance(module, str) or not module:
                raise ValueError("module must be a non-empty string")
            if not isinstance(event, str) or not event:
                raise ValueError("event names must be non-empty strings")
        self._client = client
        self._targets = tuple(targets)
        self._timeout = timeout
        self._max_pending = max_pending
        self._used = False
        self._subscription: AsyncSubscription | None = None

    def __await__(self) -> Generator[Any, None, AsyncSubscription]:
        return self._start().__await__()

    async def _start(self) -> AsyncSubscription:
        if self._used:
            raise RuntimeError("a subscribe() request can be awaited or entered only once")
        self._used = True
        self._subscription = await self._client._subscribe(self._targets, self._timeout, self._max_pending)
        return self._subscription

    async def __aenter__(self) -> AsyncSubscription:
        return await self._start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._subscription is not None:
            await self._subscription.aclose()
