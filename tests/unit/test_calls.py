from __future__ import annotations

import asyncio
import json
import math
import random
from typing import Any

import pytest
from bridge_util import standard_module

from logos_bridge import AsyncBridgeClient, unwrap_result
from logos_bridge.lidl import Interface
from logos_bridge import _protocol as proto
from logos_bridge.errors import (
    RPC_ERROR_CLASSES,
    BridgeWarning,
    ClientTimeout,
    ConnectionClosed,
    InvalidParams,
    MethodNotFound,
    ModuleResultError,
    ParseError,
    ProviderRejection,
    RpcError,
    ShuttingDown,
    UpstreamTimeout,
)
from logos_bridge.testing import CallContext, Delay, FakeBridge, FakeError, NoResponse, Reject, async_test


@async_test
async def test_the_request_is_exactly_what_the_bridge_expects() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            await client.call("m", "echo", 1, "a\N{EN DASH}", b"\x01\x02", [True, None], {"k": 1.5}, 2**64 - 1, -(2**63))
            record = await fake.wait_for_request("rpc.call")
    assert record.text == (
        '{"jsonrpc":"2.0","id":1,"method":"rpc.call","params":{"module":"m","method":"echo",'
        '"params":[1,"a\N{EN DASH}",{"_bytes":"AQI"},[true,null],{"k":1.5},18446744073709551615,'
        '-9223372036854775808]}}'
    )
    assert record.transport == "ws" and record.has_id


@async_test
async def test_results_come_back_with_bytes_decoded() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", {"blob": b"\xfb\xff", "n": 2**63 - 1, "nested": [{"_bytes": ""}]})
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.call("m", "echo") == {"blob": b"\xfb\xff", "n": 2**63 - 1, "nested": [b""]}
            assert await client.call("m", "echo", decode_bytes=False) == {
                "blob": {"_bytes": "-_8"}, "n": 2**63 - 1, "nested": [{"_bytes": ""}]}
            assert await client.call("m", "greet", "you") == "hello, you"


@async_test
async def test_responses_out_of_order() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "slow", Delay(0.3, "slow"))
        fake.on_call("m", "fast", "fast")
        fake.module("m", ["slow", "fast", ("greet", ["who"]), "echo", ("fire", ["value"])], ["tick", "tock"])
        async with AsyncBridgeClient(fake.url) as client:
            slow = asyncio.ensure_future(client.call("m", "slow"))
            await fake.wait_for_request("rpc.call")
            fast = await client.call("m", "fast")
            assert fast == "fast" and not slow.done()
            assert await slow == "slow"
            assert [r.id for r in fake.requests] == [1, 2]


@async_test
async def test_fifty_concurrent_calls() -> None:
    rng = random.Random(7)

    async def double(ctx: CallContext) -> Any:
        await asyncio.sleep(rng.random() * 0.05)
        return ctx.params[0] * 2

    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", double)
        async with AsyncBridgeClient(fake.url) as client:
            results = await asyncio.gather(*(client.call("m", "echo", i) for i in range(50)))
            assert results == [i * 2 for i in range(50)]
            assert client.in_flight == 0
            assert sorted(r.id for r in fake.requests) == list(range(1, 51))


@async_test
async def test_events_can_precede_the_response() -> None:
    def fire_three(ctx: CallContext) -> int:
        for i in range(3):
            ctx.emit("tick", i)
        return 3

    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "fire", fire_three)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            assert await client.call("m", "fire", 0) == 3
            assert sub.pending == 3
            assert [(await sub.get()).data for _ in range(3)] == [[0], [1], [2]]


@async_test
async def test_a_provider_rejection_raises() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", Reject("dispatch_failed", "expected integer at arg0, got string", "m"))
        fake.on_call("m", "greet", {"code": "user_error", "message": "m", "origin": "o"})
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(ProviderRejection) as excinfo:
                await client.call("m", "echo", "x")
            rejection = excinfo.value
            assert (rejection.code, rejection.message, rejection.origin) == (
                "dispatch_failed", "expected integer at arg0, got string", "m")
            assert (rejection.module, rejection.method) == ("m", "echo")
            assert await client.call("m", "echo", detect_rejection=False) == {
                "code": "dispatch_failed", "message": "expected integer at arg0, got string", "origin": "m"}
            # A method may return such a map with its own code: that is data.
            assert await client.call("m", "greet") == {"code": "user_error", "message": "m", "origin": "o"}


