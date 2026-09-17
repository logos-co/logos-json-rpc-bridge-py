"""A :class:`FakeBridge` running on its own :class:`~logos_bridge.portal.BlockingPortal`."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any, TypeVar

from ..portal import BlockingPortal
from ._fake import FakeBridge, FakeModule, FakeSubscription, Handshake, HttpExchange, RecordedRequest

T = TypeVar("T")


async def _run_sync(fn: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
    return fn(*args, **kwargs)


class ThreadedFakeBridge:
    """Blocking twin of :class:`FakeBridge` for synchronous tests.

    ``with ThreadedFakeBridge() as fake:``. Handlers run on the fake's own loop thread.
    Keyword arguments are passed to :class:`FakeBridge`.
    """

    def __init__(self, **options: Any) -> None:
        self._options = options
        self._portal: BlockingPortal | None = None
        self._fake: FakeBridge | None = None

    def __enter__(self) -> ThreadedFakeBridge:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    async def _start_fake(self) -> FakeBridge:
        return await FakeBridge(**self._options).start()

    def start(self) -> ThreadedFakeBridge:
        if self._portal is not None:
            return self
        portal = BlockingPortal(name="fake-bridge")
        try:
            self._fake = portal.call(self._start_fake)
        except BaseException:
            portal.stop()
            raise
        self._portal = portal
        return self

    def stop(self) -> None:
        portal, self._portal = self._portal, None
        fake, self._fake = self._fake, None
        if portal is None:
            return
        try:
            if fake is not None and not portal.stopped:
                portal.call(fake.stop)
        finally:
            portal.stop()

    @property
    def portal(self) -> BlockingPortal:
        if self._portal is None:
            raise RuntimeError("ThreadedFakeBridge is not running")
        return self._portal

    @property
    def fake(self) -> FakeBridge:
        """The underlying :class:`FakeBridge`; touch it only through this wrapper's methods."""
        if self._fake is None:
            raise RuntimeError("ThreadedFakeBridge is not running")
        return self._fake

    def _sync(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self.portal.call(_run_sync, fn, args, kwargs)

    @property
    def url(self) -> str:
        return self.fake.url

    @property
    def http_url(self) -> str:
        return self.fake.http_url

    @property
    def port(self) -> int:
        return self.fake.port

    @property
    def http_port(self) -> int:
        return self.fake.http_port

    def module(
        self,
        name: str,
        methods: Iterable[Any] | Mapping[str, Sequence[str]] | None = None,
        events: Iterable[str] | None = None,
        **options: Any,
    ) -> FakeModule:
        table = methods if methods is None or isinstance(methods, Mapping) else list(methods)
        return self._sync(self.fake.module, name, table, None if events is None else list(events), **options)

    def remove_module(self, name: str) -> None:
        self._sync(self.fake.remove_module, name)

    def mark_stale(self, name: str, stale: bool = True) -> None:
        self._sync(self.fake.mark_stale, name, stale)

    def on_call(self, module: str, method: str, handler: Any) -> None:
        self._sync(self.fake.on_call, module, method, handler)

    def on_subscribe(self, module: str, event: str, handler: Any) -> None:
        self._sync(self.fake.on_subscribe, module, event, handler)

    def emit(self, module: str, event: str, *data: Any, generation: int | None = None) -> int:
        return self._sync(self.fake.emit, module, event, *data, generation=generation)

    def emit_json(self, module: str, event: str, payload: list[Any], *, generation: int | None = None) -> int:
        return self._sync(self.fake.emit_json, module, event, payload, generation=generation)

    def terminate(self, module: str, event: str | None = None, **options: Any) -> int:
        return self._sync(self.fake.terminate, module, event, **options)

    def send_raw(self, frame: Any, *, connection: int | None = None) -> int:
        return self._sync(self.fake.send_raw, frame, connection=connection)

    def abort_connections(self) -> None:
        self._sync(self.fake.abort_connections)

    def close_connections(self, code: int = 1000, reason: str = "") -> None:
        self.portal.call(self.fake.close_connections, code, reason)

    def freeze(self) -> None:
        self._sync(self.fake.freeze)

    def unfreeze(self) -> None:
        self._sync(self.fake.unfreeze)

    def set_draining(self, draining: bool = True) -> None:
        self._sync(self.fake.set_draining, draining)

    def subscriptions(self, module: str | None = None, event: str | None = None) -> list[FakeSubscription]:
        return self._sync(self.fake.subscriptions, module, event)

    def wait_for_request(self, method: str | None = None, **options: Any) -> RecordedRequest:
        return self.portal.call(self.fake.wait_for_request, method, **options)

    def wait_for_subscription(self, module: str, event: str, **options: Any) -> FakeSubscription:
        return self.portal.call(self.fake.wait_for_subscription, module, event, **options)

    @property
    def requests(self) -> list[RecordedRequest]:
        return self._sync(lambda: list(self.fake.requests))

    @property
    def handshakes(self) -> list[Handshake]:
        return self._sync(lambda: list(self.fake.handshakes))

    @property
    def http_requests(self) -> list[HttpExchange]:
        return self._sync(lambda: list(self.fake.http_requests))

    @property
    def handler_errors(self) -> list[BaseException]:
        return self._sync(lambda: list(self.fake.handler_errors))
