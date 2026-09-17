from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from bridge_util import eventually, standard_module
from websockets.asyncio.client import connect

from logos_bridge import AsyncBridgeClient
from logos_bridge.errors import BridgeWarning, ConnectError, ConnectionClosed, RequestTooLarge
from logos_bridge.testing import FakeBridge, NoResponse, async_test, free_port


@async_test
async def test_the_handshake_is_minimal() -> None:
    async with FakeBridge() as fake, AsyncBridgeClient(fake.url) as client:
        handshake = fake.handshakes[0]
        assert handshake.accepted
        assert handshake.header_all("Host") == [f"127.0.0.1:{fake.port}"]
        assert handshake.header("Origin") is None
        assert handshake.header("Sec-WebSocket-Extensions") is None
        assert handshake.header_all("Sec-WebSocket-Protocol") == ["jsonrpc-bridge.v1"]
        user_agent = handshake.header("User-Agent")
        assert user_agent is not None and user_agent.startswith("logos-bridge-py/")
        assert handshake.path == "/ws"
        assert client.subprotocol == "jsonrpc-bridge.v1"
        assert client.connected and client.state == "open"


@async_test
async def test_host_header_reaches_a_tunnelled_bridge() -> None:
    async with FakeBridge(host_port=8645) as fake:
        with pytest.raises(ConnectError) as excinfo:
            await AsyncBridgeClient(fake.url).connect()
        assert fake.handshakes[-1].refusal == "host"
        assert any("host_header" in hint for hint in excinfo.value.hints)
        async with AsyncBridgeClient(fake.url, host_header="127.0.0.1:8645") as client:
            assert await client.ping() >= 0
        assert fake.handshakes[-1].accepted
        assert fake.handshakes[-1].header_all("Host") == ["127.0.0.1:8645"]


@async_test
async def test_refused_upgrades_raise_connect_error_with_hints() -> None:
    async with FakeBridge(max_connections_per_peer=2) as fake:
        with pytest.warns(BridgeWarning):
            origin_client = AsyncBridgeClient(fake.url, extra_headers={"Origin": "http://evil.example"})
        with pytest.raises(ConnectError, match="dropped during the WebSocket upgrade"):
            await origin_client.connect()
        assert fake.handshakes[-1].refusal == "origin"
        assert origin_client.closed

        with pytest.raises(ConnectError) as excinfo:
            await AsyncBridgeClient(fake.url, subprotocol="jsonrpc-bridge.v2").connect()
        assert fake.handshakes[-1].refusal == "subprotocol"
        assert any("subprotocol" in hint for hint in excinfo.value.hints)

        async with AsyncBridgeClient(fake.url), AsyncBridgeClient(fake.url):
            with pytest.raises(ConnectError) as excinfo:
                await AsyncBridgeClient(fake.url).connect()
            assert fake.handshakes[-1].refusal == "per_peer"
            assert any("8 connections" in hint for hint in excinfo.value.hints)
        await eventually(lambda: not any(c.open for c in fake.connections), what="server-side close")
        async with AsyncBridgeClient(fake.url) as client:  # the slots were released
            await client.ping()

    async with FakeBridge(auth_mode="bearer") as fake:
        with pytest.raises(ConnectError):
            await AsyncBridgeClient(fake.url).connect()
        assert fake.handshakes[-1].refusal == "bearer"


@async_test
async def test_connection_refused() -> None:
    port = free_port()
    with pytest.raises(ConnectError, match="refused") as excinfo:
        await AsyncBridgeClient(f"ws://127.0.0.1:{port}/ws").connect()
    assert any("json_rpc_bridge.start" in hint for hint in excinfo.value.hints)
    assert isinstance(excinfo.value, ConnectionError)


@async_test
async def test_open_timeout() -> None:
    silent: set[asyncio.StreamWriter] = set()

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        silent.add(writer)
        await reader.read()  # never answers the upgrade
        writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ConnectError, match="open_timeout"):
            await AsyncBridgeClient(f"ws://127.0.0.1:{port}/ws", open_timeout=0.2).connect()
    finally:
        server.close()
        for writer in silent:
            writer.transport.abort()
        await server.wait_closed()


@async_test
async def test_close_1008_when_the_bridge_queue_overflows() -> None:
    async with FakeBridge(max_queued_frames=4) as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("m", "tick")
            for i in range(10):  # no await: the bridge-side queue overflows
                fake.emit("m", "tick", i)
            await client.wait_closed()
            closed = client.close_exception
            assert closed is not None
            assert (closed.code, closed.code_name, closed.initiated_by) == (1008, "POLICY_VIOLATION", "server")
            assert closed.hint is not None and "max_queued_frames_per_connection" in closed.hint
            with pytest.raises(ConnectionClosed) as excinfo:
                await sub.get(1)
            assert excinfo.value.code == 1008
            assert excinfo.value is not client.close_exception  # a fresh exception for every waiter


@async_test
async def test_close_1009_for_a_frame_over_max_frame_bytes() -> None:
    async with FakeBridge(max_frame_bytes=1000) as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url, max_request_size=None) as client:
            with pytest.raises(ConnectionClosed) as excinfo:
                await client.call("m", "echo", "x" * 2000)
            assert (excinfo.value.code, excinfo.value.initiated_by) == (1009, "server")
            assert excinfo.value.hint is not None and "max_frame_bytes" in excinfo.value.hint
            with pytest.raises(ConnectionClosed):
                await client.ping()


