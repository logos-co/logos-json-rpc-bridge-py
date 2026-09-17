from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from typing import Any

import pytest

from logos_bridge.lidl import Interface
from logos_bridge import http as bridge_http
from logos_bridge.errors import (
    BridgeError,
    BridgeWarning,
    ClientTimeout,
    ConnectError,
    ConnectionClosed,
    HttpStatusError,
    InvalidParams,
    MethodNotFound,
    ModuleUnavailable,
    ProviderRejection,
    RequestTooLarge,
    ShuttingDown,
)
from logos_bridge.http import BridgeHttpClient, status_error
from logos_bridge.testing import Delay, FakeError, NoResponse, Reject, ThreadedFakeBridge, free_port


def setup_module_m(fake: ThreadedFakeBridge) -> None:
    fake.module("m", [("greet", ["who"]), "echo", "slow"], ["tick"])
    fake.on_call("m", "greet", lambda ctx: f"hello, {ctx.params[0]}")
    fake.on_call("m", "echo", lambda ctx: ctx.params)


@pytest.fixture
def fake() -> Iterator[ThreadedFakeBridge]:
    with ThreadedFakeBridge() as bridge:
        setup_module_m(bridge)
        yield bridge


def test_call_headers(fake: ThreadedFakeBridge) -> None:
    client = BridgeHttpClient(fake.http_url)
    assert client.call("m", "greet", "http") == "hello, http"
    exchange = fake.http_requests[-1]
    assert (exchange.method, exchange.path, exchange.status) == ("POST", "/rpc", 200)
    assert exchange.header_all("Host") == [f"127.0.0.1:{fake.http_port}"]
    assert exchange.header("Origin") is None
    assert exchange.header("Connection") == "close"
    assert exchange.header("Content-Type") == "application/json"
    assert exchange.header("Accept") == "application/json"
    user_agent = exchange.header("User-Agent")
    assert user_agent is not None and user_agent.startswith("logos-bridge-py/")
    assert json.loads(exchange.body) == {"jsonrpc": "2.0", "id": 1, "method": "rpc.call",
                                         "params": {"module": "m", "method": "greet", "params": ["http"]}}
    assert fake.requests[-1].transport == "http"
    client.healthz()
    get = fake.http_requests[-1]
    assert get.method == "GET" and get.header("Content-Type") is None and get.header("Connection") == "close"


def test_host_header_override() -> None:
    with ThreadedFakeBridge(host_port=8645) as fake:
        setup_module_m(fake)
        with pytest.raises(HttpStatusError) as excinfo:
            BridgeHttpClient(fake.http_url).call("m", "greet", "x")
        assert excinfo.value.status == 403
        assert excinfo.value.hint is not None and "host_header" in excinfo.value.hint
        tunnelled = BridgeHttpClient(fake.http_url, host_header="127.0.0.1:8645")
        assert tunnelled.call("m", "greet", "x") == "hello, x"
        assert fake.http_requests[-1].header_all("Host") == ["127.0.0.1:8645"]
        with pytest.raises(ValueError):
            BridgeHttpClient(fake.http_url, host_header="bad host")


def test_read_only_routes() -> None:
    interface = {"name": "typed", "methods": []}
    with ThreadedFakeBridge() as fake:
        setup_module_m(fake)
        fake.module("typed", ["f"], [], interface=interface, status="ok")
        client = BridgeHttpClient(fake.http_url + "/")
        health = client.healthz()
        assert health.ok and health.protocol == "json-rpc-2.0"
        modules = client.list_modules()
        assert [m.module for m in modules] == ["m", "typed"] and modules[1].interface is None
        typed = client.schema("typed")
        assert typed.typed and typed.interface == Interface.from_json(interface).with_identity().to_json()
        assert typed.stale is False and typed.cross_check is not None and typed.cross_check.state == "consistent"
        with pytest.raises(MethodNotFound) as excinfo:
            client.schema("nope")
        assert (excinfo.value.op, excinfo.value.module) == ("rpc.schema", "nope")
        assert fake.http_requests[-1].status == 404
        with pytest.raises(ValueError):
            client.schema("")
        assert client.url == fake.http_url and "BridgeHttpClient" in repr(client)


def test_refusals_map_to_http_status_errors() -> None:
    with ThreadedFakeBridge() as fake:
        setup_module_m(fake)
        with pytest.warns(BridgeWarning):
            browser = BridgeHttpClient(fake.http_url, extra_headers={"Origin": "http://evil.example"})
        with pytest.raises(HttpStatusError) as excinfo:
            browser.healthz()
        assert excinfo.value.status == 403 and excinfo.value.error is None
    with ThreadedFakeBridge(auth_mode="bearer") as fake:
        with pytest.raises(HttpStatusError) as excinfo:
            BridgeHttpClient(fake.http_url).call("m", "greet", "x")
        assert excinfo.value.status == 401
        assert excinfo.value.hint is not None and "bearer" in excinfo.value.hint


def test_status_error_hints_and_bodies() -> None:
    unsupported = status_error(415, "http://h/rpc", b"<html>415</html>")
    assert unsupported.hint == "POST requires Content-Type: application/json" and unsupported.error is None
    not_found = status_error(404, "http://h/x", b'{"code":-32601,"message":"method not found"}')
    assert isinstance(not_found.error, MethodNotFound)
    assert not_found.hint is not None and "/healthz" in not_found.hint
    assert status_error(500, "http://h").hint is None


