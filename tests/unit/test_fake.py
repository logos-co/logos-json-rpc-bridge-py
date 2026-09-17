"""FakeBridge must answer like the bridge (json_rpc_bridge_impl.cpp, rpc_dispatcher.h, ws_server.cpp)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from bridge_util import eventually, exchange, ping_request, raw_ws, standard_module
from http_framing import (
    BODYLESS_REFUSALS,
    OTHER_METHODS,
    UNREAD_BODIES,
    UNREADABLE_LENGTHS,
    Exchange,
    answer_head,
    assert_bodyless_refusal_keeps,
    assert_late_refused_body_dropped,
    assert_length_required,
    assert_served_as_get,
    assert_unread_body_closes,
    post_head,
    read_answer,
)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidMessage

from logos_bridge import (
    SUBPROTOCOL,
    AsyncBridgeClient,
    BridgeHttpClient,
    MethodNotFound,
    SubscriptionTerminated,
    UpstreamCallFailed,
    contract_sha256,
    interface_sha256,
)
from logos_bridge.lidl import Interface
from logos_bridge import _protocol as proto
from logos_bridge.testing import (
    CallContext,
    Delay,
    FakeBridge,
    FakeError,
    NoResponse,
    Reject,
    ThreadedFakeBridge,
    async_test,
)
from logos_bridge.testing import _fake as fake_module

pytestmark = pytest.mark.fidelity

NOT_FOUND = {"code": -32601, "message": "method not found",
             "data": {"logos_error_code": 1, "logos_error_name": "METHOD_NOT_FOUND"}}
NOT_FOUND_TEXT = ('{"code":-32601,"data":{"logos_error_code":1,"logos_error_name":"METHOD_NOT_FOUND"},'
                  '"message":"method not found"}')


def error(code: int, message: str) -> dict[str, Any]:
    return proto.ERROR_TABLE[code].to_error(message)


def subscribe_request(req_id: Any, sid: Any, module: str = "m", event: str = "tick") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "rpc.subscribe",
            "params": {"subscription": sid, "module": module, "event": event}}


async def http(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: Sequence[tuple[str, str]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One raw HTTP/1.1 exchange on a fresh connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(_http_bytes(port, method, path, body, headers))
        await writer.drain()
        return await _read_response(reader)
    finally:
        writer.close()
        await writer.wait_closed()


def _http_bytes(port: int, method: str, path: str, body: bytes,
                headers: Sequence[tuple[str, str]] | None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    given = list(headers) if headers is not None else [("Host", f"127.0.0.1:{port}"),
                                                        ("Content-Type", "application/json")]
    lines += [f"{k}: {v}" for k, v in given]
    if body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("no answer")
    status = int(status_line.split()[1])
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    return status, headers, body


# ---------------------------------------------------------------------- ops


@async_test
async def test_bridge_ops_and_their_exact_answers() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            await ws.send(json.dumps(ping_request(1)))
            assert await ws.recv() == '{"id":1,"jsonrpc":"2.0","result":"pong"}'
            assert await exchange(ws, ping_request("text-id")) == proto.make_result("text-id", "pong")
            assert await exchange(ws, ping_request(None)) == proto.make_result(None, "pong")
            assert await exchange(ws, ping_request(1.5)) == proto.make_result(1.5, "pong")
            cancel = {"jsonrpc": "2.0", "id": 2, "method": "rpc.cancel", "params": {"id": 1}}
            assert (await exchange(ws, cancel))["result"] == {"cancelled": False, "reason": "not_supported_upstream"}
            unknown = {"jsonrpc": "2.0", "id": 3, "method": "rpc.authenticate"}
            assert await exchange(ws, unknown) == proto.make_error(3, NOT_FOUND)
            listing = await exchange(ws, {"jsonrpc": "2.0", "id": 4, "method": "rpc.list_modules"})
            assert listing["result"] == [{
                "module": "m", "resolved": True, "events_declared": True,
                "methods": ["greet", "echo", "fire"], "events": ["tick", "tock"],
                "source": "getPluginInterface", "authoritative": False,
            }]
            schema = {"jsonrpc": "2.0", "id": 5, "method": "rpc.schema", "params": {"module": "m"}}
            assert (await exchange(ws, schema))["result"] == listing["result"][0]
            missing = {"jsonrpc": "2.0", "id": 6, "method": "rpc.schema", "params": {"module": "nope"}}
            assert await exchange(ws, missing) == proto.make_error(6, NOT_FOUND)
            bad = {"jsonrpc": "2.0", "id": 7, "method": "rpc.schema", "params": {"module": 1}}
            assert await exchange(ws, bad) == proto.make_error(7, error(-32602, 'rpc.schema requires a string "module"'))
            positional = {"jsonrpc": "2.0", "id": 8, "method": "rpc.schema", "params": ["m"]}
            assert (await exchange(ws, positional))["error"]["code"] == -32602
        finally:
            await ws.close()


@async_test
async def test_module_views_by_discovery_status() -> None:
    doc = {"name": "typed", "version": "1.0.0", "methods": [
        {"name": "f", "params": [{"name": "x", "type": {"kind": "primitive", "name": "int", "elements": []}}],
         "returnType": {"kind": "primitive", "name": "tstr", "elements": []}}],
        "events": [{"name": "e"}]}
    served = Interface.from_json(doc).with_identity()
    async with FakeBridge() as fake:
        fake.module("pending_mod", ["name"], status="pending")
        fake.module("typed", ["f", "version", "undeclared"], ["e"], interface=doc, lidl_text="module typed {}\n")
        broken = fake.module("broken", ["f"], [], status="invalid", interface_error="lidl() did not parse: 1:1")
        fake.module("legacy", ["g"], ["e"], status="untyped")
        fake.module("unresolved", resolved=False)
        fake.module("old_bridge", ["g"])
        async with AsyncBridgeClient(fake.url) as client:
            typed = (await client.schema("typed")).raw
            assert typed == {
                "module": "typed", "resolved": True, "events_declared": True,
                "methods": ["f", "version", "undeclared"], "events": ["e"], "source": "lidl",
                "authoritative": False, "interface_status": "ok", "stale": False,
                "exposure": {"methods": ["f", "name", "version", "lidl"], "events": ["e"]},
                "interface_sha256": served.interface_sha256(),
                "contract_sha256": contract_sha256("module typed {}\n"),
                "cross_check": {"state": "consistent", "findings": []},
                "interface": served.to_json(),
            }
            pending = (await client.schema("pending_mod")).raw
            assert pending == {
                "module": "pending_mod", "resolved": False, "events_declared": False, "methods": [],
                "events": [], "source": "getPluginInterface", "authoritative": False,
                "interface_status": "pending", "stale": False, "exposure": {"methods": [], "events": []},
                "interface_sha256": None, "contract_sha256": None, "cross_check": None,
            }
            invalid = (await client.schema("broken")).raw
            assert invalid["interface_error"] == "lidl() did not parse: 1:1" and "interface" not in invalid
            assert invalid["source"] == "getPluginInterface" and invalid["exposure"] == {"methods": ["f"], "events": []}
            assert broken.describe()["interface_sha256"] is None
            broken.interface_error = None
            assert broken.describe()["interface_error"] == (
                "the contract does not match the running module (see cross_check)")
            untyped = (await client.schema("legacy")).raw
            assert untyped["interface_status"] == "untyped" and untyped["contract_sha256"] is None
            assert untyped["exposure"] == {"methods": ["g"], "events": ["e"]} and "interface_error" not in untyped
            unresolved = await client.schema("unresolved")
            assert (unresolved.resolved, unresolved.methods, unresolved.interface_status) == (False, (), None)
            old = (await client.schema("old_bridge")).raw
            assert set(old) == {"module", "resolved", "events_declared", "methods", "events", "source",
                                "authoritative"}
            listed = {m.module: m.raw for m in await client.list_modules()}
            assert "interface" not in listed["typed"]
            assert listed["typed"]["interface_sha256"] == served.interface_sha256()
            assert {k: v for k, v in typed.items() if k != "interface"} == listed["typed"]


@async_test
async def test_typed_modules_dispatch_by_their_declarations() -> None:
    doc = json.loads((Path(__file__).parents[1] / "fixtures" / "ast" / "mini_module.json").read_bytes())
    async with FakeBridge() as fake:
        fake.module("mini_module", ["put", "find", "clear", "unlisted"], interface=doc,
                    exposure={"methods": ["put", "find"], "events": []})
        fake.on_call("mini_module", "find", lambda ctx: ctx.params)
        fake.on_call("mini_module", "unlisted", "never")
        async with AsyncBridgeClient(fake.url) as client:
            by_name = {"jsonrpc": "2.0", "id": 1, "method": "rpc.call",
                       "params": {"module": "mini_module", "method": "find", "params": {"id": "x"}}}
            ws = await raw_ws(fake)
            try:
                assert (await exchange(ws, by_name))["result"] == ["x", None]
                by_name["params"]["params"] = {"prefix": "p"}
                missing = await exchange(ws, by_name)
                assert missing["error"]["code"] == -32602
                assert missing["error"]["data"]["invalid_params_detail"] == {"reason": "schema-mismatch",
                                                                              "path": "id"}
                by_name["params"]["params"] = {"id": "x", "bogus": 1}
                assert (await exchange(ws, by_name))["error"]["data"]["invalid_params_detail"]["path"] == "bogus"
            finally:
                await ws.close()
            for method in ("unlisted", "clear", "nope"):
                with pytest.raises(MethodNotFound):
                    await client.call("mini_module", method)
            with pytest.raises(MethodNotFound):
                await client.subscribe("mini_module", "added")
            # The built-ins answer from the contract unless scripted.
            assert await client.call("mini_module", "name") == "mini_module"
            assert await client.call("mini_module", "version") == "0.1.0"
            text = await client.call("mini_module", "lidl")
            assert text == (Path(__file__).parents[1] / "fixtures" / "lidl" / "mini_module.lidl").read_text()
            assert (await client.schema("mini_module")).contract_sha256 == contract_sha256(text)
            fake.on_call("mini_module", "lidl", "scripted")
            assert await client.call("mini_module", "lidl") == "scripted"
            fake.module("empty_version", interface={"name": "empty_version"})
            assert await client.call("empty_version", "version") == "1.0.0"
            fake.module("textless", interface={"name": "textless", "methods": [{"name": "f", "params": [
                {"name": "x", "type": {"kind": "set", "name": "", "elements": []}}]}]})
            with pytest.raises(UpstreamCallFailed):
                await client.call("textless", "lidl")


@async_test
async def test_a_changed_reload_terminates_subscriptions() -> None:
    doc = json.loads((Path(__file__).parents[1] / "fixtures" / "ast" / "mini_module.json").read_bytes())
    async with FakeBridge() as fake:
        fake.module("mini_module", interface=doc)
        async with AsyncBridgeClient(fake.url) as client:
            sub = await client.subscribe("mini_module", "added")
            fake.module("mini_module", interface=doc)  # the same build: invisible
            fake.emit("mini_module", "added", {"id": "a", "body": {"_bytes": ""}})
            assert (await sub.get(timeout=5)).generation == 1
            fake.mark_stale("mini_module")
            assert (await client.schema("mini_module")).stale is True
            changed = dict(doc, version="0.2.0")
            fake.module("mini_module", interface=changed, terminate_on_change=False)
            assert (await client.schema("mini_module")).stale is False
            fake.module("mini_module", interface=doc)
            with pytest.raises(SubscriptionTerminated) as excinfo:
                await sub.get(timeout=5)
            assert excinfo.value.reason == "provider_changed"
            again = await client.subscribe("mini_module", "added")
            fake.module("mini_module", ["put"], status="untyped")
            with pytest.raises(SubscriptionTerminated, match="provider_changed"):
                await again.get(timeout=5)
            info = await client.schema("mini_module")
            assert info.interface_status == "untyped" and info.interface is None
            assert fake.generation("mini_module", "added") == 3


def test_lidl_text_is_parsed_with_the_lidl_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fixture_util import fake_lidl_cli

    text = (Path(__file__).parents[1] / "fixtures" / "lidl" / "mini_module.lidl").read_text()
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(fake_lidl_cli(tmp_path)))
    with ThreadedFakeBridge() as fake:
        module = fake.module("mini_module", lidl_text=text)
        assert module.typed and module.contract is not None and module.contract.name == "mini_module"
        assert module.lidl_text == text and module.contract_sha256 == contract_sha256(text)
        assert list(module.methods) == ["put", "find", "clear", "name", "version", "lidl"]
        assert module.methods["find"] == ["id", "prefix"] and module.events == ["added"]
        fake.mark_stale("mini_module")
        assert fake.fake.modules["mini_module"].stale
        fake.remove_module("mini_module")
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(tmp_path / "missing"))
    with ThreadedFakeBridge() as fake, pytest.raises(Exception, match="LOGOS_LIDL_CLI"):
        fake.module("mini_module", lidl_text=text)


def test_module_statuses_are_checked() -> None:
    with ThreadedFakeBridge() as fake, pytest.raises(ValueError, match="unknown interface status 'weird'"):
        fake.module("m", status="weird")


@async_test
async def test_exposure_and_builtins() -> None:
    async with FakeBridge() as fake:
        fake.module("typed", ["f", "hidden"], ["e", "secret"], interface={"name": "typed"}, status="ok",
                    exposure={"methods": ["f"], "events": ["e"]})
        fake.module("legacy", ["name", "g"], ["e"], status="untyped")
        for module, method in (("typed", "f"), ("typed", "hidden"), ("typed", "lidl"), ("typed", "version"),
                               ("legacy", "name"), ("legacy", "g"), ("legacy", "lidl")):
            fake.on_call(module, method, f"{module}.{method}")
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.call("typed", "f") == "typed.f"
            # Built-ins are callable whatever the exposure; an untyped module has only its live ones.
            assert await client.call("typed", "lidl") == "typed.lidl"
            assert await client.call("typed", "version") == "typed.version"
            assert await client.call("legacy", "name") == "legacy.name"
            assert await client.call("legacy", "g") == "legacy.g"
            for module, method in (("typed", "hidden"), ("legacy", "lidl")):
                with pytest.raises(MethodNotFound):
                    await client.call(module, method)
            await (await client.subscribe("typed", "e")).aclose()
            with pytest.raises(MethodNotFound):
                await client.subscribe("typed", "secret")


@async_test
async def test_a_module_reload_changes_its_view() -> None:
    async with FakeBridge() as fake:
        fake.module("m", ["f"], status="pending")
        async with AsyncBridgeClient(fake.url) as client:
            assert (await client.schema("m")).interface_status == "pending"
            fake.module("m", ["f"], interface={"name": "m"}, status="ok")
            assert (await client.schema("m")).typed
            fake.module("m", ["f"], status="untyped")
            assert (await client.schema("m")).interface is None
            fake.remove_module("m")
            with pytest.raises(MethodNotFound):
                await client.schema("m")


# --------------------------------------------------------------- validation


CALL_CASES: list[tuple[Any, dict[str, Any]]] = [
    (["m", "greet"], error(-32602, 'rpc.call params must be an object with "module" and "method"')),
    ({"module": "m"}, error(-32602, 'rpc.call requires string "module" and "method"')),
    ({"module": "m", "method": 5}, error(-32602, 'rpc.call requires string "module" and "method"')),
    ({"module": "", "method": "greet"}, error(-32602, '"module" and "method" must be non-empty')),
    ({"module": "m", "method": "greet", "params": "x"}, error(-32602, "rpc.call inner params must be an object or array")),
    ({"module": "m", "method": "greet", "params": 3}, error(-32602, "rpc.call inner params must be an object or array")),
]


@async_test
async def test_rpc_call_validation() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            for i, (params, expected) in enumerate(CALL_CASES):
                request = {"jsonrpc": "2.0", "id": i, "method": "rpc.call", "params": params}
                assert await exchange(ws, request) == proto.make_error(i, expected), params
            null_inner = {"jsonrpc": "2.0", "id": 99, "method": "rpc.call",
                          "params": {"module": "m", "method": "echo", "params": None}}
            assert (await exchange(ws, null_inner))["result"] == []
            absent = {"jsonrpc": "2.0", "id": 100, "method": "rpc.call", "params": {"module": "m", "method": "echo"}}
            assert (await exchange(ws, absent))["result"] == []
            notification = {"jsonrpc": "2.0", "method": "rpc.call", "params": {"module": "m", "method": "echo"}}
            assert await exchange(ws, notification) == proto.make_error(None, error(
                -32600, "notifications are not accepted for rpc.call: every module call has a reply"))
        finally:
            await ws.close()


INVALID_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("{not json", error(-32700, "parse error")),
    ("", error(-32700, "parse error")),
    ("NaN", error(-32700, "parse error")),
    ("[]", error(-32600, "empty batch")),
    ("5", error(-32600, "request must be an object")),
    ('{"jsonrpc":"1.0","id":1,"method":"rpc.ping"}', error(-32600, 'jsonrpc must be "2.0"')),
    ('{"id":1,"method":"rpc.ping"}', error(-32600, 'jsonrpc must be "2.0"')),
    ('{"jsonrpc":"2.0","id":1,"method":7}', error(-32600, "method must be a string")),
    ('{"jsonrpc":"2.0","id":true,"method":"rpc.ping"}', error(-32600, "id must be a string, number or null")),
    ('{"jsonrpc":"2.0","id":[1],"method":"rpc.ping"}', error(-32600, "id must be a string, number or null")),
    ('{"jsonrpc":"2.0","id":1,"method":"rpc.ping","params":"x"}', error(-32602, "params must be an object or array")),
    ('{"jsonrpc":"2.0","id":1,"method":"rpc.ping","params":null}', error(-32602, "params must be an object or array")),
]


@pytest.mark.parametrize(("body", "expected"), INVALID_BODIES)
@async_test
async def test_unattributable_requests_are_answered_with_a_null_id(body: str, expected: dict[str, Any]) -> None:
    async with FakeBridge() as fake:
        ws = await raw_ws(fake)
        try:
            assert await exchange(ws, body) == proto.make_error(None, expected)
        finally:
            await ws.close()


@async_test
async def test_notifications_of_other_ops_are_still_answered() -> None:
    async with FakeBridge() as fake:
        ws = await raw_ws(fake)
        try:
            assert await exchange(ws, {"jsonrpc": "2.0", "method": "rpc.ping"}) == proto.make_result(None, "pong")
        finally:
            await ws.close()


@async_test
async def test_batches() -> None:
    async with FakeBridge(max_batch=3) as fake:
        standard_module(fake)
        fake.on_call("m", "echo", Delay(0.05, "slow"))
        ws = await raw_ws(fake)
        try:
            batch = [
                {"jsonrpc": "2.0", "id": 1, "method": "rpc.call", "params": {"module": "m", "method": "echo"}},
                ping_request(2),
                "garbage",
            ]
            answer = await exchange(ws, batch)
            assert answer == [
                proto.make_result(1, "slow"),
                proto.make_result(2, "pong"),
                proto.make_error(None, error(-32600, "request must be an object")),
            ]
            assert await exchange(ws, [ping_request(3)]) == [proto.make_result(3, "pong")]
            too_many = [ping_request(i) for i in range(4)]
            assert await exchange(ws, too_many) == proto.make_error(None, error(-32029, "batch too large"))
            assert len([r for r in fake.requests if r.method == "rpc.ping"]) == 6
        finally:
            await ws.close()


@async_test
async def test_the_default_batch_cap_is_32() -> None:
    async with FakeBridge() as fake:
        ws = await raw_ws(fake)
        try:
            assert len(await exchange(ws, [ping_request(i) for i in range(32)])) == 32
            assert (await exchange(ws, [ping_request(i) for i in range(33)]))["error"]["code"] == -32029
        finally:
            await ws.close()


# ------------------------------------------------------------ subscriptions


@async_test
async def test_notification_shapes() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            ack = await exchange(ws, subscribe_request(1, "s1"))
            assert ack == proto.make_result(1, {"subscription": "s1", "operation": "subscribe", "module": "m",
                                                "event": "tick", "state": "registered"})
            fake.emit("m", "tick", 42, "x")
            event = json.loads(await ws.recv())
            assert event["method"] == "rpc.event" and "id" not in event
            params = event["params"]
            assert set(params) == {"subscription", "module", "event", "data", "generation", "ts"}
            assert (params["subscription"], params["module"], params["event"], params["data"],
                    params["generation"]) == ("s1", "m", "tick", [42, "x"], 1)
            assert isinstance(params["ts"], int)
            fake.terminate("m")
            terminated = json.loads(await ws.recv())
            assert terminated == proto.make_notification("rpc.subscription_terminated", {
                "subscription": "s1", "module": "m", "event": "tick", "reason": "provider_unavailable"})
            assert fake.subscriptions() == []
            again = await exchange(ws, subscribe_request(2, "s1"))
            assert again["result"]["state"] == "registered"  # the lost id was released
            fake.emit("m", "tick", 1)
            assert json.loads(await ws.recv())["params"]["generation"] == 2
        finally:
            await ws.close()


@async_test
async def test_a_duplicate_subscription_id_is_acked_active_and_delivers_once() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            await exchange(ws, subscribe_request(1, "dup"))
            again = await exchange(ws, subscribe_request(2, "dup", event="tock"))
            assert again["result"] == {"subscription": "dup", "operation": "subscribe", "module": "m",
                                       "event": "tock", "state": "active"}
            assert fake.emit("m", "tick", 1) == 1 and fake.emit("m", "tock", 1) == 0
        finally:
            await ws.close()


@async_test
async def test_unsubscribe_is_idempotent() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            unknown = {"jsonrpc": "2.0", "id": 1, "method": "rpc.unsubscribe", "params": {"subscription": "nope"}}
            assert (await exchange(ws, unknown))["result"] == {"subscription": "nope", "operation": "unsubscribe"}
            missing = {"jsonrpc": "2.0", "id": 2, "method": "rpc.unsubscribe", "params": {}}
            assert await exchange(ws, missing) == proto.make_error(
                2, error(-32602, '"subscription" (a caller-assigned id) is required'))
            await exchange(ws, subscribe_request(3, 7))
            by_number = {"jsonrpc": "2.0", "id": 4, "method": "rpc.unsubscribe", "params": {"subscription": 7}}
            assert (await exchange(ws, by_number))["result"] == {"subscription": 7, "operation": "unsubscribe"}
            assert fake.subscriptions() == []
        finally:
            await ws.close()


@async_test
async def test_subscribe_validation() -> None:
    cases: list[tuple[Any, dict[str, Any]]] = [
        (["s"], error(-32602, "params must be an object")),
        ({"module": "m", "event": "tick"}, error(-32602, '"subscription" (a caller-assigned id) is required')),
        ({"subscription": True, "module": "m", "event": "tick"},
         error(-32602, '"subscription" (a caller-assigned id) is required')),
        ({"subscription": "s", "module": "m"}, error(-32602, 'rpc.subscribe requires string "module" and "event"')),
        ({"subscription": "s", "module": "m", "event": ""}, error(-32602, '"module" and "event" must be non-empty')),
        ({"subscription": "s", "module": "m", "event": "undeclared"}, NOT_FOUND),
    ]
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            for i, (params, expected) in enumerate(cases):
                request = {"jsonrpc": "2.0", "id": i, "method": "rpc.subscribe", "params": params}
                assert await exchange(ws, request) == proto.make_error(i, expected), params
            assert fake.subscriptions() == []
        finally:
            await ws.close()


@async_test
async def test_string_and_numeric_ids_are_different_subscriptions() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            assert (await exchange(ws, subscribe_request(1, 1)))["result"]["state"] == "registered"
            assert (await exchange(ws, subscribe_request(2, "1")))["result"]["state"] == "registered"
            assert sorted(repr(s.subscription) for s in fake.subscriptions()) == ["'1'", "1"]
        finally:
            await ws.close()


@async_test
async def test_legacy_modules_accept_any_event_until_resolved() -> None:
    async with FakeBridge() as fake:
        fake.module("legacy", ["f"], [], events_declared=False)
        fake.module("starting", resolved=False)
        async with AsyncBridgeClient(fake.url) as client:
            await (await client.subscribe("legacy", "anything")).aclose()
            await (await client.subscribe("starting", "anything")).aclose()
            fake.on_call("starting", "whatever", "ok")
            assert await client.call("starting", "whatever") == "ok"


@pytest.mark.parametrize("poison", [False, True])
@async_test
async def test_poisoned_subscription_ids(poison: bool) -> None:
    attempts: list[Any] = []

    def fail_first(ctx: Any) -> Any:
        attempts.append(ctx.subscription)
        return FakeError(-32601) if len(attempts) == 1 else None

    async with FakeBridge(poison_failed_subscribe_ids=poison) as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", fail_first)
        ws = await raw_ws(fake)
        try:
            assert (await exchange(ws, subscribe_request(1, "same")))["error"] == NOT_FOUND
            retry = await exchange(ws, subscribe_request(2, "same"))
            if poison:
                assert retry["result"]["state"] == "active"  # a bridge without the fix
                assert fake.emit("m", "tick", 1) == 0  # and it never delivers
                assert len(attempts) == 1
            else:
                assert retry["result"]["state"] == "registered"
                assert fake.emit("m", "tick", 1) == 1
        finally:
            await ws.close()


@pytest.mark.parametrize("early", [False, True])
@async_test
async def test_events_before_the_ack_flag(early: bool) -> None:
    async with FakeBridge(emit_events_before_subscribe_ack=early) as fake:
        standard_module(fake)
        fake.on_subscribe("m", "tick", lambda ctx: ctx.emit("hello"))
        ws = await raw_ws(fake)
        try:
            await ws.send(json.dumps(subscribe_request(1, "s")))
            first, second = json.loads(await ws.recv()), json.loads(await ws.recv())
            if early:
                assert first["method"] == "rpc.event" and second["id"] == 1
            else:
                assert first["id"] == 1 and second["method"] == "rpc.event"
        finally:
            await ws.close()


@async_test
async def test_subscription_limits() -> None:
    async with FakeBridge(max_subscriptions=2) as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        try:
            await exchange(ws, subscribe_request(1, "a"))
            await exchange(ws, subscribe_request(2, "b", event="tock"))
            assert await exchange(ws, subscribe_request(3, "c")) == proto.make_error(
                3, error(-32029, "too many subscriptions"))
            # The limit is checked before the duplicate test, as the bridge does.
            assert (await exchange(ws, subscribe_request(4, "a")))["error"]["code"] == -32029
        finally:
            await ws.close()


@async_test
async def test_subscribe_over_http_is_acked_and_goes_nowhere() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        status, _, body = await http(fake.http_port, "POST", "/rpc",
                                     body=json.dumps(subscribe_request(1, "s")).encode())
        assert status == 200 and json.loads(body)["result"]["state"] == "registered"
        assert fake.subscriptions() == []


# ----------------------------------------------------------------- closing


@async_test
async def test_the_outbound_queue_cap_closes_1008() -> None:
    async with FakeBridge(max_queued_frames=4) as fake:
        standard_module(fake)
        ws = await raw_ws(fake)
        await exchange(ws, subscribe_request(1, "s"))
        sent = [fake.emit("m", "tick", i) for i in range(10)]
        assert sent == [1, 1, 1, 1, 0, 0, 0, 0, 0, 0]
        with pytest.raises(ConnectionClosed) as excinfo:
            await ws.recv()  # the close pre-empts the queued frames
        assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 1008
        assert excinfo.value.rcvd.reason == ""


@pytest.mark.slow
@async_test(timeout=90)
async def test_a_reader_that_stops_reading_is_closed_1008() -> None:
    """The negative control for the client's never-blocking reader."""
    payload = "x" * 1024
    async with FakeBridge(max_queued_frames=16) as fake:
        standard_module(fake)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", fake.port))
        ws = await connect(fake.url, sock=sock, max_queue=1, subprotocols=[SUBPROTOCOL],
                           compression=None, proxy=None)
        await exchange(ws, subscribe_request(1, "s"))
        refused_at = None
        for i in range(50_000):
            if fake.emit("m", "tick", i, payload) == 0:
                refused_at = i
                break
            if i % 4 == 3:
                await asyncio.sleep(0)
        assert refused_at is not None, "the fake never saw a slow reader"
        received = 0
        with pytest.raises(ConnectionClosed) as excinfo:
            while True:
                await ws.recv()
                received += 1
        assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 1008
        assert 0 < received < refused_at


