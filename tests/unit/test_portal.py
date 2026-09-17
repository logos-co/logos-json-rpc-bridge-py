from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import threading
import time
import traceback
from collections.abc import AsyncIterator
from typing import Any

import pytest

from logos_bridge.errors import BlockingCallInEventLoop, ClientTimeout, PortalStopped
from logos_bridge.portal import BlockingPortal


class Boom(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"boom: {detail}")
        self.detail = detail


@pytest.fixture
def portal() -> Any:
    with BlockingPortal(name="test-portal") as p:
        yield p


async def where() -> str:
    await asyncio.sleep(0)
    return threading.current_thread().name


async def add(a: int, b: int = 0) -> int:
    return a + b


def test_call_runs_on_the_loop_thread(portal: BlockingPortal) -> None:
    assert portal.call(where) == "test-portal-loop"
    assert portal.call(add, 2, b=3) == 5
    assert portal.loop_thread.daemon and portal.dispatch_thread.daemon
    assert "running" in repr(portal)


def test_exceptions_are_preserved(portal: BlockingPortal) -> None:
    async def explode() -> None:
        await asyncio.sleep(0)
        raise Boom("detail")

    with pytest.raises(Boom) as excinfo:
        portal.call(explode)
    assert excinfo.value.detail == "detail"
    frames = [frame.name for frame in traceback.extract_tb(excinfo.value.__traceback__)]
    assert "explode" in frames


def test_call_with_timeout_cancels_the_coroutine(portal: BlockingPortal) -> None:
    cancelled = threading.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ClientTimeout):
        portal.call_with_timeout(0.05, forever)
    assert cancelled.wait(5)
    assert portal.call_with_timeout(5, add, 1, 1) == 2
    assert portal.call_with_timeout(None, add, 1) == 1


def test_a_timeout_raised_by_the_coroutine_is_not_masked(portal: BlockingPortal) -> None:
    async def own_timeout() -> None:
        raise TimeoutError("mine")

    with pytest.raises(TimeoutError, match="mine") as excinfo:
        portal.call_with_timeout(5, own_timeout)
    assert not isinstance(excinfo.value, ClientTimeout)


def test_blocking_from_the_portal_loop_is_refused(portal: BlockingPortal) -> None:
    async def reenter() -> int:
        return portal.call(add, 1)

    with pytest.raises(BlockingCallInEventLoop, match="deadlock"):
        portal.call(reenter)


def test_blocking_from_another_event_loop_is_refused(portal: BlockingPortal) -> None:
    async def inside() -> int:
        return portal.call(add, 1)

    with pytest.raises(BlockingCallInEventLoop, match="allow_blocking_in_event_loop"):
        asyncio.run(inside())
    with BlockingPortal(allow_blocking_in_event_loop=True) as permissive:
        async def allowed() -> int:
            return permissive.call(add, 41, 1)

        assert asyncio.run(allowed()) == 42


def test_submit_and_run_sync_soon(portal: BlockingPortal) -> None:
    future = portal.submit(add, 20, 22)
    assert isinstance(future, concurrent.futures.Future)
    assert future.result(5) == 42
    seen: list[str] = []
    done = threading.Event()

    def mark() -> None:
        seen.append(threading.current_thread().name)
        done.set()

    portal.run_sync_soon(mark)
    assert done.wait(5) and seen == ["test-portal-loop"]


def test_iterate(portal: BlockingPortal) -> None:
    async def numbers() -> AsyncIterator[int]:
        for i in range(3):
            await asyncio.sleep(0)
            yield i

    assert list(portal.iterate(numbers())) == [0, 1, 2]


def test_dispatch_runs_in_order_on_its_own_thread(portal: BlockingPortal) -> None:
    seen: list[tuple[int, str]] = []
    done = threading.Event()
    for i in range(5):
        portal.dispatch(lambda n: seen.append((n, threading.current_thread().name)), i)
    portal.dispatch(done.set)
    assert done.wait(5)
    assert seen == [(i, "test-portal-dispatch") for i in range(5)]


def test_a_failing_dispatched_callback_is_logged(portal: BlockingPortal, caplog: pytest.LogCaptureFixture) -> None:
    done = threading.Event()

    def fail() -> None:
        raise RuntimeError("callback bug")

    portal.dispatch(fail)
    portal.dispatch(done.set)
    assert done.wait(5)
    assert "callback bug" in caplog.text


async def source(items: list[Any], error: BaseException | None = None) -> AsyncIterator[Any]:
    for item in items:
        await asyncio.sleep(0)
        yield item
    if error is not None:
        raise error