@async_test
async def test_close_1006_when_the_bridge_drops_or_stops() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", NoResponse)
        async with AsyncBridgeClient(fake.url) as client:
            pending = asyncio.ensure_future(client.call("m", "echo"))
            await fake.wait_for_request("rpc.call")
            fake.abort_connections()
            with pytest.raises(ConnectionClosed) as excinfo:
                await pending
            assert (excinfo.value.code, excinfo.value.initiated_by) == (1006, "transport")
            assert excinfo.value.code_name == "ABNORMAL_CLOSURE"

    fake = await FakeBridge().start()
    client = await AsyncBridgeClient(fake.url).connect()
    await fake.stop()
    await client.wait_closed()
    assert client.close_exception is not None and client.close_exception.code == 1006
    await client.aclose()


@async_test
async def test_a_server_close_frame_is_reported() -> None:
    async with FakeBridge() as fake, AsyncBridgeClient(fake.url) as client:
        await fake.close_connections(1001, "bye")
        await client.wait_closed()
        closed = client.close_exception
        assert closed is not None
        assert (closed.code, closed.reason, closed.initiated_by, closed.code_name) == (
            1001, "bye", "server", "GOING_AWAY")


@async_test
async def test_request_too_large_sends_nothing() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url, max_request_size=200) as client:
            with pytest.raises(RequestTooLarge) as excinfo:
                await client.call("m", "echo", "x" * 500)
            assert excinfo.value.limit == 200 and excinfo.value.size > 500
            assert client.in_flight == 0
            assert await client.call("m", "echo", "small") == ["small"]
            assert [r.method for r in fake.requests] == ["rpc.call"]


@pytest.mark.slow
@async_test(timeout=20)
async def test_keepalive_fails_a_frozen_bridge() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        client = AsyncBridgeClient(fake.url, ping_interval=0.2, ping_timeout=0.3, close_timeout=0.3)
        async with client:
            fake.freeze()
            pending = asyncio.ensure_future(client.call("m", "echo"))
            with pytest.raises(ConnectionClosed) as excinfo:
                await pending
            closed = excinfo.value
            assert (closed.code, closed.initiated_by, closed.reason) == (1011, "client", "keepalive ping timeout")
            assert closed.hint is not None and "pong" in closed.hint
            fake.unfreeze()


@async_test
async def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    dead_proxy = f"http://127.0.0.1:{free_port()}"
    for name in ("http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, dead_proxy)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    async with FakeBridge() as fake:
        with pytest.raises(OSError):  # control: websockets' default honours the environment
            async with connect(fake.url, subprotocols=["jsonrpc-bridge.v1"], open_timeout=5):
                pass
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.ping() >= 0


@async_test
async def test_aclose_is_idempotent_and_fails_what_is_pending() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.on_call("m", "echo", NoResponse)
        client = await AsyncBridgeClient(fake.url).connect()
        assert await client.connect() is client  # already open: a no-op
        pending = asyncio.ensure_future(client.call("m", "echo"))
        sub = await client.subscribe("m", "tick")
        await fake.wait_for_request("rpc.call")
        await client.aclose()
        with pytest.raises(ConnectionClosed) as excinfo:
            await pending
        assert (excinfo.value.code, excinfo.value.initiated_by) == (1000, "client")
        with pytest.raises(ConnectionClosed):
            await sub.get()
        await client.aclose()
        await client.wait_closed()
        assert client.closed and not client.connected
        assert client.close_exception is not None and client.close_exception.code == 1000
        with pytest.raises(ConnectionClosed):
            await client.ping()
        with pytest.raises(RuntimeError, match="new client"):
            await client.connect()
        await sub.unsubscribe()  # the connection is gone: nothing to do, no error

    never = AsyncBridgeClient("ws://127.0.0.1:1/ws")
    await never.aclose()
    assert never.closed and never.close_exception is None
    await never.wait_closed()


@async_test
async def test_calls_before_connect_are_refused() -> None:
    client = AsyncBridgeClient("ws://127.0.0.1:1/ws")
    with pytest.raises(RuntimeError, match="not connected"):
        await client.ping()


@async_test
async def test_the_client_is_bound_to_its_loop() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        async with AsyncBridgeClient(fake.url) as client:
            outcome: dict[str, Any] = {}

            def other_loop() -> None:
                async def use() -> None:
                    for attempt in (client.ping, lambda: client.call("m", "echo"), client.list_modules):
                        try:
                            await attempt()
                        except RuntimeError as exc:
                            outcome.setdefault("errors", []).append(str(exc))

                asyncio.run(use())

            thread = threading.Thread(target=other_loop)
            thread.start()
            await asyncio.to_thread(thread.join)
            assert len(outcome["errors"]) == 3
            assert all("bound to the event loop" in e for e in outcome["errors"])
            assert await client.ping() >= 0  # still fine on its own loop


@async_test
async def test_noise_from_the_bridge_is_ignored() -> None:
    async with FakeBridge() as fake, AsyncBridgeClient(fake.url) as client:
        fake.send_raw(b"\x00\x01")
        fake.send_raw("not json")
        fake.send_raw("")  # the bridge's answer to an all-notification body
        fake.send_raw({"jsonrpc": "2.0"})
        fake.send_raw({"jsonrpc": "2.0", "method": "rpc.something", "params": {}})
        fake.send_raw({"jsonrpc": "2.0", "id": "x", "result": 1})
        fake.send_raw({"jsonrpc": "2.0", "id": True, "result": 1})
        fake.send_raw([{"jsonrpc": "2.0", "id": 999, "result": 1}])
        assert await client.ping() >= 0
        await eventually(lambda: client.ignored_frames == 4, what="ignored frames")
        assert client.late_responses == 3
        assert client.connected