@async_test
async def test_an_oversized_frame_closes_1009() -> None:
    async with FakeBridge(max_frame_bytes=100) as fake:
        ws = await raw_ws(fake)
        prefix = '{"jsonrpc":"2.0","id":1,"method":"rpc.ping","params":{"p":"'
        frame = prefix + "x" * (100 - len(prefix) - 3) + '"}}'
        assert len(frame) == 100
        await ws.send(frame)
        assert json.loads(await ws.recv())["result"] == "pong"  # exactly the limit: accepted
        await ws.send("\N{EN DASH}" * 34)  # 34 characters, 102 bytes
        with pytest.raises(ConnectionClosed) as excinfo:
            await ws.recv()
        assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 1009


@async_test
async def test_a_binary_frame_closes_1003() -> None:
    async with FakeBridge() as fake:
        ws = await raw_ws(fake)
        await ws.send(json.dumps(ping_request(1)).encode())
        with pytest.raises(ConnectionClosed) as excinfo:
            await ws.recv()
        assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 1003


@async_test
async def test_stop_drops_connections_without_a_close_frame() -> None:
    fake = await FakeBridge().start()
    ws = await raw_ws(fake)
    await fake.stop()
    with pytest.raises(ConnectionClosed) as excinfo:
        await ws.recv()
    assert excinfo.value.rcvd is None
    await fake.stop()  # idempotent


