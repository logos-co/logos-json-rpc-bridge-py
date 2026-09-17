"""A background event loop that blocking code can call into.

:class:`BlockingPortal` owns two daemon threads: one runs an asyncio loop, the other
runs callbacks (:meth:`BlockingPortal.dispatch`, :meth:`BlockingPortal.pump`) so user
code never runs on the loop and may itself make blocking calls through the portal.
The threads reference only an internal core object, so an unreferenced portal is
garbage collected and ``weakref.finalize`` stops them (also at interpreter exit).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import queue
import threading
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator
from types import TracebackType
from typing import Any, Generic, ParamSpec, TypeVar

from .errors import BlockingCallInEventLoop, ClientTimeout, PortalStopped

logger = logging.getLogger("logos_bridge")

P = ParamSpec("P")
T = TypeVar("T")

_Job = tuple[Callable[..., object], tuple[Any, ...]]


async def _invoke(fn: Callable[..., Awaitable[T]], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
    return await fn(*args, **kwargs)


async def _anext(iterator: AsyncIterator[T]) -> T:
    return await iterator.__anext__()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class _PortalCore:
    """State shared with the threads. Holds no reference to the portal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.loop = asyncio.new_event_loop()
        self.stopped = False
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        ready = threading.Event()
        self.loop_thread = threading.Thread(target=self._run_loop, args=(ready,), name=f"{name}-loop", daemon=True)
        self.dispatch_thread = threading.Thread(target=self._run_dispatch, name=f"{name}-dispatch", daemon=True)
        self.loop_thread.start()
        self.dispatch_thread.start()
        ready.wait()

    def _run_loop(self, ready: threading.Event) -> None:
        loop = self.loop
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:  # pragma: no cover - best effort teardown
                logger.debug("portal %s: loop teardown failed", self.name, exc_info=True)
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def _run_dispatch(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            fn, args = job
            try:
                fn(*args)
            except Exception:
                logger.exception("portal %s: dispatched callback failed", self.name)

    def dispatch(self, fn: Callable[..., object], *args: Any) -> None:
        if self.stopped:
            raise PortalStopped(f"portal {self.name} is stopped")
        self._queue.put((fn, args))

    def shutdown(self, timeout: float | None = 5.0) -> None:
        with self._lock:
            if self.stopped:
                first = False
            else:
                self.stopped = True
                first = True
        current = threading.current_thread()
        if first:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:  # the loop is already closed
                pass
            self._queue.put(None)
        if current is not self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout)
        if current is not self.dispatch_thread and self.dispatch_thread.is_alive():
            self.dispatch_thread.join(timeout)


