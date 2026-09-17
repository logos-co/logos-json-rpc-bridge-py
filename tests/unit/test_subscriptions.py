from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bridge_util import eventually, standard_module

from logos_bridge import AsyncBridgeClient
from logos_bridge.errors import (
    BridgeWarning,
    ClientTimeout,
    ConnectionClosed,
    MethodNotFound,
    Overloaded,
    SubscriptionClosed,
    SubscriptionOverflow,
    SubscriptionTerminated,
)
from logos_bridge.testing import FakeBridge, FakeError, NoResponse, SubscribeContext, async_test


def emit_twice(ctx: SubscribeContext) -> None:
    ctx.emit("first")
    ctx.emit("second")


@async_test
async def test_events_may_arrive_before_the_ack() -> None:
    async with FakeBridge(emit_events_before_subscribe_ack=True) as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", emit_twice)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            assert sub.pending == 2  # routed while the ack was still outstanding
            assert sub.acks[0]["state"] == "registered"
            assert [(await sub.get()).data for _ in range(2)] == [["first"], ["second"]]


@async_test
async def test_events_after_the_ack() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", emit_twice)
        async with AsyncBridgeClient(fake.url) as client, client.subscribe("m", "tick") as sub:
            assert [(await sub.get(2)).data for _ in range(2)] == [["first"], ["second"]]


@async_test
async def test_event_fields() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            fake.emit("m", "tick", b"\xfb\xff", {"k": [1]}, generation=5)
            event = await sub.get(2)
            assert (event.subscription, event.module, event.event, event.generation) == (sub.ids[0], "m", "tick", 5)
            assert event.data == [{"_bytes": "-_8"}, {"k": [1]}]  # verbatim
            assert event.decoded_data() == [b"\xfb\xff", {"k": [1]}]
            assert event.ts > 1_600_000_000_000
            assert sub.module == "m" and sub.events == ("tick",) and sub.targets == (("m", "tick"),)
            assert sub.received == 1 and sub.high_water == 1 and sub.pending == 0
            assert sub.ids[0].startswith("s") and "-" in sub.ids[0]


@async_test
async def test_termination_drains_then_raises() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            for i in range(3):
                fake.emit("m", "tick", i)
            fake.terminate("m")
            received = []
            with pytest.raises(SubscriptionTerminated) as excinfo:
                async for event in sub:
                    received.append(event.data[0])
            assert received == [0, 1, 2]
            terminated = excinfo.value
            assert (terminated.subscription, terminated.module, terminated.event, terminated.reason) == (
                sub.ids[0], "m", "tick", "provider_unavailable")
            assert sub.ended and client.active_subscriptions == 0
            with pytest.raises(SubscriptionTerminated):  # stays terminated
                await sub.get()

            again = await client.subscribe("m", "tick")
            assert again.ids[0] != sub.ids[0]
            fake.emit("m", "tick", "after")
            event = await again.get(2)
            assert event.generation == 2 and event.data == ["after"]


@async_test
async def test_a_failed_subscribe_is_retried_under_a_fresh_id() -> None:
    attempts: list[Any] = []

    def fail_once(ctx: SubscribeContext) -> Any:
        attempts.append(ctx.subscription)
        return FakeError(-32601) if len(attempts) == 1 else None

    async with FakeBridge(poison_failed_subscribe_ids=True) as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", fail_once)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(MethodNotFound):
                await client.subscribe("m", "tick")
            assert client.active_subscriptions == 0
            sub = await client.subscribe("m", "tick")
            assert attempts[1] != attempts[0]
            assert sub.acks[0]["state"] == "registered"  # a reused id would be acked "active"
            fake.emit("m", "tick", 7)
            assert (await sub.get(2)).data == [7]


