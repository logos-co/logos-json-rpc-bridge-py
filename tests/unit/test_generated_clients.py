"""Generated clients against FakeBridge: rpc.call only, folding, events, compatibility."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from codegen_util import fixture_client
from fixture_util import ast_doc, interface, lidl_text

from logos_bridge import (
    AsyncBridgeClient,
    BridgeClient,
    DiscoveryPending,
    Event,
    IncompatibleModule,
    LogosResult,
    MethodNotFound,
    ProviderRejection,
    SubscriptionTerminated,
    UntypedModule,
)
from logos_bridge.digest import contract_sha256
from logos_bridge.errors import ArgumentError, EventDecodeError, ResultDecodeError
from logos_bridge.testing import FakeBridge, Reject, ThreadedFakeBridge, Wire, async_test

BRIDGE_OPS = {"rpc.call", "rpc.subscribe", "rpc.unsubscribe", "rpc.schema"}


@pytest.fixture(scope="module")
def clients(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    directory = tmp_path_factory.mktemp("generated")
    return {name: fixture_client(directory / name, name) for name in ("mini_module", "test_fullapi_ext_cpp")}


def serve(fake: FakeBridge | ThreadedFakeBridge, name: str, *, doc: dict[str, Any] | None = None,
          status: str = "ok", exposure: dict[str, list[str]] | None = None) -> None:
    served = doc if doc is not None else ast_doc(name)
    iface = interface(name)
    module = fake.module(name, list(iface.method_names), list(iface.event_names), interface=served,
                         status=status, exposure=exposure)
    if status in ("ok", "invalid"):
        module.contract_sha256 = contract_sha256(lidl_text(name))
    fake.on_call(name, "lidl", lidl_text(name))
    fake.on_call(name, "name", name)
    fake.on_call(name, "version", iface.version)


def assert_only_bridge_ops(fake: FakeBridge) -> None:
    methods = {r.method for r in fake.requests}
    assert methods <= BRIDGE_OPS, methods  # never a "<module>.<method>" alias


@async_test
async def test_calls_round_trip_through_rpc_call(clients: dict[str, ModuleType]) -> None:
    mini = clients["mini_module"]
    async with FakeBridge() as fake:
        serve(fake, "mini_module")
        fake.on_call("mini_module", "put", lambda ctx: ctx.params[0])
        fake.on_call("mini_module", "find", lambda ctx: {"success": ctx.params[1] is None, "value": ctx.params[0],
                                                         "error": None})
        fake.on_call("mini_module", "clear", True)
        async with AsyncBridgeClient(fake.url) as bridge:
            client = mini.AsyncMiniModuleClient(bridge)
            assert client.bridge is bridge and client.module == "mini_module"
            assert repr(client) == f"<AsyncMiniModuleClient mini_module on {fake.url}>"
            note = mini.Note(id="n1", body=b"\x00\xff", title="t")
            assert await client.put(note) == note
            assert await client.find("x") == LogosResult(True, "x", None)
            assert await client.find("x", "p", timeout=5) == LogosResult(False, "x", None)
            assert await client.clear() is None
            assert await client.name() == "mini_module" and await client.version() == "0.1.0"
            assert await client.lidl() == lidl_text("mini_module")
            puts = [r for r in fake.requests if r.method == "rpc.call" and r.params["method"] == "put"]
            assert puts[0].params == {"module": "mini_module", "method": "put", "params": [
                {"id": "n1", "body": {"_bytes": "AP8"}, "title": "t"}]}
            finds = [r.params["params"] for r in fake.requests if r.method == "rpc.call"
                     and r.params["method"] == "find"]
            assert finds == [["x", None], ["x", "p"]]
            with pytest.raises(ArgumentError, match="expected Note at arg0, got object"):
                await client.put({"id": "n1", "body": b""})  # type: ignore[arg-type]
            assert_only_bridge_ops(fake)
            other = mini.AsyncMiniModuleClient(bridge, "notes_v2")
            fake.module("notes_v2", ["name"], status="untyped")
            fake.on_call("notes_v2", "name", "notes_v2")
            assert await other.name() == "notes_v2"


@async_test
async def test_rejections_fold_and_bad_results_are_decode_errors(clients: dict[str, ModuleType]) -> None:
    ext = clients["test_fullapi_ext_cpp"]
    async with FakeBridge() as fake:
        serve(fake, "test_fullapi_ext_cpp")
        fake.on_call("test_fullapi_ext_cpp", "echoBlob",
                     Reject("dispatch_failed", "expected integer at arg0.n, got string", "test_fullapi_ext_cpp"))
        fake.on_call("test_fullapi_ext_cpp", "echoStringMap",
                     {"code": "invalid_args", "message": "expected 1 arguments, got 0", "origin": "p"})
        fake.on_call("test_fullapi_ext_cpp", "echoOpt", Wire({"maybe": 1}))
        fake.on_call("test_fullapi_ext_cpp", "echoOptional", Wire(None))
        async with AsyncBridgeClient(fake.url) as bridge:
            client = ext.AsyncTestFullapiExtCppClient(bridge)
            with pytest.raises(ProviderRejection) as rejected:
                await client.echo_blob(ext.Blob(id="x", n=1, payload=b""))
            assert rejected.value.code == "dispatch_failed" and rejected.value.method == "echoBlob"
            with pytest.raises(ProviderRejection, match="expected 1 arguments"):
                await client.echo_string_map({"k": "v"})  # rejection-ambiguous, still folded
            with pytest.raises(ResultDecodeError, match="expected string at result.required, got null"):
                await client.echo_opt(ext.Opt(required="r"))
            assert await client.echo_optional(None) is None
            assert_only_bridge_ops(fake)


@async_test
async def test_records_optionals_and_nested_bytes(clients: dict[str, ModuleType]) -> None:
    ext = clients["test_fullapi_ext_cpp"]
    async with FakeBridge() as fake:
        serve(fake, "test_fullapi_ext_cpp")
        for method in ("echoWrapper", "echoOptList", "echoBlobMap", "echoMapOfBytesLists", "echoNestedInts"):
            fake.on_call("test_fullapi_ext_cpp", method, lambda ctx: Wire(ctx.params[0]))
        async with AsyncBridgeClient(fake.url) as bridge:
            client = ext.AsyncTestFullapiExtCppClient(bridge)
            blob = ext.Blob(id="b", n=2**64 - 1, payload=bytes(range(256)))
            wrapper = ext.Wrapper(inner=blob, tags=["a"], blobs=[blob, blob])
            assert await client.echo_wrapper(wrapper) == wrapper
            opts = [ext.Opt(required="a", maybe="m", count=0, blob=b""), ext.Opt(required="b")]
            assert await client.echo_opt_list(opts) == opts
            sent = [r for r in fake.requests if r.method == "rpc.call" and r.params["method"] == "echoOptList"]
            assert sent[0].params["params"] == [[
                {"required": "a", "maybe": "m", "count": 0, "blob": {"_bytes": ""}}, {"required": "b"}]]
            assert await client.echo_blob_map({"k": blob}) == {"k": blob}
            assert await client.echo_map_of_bytes_lists({"_bytes": [b"x"]}) == {"_bytes": [b"x"]}
            assert await client.echo_nested_ints(((1, 2), (3,))) == [[1, 2], [3]]


@async_test
async def test_typed_events_decode_one_item_at_a_time(clients: dict[str, ModuleType]) -> None:
    mini = clients["mini_module"]
    async with FakeBridge() as fake:
        serve(fake, "mini_module")
        async with AsyncBridgeClient(fake.url) as bridge:
            client = mini.AsyncMiniModuleClient(bridge)
            async with client.on_added() as sub:
                fake.emit("mini_module", "added", {"id": "a", "body": {"_bytes": "AA"}})
                fake.emit("mini_module", "added", {"id": "b"})
                fake.emit("mini_module", "added", {"id": "c", "body": {"_bytes": ""}})
                first = await sub.get(timeout=5)
                assert isinstance(first, mini.AddedEvent) and first.note == mini.Note(id="a", body=b"\x00")
                assert first.meta.event == "added" and first.meta.generation == 1
                with pytest.raises(EventDecodeError, match="expected bytes at arg0.body, got null"):
                    await sub.get(timeout=5)
                third = await sub.get(timeout=5)
                assert third.note.id == "c"
                fake.emit("mini_module", "added", "garbage")
                fake.emit("mini_module", "added", {"id": "d", "body": {"_bytes": ""}})
                seen = []
                async for item in sub.results():
                    seen.append(item)
                    if len(seen) == 2:
                        break
                assert isinstance(seen[0], EventDecodeError) and seen[1].note.id == "d"  # type: ignore[union-attr]
            async with client.events() as every:
                assert every.events == ("added",)
                fake.terminate("mini_module", reason="provider_changed")
                with pytest.raises(SubscriptionTerminated) as ended:
                    await every.get(timeout=5)
                assert ended.value.reason == "provider_changed"
            raw = Event("s", "mini_module", "added", [{"id": "z", "body": {"_bytes": ""}}], 4, 0)
            assert client.decode_added(raw).note.id == "z"
            with pytest.raises(EventDecodeError, match="expected a 'added' event, got 'other'"):
                client.decode_added(Event("s", "mini_module", "other", [], 1, 0))
            assert_only_bridge_ops(fake)


@async_test
async def test_every_compatibility_level(clients: dict[str, ModuleType]) -> None:
    ext = clients["test_fullapi_ext_cpp"]
    async with FakeBridge() as fake:
        async with AsyncBridgeClient(fake.url) as bridge:
            client = ext.AsyncTestFullapiExtCppClient(bridge)
            serve(fake, "test_fullapi_ext_cpp")
            exact = await client.check_compat()
            assert exact.level == "exact" and exact.served_contract_sha256 == ext.CONTRACT_SHA256
            assert exact.expected_interface_sha256 == ext.INTERFACE_SHA256
            assert exact.expected_shape_sha256 == ext.SHAPE_SHA256

            reworded = ast_doc("test_fullapi_ext_cpp")
            reworded["description"] = "reworded"
            reworded["version"] = "1.0.1"
            serve(fake, "test_fullapi_ext_cpp", doc=reworded)
            assert (await client.check_compat()).level == "shape"

            other = json.loads(json.dumps(ast_doc("test_fullapi_ext_cpp")).replace(
                '"test_fullapi_ext_cpp"', '"test_fullapi_ext_rust"'))
            serve_other = copy.deepcopy(other)
            fake.module("test_fullapi_ext_rust", ["whoAmI"], interface=serve_other, status="ok")
            rust = ext.AsyncTestFullapiExtCppClient(bridge, "test_fullapi_ext_rust")
            assert (await rust.check_compat()).level == "structural"

            fake.module("test_fullapi_ext_cpp", ["whoAmI", "echoBlob", "lidl"], ["blobEvent"], status="untyped")
            with pytest.raises(UntypedModule, match=r"status: untyped"):
                await client.check_compat()
            names = await client.check_compat(allow_untyped=True)
            assert names.level == "names" and "echoWrapper" in names.missing.methods

            changed = ast_doc("test_fullapi_ext_cpp")
            changed["methods"][1]["params"][0]["type"]["name"] = "Wrapper"
            changed["methods"][1]["params"][0]["valueType"]["name"] = "Wrapper"
            serve(fake, "test_fullapi_ext_cpp", doc=changed)
            with pytest.raises(IncompatibleModule, match="different: methods echoBlob") as incompatible:
                await client.check_compat()
            assert incompatible.value.report.mismatched.methods == ("echoBlob",)

            fake.module("test_fullapi_ext_cpp", ["whoAmI"], status="pending")

            async def resolve() -> None:
                await fake.wait_for_request("rpc.schema", predicate=lambda r: True, count=8)
                serve(fake, "test_fullapi_ext_cpp")

            task = asyncio.ensure_future(resolve())
            waited = await client.check_compat(discovery_wait=5)
            await task
            assert waited.level == "exact"
            fake.module("test_fullapi_ext_cpp", ["whoAmI"], status="pending")
            with pytest.raises(DiscoveryPending):
                await client.check_compat(discovery_wait=0.1)
            assert_only_bridge_ops(fake)


@async_test
async def test_not_exposed_members(clients: dict[str, ModuleType]) -> None:
    ext = clients["test_fullapi_ext_cpp"]
    methods = [m for m in interface("test_fullapi_ext_cpp").method_names if m != "echoWrapper"]
    async with FakeBridge() as fake:
        serve(fake, "test_fullapi_ext_cpp", exposure={"methods": methods, "events": []})
        async with AsyncBridgeClient(fake.url) as bridge:
            client = ext.AsyncTestFullapiExtCppClient(bridge)
            report = await client.check_compat()
            assert report.level == "exact"
            assert report.not_exposed.methods == ("echoWrapper",) and report.not_exposed.events == ("blobEvent",)
            report.require(methods=["echoBlob", "lidl"])
            with pytest.raises(IncompatibleModule, match="method echoWrapper is not exposed"):
                report.require(methods=["echoWrapper"])
            blob = ext.Blob(id="b", n=1, payload=b"")
            with pytest.raises(MethodNotFound):  # the bridge refuses it: -32601
                await client.echo_wrapper(ext.Wrapper(inner=blob, tags=[], blobs=[]))
            assert await client.lidl() == lidl_text("test_fullapi_ext_cpp")  # always callable


def test_the_blocking_client(clients: dict[str, ModuleType]) -> None:
    mini = clients["mini_module"]
    with ThreadedFakeBridge() as fake:
        serve(fake, "mini_module")
        fake.on_call("mini_module", "put", lambda ctx: ctx.params[0])
        fake.on_call("mini_module", "clear", True)
        with BridgeClient(fake.url) as bridge:
            client = mini.MiniModuleClient(bridge)
            assert client.bridge is bridge and client.portal is bridge.portal and client.module == "mini_module"
            assert client.aio.bridge is bridge.aio
            assert repr(client) == f"<MiniModuleClient mini_module on {fake.url}>"
            note = mini.Note(id="n", body=b"x")
            assert client.put(note) == note and client.clear() is None
            assert client.lidl() == lidl_text("mini_module")
            assert client.check_compat().level == "exact"
            with client.on_added() as sub:
                fake.emit("mini_module", "added", {"id": "e", "body": {"_bytes": ""}})
                assert sub.get(timeout=5).note.id == "e"
            with client.events("added") as every:
                fake.emit("mini_module", "added", {"id": "f", "body": {"_bytes": ""}})
                assert next(iter(every)).note.id == "f"
            raw = Event("s", "mini_module", "added", [{"id": "g", "body": {"_bytes": ""}}], 1, 0)
            assert client.decode_added(raw).note.id == "g"


@pytest.mark.parametrize("name", ["mini_module", "test_fullapi_ext_cpp"])
def test_blocking_signatures_mirror_the_async_ones(clients: dict[str, ModuleType], name: str) -> None:
    module = clients[name]
    base = name.title().replace("_", "")
    aio, sync = getattr(module, f"Async{base}Client"), getattr(module, f"{base}Client")
    members = [m for m in vars(aio) if not m.startswith("_") and callable(getattr(aio, m))]
    assert members and set(members) <= set(vars(sync))
    for member in members:
        a, s = inspect.signature(getattr(aio, member)), inspect.signature(getattr(sync, member))
        assert list(a.parameters) == list(s.parameters), member
        if member.startswith("on_") or member == "events":
            continue
        assert a.return_annotation == s.return_annotation, member


def test_generated_clients_import_without_a_bridge(clients: dict[str, ModuleType], tmp_path: Path) -> None:
    mini = clients["mini_module"]
    assert mini.RECORD_TYPES == {"Note": mini.Note} and mini.EVENT_TYPES == {"added": mini.AddedEvent}
    assert mini._BINDING.interface.name == "mini_module"