@async_test
async def test_close_connections_sends_the_code() -> None:
    async with FakeBridge() as fake:
        ws = await raw_ws(fake)
        await fake.close_connections(1001, "restart")
        with pytest.raises(ConnectionClosed) as excinfo:
            await ws.recv()
        assert excinfo.value.rcvd is not None
        assert (excinfo.value.rcvd.code, excinfo.value.rcvd.reason) == (1001, "restart")


# ----------------------------------------------------------------- upgrades


async def raw_upgrade(port: int, headers: Sequence[tuple[str, str]]) -> bytes:
    """The head of the answer to a hand-written upgrade request (b"" when dropped)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    base = [("Upgrade", "websocket"), ("Connection", "Upgrade"),
            ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="), ("Sec-WebSocket-Version", "13")]
    writer.write(_http_bytes(port, "GET", "/ws", b"", list(headers) + base))
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except ConnectionError:
        return b""
    finally:
        writer.transport.abort()


@async_test
async def test_refused_upgrades_get_no_http_answer() -> None:
    async with FakeBridge(max_connections_per_peer=1) as fake:
        host = ("Host", f"127.0.0.1:{fake.port}")
        proto_header = ("Sec-WebSocket-Protocol", SUBPROTOCOL)
        accepted = await raw_upgrade(fake.port, [host, proto_header])
        assert accepted.startswith(b"HTTP/1.1 101")
        await eventually(lambda: not any(c.open for c in fake.connections), what="the probe to close")
        for headers, refusal in [
            ([("Host", "evil.example"), proto_header], "host"),
            ([("Host", f"127.0.0.1:{fake.port + 1}"), proto_header], "host"),
            ([host, host, proto_header], "host"),
            ([host, proto_header, ("Origin", "http://localhost")], "origin"),
            ([host, ("Sec-WebSocket-Protocol", "other, another")], "subprotocol"),
        ]:
            assert await raw_upgrade(fake.port, headers) == b"", refusal
            assert fake.handshakes[-1].refusal == refusal
        with pytest.raises(InvalidMessage):
            await connect(fake.url, origin="http://localhost", subprotocols=[SUBPROTOCOL], proxy=None)
        first = await raw_ws(fake)
        try:
            with pytest.raises(InvalidMessage):
                await raw_ws(fake)
            assert fake.handshakes[-1].refusal == "per_peer"
        finally:
            await first.close()


@async_test
async def test_hosts_and_origins_the_bridge_accepts() -> None:
    async with FakeBridge(allowed_origins=["http://app.local"]) as fake:
        for host in ("127.0.0.1", "localhost", f"localhost:{fake.port}", "[::1]", f"[::1]:{fake.port}"):
            answer = await raw_upgrade(fake.port, [("Host", host)])
            assert answer.startswith(b"HTTP/1.1 101"), host
        allowed = await raw_upgrade(fake.port, [("Host", "localhost"), ("Origin", "http://app.local")])
        assert allowed.startswith(b"HTTP/1.1 101")


@async_test
async def test_an_upgrade_without_a_subprotocol_is_accepted() -> None:
    async with FakeBridge() as fake:
        async with connect(fake.url, proxy=None) as ws:
            assert ws.subprotocol is None
            assert (await exchange(ws, ping_request(1)))["result"] == "pong"


@async_test
async def test_bearer_mode_refuses_non_browser_upgrades() -> None:
    async with FakeBridge(auth_mode="bearer", allowed_origins=["http://app.local"]) as fake:
        assert await raw_upgrade(fake.port, [("Host", "localhost")]) == b""
        allowed = await raw_upgrade(fake.port, [("Host", "localhost"), ("Origin", "http://app.local")])
        assert allowed.startswith(b"HTTP/1.1 101")  # browsers authenticate in-band


@async_test
async def test_max_connections() -> None:
    async with FakeBridge(max_connections=1) as fake:
        first = await raw_ws(fake)
        try:
            with pytest.raises(InvalidMessage):
                await raw_ws(fake)
            assert fake.handshakes[-1].refusal == "max_connections"
        finally:
            await first.close()


# --------------------------------------------------------------------- HTTP


@async_test
async def test_http_read_routes() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        port = fake.http_port
        status, headers, body = await http(port, "GET", "/healthz")
        assert status == 200 and headers["content-type"] == "application/json"
        health = json.loads(body)
        assert list(health) == ["protocol", "status", "uptime_seconds"]
        assert (health["status"], health["protocol"]) == ("ok", "json-rpc-2.0")
        assert (await http(port, "GET", "/healthz?verbose=1"))[0] == 200
        assert (await http(port, "PUT", "/healthz"))[0] == 200  # every non-POST is a GET
        status, _, body = await http(port, "GET", "/modules")
        assert status == 200 and [m["module"] for m in json.loads(body)] == ["m"]
        status, _, body = await http(port, "GET", "/modules/m")
        assert status == 200 and json.loads(body)["methods"] == ["greet", "echo", "fire"]
        for path in ("/modules/nope", "/nowhere", "/"):
            status, _, body = await http(port, "GET", path)
            assert (status, body.decode()) == (404, NOT_FOUND_TEXT), path


@async_test
async def test_http_gating() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        port = fake.http_port
        ping = json.dumps(ping_request(1)).encode()
        plain = [("Host", f"127.0.0.1:{port}"), ("Content-Type", "text/plain")]
        assert (await http(port, "POST", "/rpc", body=ping, headers=plain))[0] == 415
        missing = [("Host", f"127.0.0.1:{port}")]
        assert (await http(port, "POST", "/rpc", body=ping, headers=missing))[0] == 415
        charset = [("Host", f"127.0.0.1:{port}"), ("Content-Type", "application/json; charset=utf-8")]
        assert (await http(port, "POST", "/rpc", body=ping, headers=charset))[0] == 200
        origin = [("Host", f"127.0.0.1:{port}"), ("Content-Type", "application/json"),
                  ("Origin", "http://evil.example")]
        status, headers, _ = await http(port, "POST", "/rpc", body=ping, headers=origin)
        assert status == 403 and headers["content-type"] == "text/html"
        assert (await http(port, "GET", "/healthz", headers=[("Host", "evil.example")]))[0] == 403
        assert (await http(port, "GET", "/healthz", headers=[]))[0] == 403  # no Host at all
        assert [e.status for e in fake.http_requests][-3:] == [403, 403, 403]
    async with FakeBridge(auth_mode="bearer") as fake:
        status, headers, _ = await http(fake.http_port, "GET", "/healthz")
        assert (status, headers.get("connection")) == (401, None)
        status, headers, _ = await http(fake.http_port, "POST", "/rpc", body=b"{}")
        assert (status, headers.get("connection")) == (401, "close")


@async_test
async def test_http_json_rpc() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        port = fake.http_port
        status, _, body = await http(port, "POST", "/rpc", body=json.dumps(ping_request(1)).encode())
        assert (status, body) == (200, b'{"id":1,"jsonrpc":"2.0","result":"pong"}')
        status, _, body = await http(port, "POST", "/anything", body=json.dumps([ping_request(2)]).encode())
        assert json.loads(body) == [proto.make_result(2, "pong")]
        status, _, body = await http(port, "POST", "/rpc", body=b"\xff\xfe")
        assert json.loads(body) == proto.make_error(None, error(-32700, "parse error"))
        status, _, body = await http(port, "POST", "/rpc",
                                     body=json.dumps({"jsonrpc": "2.0", "method": "rpc.ping"}).encode())
        assert json.loads(body) == proto.make_result(None, "pong")
        assert fake.requests[0].transport == "http" and fake.requests[0].connection is None


@async_test
async def test_http_rest_projection() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        port = fake.http_port
        status, _, body = await http(port, "POST", "/modules/m/greet", body=b'{"who":"rest"}')
        assert (status, body) == (200, b'{"result":"hello, rest"}')
        assert fake.requests[-1].raw == proto.make_request(1, "rpc.call", {
            "module": "m", "method": "greet", "params": {"who": "rest"}})
        status, _, body = await http(port, "POST", "/modules/m/greet", body=b'["positional"]')
        assert body == b'{"result":"hello, positional"}'
        status, _, body = await http(port, "POST", "/modules/m/greet")
        assert body == b'{"result":"hello, None"}'  # an empty body is {}: every param missing
        status, _, body = await http(port, "POST", "/modules/m/nope", body=b"{}")
        assert json.loads(body) == {"error": NOT_FOUND}
        status, _, body = await http(port, "POST", "/modules/m/greet", body=b"[")
        assert json.loads(body) == proto.make_error(None, error(-32700, "parse error"))
        for path in ("/modules/m", "/modules//greet", "/modules/m/"):
            status, _, body = await http(port, "POST", path, body=b"{}")
            assert json.loads(body) == proto.make_error(None, NOT_FOUND), path
        fake.on_call("m", "echo", FakeError(-32001))
        status, _, body = await http(port, "POST", "/modules/m/echo", body=b"[]")
        assert json.loads(body) == {"error": proto.ERROR_TABLE[-32001].to_error()}


@async_test
async def test_http_keep_alive_and_body_cap() -> None:
    async with FakeBridge(max_body_bytes=64) as fake:
        port = fake.http_port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            for i in range(3):
                writer.write(_http_bytes(port, "POST", "/rpc", json.dumps(ping_request(i)).encode(), None))
                await writer.drain()
                status, headers, body = await _read_response(reader)
                assert status == 200 and "connection" not in headers  # kept alive: lws sends no header
                assert json.loads(body)["id"] == i
            writer.write(_http_bytes(port, "POST", "/rpc", b"x" * 65, None))
            await writer.drain()
            with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
                await _read_response(reader)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        assert fake.http_requests[-1].status is None
        closing = [("Host", f"127.0.0.1:{port}"), ("Content-Type", "application/json"), ("Connection", "close")]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(_http_bytes(port, "POST", "/rpc", json.dumps(ping_request(9)).encode(), closing))
            await writer.drain()
            status, headers, _ = await _read_response(reader)
            assert (status, headers.get("connection")) == (200, None)  # no header back, then a close
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()


@async_test
async def test_http_the_body_cap_applies_to_an_accepted_post_only() -> None:
    async with FakeBridge(max_body_bytes=64) as fake:
        port = fake.http_port
        big = b"x" * 65
        plain = [("Host", f"127.0.0.1:{port}"), ("Content-Type", "text/plain")]
        status, headers, _ = await http(port, "POST", "/rpc", body=big, headers=plain)
        assert (status, headers.get("connection")) == (415, "close")
        status, headers, _ = await http(port, "GET", "/healthz", body=big, headers=[("Host", f"127.0.0.1:{port}")])
        assert (status, headers.get("connection")) == (200, "close")
        assert [(e.status, e.body) for e in fake.http_requests] == [(415, b""), (200, b"")]


@pytest.fixture
def threaded() -> Iterator[ThreadedFakeBridge]:
    with ThreadedFakeBridge() as fake:
        yield fake


# The bridge's HTTP framing rules, shared with tests/integration/test_live_gating.py.
@pytest.mark.parametrize("framing", list(UNREADABLE_LENGTHS.values()), ids=list(UNREADABLE_LENGTHS))
def test_http_a_post_needs_a_readable_length(threaded: ThreadedFakeBridge, framing: tuple[str, ...]) -> None:
    assert_length_required(threaded.http_port, framing)
    assert threaded.fake.http_requests[-1].status == 411


@pytest.mark.parametrize("case", list(UNREAD_BODIES.values()), ids=list(UNREAD_BODIES))
def test_http_an_unread_body_closes_its_connection(threaded: ThreadedFakeBridge, case: Exchange) -> None:
    assert_unread_body_closes(threaded.http_port, case)
    unread, retry = threaded.fake.http_requests[-2:]
    assert (unread.status, unread.body, retry.status) == (case[0], b"", 200)


@pytest.mark.parametrize("case", list(BODYLESS_REFUSALS.values()), ids=list(BODYLESS_REFUSALS))
def test_http_a_bodyless_refusal_keeps_its_connection(threaded: ThreadedFakeBridge, case: Exchange) -> None:
    assert_bodyless_refusal_keeps(threaded.http_port, case)


def test_http_a_refused_body_that_arrives_late_is_dropped(threaded: ThreadedFakeBridge) -> None:
    assert_late_refused_body_dropped(threaded.http_port)
    assert [e.status for e in threaded.fake.http_requests] == [403, 200]  # the request behind the body too


@pytest.mark.parametrize("method", OTHER_METHODS)
def test_http_every_other_method_is_served_as_get(threaded: ThreadedFakeBridge, method: str) -> None:
    assert_served_as_get(threaded.http_port, method)


@async_test
async def test_http_pipelined_requests_are_answered_in_order() -> None:
    # A known difference: the bridge (libwebsockets 4.3.5) answers none of them.
    async with FakeBridge() as fake:
        port = fake.http_port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"".join(_http_bytes(port, "POST", "/rpc", json.dumps(ping_request(i)).encode(), None)
                                  for i in (1, 2)))
            await writer.drain()
            assert [json.loads((await _read_response(reader))[2])["id"] for _ in range(2)] == [1, 2]
        finally:
            writer.close()
            await writer.wait_closed()


def test_http_a_lingering_connection_closes_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_module, "_LINGER_SECONDS", 0.2)
    with ThreadedFakeBridge() as threaded, socket.create_connection(("127.0.0.1", threaded.http_port), 5) as sock:
        sock.sendall(post_head(threaded.http_port, 2, "Origin: http://evil.example"))
        assert answer_head(read_answer(sock))[1]["connection"] == "close"
        sock.sendall(b"{}")  # read and dropped
        time.sleep(0.5)
        with pytest.raises(OSError):  # past the deadline the fake has closed: the next sends are reset
            for _ in range(50):
                sock.sendall(b" ")
                time.sleep(0.02)


@async_test
async def test_http_stop_ends_a_lingering_connection() -> None:
    fake = await FakeBridge().start()
    reader, writer = await asyncio.open_connection("127.0.0.1", fake.http_port)
    try:
        writer.write(_http_bytes(fake.http_port, "POST", "/rpc", b"x", [("Host", f"127.0.0.1:{fake.http_port}")]))
        await writer.drain()
        status, headers, _ = await _read_response(reader)
        assert (status, headers["connection"]) == (415, "close")  # lingering: the client never closes
        loop = asyncio.get_running_loop()
        started = loop.time()
        await fake.stop()
        assert loop.time() - started < 1
    finally:
        await fake.stop()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


@async_test
async def test_draining() -> None:
    async with FakeBridge() as fake:
        standard_module(fake)
        fake.set_draining()
        assert fake.draining
        shutting_down = proto.make_error(None, proto.ERROR_TABLE[-32006].to_error())
        ws = await raw_ws(fake)
        try:
            assert await exchange(ws, ping_request(1)) == shutting_down
        finally:
            await ws.close()
        _, _, body = await http(fake.http_port, "POST", "/modules/m/greet", body=b"{}")
        assert json.loads(body) == shutting_down
        _, _, body = await http(fake.http_port, "GET", "/healthz")
        assert json.loads(body)["status"] == "draining"
        fake.set_draining(False)
        _, _, body = await http(fake.http_port, "GET", "/healthz")
        assert json.loads(body)["status"] == "ok"


# ----------------------------------------------------------------- handlers


@async_test
async def test_handler_outcomes(caplog: pytest.LogCaptureFixture) -> None:
    async def async_error(ctx: CallContext) -> Any:
        await asyncio.sleep(0)
        return FakeError(-32004)

    def explode(ctx: CallContext) -> Any:
        raise RuntimeError("handler bug")

    async with FakeBridge() as fake:
        methods = ["chain", "async_error", "explode", "unencodable", "no_data", "custom", "unknown_code",
                   "blob", "reject", "silent", "unscripted", "context"]
        fake.module("h", methods)
        fake.on_call("h", "chain", Delay(0.01, Delay(0.01, lambda ctx: f"chained {ctx.params}")))
        fake.on_call("h", "async_error", async_error)
        fake.on_call("h", "explode", explode)
        fake.on_call("h", "unencodable", lambda ctx: object())
        fake.on_call("h", "no_data", FakeError(-32001, data=None))
        fake.on_call("h", "custom", FakeError(-32001, "custom text"))
        fake.on_call("h", "unknown_code", FakeError(-32099))
        fake.on_call("h", "blob", b"\x00\xff")
        fake.on_call("h", "reject", Reject("unknown_method", "no such method", "h"))
        fake.on_call("h", "silent", NoResponse)
        fake.on_call("h", "context", lambda ctx: [ctx.module, ctx.method, ctx.transport, ctx.request_id,
                                                  ctx.raw_params, ctx.connection is not None])
        ws = await raw_ws(fake)

        async def call(method: str, *args: Any) -> Any:
            return await exchange(ws, {"jsonrpc": "2.0", "id": method, "method": "rpc.call",
                                       "params": {"module": "h", "method": method, "params": list(args)}})

        try:
            with caplog.at_level(logging.WARNING, logger="logos_bridge.testing"):
                assert (await call("chain", 1))["result"] == "chained [1]"
                assert (await call("async_error"))["error"]["code"] == -32004
                assert (await call("explode"))["error"] == proto.ERROR_TABLE[-32603].to_error()
                assert (await call("unencodable"))["error"]["code"] == -32603
                assert (await call("unscripted"))["error"]["code"] == -32603
            assert [type(e) for e in fake.handler_errors] == [RuntimeError, TypeError]
            assert "handler bug" in caplog.text and "unscripted" in caplog.text
            assert (await call("no_data"))["error"] == {"code": -32001, "message": "module unavailable"}
            assert (await call("custom"))["error"] == proto.ERROR_TABLE[-32001].to_error("custom text")
            assert (await call("unknown_code"))["error"] == {"code": -32099, "message": "error"}
            assert (await call("blob"))["result"] == {"_bytes": "AP8"}
            assert (await call("reject"))["result"] == {"code": "unknown_method", "message": "no such method",
                                                        "origin": "h"}
            assert (await call("context", 5))["result"] == ["h", "context", "ws", "context", [5], True]
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": "s", "method": "rpc.call",
                                      "params": {"module": "h", "method": "silent"}}))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
        finally:
            await ws.close()
    assert FakeError.upstream("unauthorized") == FakeError(-32004)
    assert FakeError.upstream("brand_new") == FakeError(-32603)
    assert repr(NoResponse) == "NoResponse"


@async_test
async def test_by_name_params() -> None:
    async with FakeBridge() as fake:
        fake.module("p", [("two", ["a", "b"]), "none"])
        fake.module("unresolved", resolved=False)
        for module, method in (("p", "two"), ("p", "none"), ("unresolved", "f")):
            fake.on_call(module, method, lambda ctx: ctx.params)
        ws = await raw_ws(fake)

        async def call(module: str, method: str, params: Any) -> Any:
            return await exchange(ws, {"jsonrpc": "2.0", "id": 1, "method": "rpc.call",
                                       "params": {"module": module, "method": method, "params": params}})

        try:
            assert (await call("p", "two", {"b": 2}))["result"] == [None, 2]
            assert (await call("p", "two", {"b": 2, "a": 1}))["result"] == [1, 2]
            assert (await call("p", "two", [9]))["result"] == [9]  # positional passes through
            assert (await call("p", "none", {}))["result"] == []
            mismatch = await call("p", "two", {"zeta": 1, "alpha": 2, "a": 0})
            assert mismatch["error"]["data"]["invalid_params_detail"] == {"reason": "schema-mismatch",
                                                                          "path": "alpha"}
            assert mismatch["error"]["message"] == "invalid params"
            unresolved = await call("unresolved", "f", {"x": 1})
            assert unresolved["error"]["data"]["invalid_params_detail"] == {"reason": "schema-mismatch",
                                                                            "path": "f"}
            assert (await call("unresolved", "f", [1]))["result"] == [1]
        finally:
            await ws.close()


@async_test
async def test_wait_helpers_time_out() -> None:
    async with FakeBridge() as fake:
        with pytest.raises(TimeoutError, match="rpc.nope"):
            await fake.wait_for_request("rpc.nope", timeout=0.05)
        with pytest.raises(TimeoutError, match="m.e"):
            await fake.wait_for_subscription("m", "e", timeout=0.05)
        with pytest.raises(RuntimeError):
            _ = FakeBridge(http=False).http_url
        with pytest.raises(ValueError):
            FakeBridge(auth_mode="basic")
        with pytest.raises(ValueError):
            fake.module("m", status="weird")


def test_the_threaded_fake_bridge() -> None:
    with ThreadedFakeBridge() as fake:
        fake.module("m", [("greet", ["who"])], ["tick"])
        fake.on_call("m", "greet", lambda ctx: f"hello, {ctx.params[0]}")
        assert fake.start() is fake
        client = BridgeHttpClient(fake.http_url)
        assert client.call("m", "greet", "thread") == "hello, thread"
        assert fake.wait_for_request("rpc.call").params["params"] == ["thread"]
        assert [r.method for r in fake.requests] == ["rpc.call"]
        assert fake.http_requests[0].status == 200
        assert fake.handshakes == [] and fake.handler_errors == []
        assert fake.url.startswith("ws://127.0.0.1:") and fake.port > 0 and fake.http_port > 0
        fake.set_draining()
        fake.set_draining(False)
        fake.freeze()
        fake.unfreeze()
        assert fake.emit("m", "tick", 1) == 0
        assert fake.terminate("m") == 0
        assert fake.send_raw("x") == 0
        assert fake.subscriptions() == []
        fake.abort_connections()
        fake.close_connections()
        fake.on_subscribe("m", "tick", None)
    fake.stop()  # idempotent
    with pytest.raises(RuntimeError):
        _ = fake.url