@async_test
async def test_a_subscribe_timeout_leaves_a_zombie_and_unsubscribes() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", NoResponse)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(ClientTimeout):
                await client.subscribe("m", "tick", timeout=0.1)
            subscribed = (await fake.wait_for_request("rpc.subscribe")).params["subscription"]
            unsubscribe = await fake.wait_for_request("rpc.unsubscribe")
            assert unsubscribe.params == {"subscription": subscribed}
            await eventually(lambda: not fake.subscriptions(), what="bridge-side unsubscribe")
            await eventually(lambda: not client._subs, what="zombie removal")
            assert client.active_subscriptions == 0 and client.unrouted_events == 0


@async_test
async def test_a_zombie_drops_its_events() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", NoResponse)
        async with AsyncBridgeClient(fake.url) as client:
            pending = asyncio.ensure_future(client.subscribe("m", "tick", timeout=0.3))
            await fake.wait_for_subscription("m", "tick")
            fake.freeze()  # hold the unsubscribe back so the zombie meets an event
            with pytest.raises(ClientTimeout):
                await pending
            assert fake.emit("m", "tick", 1) == 1
            fake.unfreeze()
            await fake.wait_for_request("rpc.unsubscribe")
            await eventually(lambda: not client._subs, what="zombie removal")
            assert client.unrouted_events == 0


@async_test
async def test_unsubscribe_semantics() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            fake.emit("m", "tick", 1)
            fake.emit("m", "tick", 2)
            await eventually(lambda: sub.pending == 2, what="two events")
            await sub.unsubscribe()
            request = await fake.wait_for_request("rpc.unsubscribe")
            assert request.params == {"subscription": sub.ids[0]}
            assert fake.subscriptions() == [] and sub.ended
            assert fake.emit("m", "tick", 3) == 0
            assert [event.data async for event in sub] == [[1], [2]]  # queued events still drain
            with pytest.raises(SubscriptionClosed):
                await sub.get()
            await sub.unsubscribe()  # idempotent: no second request
            assert [r.method for r in fake.requests].count("rpc.unsubscribe") == 1


@async_test
async def test_async_with_and_cancel() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            async with client.subscribe("m", "tick") as sub:
                assert fake.subscriptions("m", "tick")
            await fake.wait_for_request("rpc.unsubscribe")
            assert sub.ended

            second = await client.subscribe("m", "tock")
            second.cancel()
            assert second.ended
            await fake.wait_for_request("rpc.unsubscribe", count=2)
            await eventually(lambda: not fake.subscriptions(), what="background unsubscribe")

            request = client.subscribe("m", "tick")
            third = await request
            with pytest.raises(RuntimeError, match="once"):
                await request
            await third.aclose()


@async_test
async def test_subscribe_many_is_one_ordered_stream() -> None:
    order = [("tick", 1), ("tock", 1), ("tick", 2), ("tock", 2), ("tock", 3), ("tick", 3)]
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe_many("m", ["tick", "tock"])
            assert len(set(sub.ids)) == 2 and len(sub.acks) == 2
            assert sub.events == ("tick", "tock")
            for event, value in order:
                fake.emit("m", event, value)
            received = [await sub.get(2) for _ in order]
            assert [(e.event, e.data[0]) for e in received] == order
            assert {e.subscription for e in received} == set(sub.ids)

            with pytest.raises(TypeError):
                client.subscribe_many("m", "tick")
            with pytest.raises(ValueError):
                client.subscribe_many("m", [])
            with pytest.raises(ValueError):
                client.subscribe_many("m", ["tick", "tick"])


@async_test
async def test_subscribe_many_ends_when_any_part_terminates() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe_many("m", ["tick", "tock"])
            fake.emit("m", "tock", "x")
            fake.terminate("m", "tick")
            assert (await sub.get(2)).data == ["x"]
            with pytest.raises(SubscriptionTerminated) as excinfo:
                await sub.get(2)
            assert excinfo.value.event == "tick"
            request = await fake.wait_for_request("rpc.unsubscribe")
            assert request.params == {"subscription": sub.ids[1]}


