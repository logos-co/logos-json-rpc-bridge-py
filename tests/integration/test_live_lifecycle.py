"""The lifecycle canary: a provider unloaded and loaded again under a live bridge, and a
reload onto the same build, which the transport reports without any revalidation."""

from __future__ import annotations

import pytest
from harness import NodeFactory
from live_util import blocking, eventually

from logos_bridge import (
    AsyncBridgeClient,
    AsyncSubscription,
    ClientTimeout,
    Event,
    ModuleUnavailable,
    SubscriptionTerminated,
)
from logos_bridge.testing import async_test
from logos_bridge.testing.live import LiveBridge, bridge_config

pytestmark = pytest.mark.integration

CPP = "test_fullapi_cpp"


CALL_TIMEOUT_MS = 5000
#: Long enough that no revalidation pass can run during the reload test.
NO_REVALIDATION_MS = 600_000


@pytest.fixture
def canary(node_factory: NodeFactory) -> LiveBridge:
    # A call to an unloaded provider is answered only when call_timeout_ms runs out.
    config = bridge_config([CPP], limits={"call_timeout_ms": CALL_TIMEOUT_MS})
    return node_factory(name="canary", providers=(CPP,), config=config)


async def next_event_after_firing(bridge: AsyncBridgeClient, stream: AsyncSubscription, value: int) -> Event:
    """Fire until the (freshly armed) subscription delivers ``value``."""
    async def attempt() -> Event | None:
        await bridge.call(CPP, "fireIntEvent", value)
        try:
            event = await stream.get(timeout=0.5)
        except ClientTimeout:
            return None
        return event if event.data == [value] else None
    event: Event = await eventually(attempt, 30, f"intEvent({value})", interval=0)
    return event


@async_test(timeout=240)
async def test_the_lifecycle_canary(canary: LiveBridge) -> None:
    info = await blocking(canary.info)
    assert info["subscription_continuity"] is True, f"provider loss is not reported: {info}"
    before = canary.views()[CPP]
    async with AsyncBridgeClient(canary.ws_url) as bridge:
        stream = await bridge.subscribe(CPP, "intEvent")
        await bridge.call(CPP, "fireIntEvent", 1)
        first = await stream.get(timeout=15)
        assert first.data == [1]

        # 1-2. Unload the provider: the subscription ends, and says why.
        await blocking(canary.unload, CPP)
        with pytest.raises(SubscriptionTerminated) as ended:
            await stream.get(timeout=30)
        assert (ended.value.reason, ended.value.module, ended.value.event) == ("provider_unavailable", CPP,
                                                                                  "intEvent")
        assert ended.value.subscription == stream.ids[0]

        # 3. Calls fail as unavailable, not as unknown, and the view is stale.
        with pytest.raises(ModuleUnavailable) as unavailable:
            await bridge.call(CPP, "echoInt", 1, timeout=CALL_TIMEOUT_MS / 1000 + 30)
        assert unavailable.value.logos_error_name == "NOT_READY"
        assert canary.views()[CPP]["stale"] is True

        # 4-5. Load it again: the view settles back to what it was.
        await blocking(canary.load, CPP)
        after = await blocking(canary.wait_status, CPP, before["interface_status"], 60)
        for key in ("interface_sha256", "contract_sha256", "methods", "events", "exposure"):
            assert after[key] == before[key], key

        # 6. A fresh subscription delivers again.
        async with bridge.subscribe(CPP, "intEvent") as again:
            event = await next_event_after_firing(bridge, again, 2)
            assert event.subscription == again.ids[0]
            assert event.generation > first.generation
        assert await bridge.call(CPP, "echoInt", 3) == 3


@async_test(timeout=180)
async def test_a_reload_onto_the_same_build_ends_the_subscription(node_factory: NodeFactory) -> None:
    """logos-protocol#91 (``dcf4f05``): the transport reports the swap, revalidation does not."""
    config = bridge_config([CPP], revalidate_ms=NO_REVALIDATION_MS,
                           limits={"call_timeout_ms": CALL_TIMEOUT_MS})
    live = node_factory(name="reload", providers=(CPP,), config=config)
    assert (await blocking(live.info))["subscription_continuity"] is True
    before = live.views()[CPP]
    async with AsyncBridgeClient(live.ws_url) as bridge:
        stream = await bridge.subscribe(CPP, "intEvent")
        await bridge.call(CPP, "fireIntEvent", 1)
        first = await stream.get(timeout=15)
        assert first.data == [1]

        # The same build comes back: the loss path reports it, not discovery.
        await blocking(live.reload, CPP)
        with pytest.raises(SubscriptionTerminated) as ended:
            await stream.get(timeout=60)
        assert (ended.value.reason, ended.value.module, ended.value.event) == ("provider_unavailable", CPP,
                                                                              "intEvent")
        # It really was the same build: the digests never moved.
        after = await blocking(live.wait_status, CPP, before["interface_status"], 60)
        assert (after["interface_sha256"], after["contract_sha256"]) == (before["interface_sha256"],
                                                                        before["contract_sha256"])

        async with bridge.subscribe(CPP, "intEvent") as again:
            event = await next_event_after_firing(bridge, again, 2)
            assert event.generation > first.generation
