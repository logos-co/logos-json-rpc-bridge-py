"""Events through a real bridge: both conformance tables, fan-out, duplicates, unsubscribe."""

from __future__ import annotations

import json

import pytest
from live_util import event_tables, frames_until, frames_within, raw_ws

from logos_bridge import AsyncBridgeClient, MethodNotFound, SubscriptionClosed
from logos_bridge.testing import async_test
from logos_bridge.testing.conformance import ConformanceTable, materialize
from logos_bridge.testing.live import LiveBridge

pytestmark = pytest.mark.integration

CPP = "test_fullapi_cpp"


@pytest.mark.parametrize(("table", "provider", "conformance"), list(event_tables()))
@async_test(timeout=120)
async def test_event_cases(node: LiveBridge, table: str, provider: str, conformance: ConformanceTable) -> None:
    names = sorted({case.event for case in conformance.events})
    async with AsyncBridgeClient(node.ws_url) as bridge:
        async with bridge.subscribe_many(provider, names) as stream:
            assert [ack["state"] for ack in stream.acks] == ["registered"] * len(names)
            for case in conformance.events:
                values = materialize(case.values)
                assert await bridge.call(provider, case.fire, *values) is True, case.id
                event = await stream.get(timeout=15)
                assert (event.module, event.event) == (provider, case.event), case.id
                assert event.decoded_data() == values, case.id
                assert event.generation >= 1 and event.ts > 0, case.id
            assert stream.pending == 0


@async_test(timeout=60)
async def test_two_clients_get_one_event_under_their_own_ids(node: LiveBridge) -> None:
    async with AsyncBridgeClient(node.ws_url) as one, AsyncBridgeClient(node.ws_url) as two:
        async with one.subscribe(CPP, "stringEvent") as first, two.subscribe(CPP, "stringEvent") as second:
            assert first.ids != second.ids
            await one.call(CPP, "fireStringEvent", "fan-out")
            a, b = await first.get(timeout=15), await second.get(timeout=15)
            assert (a.subscription, b.subscription) == (first.ids[0], second.ids[0])
            assert a.data == b.data == ["fan-out"]
            assert a.generation == b.generation


@async_test(timeout=60)
async def test_unsubscribe_stops_delivery(node: LiveBridge) -> None:
    async with AsyncBridgeClient(node.ws_url) as bridge:
        stream = await bridge.subscribe(CPP, "uintEvent")
        await bridge.call(CPP, "fireUintEvent", 1)
        assert (await stream.get(timeout=15)).data == [1]
        await stream.unsubscribe()
        async with bridge.subscribe(CPP, "uintEvent") as other:
            await bridge.call(CPP, "fireUintEvent", 2)
            assert (await other.get(timeout=15)).data == [2]
        with pytest.raises(SubscriptionClosed):
            await stream.get(timeout=0.5)
        assert bridge.unrouted_events == 0


@async_test(timeout=60)
async def test_a_duplicate_subscribe_is_acknowledged_once_and_delivers_once(node: LiveBridge) -> None:
    subscribe = {"subscription": "dup", "module": CPP, "event": "boolEvent"}
    async with raw_ws(node) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "rpc.subscribe", "params": subscribe}))
        first = json.loads(await ws.recv())
        assert first["result"] == {**subscribe, "operation": "subscribe", "state": "registered"}
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "rpc.subscribe", "params": subscribe}))
        assert json.loads(await ws.recv())["result"]["state"] == "active"
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "rpc.call",
                                  "params": {"module": CPP, "method": "fireBoolEvent", "params": [True]}}))
        frames = await frames_until(ws, lambda f: any(x.get("id") == 3 for x in f)
                                    and any(x.get("method") == "rpc.event" for x in f), 15)
        frames += await frames_within(ws, 1.0)
        events = [f for f in frames if f.get("method") == "rpc.event"]
        assert len(events) == 1 and events[0]["params"]["subscription"] == "dup"
        assert events[0]["params"]["data"] == [True]


@async_test(timeout=60)
async def test_undeclared_and_denied_events_are_refused_alike(node: LiveBridge, policy_node: LiveBridge) -> None:
    async with AsyncBridgeClient(node.ws_url) as bridge:
        with pytest.raises(MethodNotFound) as unknown:
            await bridge.subscribe(CPP, "noSuchEvent")
    async with AsyncBridgeClient(policy_node.ws_url) as bridge:
        with pytest.raises(MethodNotFound) as denied:
            await bridge.subscribe(CPP, "intEvent")
        async with bridge.subscribe(CPP, "uintEvent") as allowed:
            assert allowed.acks[0]["state"] == "registered"
    assert (unknown.value.code, unknown.value.message, unknown.value.data) == (
        denied.value.code, denied.value.message, denied.value.data)