def test_pump_delivers_and_reports(portal: BlockingPortal) -> None:
    received: list[Any] = []
    errors: list[BaseException] = []

    def callback(item: Any) -> None:
        if item == "bad":
            raise ValueError("callback failed")
        received.append((item, threading.current_thread().name))

    handle = portal.pump(source([1, "bad", 2], Boom("end")), callback, error_callback=errors.append)
    assert handle.wait(5) and handle.finished and not handle.cancelled
    assert received == [(1, "test-portal-dispatch"), (2, "test-portal-dispatch")]
    assert [type(e) for e in errors] == [ValueError, Boom]


def test_pump_errors_go_to_the_log_without_an_error_callback(
    portal: BlockingPortal, caplog: pytest.LogCaptureFixture
) -> None:
    handle = portal.pump(source([], Boom("quiet")), lambda item: None)
    assert handle.wait(5)
    assert "boom: quiet" in caplog.text


def test_pump_cancel_waits_for_a_running_callback(portal: BlockingPortal) -> None:
    started = threading.Event()
    log: list[str] = []

    def slow(item: Any) -> None:
        started.set()
        time.sleep(0.2)
        log.append(f"done {item}")

    async def endless() -> AsyncIterator[int]:
        i = 0
        while True:
            yield i
            i += 1
            await asyncio.sleep(0.01)

    handle = portal.pump(endless(), slow)
    assert started.wait(5)
    handle.cancel()
    finished_at = len(log)
    assert finished_at >= 1  # cancel() returned only after the running callback ended
    time.sleep(0.3)
    assert len(log) == finished_at and handle.cancelled
    assert handle.wait(5)


def test_pump_cancel_from_inside_a_callback(portal: BlockingPortal) -> None:
    calls: list[int] = []
    cancelled_hook = threading.Event()
    holder: dict[str, Any] = {}

    def once(item: int) -> None:
        calls.append(item)
        holder["handle"].cancel()

    ready = threading.Event()

    async def burst() -> AsyncIterator[int]:
        await asyncio.to_thread(ready.wait, 5)
        for i in range(10):
            yield i

    holder["handle"] = portal.pump(burst(), once, on_cancel=cancelled_hook.set)
    ready.set()
    assert cancelled_hook.wait(5)
    time.sleep(0.1)
    assert calls == [0]


def test_pump_cancel_from_the_loop_thread_does_not_block(portal: BlockingPortal) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(item: Any) -> None:
        started.set()
        release.wait(5)

    async def one() -> AsyncIterator[int]:
        yield 1
        await asyncio.sleep(60)

    handle = portal.pump(one(), blocking)
    assert started.wait(5)

    async def cancel_on_loop() -> None:
        handle.cancel()  # must not wait for the callback: that would stall the loop

    portal.call_with_timeout(2, cancel_on_loop)
    release.set()
    assert handle.wait(5)


def test_stop_joins_and_refuses_new_work() -> None:
    portal = BlockingPortal(name="stopper")
    loop_thread, dispatch_thread = portal.loop_thread, portal.dispatch_thread
    portal.stop()
    assert portal.stopped and "stopped" in repr(portal)
    assert not loop_thread.is_alive() and not dispatch_thread.is_alive()
    assert portal.loop.is_closed()
    with pytest.raises(PortalStopped):
        portal.call(add, 1)
    with pytest.raises(PortalStopped):
        portal.submit(add, 1)
    with pytest.raises(PortalStopped):
        portal.dispatch(print)
    with pytest.raises(PortalStopped):
        portal.run_sync_soon(print)
    portal.stop()  # idempotent


def test_stop_ends_blocked_calls() -> None:
    portal = BlockingPortal()
    outcome: list[BaseException] = []

    def blocked() -> None:
        try:
            portal.call(asyncio.sleep, 60)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=blocked)
    thread.start()
    time.sleep(0.1)
    portal.stop()
    thread.join(5)
    assert len(outcome) == 1 and isinstance(outcome[0], PortalStopped)


def test_stop_from_the_portal_threads() -> None:
    for via in ("dispatch", "loop"):
        portal = BlockingPortal()
        threads = (portal.loop_thread, portal.dispatch_thread)
        if via == "dispatch":
            portal.dispatch(portal.stop)
        else:
            portal.run_sync_soon(portal.stop)
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert portal.stopped


def test_queued_callbacks_still_run_at_stop() -> None:
    portal = BlockingPortal()
    seen: list[int] = []
    for i in range(3):
        portal.dispatch(seen.append, i)
    portal.stop()
    assert seen == [0, 1, 2]


def test_an_unreferenced_portal_is_finalized() -> None:
    portal = BlockingPortal(name="garbage")
    threads = (portal.loop_thread, portal.dispatch_thread)
    del portal
    gc.collect()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


def test_the_threads_do_not_keep_the_portal_alive() -> None:
    import weakref

    portal = BlockingPortal()
    ref = weakref.ref(portal)
    assert portal.call(add, 1) == 1
    del portal
    gc.collect()
    assert ref() is None
