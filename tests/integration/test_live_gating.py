"""The bridge's socket edge: Host/Origin gating, body and frame limits, connection slots."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
from typing import Any

import pytest
from harness import NodeFactory, Stack
from http_framing import (
    BODYLESS_REFUSALS,
    OTHER_METHODS,
    UNREAD_BODIES,
    UNREADABLE_LENGTHS,
    Exchange,
    assert_bodyless_refusal_keeps,
    assert_late_refused_body_dropped,
    assert_length_required,
    assert_served_as_get,
    assert_unread_body_closes,
    read_until_closed,
)
from live_util import blocking, raw_ws
from websockets.exceptions import ConnectionClosed as WsConnectionClosed

from logos_bridge import (
    SUBPROTOCOL,
    AsyncBridgeClient,
    BridgeHttpClient,
    ConnectError,
    ConnectionClosed,
    HttpStatusError,
    RequestTooLarge,
)
from logos_bridge.errors import DROPPED_UPGRADE_HINTS
from logos_bridge.testing import async_test
from logos_bridge.testing.live import LiveBridge, bridge_config, http_request, rpc_http

pytestmark = pytest.mark.integration

CPP = "test_fullapi_cpp"
PING = {"jsonrpc": "2.0", "id": 1, "method": "rpc.ping"}
PER_PEER = 8  # limits.max_connections_per_peer


@pytest.fixture(scope="module")
def gate(module_node_factory: NodeFactory) -> LiveBridge:
    # A node of its own, so the connection counts below are this module's alone.
    return module_node_factory(name="gating", providers=(CPP,), config=bridge_config([CPP], revalidate_ms=None))


def wait_idle(node: LiveBridge) -> None:
    node.wait_until(lambda: node.info()["connections"] == 0, 15, "the bridge to hold no connection")


def raw_upgrade(port: int, headers: dict[str, str]) -> bytes:
    """A WebSocket upgrade written by hand: what comes back before the socket closes (or the 101)."""
    request = {
        "Host": f"127.0.0.1:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
        "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(), "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Protocol": SUBPROTOCOL, **headers,
    }
    lines = ["GET /ws HTTP/1.1", *(f"{k}: {v}" for k, v in request.items()), "", ""]
    data = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall("\r\n".join(lines).encode())
        while b"\r\n\r\n" not in data:
            try:
                chunk = sock.recv(4096)
            except ConnectionResetError:
                break
            if not chunk:
                break
            data += chunk
    return data


FOREIGN = {
    "foreign Host": {"Host": "evil.example"},
    "foreign Host with the port": {"Host": "evil.example:8645"},
    "an Origin": {"Origin": "http://evil.example"},
    "a loopback Origin": {"Origin": "http://127.0.0.1"},
}


@pytest.mark.parametrize("headers", list(FOREIGN.values()), ids=list(FOREIGN))
def test_foreign_hosts_and_origins_are_refused(gate: LiveBridge, headers: dict[str, str]) -> None:
    port = gate.port
    assert rpc_http(port, PING, headers=headers).status == 403
    for path in ("/healthz", "/modules", f"/modules/{CPP}", "/openapi.json", "/asyncapi.json"):
        assert http_request(port, "GET", path, headers=headers).status == 403, path
    assert raw_upgrade(port, headers) == b"", "a refused upgrade gets no HTTP answer"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_hosts_are_accepted(gate: LiveBridge, host: str) -> None:
    for value in (host, f"{host}:{gate.port}"):
        assert rpc_http(gate.port, PING, headers={"Host": value}).json()["result"] == "pong"
        assert raw_upgrade(gate.port, {"Host": value}).startswith(b"HTTP/1.1 101"), value


@async_test(timeout=60)
async def test_the_clients_report_refusals(gate: LiveBridge) -> None:
    with pytest.raises(ConnectError) as excinfo:
        async with AsyncBridgeClient(gate.ws_url, host_header="evil.example"):
            pass
    assert excinfo.value.hints == DROPPED_UPGRADE_HINTS
    async with AsyncBridgeClient(gate.ws_url, host_header=f"localhost:{gate.port}") as bridge:
        assert await bridge.ping() >= 0
    with pytest.raises(HttpStatusError) as refused:
        await blocking(BridgeHttpClient(gate.http_url, host_header="evil.example").healthz)
    assert refused.value.status == 403 and refused.value.hint


def test_bodies_must_be_json(gate: LiveBridge) -> None:
    assert http_request(gate.port, "POST", "/rpc", b"x", headers={"Content-Type": "text/plain"}).status == 415


# The framing rules are shared with FakeBridge's fidelity tests (tests/http_framing.py).
@pytest.mark.parametrize("framing", list(UNREADABLE_LENGTHS.values()), ids=list(UNREADABLE_LENGTHS))
def test_a_post_needs_a_readable_length(gate: LiveBridge, framing: tuple[str, ...]) -> None:
    assert_length_required(gate.port, framing)


def test_a_body_over_the_limit_is_dropped_without_an_answer(gate: LiveBridge) -> None:
    body = json.dumps({**PING, "params": {"pad": "x" * 2**20}}).encode()
    with socket.create_connection(("127.0.0.1", gate.port), timeout=15) as sock:
        sock.sendall(f"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{gate.port}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\n\r\n".encode())
        try:
            sock.sendall(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        assert read_until_closed(sock) == b""


@pytest.mark.parametrize("exchange", list(UNREAD_BODIES.values()), ids=list(UNREAD_BODIES))
def test_an_unread_body_closes_its_connection(gate: LiveBridge, exchange: Exchange) -> None:
    assert_unread_body_closes(gate.port, exchange)


@pytest.mark.parametrize("exchange", list(BODYLESS_REFUSALS.values()), ids=list(BODYLESS_REFUSALS))
def test_a_bodyless_refusal_keeps_its_connection(gate: LiveBridge, exchange: Exchange) -> None:
    assert_bodyless_refusal_keeps(gate.port, exchange)


def test_a_refused_body_that_arrives_late_is_dropped(gate: LiveBridge) -> None:
    assert_late_refused_body_dropped(gate.port)


@pytest.mark.parametrize("method", OTHER_METHODS)
def test_every_other_method_is_served_as_get(gate: LiveBridge, method: str) -> None:
    # Known bridge limitations, which FakeBridge reproduces.
    assert_served_as_get(gate.port, method)


@async_test(timeout=60)
async def test_frame_limits_close_the_connection(gate: LiveBridge) -> None:
    oversized = json.dumps({**PING, "params": {"pad": "x" * 2**20}})
    for frame, code in ((oversized, 1009), (b"\x00\x01", 1003)):
        async with raw_ws(gate) as ws:
            await ws.send(frame)
            with pytest.raises(WsConnectionClosed) as excinfo:
                await ws.recv()
            assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == code
    async with AsyncBridgeClient(gate.ws_url) as bridge:
        with pytest.raises(RequestTooLarge):
            await bridge.call(CPP, "echoString", "x" * 2**20)
        assert await bridge.call(CPP, "echoString", "still open") == "still open"
    async with AsyncBridgeClient(gate.ws_url, max_request_size=None) as bridge:
        with pytest.raises(ConnectionClosed) as closed:
            await bridge.call(CPP, "echoString", "x" * 2**20)
        assert (closed.value.code, closed.value.initiated_by) == (1009, "server")
        assert "max_frame_bytes" in (closed.value.hint or "")


@async_test(timeout=60)
async def test_a_batch_over_the_limit_is_refused_whole(gate: LiveBridge) -> None:
    batch: list[dict[str, Any]] = [{**PING, "id": i} for i in range(33)]  # limits.max_in_flight_per_connection + 1
    refused = (await blocking(rpc_http, gate.port, batch)).json()
    assert refused["id"] is None and refused["error"]["code"] == -32029
    answered = (await blocking(rpc_http, gate.port, batch[:32])).json()
    assert sorted(r["id"] for r in answered) == list(range(32))
    assert {r["result"] for r in answered} == {"pong"}
    async with raw_ws(gate) as ws:
        await ws.send(json.dumps(batch))
        error = json.loads(await ws.recv())
        assert error["id"] is None and error["error"]["code"] == -32029


@async_test(timeout=90)
async def test_the_http_client_never_locks_out_websocket_clients(gate: LiveBridge) -> None:
    http = BridgeHttpClient(gate.http_url)
    for _ in range(3 * PER_PEER):
        await blocking(http.ping)
    await blocking(wait_idle, gate)
    clients = [AsyncBridgeClient(gate.ws_url) for _ in range(PER_PEER)]
    try:
        for client in clients:
            await client.connect()
        for client in clients:
            assert await client.ping() >= 0
    finally:
        for client in clients:
            await client.aclose()


@async_test(timeout=120)
async def test_a_kept_alive_connection_holds_one_slot(gate: LiveBridge, stack: Stack) -> None:
    if not stack.fixes:
        pytest.skip("[optional] LOGOS_BRIDGE_FIXES is not set: a bridge without the keep-alive fix leaks slots")
    await blocking(wait_idle, gate)
    conn = http.client.HTTPConnection("127.0.0.1", gate.port, timeout=10)
    clients: list[AsyncBridgeClient] = []
    try:
        local = None
        for index in range(12):
            await blocking(conn.request, "POST", "/rpc", json.dumps({**PING, "id": index}),
                           {"Content-Type": "application/json"})
            answer = await blocking(conn.getresponse)
            assert json.loads(answer.read())["result"] == "pong"
            assert conn.sock is not None
            local = local or conn.sock.getsockname()
            assert conn.sock.getsockname() == local, "the requests did not share one connection"
        # Twelve requests, one slot: seven WebSocket clients fit beside it, an eighth does not.
        for _ in range(PER_PEER - 1):
            clients.append(await AsyncBridgeClient(gate.ws_url).connect())
        with pytest.raises(ConnectError):
            await AsyncBridgeClient(gate.ws_url).connect()
        conn.close()
        await blocking(gate.wait_until, lambda: gate.info()["connections"] == PER_PEER - 1, 15,
                       "the kept-alive connection's slot to be released")
        clients.append(await AsyncBridgeClient(gate.ws_url).connect())
        assert await clients[-1].ping() >= 0
    finally:
        conn.close()
        for client in clients:
            await client.aclose()
