"""The dynamic proxy: typed when the contract is served, names-only otherwise."""

from __future__ import annotations

import asyncio
import inspect
import warnings
from typing import Any

import pytest
from fixture_util import ast_doc, edge_doc, interface

from logos_bridge import (
    AsyncBridgeClient,
    BridgeClient,
    BridgeWarning,
    Event,
    LogosResult,
    MethodNotFound,
    SubscriptionTerminated,
)
from logos_bridge.dynamic import (
    BLOCKING_PROXY_ATTRIBUTES,
    PROXY_ATTRIBUTES,
    BlockingDynamicMethod,
    BlockingDynamicModule,
    DynamicMethod,
    DynamicModule,
)
from logos_bridge.errors import ArgumentError, ArityError, MethodNotExposed, UntypedModule
from logos_bridge.lidl import IDENTITY_METHODS, Interface
from logos_bridge.testing import FakeBridge, ThreadedFakeBridge, Wire, async_test
from logos_bridge.typed import BlockingTypedSubscription, DecodedEvent, EventDecodeError

MINI = interface("mini_module")


def serve_mini(fake: FakeBridge | ThreadedFakeBridge, *, exposure: dict[str, list[str]] | None = None) -> None:
    fake.module("mini_module", list(MINI.method_names), list(MINI.event_names), interface=ast_doc("mini_module"),
                status="ok", exposure=exposure)
    fake.on_call("mini_module", "put", lambda ctx: ctx.params[0])
    fake.on_call("mini_module", "find", lambda ctx: {"success": True, "value": [ctx.params[0], ctx.params[1]],
                                                     "error": None})
    fake.on_call("mini_module", "clear", True)