class PumpHandle(Generic[T]):
    """A running :meth:`BlockingPortal.pump`. :meth:`cancel` stops callbacks for good."""

    def __init__(
        self,
        core: _PortalCore,
        callback: Callable[[T], object],
        error_callback: Callable[[BaseException], object] | None,
        on_cancel: Callable[[], object] | None,
    ) -> None:
        self._core = core
        self._callback = callback
        self._error_callback = error_callback
        self._on_cancel = on_cancel
        self._lock = threading.RLock()
        self._cancelled = False
        self._finished = threading.Event()
        self._future: concurrent.futures.Future[None] | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def finished(self) -> bool:
        """The source is exhausted (or failed, or cancelled) and no callback is queued."""
        return self._finished.is_set()

    async def _run(self, source: AsyncIterable[T]) -> None:
        try:
            async for item in source:
                if self._cancelled:
                    return
                self._core.dispatch(self._deliver, item)
        except PortalStopped:
            return
        except Exception as exc:
            if not self._cancelled:
                with contextlib.suppress(PortalStopped):
                    self._core.dispatch(self._report_failure, exc)
        finally:
            try:
                self._core.dispatch(self._finished.set)
            except Exception:
                self._finished.set()

    def _deliver(self, item: T) -> None:
        with self._lock:
            if self._cancelled:
                return
            try:
                self._callback(item)
            except Exception as exc:
                self._report(exc)

    def _report_failure(self, exc: BaseException) -> None:
        with self._lock:
            if not self._cancelled:
                self._report(exc)

    def _report(self, exc: BaseException) -> None:
        if self._error_callback is None:
            logger.error("logos-bridge: event callback failed", exc_info=exc)
            return
        try:
            self._error_callback(exc)
        except Exception:
            logger.exception("logos-bridge: error_callback failed")

    def cancel(self) -> None:
        """Guarantee no further callbacks.

        From any thread but the portal's loop thread this also waits for a callback that
        is running right now; from inside a callback it returns immediately.
        """
        first = not self._cancelled
        self._cancelled = True
        if threading.current_thread() is not self._core.loop_thread:
            with self._lock:  # re-entrant: fine from inside our own callback
                pass
        if not first:
            return
        if self._future is not None:
            self._future.cancel()
        if self._on_cancel is not None and not self._core.stopped:
            try:
                self._core.loop.call_soon_threadsafe(self._on_cancel)
            except RuntimeError:
                pass

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until :attr:`finished`; returns whether it did."""
        return self._finished.wait(timeout)


class BlockingPortal:
    """Run coroutines on a private event loop from synchronous code.

    Blocking entry points (:meth:`call`, :meth:`call_with_timeout`, :meth:`iterate`)
    refuse to run on the portal's own loop thread (a deadlock) and, unless
    ``allow_blocking_in_event_loop`` is set, on any thread running an event loop (a
    stall): both raise :class:`~logos_bridge.errors.BlockingCallInEventLoop`.
    """

    def __init__(self, *, name: str = "logos-bridge", allow_blocking_in_event_loop: bool = False) -> None:
        self._core = _PortalCore(name)
        self._allow_blocking_in_event_loop = allow_blocking_in_event_loop
        self._finalizer = weakref.finalize(self, _PortalCore.shutdown, self._core)

    def __repr__(self) -> str:
        return f"<BlockingPortal {self._core.name} {'stopped' if self.stopped else 'running'}>"

    @property
    def name(self) -> str:
        return self._core.name

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._core.loop

    @property
    def stopped(self) -> bool:
        return self._core.stopped

    @property
    def loop_thread(self) -> threading.Thread:
        return self._core.loop_thread

    @property
    def dispatch_thread(self) -> threading.Thread:
        return self._core.dispatch_thread

    def __enter__(self) -> BlockingPortal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _check_stopped(self) -> None:
        if self._core.stopped:
            raise PortalStopped(f"portal {self._core.name} is stopped")

    def _check_blocking(self) -> None:
        self._check_stopped()
        if threading.current_thread() is self._core.loop_thread:
            raise BlockingCallInEventLoop(
                "blocking portal call from the portal's own event loop would deadlock; await the coroutine instead"
            )
        if not self._allow_blocking_in_event_loop and _running_loop() is not None:
            raise BlockingCallInEventLoop(
                "blocking portal call from a thread running an event loop would stall it; "
                "use the async API, or pass allow_blocking_in_event_loop=True"
            )

    def submit(self, fn: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs) -> concurrent.futures.Future[T]:
        """Schedule ``fn(*args, **kwargs)`` on the loop; never blocks."""
        self._check_stopped()
        coro = _invoke(fn, args, kwargs)
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._core.loop)
        except RuntimeError:
            coro.close()
            raise PortalStopped(f"portal {self._core.name} is stopped") from None

    def call(self, fn: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run ``fn(*args, **kwargs)`` on the loop and return its result (or raise its exception)."""
        self._check_blocking()
        return self._wait(self.submit(fn, *args, **kwargs), None)

    def call_with_timeout(
        self, timeout: float | None, fn: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """Like :meth:`call`; after ``timeout`` the coroutine is cancelled and
        :class:`~logos_bridge.errors.ClientTimeout` is raised."""
        self._check_blocking()
        return self._wait(self.submit(fn, *args, **kwargs), timeout)

    def _wait(self, future: concurrent.futures.Future[T], timeout: float | None) -> T:
        try:
            done, _ = concurrent.futures.wait([future], timeout=timeout)
        except BaseException:  # e.g. KeyboardInterrupt: do not leave the coroutine running
            future.cancel()
            raise
        if not done:
            future.cancel()
            assert timeout is not None
            raise ClientTimeout(timeout, what="blocking portal call")
        try:
            return future.result()
        except concurrent.futures.CancelledError:
            if self._core.stopped:
                raise PortalStopped(f"portal {self._core.name} stopped during the call") from None
            raise

    def run_sync_soon(self, fn: Callable[..., object], /, *args: Any) -> None:
        """Call ``fn(*args)`` on the loop thread, soon. Never blocks."""
        self._check_stopped()
        try:
            self._core.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            raise PortalStopped(f"portal {self._core.name} is stopped") from None

    def iterate(self, source: AsyncIterable[T]) -> Iterator[T]:
        """Iterate an async iterable from blocking code."""
        iterator = source.__aiter__()
        while True:
            try:
                item = self.call(_anext, iterator)
            except StopAsyncIteration:
                return
            yield item

    def dispatch(self, fn: Callable[..., object], /, *args: Any) -> None:
        """Run ``fn(*args)`` on the dispatch thread, in order. Exceptions are logged."""
        self._core.dispatch(fn, *args)

    def pump(
        self,
        source: AsyncIterable[T],
        callback: Callable[[T], object],
        *,
        error_callback: Callable[[BaseException], object] | None = None,
        on_cancel: Callable[[], object] | None = None,
    ) -> PumpHandle[T]:
        """Consume ``source`` on the loop and hand each item to ``callback`` on the dispatch thread.

        Exceptions from ``callback`` and the one that ends ``source`` go to
        ``error_callback`` (or the log). ``on_cancel`` runs on the loop thread when the
        handle is cancelled.
        """
        handle: PumpHandle[T] = PumpHandle(self._core, callback, error_callback, on_cancel)
        handle._future = self.submit(handle._run, source)
        return handle

    def stop(self, timeout: float | None = 5.0) -> None:
        """Stop the loop (cancelling its tasks) and the dispatch thread. Idempotent.

        Callbacks already queued still run. Safe from the portal's own threads, which
        are then not joined.
        """
        self._finalizer.detach()
        self._core.shutdown(timeout)