def test_a_null_id_error_is_raised_over_http(fake: ThreadedFakeBridge) -> None:
    fake.set_draining()
    client = BridgeHttpClient(fake.http_url)
    with pytest.raises(ShuttingDown) as excinfo:
        client.call("m", "greet", "x")
    assert excinfo.value.request_id is None and (excinfo.value.module, excinfo.value.method) == ("m", "greet")
    assert client.healthz().draining


def test_errors_rejections_and_values(fake: ThreadedFakeBridge) -> None:
    client = BridgeHttpClient(fake.http_url)
    fake.on_call("m", "echo", FakeError.upstream("object_unavailable"))
    with pytest.raises(ModuleUnavailable):
        client.call("m", "echo")
    fake.on_call("m", "echo", Reject("invalid_args", "expected 1 arguments, got 0", "m"))
    with pytest.raises(ProviderRejection):
        client.call("m", "echo")
    assert client.call("m", "echo", detect_rejection=False)["code"] == "invalid_args"
    fake.on_call("m", "echo", {"blob": b"\xfb\xff"})
    assert client.call("m", "echo") == {"blob": b"\xfb\xff"}
    assert client.call("m", "echo", decode_bytes=False) == {"blob": {"_bytes": "-_8"}}
    with pytest.raises(MethodNotFound):
        client.call("nope", "echo")
    with pytest.raises(InvalidParams) as excinfo:
        client.request("rpc.call", {"module": "m", "method": "greet", "params": {"nope": 1}})
    assert excinfo.value.detail is not None and excinfo.value.detail.path == "nope"
    for bad in (("", "x"), ("m", "")):
        with pytest.raises(ValueError):
            client.call(*bad)


def test_raw_requests_and_ping(fake: ThreadedFakeBridge) -> None:
    client = BridgeHttpClient(fake.http_url)
    for op in ("rpc.subscribe", "rpc.unsubscribe"):
        with pytest.raises(ValueError, match="not available over HTTP"):
            client.request(op, {"subscription": "s"})
    assert fake.http_requests == []
    assert client.request("rpc.ping") == "pong"
    assert client.request("rpc.cancel", {"id": 3}) == {"cancelled": False, "reason": "not_supported_upstream"}
    assert client.ping() >= 0
    assert "params" not in json.loads(fake.http_requests[0].body)


def test_the_request_size_guard(fake: ThreadedFakeBridge) -> None:
    client = BridgeHttpClient(fake.http_url, max_request_size=100)
    with pytest.raises(RequestTooLarge):
        client.call("m", "echo", "x" * 200)
    assert fake.http_requests == []
    assert client.call("m", "echo", "ok") == ["ok"]


def test_a_body_over_the_bridge_limit_is_dropped() -> None:
    with ThreadedFakeBridge(max_body_bytes=100) as fake:
        setup_module_m(fake)
        client = BridgeHttpClient(fake.http_url, max_request_size=None)
        with pytest.raises(ConnectionClosed) as excinfo:
            client.call("m", "echo", "x" * 500)
        assert excinfo.value.hint is not None and "max_body_bytes" in excinfo.value.hint


def test_connection_refused_and_timeouts(fake: ThreadedFakeBridge) -> None:
    with pytest.raises(ConnectError, match="refused"):
        BridgeHttpClient(f"http://127.0.0.1:{free_port()}").healthz()
    fake.on_call("m", "slow", NoResponse)
    client = BridgeHttpClient(fake.http_url, call_timeout=0.2)
    with pytest.raises(ClientTimeout):
        client.call("m", "slow")
    fake.on_call("m", "slow", Delay(0.1, "patient"))
    assert client.call("m", "slow", timeout=5) == "patient"


def test_a_response_over_the_size_limit(fake: ThreadedFakeBridge) -> None:
    client = BridgeHttpClient(fake.http_url, max_response_size=50)
    with pytest.raises(BridgeError, match="max_response_size"):
        client.call("m", "echo", "x" * 100)


def test_environment_proxies_are_ignored(fake: ThreadedFakeBridge, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = f"http://127.0.0.1:{free_port()}"
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, dead)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert BridgeHttpClient(fake.http_url).call("m", "greet", "direct") == "hello, direct"


def test_module_level_helpers(fake: ThreadedFakeBridge) -> None:
    assert bridge_http.healthz(fake.http_url).ok
    assert bridge_http.call("m", "greet", "helper", url=fake.http_url) == "hello, helper"
    assert bridge_http.call("m", "echo", b"\x01", url=fake.http_url, decode_bytes=False) == [{"_bytes": "AQ"}]


@pytest.mark.parametrize("url", ["ws://127.0.0.1:8645/ws", "127.0.0.1:8645", "http://"])
def test_bad_urls(url: str) -> None:
    with pytest.raises(ValueError):
        BridgeHttpClient(url)


def test_call_signature_matches_the_websocket_client() -> None:
    from logos_bridge import AsyncBridgeClient

    def shape(fn: Any) -> list[tuple[str, Any, Any]]:
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]

    assert shape(BridgeHttpClient.call) == shape(AsyncBridgeClient.call)
    assert shape(BridgeHttpClient.request) == shape(AsyncBridgeClient.request)
