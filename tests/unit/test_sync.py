from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from logos_bridge import AsyncBridgeClient, AsyncSubscription, BridgeClient, Event, Subscription
from logos_bridge.errors import (
    BlockingCallInEventLoop,
    ClientTimeout,
    ConnectionClosed,
    MethodNotFound,
    PortalStopped,
    SubscriptionClosed,
    SubscriptionTerminated,
)
from logos_bridge.portal import BlockingPortal
from logos_bridge.testing import ThreadedFakeBridge

MIRRORED = ["call", "subscribe", "subscribe_many", "ping", "list_modules", "schema", "request"]


def parameters(fn: Any) -> list[tuple[str, Any, Any, Any]]:
    return [(p.name, p.kind, p.default, p.annotation) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize("name", MIRRORED)
def test_signatures_mirror_the_async_client(name: str) -> None:
    assert parameters(getattr(BridgeClient, name)) == parameters(getattr(AsyncBridgeClient, name))


def test_the_constructor_mirrors_the_async_client_plus_portal() -> None:
    sync_params = parameters(BridgeClient.__init__)
    assert sync_params[:-1] == parameters(AsyncBridgeClient.__init__)
    assert sync_params[-1][:3] == ("portal", inspect.Parameter.KEYWORD_ONLY, None)


def test_the_subscription_mirrors_the_async_one() -> None:
    assert parameters(Subscription.get) == parameters(AsyncSubscription.get)
    assert parameters(Subscription.unsubscribe) == parameters(AsyncSubscription.unsubscribe)
    assert parameters(Subscription.cancel) == parameters(AsyncSubscription.cancel)
    for name in ("ids", "targets", "module", "events", "acks", "pending", "high_water", "received",
                 "max_pending", "ended"):
        assert isinstance(getattr(Subscription, name), property)


@pytest.fixture
def fake() -> Iterator[ThreadedFakeBridge]:
    with ThreadedFakeBridge() as bridge:
        bridge.module("m", [("greet", ["who"]), "echo", "fire"], ["tick", "tock"])
        bridge.on_call("m", "greet", lambda ctx: f"hello, {ctx.params[0]}")
        bridge.on_call("m", "echo", lambda ctx: ctx.params)

        def fire(ctx: Any) -> int:
            for value in ctx.params:
                ctx.emit("tick", value)
            return len(ctx.params)

        bridge.on_call("m", "fire", fire)
        yield bridge


def test_blocking_round_trip(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:
        assert bridge.connected and not bridge.closed
        assert bridge.call("m", "greet", "sync") == "hello, sync"
        assert bridge.call("m", "echo", b"\x00", decode_bytes=False) == [{"_bytes": "AA"}]
        assert bridge.ping() >= 0
        assert [m.module for m in bridge.list_modules()] == ["m"]
        assert bridge.schema("m").events == ("tick", "tock")
        assert bridge.request("rpc.ping") == "pong"
        with pytest.raises(MethodNotFound):
            bridge.call("m", "missing")
        with pytest.raises(ValueError):
            bridge.request("rpc.subscribe")
        assert "BridgeClient" in repr(bridge) and bridge.url == fake.url
    assert bridge.closed and bridge.close_exception is not None
    assert bridge.close_exception.code == 1000


def test_subscription_iteration(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:
        with bridge.subscribe("m", "tick") as sub:
            assert bridge.call("m", "fire", 1, 2, 3) == 3
            assert [next(sub).data for _ in range(3)] == [[1], [2], [3]]
            with pytest.raises(ClientTimeout):
                sub.get(0.05)
            bridge.call("m", "fire", 4)
            assert sub.get(2).data == [4]
            assert (sub.module, sub.events, sub.received, sub.ended) == ("m", ("tick",), 4, False)
            assert "Subscription" in repr(sub) and sub.aio.ids == sub.ids
            bridge.call("m", "fire", 5)
            fake.wait_for_request("rpc.call", count=3)
            deadline = time.monotonic() + 5
            while sub.pending < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            sub.unsubscribe()
            assert [event.data for event in sub] == [[5]]  # queued events drain, then iteration stops
        assert fake.subscriptions() == []
        with pytest.raises(SubscriptionClosed):
            sub.get(0)


def test_iteration_ends_after_unsubscribe_and_drain(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:
        sub = bridge.subscribe_many("m", ["tick", "tock"])
        assert len(sub.ids) == 2 and len(sub.acks) == 2 and sub.targets == (("m", "tick"), ("m", "tock"))
        bridge.call("m", "fire", 1, 2)
        fake.emit("m", "tock", "x")
        received: list[Any] = []
        while len(received) < 3:
            received.append(sub.get(2).data)
        bridge.call("m", "fire", 9)
        deadline = time.monotonic() + 5
        while sub.pending < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        sub.unsubscribe()
        assert [event.data for event in sub] == [[9]]
        assert received == [[1], [2], ["x"]]
        with pytest.raises(TypeError):
            bridge.subscribe_many("m", "tick")


def test_callbacks_run_on_the_dispatch_thread_and_may_call_the_client(fake: ThreadedFakeBridge) -> None:
    results: list[tuple[str, Any]] = []
    done = threading.Event()
    with BridgeClient(fake.url) as bridge:

        def on_tick(event: Event) -> None:
            reply = bridge.call("m", "greet", f"#{event.data[0]}")
            results.append((threading.current_thread().name, reply))
            if len(results) == 2:
                done.set()

        handle = bridge.on_event("m", "tick", on_tick)
        bridge.call("m", "fire", 1, 2)
        assert done.wait(5)
        handle.cancel()
    assert results == [("logos-bridge-dispatch", "hello, #1"), ("logos-bridge-dispatch", "hello, #2")]


def test_no_callback_after_cancel_from_inside(fake: ThreadedFakeBridge) -> None:
    calls: list[Any] = []
    with BridgeClient(fake.url) as bridge:
        holder: dict[str, Any] = {}

        def once(event: Event) -> None:
            calls.append(event.data[0])
            holder["handle"].cancel()

        holder["handle"] = bridge.on_event("m", "tick", once)
        bridge.call("m", "fire", *range(20))
        fake.wait_for_request("rpc.unsubscribe")
        time.sleep(0.1)
        assert calls == [0]
        assert holder["handle"].cancelled


def test_no_callback_after_cancel_from_outside(fake: ThreadedFakeBridge) -> None:
    calls: list[Any] = []
    running = threading.Event()
    with BridgeClient(fake.url) as bridge:

        def slow(event: Event) -> None:
            running.set()
            time.sleep(0.1)
            calls.append(event.data[0])

        handle = bridge.on_event("m", "tick", slow)
        bridge.call("m", "fire", *range(10))
        assert running.wait(5)
        handle.cancel()
        count = len(calls)
        assert count >= 1  # the callback that was running finished before cancel() returned
        bridge.call("m", "fire", 99)
        time.sleep(0.3)
        assert len(calls) == count and 99 not in calls
        assert handle.wait(5)


def test_error_callback_receives_callback_errors_and_the_end_of_the_stream(fake: ThreadedFakeBridge) -> None:
    errors: list[BaseException] = []
    done = threading.Event()
    with BridgeClient(fake.url) as bridge:

        def failing(event: Event) -> None:
            raise RuntimeError(f"bad {event.data[0]}")

        def record(exc: BaseException) -> None:
            errors.append(exc)
            if isinstance(exc, SubscriptionTerminated):
                done.set()

        bridge.on_event("m", "tick", failing, error_callback=record)
        bridge.call("m", "fire", 1)
        fake.wait_for_request("rpc.call")
        deadline = time.monotonic() + 5
        while not errors and time.monotonic() < deadline:
            time.sleep(0.01)
        fake.terminate("m")
        assert done.wait(5)
    assert [type(e) for e in errors] == [RuntimeError, SubscriptionTerminated]
    assert str(errors[0]) == "bad 1"


def test_close_ends_callbacks_and_subscriptions(fake: ThreadedFakeBridge) -> None:
    bridge = BridgeClient(fake.url).connect()
    calls: list[Any] = []
    handle = bridge.on_event("m", "tick", calls.append)
    sub = bridge.subscribe("m", "tock")
    portal = bridge.portal
    bridge.close()
    bridge.close()  # idempotent
    assert handle.cancelled
    assert portal.stopped  # the client owned it
    assert not portal.loop_thread.is_alive() and not portal.dispatch_thread.is_alive()
    with pytest.raises(PortalStopped):  # the portal is gone with the client
        sub.get(0)
    sub.cancel()  # harmless after the portal stopped
    with pytest.raises(PortalStopped):
        bridge.ping()


def test_a_shared_portal_is_kept_alive(fake: ThreadedFakeBridge) -> None:
    with BlockingPortal(name="shared") as portal:
        first = BridgeClient(fake.url, portal=portal).connect()
        second = BridgeClient(fake.url, portal=portal).connect()
        assert first.portal is second.portal is portal
        first.close()
        assert not portal.stopped and first.closed
        assert second.call("m", "greet", "still") == "hello, still"
        second.close()
        assert not portal.stopped
        with BridgeClient(fake.url, portal=portal) as third:
            assert third.ping() >= 0
    assert portal.stopped


def test_blocking_calls_inside_an_event_loop_are_refused(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:

        async def misuse() -> None:
            bridge.ping()

        with pytest.raises(BlockingCallInEventLoop):
            asyncio.run(misuse())
        assert bridge.ping() >= 0


def test_wait_closed(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:
        with pytest.raises(ClientTimeout):
            bridge.wait_closed(0.05)
        fake.abort_connections()
        bridge.wait_closed(5)
        closed = bridge.close_exception
        assert closed is not None and closed.code == 1006
        with pytest.raises(ConnectionClosed):
            bridge.ping()
        assert bridge.last_uncorrelated_error is None


def test_an_unclosed_client_is_cleaned_up_by_the_garbage_collector(fake: ThreadedFakeBridge) -> None:
    import gc

    bridge = BridgeClient(fake.url).connect()
    threads = (bridge.portal.loop_thread, bridge.portal.dispatch_thread)
    assert bridge.ping() >= 0
    del bridge
    gc.collect()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    deadline = time.monotonic() + 5
    while any(c.open for c in fake.fake.connections) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.portal.call(_open_connections, fake) == 0


async def _open_connections(fake: ThreadedFakeBridge) -> int:
    return sum(1 for c in fake.fake.connections if c.open)


def test_a_bad_constructor_argument_leaks_no_threads() -> None:
    before = threading.active_count()
    with pytest.raises(ValueError):
        BridgeClient(extra_headers={"Host": "x"})
    assert threading.active_count() == before


def test_subscribe_errors_propagate(fake: ThreadedFakeBridge) -> None:
    with BridgeClient(fake.url) as bridge:
        with pytest.raises(MethodNotFound):
            bridge.subscribe("m", "undeclared")
        with pytest.raises(MethodNotFound):
            bridge.on_event("m", "undeclared", print)
        with pytest.raises(ValueError):
            bridge.subscribe("m", "tick", max_pending=0)