@async_test
async def test_an_application_failure_is_a_result() -> None:
    failure = {"success": False, "value": None, "error": "the widget was not frobnicated"}
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", failure)
        async with AsyncBridgeClient(fake.url) as client:
            result = await client.call("m", "echo")
            assert result == failure
            with pytest.raises(ModuleResultError, match="frobnicated"):
                unwrap_result(result)


@async_test
async def test_bridge_errors_raise_their_classes() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            for code, cls in RPC_ERROR_CLASSES.items():
                fake.on_call("m", "echo", FakeError(code))
                with pytest.raises(cls) as excinfo:
                    await client.call("m", "echo")
                error = excinfo.value
                assert (error.code, error.module, error.method, error.op) == (code, "m", "echo", "rpc.call")
                assert error.logos_error_name == proto.ERROR_TABLE[code].logos_error_name
            fake.on_call("m", "echo", FakeError.upstream("timeout"))
            with pytest.raises(UpstreamTimeout) as timeout:
                await client.call("m", "echo")
            assert not isinstance(timeout.value, TimeoutError)
            fake.on_call("m", "echo", FakeError(-32099, "custom", data={"x": 1}))
            with pytest.raises(RpcError) as custom:
                await client.call("m", "echo")
            assert (type(custom.value), custom.value.code, custom.value.data) == (RpcError, -32099, {"x": 1})


@async_test
async def test_unknown_targets_are_indistinguishable() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(MethodNotFound) as unknown_module:
                await client.call("nope", "echo")
            with pytest.raises(MethodNotFound) as unknown_method:
                await client.call("m", "nope")
            assert unknown_module.value.message == unknown_method.value.message == "method not found"
            assert unknown_module.value.data == unknown_method.value.data


@async_test
async def test_invalid_params_detail_from_by_name_params() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(InvalidParams) as excinfo:
                await client.request("rpc.call", {"module": "m", "method": "greet", "params": {"nope": 1}})
            detail = excinfo.value.detail
            assert detail is not None and (detail.reason, detail.path) == ("schema-mismatch", "nope")
            assert await client.request(
                "rpc.call", {"module": "m", "method": "greet", "params": {"who": "by name"}}) == "hello, by name"


@async_test
async def test_an_uncorrelated_error_is_kept_warned_and_attached_to_the_close() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", Delay(0.1, "still answered"))
        async with AsyncBridgeClient(fake.url) as client:
            pending = asyncio.ensure_future(client.call("m", "echo"))
            await fake.wait_for_request("rpc.call")
            with pytest.warns(BridgeWarning, match="could not attribute"):
                fake.send_raw(proto.make_error(None, proto.ERROR_TABLE[-32700].to_error()))
                await client.ping()
            assert isinstance(client.last_uncorrelated_error, ParseError)
            assert await pending == "still answered"
            fake.abort_connections()
            await client.wait_closed()
            closed = client.close_exception
            assert closed is not None and isinstance(closed.server_error, ParseError)
            assert "last bridge error: parse error" in str(closed)


@async_test
async def test_a_draining_bridge() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        client = await AsyncBridgeClient(fake.url).connect()
        fake.set_draining()
        with pytest.warns(BridgeWarning):
            with pytest.raises(ClientTimeout):
                await client.call("m", "echo", timeout=0.3)
        assert isinstance(client.last_uncorrelated_error, ShuttingDown)
        await fake.stop()
        await client.wait_closed()
        closed = client.close_exception
        assert closed is not None and closed.hint == "the bridge is shutting down"
        await client.aclose()


