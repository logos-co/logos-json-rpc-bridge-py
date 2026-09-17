"""Typed event streams over :class:`~logos_bridge.subscription.AsyncSubscription`.

A payload that does not match its declaration fails that one item with
:class:`~logos_bridge.errors.EventDecodeError`; the stream goes on.
:meth:`TypedSubscription.results` yields such errors as items instead. The stream
ends as the raw one does: :class:`~logos_bridge.errors.SubscriptionTerminated`
(``provider_unavailable`` or ``provider_changed``; after the latter, check
compatibility again), :class:`~logos_bridge.errors.ConnectionClosed`, or plain
iteration end after an unsubscribe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Generator, Iterator, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..errors import EventDecodeError
from ..models import Event
from ..subscription import AsyncSubscription, SubscribeRequest

if TYPE_CHECKING:
    from ..portal import BlockingPortal

E = TypeVar("E")


class TypedSubscription(Generic[E]):
    """An ordered stream of decoded events (``E``)."""

    def __init__(self, raw: AsyncSubscription, decode: Callable[[Event], E]) -> None:
        self._raw = raw
        self._decode = decode

    def __repr__(self) -> str:
        return f"<TypedSubscription {self._raw!r}>"

    @property
    def raw(self) -> AsyncSubscription:
        return self._raw

    @property
    def ids(self) -> tuple[str, ...]:
        return self._raw.ids

    @property
    def module(self) -> str:
        return self._raw.module

    @property
    def events(self) -> tuple[str, ...]:
        return self._raw.events

    @property
    def acks(self) -> tuple[Mapping[str, Any], ...]:
        return self._raw.acks

    @property
    def pending(self) -> int:
        return self._raw.pending

    @property
    def high_water(self) -> int:
        return self._raw.high_water

    @property
    def received(self) -> int:
        return self._raw.received

    @property
    def ended(self) -> bool:
        return self._raw.ended

    def decode(self, event: Event) -> E:
        return self._decode(event)

    def _decode_or_error(self, event: Event) -> E | EventDecodeError:
        try:
            return self._decode(event)
        except EventDecodeError as exc:
            return exc

    def __aiter__(self) -> TypedSubscription[E]:
        return self

    async def __anext__(self) -> E:
        return self._decode(await self._raw.__anext__())

    async def get(self, timeout: float | None = None) -> E:
        """The next event (see :meth:`AsyncSubscription.get`)."""
        return self._decode(await self._raw.get(timeout))

    async def next_result(self) -> E | EventDecodeError:
        """The next event or its decode error; ``StopAsyncIteration`` after unsubscribe."""
        return self._decode_or_error(await self._raw.__anext__())

    async def results(self) -> AsyncIterator[E | EventDecodeError]:
        """Every item, with decode failures as values rather than exceptions."""
        while True:
            try:
                item = await self.next_result()
            except StopAsyncIteration:
                return
            yield item

    async def unsubscribe(self) -> None:
        await self._raw.unsubscribe()

    def cancel(self) -> None:
        self._raw.cancel()

    async def aclose(self) -> None:
        await self._raw.aclose()

    async def __aenter__(self) -> TypedSubscription[E]:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()


class TypedSubscribeRequest(Generic[E]):
    """What a typed ``on_*()``/``events()`` returns: ``await`` it or use ``async with``."""

    def __init__(self, raw: SubscribeRequest, decode: Callable[[Event], E]) -> None:
        self._raw = raw
        self._decode = decode
        self._subscription: TypedSubscription[E] | None = None

    def __await__(self) -> Generator[Any, None, TypedSubscription[E]]:
        return self._start().__await__()

    async def _start(self) -> TypedSubscription[E]:
        self._subscription = TypedSubscription(await self._raw, self._decode)
        return self._subscription

    async def __aenter__(self) -> TypedSubscription[E]:
        return await self._start()

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        if self._subscription is not None:
            await self._subscription.aclose()


async def _open(request: TypedSubscribeRequest[E]) -> TypedSubscription[E]:
    return await request


class BlockingTypedSubscription(Generic[E]):
    """The blocking view of a :class:`TypedSubscription`."""

    def __init__(self, aio: TypedSubscription[E], portal: BlockingPortal) -> None:
        self._aio = aio
        self._portal = portal

    @classmethod
    def open(cls, request: TypedSubscribeRequest[E], portal: BlockingPortal) -> BlockingTypedSubscription[E]:
        return cls(portal.call(_open, request), portal)

    def __repr__(self) -> str:
        return f"<BlockingTypedSubscription {self._aio.raw!r}>"

    @property
    def aio(self) -> TypedSubscription[E]:
        return self._aio

    @property
    def ids(self) -> tuple[str, ...]:
        return self._aio.ids

    @property
    def events(self) -> tuple[str, ...]:
        return self._aio.events

    @property
    def pending(self) -> int:
        return self._aio.pending

    @property
    def ended(self) -> bool:
        return self._aio.ended

    def __iter__(self) -> Iterator[E]:
        return self

    def __next__(self) -> E:
        try:
            return self._portal.call(self._aio.__anext__)
        except StopAsyncIteration:
            raise StopIteration from None

    def get(self, timeout: float | None = None) -> E:
        return self._portal.call(self._aio.get, timeout)

    def results(self) -> Iterator[E | EventDecodeError]:
        while True:
            try:
                item = self._portal.call(self._aio.next_result)
            except StopAsyncIteration:
                return
            yield item

    def unsubscribe(self) -> None:
        self._portal.call(self._aio.unsubscribe)

    def cancel(self) -> None:
        self._portal.run_sync_soon(self._aio.cancel)

    def close(self) -> None:
        if not self._portal.stopped:
            self._portal.call(self._aio.aclose)

    def __enter__(self) -> BlockingTypedSubscription[E]:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()