@async_test
async def test_a_typed_module() -> None:
    async with FakeBridge() as fake:
        serve_mini(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                mod = await client.module("mini_module", require_typed=True)
            assert mod.is_typed and mod.interface_status == "ok" and mod.untyped_reason is None
            assert mod.module_name == "mini_module" and mod.served_interface == MINI and mod.typed_plans is not None
            assert mod.module_info.typed and mod.bridge_client is client
            assert repr(mod) == "<DynamicModule mini_module typed status=ok>"
            note = {"id": "n", "body": b"\x01", "title": None}
            assert await mod.put(note) == {"id": "n", "body": b"\x01"}
            assert await mod.find("x") == LogosResult(True, ["x", None], None)
            assert await mod.find(id="y", prefix="p") == LogosResult(True, ["y", "p"], None)
            assert await mod.call("find", "z", timeout=5) == LogosResult(True, ["z", None], None)
            assert await mod["clear"]() is None
            assert mod["clear"] is mod.clear
            with pytest.raises(ArgumentError, match="expected bytes at arg0.body, got string"):
                await mod.put({"id": "n", "body": "text"})
            with pytest.raises(ArityError, match="expected 1 arguments, got 0"):
                await mod.find()
            with pytest.raises(TypeError, match="unexpected keyword argument 'nope'"):
                await mod.find("x", nope=1)
            with pytest.raises(AttributeError):
                mod.nope  # noqa: B018
            with pytest.raises(MethodNotFound, match="declares no method 'nope'"):
                await mod.call("nope")
            assert "put" in dir(mod) and "lidl" in dir(mod)
            assert mod.declared_methods == MINI.method_names and mod.declared_events == ("added",)
            calls = [r.params["method"] for r in fake.requests if r.method == "rpc.call"]
            assert calls == ["put", "find", "find", "find", "clear"]


def test_signatures_and_docs_come_from_the_contract() -> None:
    mod = DynamicModule.from_module_info(None, _info(ast_doc("mini_module")))  # type: ignore[arg-type]
    find = mod.find
    signature = inspect.signature(find)
    assert list(signature.parameters) == ["id", "prefix", "timeout"]
    assert signature.parameters["id"].annotation is str
    assert signature.parameters["id"].default is inspect.Parameter.empty
    assert signature.parameters["prefix"].default is None
    assert signature.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.return_annotation == LogosResult[Any]
    assert find.__name__ == "find" and find.__qualname__ == "mini_module.find"
    assert find.__doc__ is not None and find.__doc__.startswith("Look a note up.")
    assert "LIDL: method find(id: tstr, prefix: ? tstr) -> result" in find.__doc__
    assert inspect.signature(mod.clear).return_annotation is None
    assert inspect.signature(mod.put).parameters["note"].annotation == dict[str, Any]
    assert repr(find).startswith("<DynamicMethod mini_module.find(id: str")


def _info(doc: dict[str, Any], *, identity: bool = False, **view: Any) -> Any:
    from logos_bridge import ModuleInfo

    iface = Interface.from_json(doc)
    if identity:
        iface = iface.with_identity()
        doc = iface.to_json()
    return ModuleInfo.from_json({
        "module": iface.name, "resolved": True, "events_declared": True,
        "methods": list(iface.method_names), "events": list(iface.event_names), "source": "lidl",
        "authoritative": False, "interface_status": "ok", "interface": doc,
        "interface_sha256": iface.interface_sha256(),
        "exposure": {"methods": list(iface.method_names), "events": list(iface.event_names)}, **view,
    })


def test_python_keywords_as_parameter_names() -> None:
    mod = DynamicModule.from_module_info(None, _info(edge_doc("names"), identity=True))  # type: ignore[arg-type]
    params = inspect.signature(mod["import"]).parameters
    assert list(params) == ["lambda_", "timeout_", "self", "timeout"]
    assert params["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(mod["async"]).parameters) == ["await_", "timeout"]


@async_test
async def test_keyword_aliases_reach_the_contract_names() -> None:
    doc = Interface.from_json(edge_doc("names")).with_identity().to_json()
    async with FakeBridge() as fake:
        fake.module("names_module", ["async", "import"], interface=doc, status="ok")
        fake.on_call("names_module", "async", lambda ctx: Wire({"k": {
            "class": ctx.params[0], "int": True, "self": "s", "meta": "m", "__init__": "i",
            "_bytes": {"_bytes": ""}, "list": []}}))
        fake.on_call("names_module", "import", lambda ctx: Wire({
            "class": str(ctx.params), "int": False, "self": "", "meta": "", "__init__": "", "_bytes": {"_bytes": "AA"},
            "list": []}))
        async with AsyncBridgeClient(fake.url) as client:
            mod = await client.module("names_module")
            result = await mod["async"](await_="v")
            assert result["k"]["class"] == "v" and result["k"]["_bytes"] == b""
            imported = await mod["import"]("l", timeout_=5, self=True, timeout=10)
            assert imported["class"] == "['l', 5, True]" and imported["_bytes"] == b"\x00"


@async_test
async def test_exposure_is_enforced_locally() -> None:
    async with FakeBridge() as fake:
        serve_mini(fake, exposure={"methods": ["find", "name", "version", "lidl"], "events": []})
        async with AsyncBridgeClient(fake.url) as client:
            mod = await client.module("mini_module")
            assert mod.exposed_methods == ("find", "name", "version", "lidl") and mod.exposed_events == ()
            assert not mod.put.exposed and mod.find.exposed
            with pytest.raises(MethodNotExposed) as excinfo:
                await mod.put({"id": "n", "body": b""})
            assert isinstance(excinfo.value, MethodNotFound) and excinfo.value.code == -32601
            assert "declares method 'put', but this bridge does not expose it" in str(excinfo.value)
            with pytest.raises(MethodNotExposed, match="declares event 'added'"):
                mod.on("added")
            with pytest.raises(ValueError, match="no events this bridge lets a client subscribe to"):
                mod.events()
            with pytest.raises(MethodNotFound, match="declares no event 'nope'"):
                mod.on("nope")
            assert not [r for r in fake.requests if r.method == "rpc.call"]


@async_test
async def test_typed_events() -> None:
    async with FakeBridge() as fake:
        serve_mini(fake)
        async with AsyncBridgeClient(fake.url) as client:
            mod = await client.module("mini_module")
            async with mod.on("added") as sub:
                fake.emit("mini_module", "added", {"id": "a", "body": {"_bytes": "AA"}})
                fake.emit("mini_module", "added", {"id": 1})
                first = await sub.get(timeout=5)
                assert isinstance(first, DecodedEvent) and first["note"] == {"id": "a", "body": b"\x00"}
                with pytest.raises(EventDecodeError):
                    await sub.get(timeout=5)
                fake.terminate("mini_module", reason="provider_changed")
                with pytest.raises(SubscriptionTerminated, match="provider_changed"):
                    await sub.get(timeout=5)
            async with mod.events() as every:
                assert every.events == ("added",)
            raw = Event("s", "mini_module", "added", [{"id": "x", "body": {"_bytes": ""}}], 1, 0)
            decoded = mod.decode_event(raw)
            assert isinstance(decoded, DecodedEvent) and decoded.args[0]["body"] == b""


@async_test
async def test_untyped_and_invalid_modules_are_names_only() -> None:
    async with FakeBridge() as fake:
        fake.module("legacy", ["ping", ("add", ["a", "b"])], ["ticked"], status="untyped")
        broken = fake.module("broken", ["reset"], [], status="invalid")
        broken.interface_error = "lidl() did not parse: 3:17: expected ')'"
        fake.module("old", ["ping"], ["ticked"])
        fake.on_call("legacy", "add", lambda ctx: {"sum": ctx.params[0] + ctx.params[1], "blob": {"_bytes": "AA"}})
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.warns(BridgeWarning, match=r"legacy: calls are untyped \(status untyped\)"):
                legacy = await client.module("legacy")
            assert not legacy.is_typed and legacy.served_interface is None and legacy.typed_plans is None
            assert legacy.untyped_reason == "status untyped"
            assert await legacy.add(1, 2) == {"sum": 3, "blob": b"\x00"}
            assert await legacy.call("add", 2, 2) == {"sum": 4, "blob": b"\x00"}
            with pytest.raises(TypeError, match="untyped: pass arguments by position"):
                await legacy.add(a=1, b=2)
            signature = inspect.signature(legacy.add)
            assert list(signature.parameters) == ["args", "timeout"]
            assert legacy.add.__doc__ is not None and "untyped" in legacy.add.__doc__
            with pytest.raises(AttributeError):
                legacy.nope  # noqa: B018
            with pytest.raises(MethodNotFound):  # names-only still sends it; the bridge refuses
                await legacy.call("nope")
            async with legacy.on("ticked") as sub:
                fake.emit("legacy", "ticked", 7)
                event = await sub.get(timeout=5)
                assert isinstance(event, Event) and event.data == [7]
                assert legacy.decode_event(event) is event
            async with legacy.events("ticked") as many:
                assert many.events == ("ticked",)
            with pytest.warns(BridgeWarning, match="status invalid: lidl\\(\\) did not parse: 3:17"):
                await client.module("broken")
            with pytest.warns(BridgeWarning, match="status no lidl discovery"):
                old = await client.module("old")
            assert old.interface_status is None
            with pytest.raises(UntypedModule, match=r"legacy has no usable lidl\(\) contract \(status untyped\)"):
                await client.module("legacy", require_typed=True)


@async_test
async def test_a_served_contract_that_fails_local_validation_is_names_only() -> None:
    doc = ast_doc("mini_module")
    doc["methods"][0]["name"] = "not-an-identifier"
    async with FakeBridge() as fake:
        fake.module("mini_module", ["put"], interface=doc, status="ok")
        async with AsyncBridgeClient(fake.url) as client:
            with pytest.warns(BridgeWarning, match="the served contract fails local validation"):
                mod = await client.module("mini_module")
            assert not mod.is_typed and mod.interface_status == "ok"


@async_test
async def test_unmappable_types_degrade_per_slot() -> None:
    doc = Interface.from_json(edge_doc("forward")).with_identity().to_json()
    async with FakeBridge() as fake:
        fake.module("forward_module", ["put", "scores", "keep"], ["changed"], interface=doc, status="ok")
        fake.on_call("forward_module", "put", lambda ctx: ctx.params[1] == {"any": "thing"})
        async with AsyncBridgeClient(fake.url) as client:
            mod = await client.module("forward_module")
            assert mod.is_typed
            assert await mod.put({"id": "i", "tags": ["t"]}, {"any": "thing"}) is True
            with pytest.raises(ArgumentError, match="arg0.tags"):
                await mod.put({"id": "i", "tags": "t"}, 1)


@async_test
async def test_a_reload_flips_the_mode() -> None:
    async with FakeBridge() as fake:
        fake.module("mini_module", ["put"], status="pending")
        async with AsyncBridgeClient(fake.url) as client:

            async def resolve() -> None:
                await fake.wait_for_request("rpc.schema", count=2)
                serve_mini(fake)

            task = asyncio.ensure_future(resolve())
            typed = await client.module("mini_module", discovery_wait=5)
            await task
            assert typed.is_typed
            fake.module("mini_module", ["put", "clear"], status="untyped")
            with pytest.warns(BridgeWarning):
                untyped = await typed.refreshed()
            assert not untyped.is_typed and untyped.exposed_methods == ("put", "clear")
            serve_mini(fake)
            again = await untyped.refreshed(require_typed=True)
            assert again.is_typed and again.find.plan is not None


def test_the_blocking_proxy() -> None:
    with ThreadedFakeBridge() as fake:
        serve_mini(fake)
        fake.module("legacy", ["ping"], ["ticked"], status="untyped")
        fake.on_call("legacy", "ping", "pong")
        with BridgeClient(fake.url) as bridge:
            mod = bridge.module("mini_module")
            assert isinstance(mod, BlockingDynamicModule) and mod.is_typed and mod.interface_status == "ok"
            assert mod.module_name == "mini_module" and mod.served_interface == MINI
            assert repr(mod).startswith("<BlockingDynamic") and mod.bridge_client is bridge
            assert mod.module_info.typed and mod.untyped_reason is None and mod.typed_plans is mod.aio.typed_plans
            assert mod.declared_methods == MINI.method_names and mod.declared_events == ("added",)
            assert mod.find("x") == LogosResult(True, ["x", None], None)
            assert mod["find"](id="y") == LogosResult(True, ["y", None], None)
            assert mod.call("clear") is None
            assert inspect.signature(mod.find) == inspect.signature(mod.aio.find)
            assert repr(mod.find).startswith("<BlockingDynamicMethod mini_module.find")
            assert "put" in dir(mod) and mod.exposed_methods[-1] == "lidl" and mod.exposed_events == ("added",)
            with pytest.raises(AttributeError):
                mod._private  # noqa: B018
            with mod.on("added") as sub:
                assert isinstance(sub, BlockingTypedSubscription)
                fake.emit("mini_module", "added", {"id": "a", "body": {"_bytes": ""}})
                assert sub.get(timeout=5)["note"]["id"] == "a"
            raw = Event("s", "mini_module", "added", [{"id": "x", "body": {"_bytes": ""}}], 1, 0)
            assert isinstance(mod.decode_event(raw), DecodedEvent)
            with pytest.warns(BridgeWarning):
                legacy = bridge.module("legacy")
            assert legacy.ping() == "pong"
            with legacy.events("ticked") as ticks:
                fake.emit("legacy", "ticked", 1)
                assert ticks.get(timeout=5).data == [1]
            fake.module("mini_module", ["put"], status="untyped")
            with pytest.warns(BridgeWarning):
                assert not mod.refreshed().is_typed


# ------------------------------------------------------------- attribute names


def test_the_proxy_api_never_shadows_an_identity_method() -> None:
    aio = DynamicModule.from_module_info(None, _info(ast_doc("mini_module"), identity=True))  # type: ignore[arg-type]
    blocking = BlockingDynamicModule(None, aio)  # type: ignore[arg-type]
    for proxy, declared in ((aio, PROXY_ATTRIBUTES), (blocking, BLOCKING_PROXY_ATTRIBUTES)):
        # object.__dir__: class and instance attributes, without the declared methods __dir__ adds
        public = {n for n in object.__dir__(proxy) if not n.startswith("_")}
        assert public == declared, type(proxy)
        assert not public & set(IDENTITY_METHODS), type(proxy)
        assert set(IDENTITY_METHODS) <= set(dir(proxy))


def serve_identity(fake: FakeBridge | ThreadedFakeBridge) -> None:
    fake.on_call("mini_module", "name", "mini_module")
    fake.on_call("mini_module", "version", "0.1.0")
    fake.on_call("mini_module", "lidl", "module mini_module {}\n")


@async_test
async def test_the_identity_built_ins_are_methods() -> None:
    async with FakeBridge() as fake:
        serve_mini(fake)
        serve_identity(fake)
        fake.module("legacy", ["ping", "name", "version"], [], status="untyped")
        fake.on_call("legacy", "name", "legacy")
        async with AsyncBridgeClient(fake.url) as client:
            mod = await client.module("mini_module")
            assert set(vars(mod)).isdisjoint(IDENTITY_METHODS)
            for name in IDENTITY_METHODS:
                method = getattr(mod, name)
                assert isinstance(method, DynamicMethod) and method.__name__ == name and method.plan is not None
            assert (await mod.name(), await mod.version(), await mod.lidl()) == (
                "mini_module", "0.1.0", "module mini_module {}\n")
            with pytest.warns(BridgeWarning):
                legacy = await client.module("legacy")
            assert isinstance(legacy.name, DynamicMethod) and await legacy.name() == "legacy"
            with pytest.raises(AttributeError):
                legacy.lidl  # noqa: B018  (an untyped module lists no lidl)
            calls = [r.params["method"] for r in fake.requests if r.method == "rpc.call"]
            assert calls == ["name", "version", "lidl", "name"]


def test_the_blocking_identity_built_ins_are_methods() -> None:
    with ThreadedFakeBridge() as fake:
        serve_mini(fake)
        serve_identity(fake)
        with BridgeClient(fake.url) as bridge:
            mod = bridge.module("mini_module")
            for name in IDENTITY_METHODS:
                assert isinstance(getattr(mod, name), BlockingDynamicMethod)
            assert (mod.name(), mod.version(), mod.lidl()) == ("mini_module", "0.1.0", "module mini_module {}\n")


# LIDL methods named like the old proxy attributes (now free), the kept ones, and a private one.
CLASH = Interface.from_shape({
    "shape": 1, "module": "clash_module", "records": {}, "events": {"tick": [["v", "int"]]},
    "methods": {name: {"params": [], "returns": "tstr"} for name in (
        "status", "info", "interface", "typed", "bridge", "module", "plans", "method", "refresh", "create",
        "method_names", "call", "on", "events", "aio", "portal", "_hidden")},
}).with_identity()


def serve_clash(fake: FakeBridge | ThreadedFakeBridge) -> None:
    fake.module("clash_module", list(CLASH.method_names), ["tick"], interface=CLASH.to_json(), status="ok")
    for name in CLASH.method_names:
        fake.on_call("clash_module", name, f"called {name}")


@async_test
async def test_members_named_like_the_proxy_api() -> None:
    async with FakeBridge() as fake:
        serve_clash(fake)
        async with AsyncBridgeClient(fake.url) as client:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                mod = await client.module("clash_module", require_typed=True)
            for name in ("status", "info", "interface", "typed", "bridge", "module", "plans", "method", "refresh",
                         "create", "method_names", "aio", "portal"):
                assert isinstance(getattr(mod, name), DynamicMethod), name
                assert await getattr(mod, name)() == f"called {name}"
            # The proxy's own names win; the module's members stay reachable by key.
            for name in ("call", "on", "events", "_hidden"):
                assert not isinstance(getattr(mod, name, None), DynamicMethod), name
                assert await mod[name]() == f"called {name}"
            assert await mod.call("events") == "called events"
            assert {"status", "events", "call"} <= set(dir(mod))


def test_blocking_members_named_like_the_proxy_api() -> None:
    with ThreadedFakeBridge() as fake:
        serve_clash(fake)
        with BridgeClient(fake.url) as bridge:
            mod = bridge.module("clash_module")
            assert mod.status() == "called status" and mod.refresh() == "called refresh"
            assert mod["aio"]() == "called aio" and mod["portal"]() == "called portal"
            assert mod.aio is not mod["aio"] and mod.call("on") == "called on"
            with pytest.raises(AttributeError):
                mod._hidden  # noqa: B018
            with pytest.raises(AttributeError, match="no attribute or declared method 'nope'"):
                mod.nope  # noqa: B018


def test_a_half_built_proxy_has_no_members() -> None:
    for cls in (DynamicModule, BlockingDynamicModule):
        bare = cls.__new__(cls)
        assert not hasattr(bare, "status") and not hasattr(bare, "name")