@async_test
async def test_a_late_response_after_a_client_timeout_is_dropped() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", Delay(0.3, "late"))
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(ClientTimeout) as excinfo:
                await client.call("m", "echo", timeout=0.05)
            timeout = excinfo.value
            assert isinstance(timeout, TimeoutError)
            assert (timeout.module, timeout.method, timeout.timeout, timeout.request_id) == ("m", "echo", 0.05, 1)
            assert client.in_flight == 0
            fake.on_call("m", "echo", "fresh")
            await asyncio.sleep(0.4)
            assert client.late_responses == 1
            assert await client.call("m", "echo") == "fresh"


@async_test
async def test_timeout_defaults() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", NoResponse)
        fake.on_call("m", "greet", Delay(0.1, "patient"))
        async with AsyncBridgeClient(fake.url, call_timeout=0.05, op_timeout=5) as client:
            with pytest.raises(ClientTimeout):
                await client.call("m", "echo")
            assert await client.call("m", "greet", timeout=math.inf) == "patient"
            assert await client.call("m", "greet", timeout=1) == "patient"
            with pytest.raises(ValueError):
                await client.call("m", "greet", timeout=-1)
            with pytest.raises(ClientTimeout):
                await client.request("rpc.call", {"module": "m", "method": "echo"}, timeout=0.05)


@async_test
async def test_caller_cancellation_cleans_up() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", NoResponse)
        async with AsyncBridgeClient(fake.url) as client:
            task = asyncio.ensure_future(client.call("m", "echo"))
            await fake.wait_for_request("rpc.call")
            assert client.in_flight == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.in_flight == 0
            assert await client.ping() >= 0


@async_test
async def test_raw_requests() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            for op in ("rpc.subscribe", "rpc.unsubscribe"):
                with pytest.raises(ValueError, match="subscribe"):
                    await client.request(op, {"subscription": "x"})
            assert fake.requests == []
            assert await client.request("rpc.cancel", {"id": 1}) == {
                "cancelled": False, "reason": "not_supported_upstream"}
            assert await client.request("rpc.ping") == "pong"
            with pytest.raises(MethodNotFound):
                await client.request("rpc.nope")
            with pytest.raises(InvalidParams):
                await client.request("rpc.schema", {})
            sent = json.loads(fake.requests[1].text)
            assert "params" not in sent  # absent params are omitted


@async_test
async def test_discovery() -> None:
    interface = {"name": "typed", "version": "1.0.0", "methods": [{"name": "f"}]}
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.module("typed", ["f"], ["e"], interface=interface, status="ok")
        async with AsyncBridgeClient(fake.url) as client:
            modules = await client.list_modules()
            assert [m.module for m in modules] == ["m", "typed"]
            assert modules[1].interface is None and modules[1].interface_sha256 is not None
            legacy = await client.schema("m")
            assert legacy.methods == ("greet", "echo", "fire") and legacy.events == ("tick", "tock")
            assert not legacy.typed and legacy.source == "getPluginInterface"
            typed = await client.schema("typed")
            served = Interface.from_json(interface).with_identity().to_json()
            assert typed.typed and typed.interface == served and typed.source == "lidl"
            assert typed.exposure is not None and typed.exposure.methods == ("f", "name", "version", "lidl")
            with pytest.raises(MethodNotFound) as excinfo:
                await client.schema("nope")
            assert excinfo.value.op == "rpc.schema"


@async_test
async def test_argument_errors_send_nothing() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.raises(ValueError):
                await client.call("", "echo")
            with pytest.raises(ValueError):
                await client.call("m", "")
            with pytest.raises(TypeError):
                await client.call("m", "echo", object())
            with pytest.raises(ValueError):
                await client.call("m", "echo", float("nan"))
            with pytest.raises(ValueError):
                await client.call("m", "echo", "\ud800")
            assert fake.requests == [] and client.in_flight == 0


@async_test
async def test_calls_fail_fast_on_a_closed_connection() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        client = await AsyncBridgeClient(fake.url).connect()
        fake.abort_connections()
        await client.wait_closed()
        for attempt in range(2):
            with pytest.raises(ConnectionClosed) as excinfo:
                await client.call("m", "echo", attempt)
            assert excinfo.value.code == 1006
        await client.aclose()