@async_test
async def test_a_failure_partway_through_subscribe_many_releases_the_rest() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tock", FakeError(-32029, "too many subscriptions"))
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(Overloaded):
                await client.subscribe_many("m", ["tick", "tock", "tick2"])
            await fake.wait_for_request("rpc.unsubscribe")
            assert [r.method for r in fake.requests] == ["rpc.subscribe", "rpc.subscribe", "rpc.unsubscribe"]
            assert fake.requests[2].params["subscription"] == fake.requests[0].params["subscription"]
            assert client.active_subscriptions == 0
            await eventually(lambda: not client._subs, what="release")


@pytest.mark.slow
@async_test(timeout=90)
async def test_the_reader_never_blocks_on_unconsumed_events() -> None:
    payload = "x" * 1024
    async with FakeBridge(max_queued_frames=16) as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            with pytest.warns(BridgeWarning, match="10000 unconsumed events"):
                for i in range(10_000):
                    assert fake.emit("m", "tick", i, payload) == 1, f"bridge refused event {i}"
                    if i % 4 == 3:
                        await asyncio.sleep(0)
                await eventually(lambda: sub.pending == 10_000, timeout=30, what="all events")
            assert client.connected and sub.high_water == 10_000
            assert [(await sub.get()).data[0] for _ in range(10_000)] == list(range(10_000))


@async_test
async def test_max_pending_overflow_drains_then_raises() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick", max_pending=5)
            for i in range(8):
                fake.emit("m", "tick", i)
            await eventually(lambda: sub.ended, what="overflow")
            received = [(await sub.get()).data[0] for _ in range(5)]
            assert received == [0, 1, 2, 3, 4]
            with pytest.raises(SubscriptionOverflow) as excinfo:
                await sub.get()
            assert excinfo.value.max_pending == 5 and excinfo.value.subscriptions == sub.ids
            await fake.wait_for_request("rpc.unsubscribe")
            with pytest.raises(ValueError):
                client.subscribe("m", "tick", max_pending=0)


@async_test
async def test_get_with_a_timeout() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            with pytest.raises(ClientTimeout):
                await sub.get(0.05)
            with pytest.raises(ClientTimeout):
                await sub.get(0)
            fake.emit("m", "tick", 1)
            await eventually(lambda: sub.pending == 1, what="event")
            assert (await sub.get(0)).data == [1]
            waiter = asyncio.ensure_future(sub.get())
            await asyncio.sleep(0.01)
            waiter.cancel()  # a cancelled reader loses nothing
            fake.emit("m", "tick", 2)
            assert (await sub.get(2)).data == [2]


@async_test
async def test_a_disconnect_drains_then_raises() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            fake.emit("m", "tick", 1)
            fake.emit("m", "tick", 2)
            await eventually(lambda: sub.pending == 2, what="events")
            fake.abort_connections()
            received = []
            with pytest.raises(ConnectionClosed) as excinfo:
                async for event in sub:
                    received.append(event.data[0])
            assert received == [1, 2] and excinfo.value.code == 1006


@async_test
async def test_subscribe_refusals() -> None:
    async with FakeBridge(max_subscriptions=1) as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(MethodNotFound):
                await client.subscribe("m", "undeclared")
            with pytest.raises(MethodNotFound):
                await client.subscribe("nope", "tick")
            assert client.active_subscriptions == 0 and not client._subs
            await client.subscribe("m", "tick")
            with pytest.raises(Overloaded, match="too many subscriptions"):
                await client.subscribe("m", "tock")
            for module, event in (("", "tick"), ("m", "")):
                with pytest.raises(ValueError):
                    client.subscribe(module, event)


@async_test
async def test_unrouted_events_are_counted() -> None:
    async with FakeBridge() as fake, AsyncBridgeClient(fake.url) as client:
        fake.send_raw({"jsonrpc": "2.0", "method": "rpc.event",
                       "params": {"subscription": "unknown", "module": "m", "event": "e", "data": []}})
        fake.send_raw({"jsonrpc": "2.0", "method": "rpc.subscription_terminated",
                       "params": {"subscription": "unknown", "module": "m", "event": "e", "reason": "x"}})
        await client.ping()
        assert client.unrouted_events == 1
