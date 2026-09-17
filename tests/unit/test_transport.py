from __future__ import annotations

import inspect
from typing import Any

import pytest
from websockets.asyncio.client import connect

from logos_bridge import AsyncBridgeClient, __version__
from logos_bridge._transport import USER_AGENT, build_connect_plan
from logos_bridge.errors import BridgeWarning


def plan(url: str = "ws://127.0.0.1:8645/ws", **overrides: Any) -> Any:
    options: dict[str, Any] = dict(
        subprotocol="jsonrpc-bridge.v1",
        open_timeout=10.0,
        ping_interval=20.0,
        ping_timeout=20.0,
        close_timeout=5.0,
        max_message_size=64 * 2**20,
    )
    options.update(overrides)
    return build_connect_plan(url, **options)


def test_default_kwargs() -> None:
    p = plan()
    assert p.uri == "ws://127.0.0.1:8645/ws"
    assert p.kwargs == {
        "origin": None,
        "extensions": None,
        "compression": None,
        "proxy": None,
        "subprotocols": ["jsonrpc-bridge.v1"],
        "additional_headers": None,
        "user_agent_header": USER_AGENT,
        "open_timeout": 10.0,
        "ping_interval": 20.0,
        "ping_timeout": 20.0,
        "close_timeout": 5.0,
        "max_size": 64 * 2**20,
        "max_queue": None,
    }
    assert (p.dial_host, p.dial_port) == ("127.0.0.1", 8645)


def test_the_kwargs_are_accepted_by_websockets() -> None:
    signature = inspect.signature(connect.__init__)
    signature.bind(object(), plan().uri, **plan().kwargs)
    tunnel = plan("wss://127.0.0.1:18645/ws", host_header="127.0.0.1:8645")
    signature.bind(object(), tunnel.uri, **tunnel.kwargs)


def test_user_agent() -> None:
    assert USER_AGENT.startswith(f"logos-bridge-py/{__version__} websockets/")
    assert "Python/" in USER_AGENT


def test_host_header_is_spelled_in_the_uri_and_the_real_target_is_dialled() -> None:
    p = plan("ws://127.0.0.1:18645/ws?x=1", host_header="127.0.0.1:8645")
    assert p.uri == "ws://127.0.0.1:8645/ws?x=1"
    assert (p.kwargs["host"], p.kwargs["port"]) == ("127.0.0.1", 18645)
    assert "server_hostname" not in p.kwargs


@pytest.mark.parametrize(
    ("host_header", "uri"),
    [
        ("[::1]:8645", "ws://[::1]:8645/ws"),
        ("localhost", "ws://localhost/ws"),
        ("127.0.0.1", "ws://127.0.0.1/ws"),
    ],
)
def test_host_header_forms(host_header: str, uri: str) -> None:
    assert plan("ws://localhost:9999/ws", host_header=host_header).uri == uri


def test_wss_keeps_the_dial_host_for_tls() -> None:
    p = plan("wss://tunnel.example:443/ws", host_header="127.0.0.1:8645")
    assert p.kwargs["server_hostname"] == "tunnel.example"
    assert p.kwargs["port"] == 443


@pytest.mark.parametrize("bad", ["http://x", "a b", "x:99999", "x:0", "x/y", "[::1", "", "x:y"])
def test_bad_host_headers_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        plan(host_header=bad)


def test_default_ports() -> None:
    assert plan("ws://localhost/ws").dial_port == 80
    assert plan("wss://localhost/ws").dial_port == 443


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8645/ws", "ws://", "ws://127.0.0.1:8645/ws#frag", "ws://u:p@127.0.0.1/ws",
     "ws://127.0.0.1:99999/ws", "127.0.0.1:8645"],
)
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        plan(url)


def test_empty_subprotocol_is_refused() -> None:
    with pytest.raises(ValueError):
        plan(subprotocol="")


@pytest.mark.parametrize("headers", [{"Host": "127.0.0.1"}, [("host", "x")], {" HOST": "x"}])
def test_a_host_header_in_extra_headers_is_refused(headers: Any) -> None:
    with pytest.raises(ValueError, match="host_header"):
        plan(extra_headers=headers)


def test_an_origin_header_warns() -> None:
    with pytest.warns(BridgeWarning, match="allowed_origins"):
        p = plan(extra_headers={"Origin": "http://localhost"})
    assert p.kwargs["additional_headers"] == [("Origin", "http://localhost")]
    assert p.kwargs["origin"] is None


@pytest.mark.parametrize("name", ["Upgrade", "connection", "Sec-WebSocket-Protocol", "sec-websocket-extensions",
                                  "Sec-WebSocket-Key", "Sec-WebSocket-Version"])
def test_handshake_headers_cannot_be_overridden(name: str) -> None:
    with pytest.raises(ValueError, match="managed"):
        plan(extra_headers={name: "x"})


def test_extra_headers_are_passed_through() -> None:
    assert plan(extra_headers={"X-Trace": "1"}).kwargs["additional_headers"] == [("X-Trace", "1")]
    assert plan(extra_headers=[("X-A", "1"), ("X-A", "2")]).kwargs["additional_headers"] == [
        ("X-A", "1"), ("X-A", "2")]


@pytest.mark.parametrize("headers", [{"X-A": "1\r\nHost: evil"}, {"X-A\n": "1"}, {"X": 1}, [("X",)], ["X"]])
def test_malformed_headers_are_refused(headers: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        plan(extra_headers=headers)


def test_the_client_validates_options_up_front() -> None:
    with pytest.raises(ValueError):
        AsyncBridgeClient(extra_headers={"Host": "x"})
    with pytest.raises(ValueError):
        AsyncBridgeClient("http://127.0.0.1:8645")
    for option in ("open_timeout", "call_timeout", "op_timeout", "close_timeout", "ping_interval", "ping_timeout"):
        with pytest.raises(ValueError):
            AsyncBridgeClient(**{option: -1})
        with pytest.raises(ValueError):
            AsyncBridgeClient(**{option: float("nan")})
    for option in ("max_message_size", "max_request_size"):
        with pytest.raises(ValueError):
            AsyncBridgeClient(**{option: 0})
    client = AsyncBridgeClient(call_timeout=None, op_timeout=None, max_request_size=None, max_message_size=None)
    assert client.state == "new" and not client.connected and not client.closed


def test_client_constructor_signature_matches_the_plan() -> None:
    parameters = inspect.signature(AsyncBridgeClient.__init__).parameters
    assert [(p.name, p.default) for p in list(parameters.values())[1:]] == [
        ("url", "ws://127.0.0.1:8645/ws"),
        ("open_timeout", 10.0),
        ("call_timeout", None),
        ("op_timeout", 30.0),
        ("close_timeout", 5.0),
        ("ping_interval", 20.0),
        ("ping_timeout", 20.0),
        ("max_message_size", 64 * 2**20),
        ("max_request_size", 2**20),
        ("host_header", None),
        ("subprotocol", "jsonrpc-bridge.v1"),
        ("extra_headers", None),
    ]
