"""FakeProvider: a contract-checked provider, answering like a C++ one."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from codegen_util import fixture_client
from fixture_util import ast_doc, interface, lidl_text
from test_conformance import PINNED, TABLES

from logos_bridge import (
    AsyncBridgeClient,
    BridgeClient,
    LogosResult,
    MethodNotFound,
    ProviderRejection,
    UpstreamCallFailed,
)
from logos_bridge.digest import contract_sha256
from logos_bridge.lidl import Interface
from logos_bridge.testing import (
    FakeBridge,
    FakeError,
    FakeProvider,
    NoResponse,
    Reject,
    ThreadedFakeBridge,
    async_test,
)
from logos_bridge.testing.conformance import wire


class FullApi:
    """test_fullapi_cpp's echoes, in Python."""

    def __init__(self, provider: FakeProvider | None = None) -> None:
        self.provider = provider
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("echo"):
            def echo(*args: Any) -> Any:
                self.calls.append((name, args))
                return args[0]
            return echo
        raise AttributeError(name)

    # Real attributes win over __getattr__, so these use the LIDL names.
    def whoAmI(self) -> str:  # noqa: N802
        return "test_fullapi_cpp"

    def echoTriple(self, i: int, s: str, b: bytes) -> str:  # noqa: N802
        return f"i={i}|s={s}|b={b.hex()}"

    def doVoid(self) -> str:  # noqa: N802
        return "ignored: a no-return method answers true"

    def makeResult(self, ok: bool) -> LogosResult[Any]:  # noqa: N802
        if ok:
            return LogosResult(True, {"ok": True, "provider": "fake"})
        return LogosResult(False, None, "deliberate error for testing")

    def fireUintEvent(self, v: int) -> bool:  # noqa: N802
        assert self.provider is not None
        self.provider.emit("uintEvent", v)
        return True


def full_api(fake: FakeBridge | ThreadedFakeBridge) -> tuple[FakeProvider, FullApi]:
    impl = FullApi()
    provider = FakeProvider(ast_doc("test_fullapi_cpp"), impl, lidl_text=lidl_text("test_fullapi_cpp"))
    impl.provider = provider
    if isinstance(fake, FakeBridge):
        provider.install(fake)
    return provider, impl


@async_test
async def test_calls_decode_like_a_provider_and_encode_per_contract() -> None:
    async with FakeBridge() as fake:
        provider, impl = full_api(fake)
        assert repr(provider) == "<FakeProvider test_fullapi_cpp impl=FullApi>" and provider.bridge is fake
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.call("test_fullapi_cpp", "whoAmI") == "test_fullapi_cpp"
            assert await client.call("test_fullapi_cpp", "echoUint", 2**64 - 1) == 2**64 - 1
            assert await client.call("test_fullapi_cpp", "echoBytes", b"\x00\xff") == b"\x00\xff"
            # Lenient like logos_codec.h: a plain string for bstr, a whole float for int.
            assert await client.call("test_fullapi_cpp", "echoBytes", "hi") == b"hi"
            assert await client.call("test_fullapi_cpp", "echoInt", 3.0) == 3
            assert impl.calls[-1] == ("echoInt", (3,))
            assert await client.call("test_fullapi_cpp", "echoTriple", -7, "hé", b"\x00\xff") == \
                "i=-7|s=hé|b=00ff"
            assert await client.call("test_fullapi_cpp", "doVoid") is True
            assert await client.call("test_fullapi_cpp", "makeResult", False) == {
                "success": False, "value": None, "error": "deliberate error for testing"}
            assert await client.call("test_fullapi_cpp", "name") == "test_fullapi_cpp"
            assert await client.call("test_fullapi_cpp", "version") == "1.0.0"
            assert await client.call("test_fullapi_cpp", "lidl") == lidl_text("test_fullapi_cpp")
            info = await client.schema("test_fullapi_cpp")
            assert info.typed and info.contract_sha256 == contract_sha256(lidl_text("test_fullapi_cpp"))
            async with client.subscribe("test_fullapi_cpp", "uintEvent") as sub:
                assert await client.call("test_fullapi_cpp", "fireUintEvent", 2**64 - 1) is True
                assert (await sub.get(timeout=5)).data == [2**64 - 1]


@async_test
async def test_refusals_use_the_providers_words() -> None:
    async with FakeBridge() as fake:
        full_api(fake)
        async with AsyncBridgeClient(fake.url) as client:
            for args, code, message in (
                ((), "invalid_args", "expected 1 arguments, got 0"),
                ((1, 2), "invalid_args", "expected 1 arguments, got 2"),
                (("x",), "dispatch_failed", "expected integer at arg0, got string"),
                ((3.5,), "dispatch_failed", "expected integer at arg0, got number"),
                ((2**63,), "dispatch_failed", "expected signed integer in range at arg0, got number"),
            ):
                result = await client.call("test_fullapi_cpp", "echoInt", *args, detect_rejection=False)
                assert result == {"code": code, "message": message, "origin": "test_fullapi_cpp"}, args
            with pytest.raises(ProviderRejection, match="expected 0 arguments, got 1"):
                await client.call("test_fullapi_cpp", "version", "junk")
            with pytest.raises(MethodNotFound):  # undeclared: the bridge refuses it for a typed module
                await client.call("test_fullapi_cpp", "noSuchMethod")


@async_test
async def test_every_pinned_rejection_matches_when_sent_raw() -> None:
    async with FakeBridge() as fake:
        FakeProvider(ast_doc("test_fullapi_cpp"), FullApi()).install(fake)
        FakeProvider(ast_doc("test_fullapi_ext_cpp"), FullApi()).install(fake)
        async with AsyncBridgeClient(fake.url) as client:
            for (table, case_id), message in sorted(PINNED.items()):
                contract, conformance = TABLES[table]
                case = conformance.case(case_id)
                result = await client.call(contract, case.method, *case.wire_args(), decode_bytes=False,
                                           detect_rejection=False)
                assert result == {"code": case.expected_error, "message": message, "origin": contract}, case_id


@async_test
async def test_records_as_dicts_or_generated_classes(tmp_path: Path) -> None:
    ext: ModuleType = fixture_client(tmp_path, "test_fullapi_ext_cpp")
    seen: list[Any] = []

    class Ext:
        def echo_blob(self, v: Any) -> Any:
            seen.append(v)
            return v

        def echo_opt(self, v: Any) -> Any:
            seen.append(v)
            return v

    async with FakeBridge() as fake:
        FakeProvider(ast_doc("test_fullapi_ext_cpp"), Ext()).install(fake)
        FakeProvider(ext.INTERFACE, Ext(), name="typed_ext", records=ext.RECORD_TYPES).install(fake)
        async with AsyncBridgeClient(fake.url) as bridge:
            raw = {"id": "x", "n": 1, "payload": {"_bytes": "aGk"}, "extra": True}
            assert await bridge.call("test_fullapi_ext_cpp", "echoBlob", raw, decode_bytes=False) == {
                "id": "x", "n": 1, "payload": {"_bytes": "aGk"}}
            assert seen[-1] == {"id": "x", "n": 1, "payload": b"hi"}
            client = ext.AsyncTestFullapiExtCppClient(bridge, "typed_ext")
            blob = ext.Blob(id="y", n=2, payload=b"\x00")
            assert await client.echo_blob(blob) == blob and seen[-1] == blob
            assert await client.echo_opt(ext.Opt(required="r", count=3)) == ext.Opt(required="r", count=3)
            nulls = {"required": "r", "maybe": None, "count": None}
            assert await bridge.call("typed_ext", "echoOpt", nulls) == {"required": "r"}
            report = await client.check_compat()
            assert report.level == "shape"  # served from the embedded shape: descriptions are gone


@async_test
async def test_implementations_can_be_async_mappings_and_outcomes() -> None:
    calls: list[str] = []

    async def slow_echo(v: str) -> str:
        await asyncio.sleep(0.01)
        calls.append(provider.context.method)
        return v.upper()

    impl = {
        "echoString": slow_echo,
        "echoBool": lambda v: Reject("dispatch_failed", "custom", "somewhere"),
        "echoUint": lambda v: FakeError(-32002),
        "echoDouble": lambda v: NoResponse,
        "echoInt": lambda v: "not an int",
    }
    provider = FakeProvider(ast_doc("test_fullapi_cpp"), impl)
    async with FakeBridge() as fake:
        provider.install(fake, exposure={"methods": ["echoString", "echoBool", "echoUint", "echoDouble",
                                                     "echoInt", "echoAny"], "events": []})
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.call("test_fullapi_cpp", "echoString", "a") == "A" and calls == ["echoString"]
            with pytest.raises(ProviderRejection, match="custom"):
                await client.call("test_fullapi_cpp", "echoBool", True)
            with pytest.raises(Exception, match="upstream call timed out"):
                await client.call("test_fullapi_cpp", "echoUint", 1)
            with pytest.raises(Exception, match="got no answer"):
                await client.call("test_fullapi_cpp", "echoDouble", 1.0, timeout=0.1)
            with pytest.raises(UpstreamCallFailed):
                await client.call("test_fullapi_cpp", "echoInt", 1)
            assert isinstance(fake.handler_errors[-1], TypeError)
            assert "returned a value its contract refuses" in str(fake.handler_errors[-1])
            with pytest.raises(UpstreamCallFailed):
                await client.call("test_fullapi_cpp", "echoAny", 1)
            assert isinstance(fake.handler_errors[-1], NotImplementedError)
            with pytest.raises(MethodNotFound):
                await client.call("test_fullapi_cpp", "whoAmI")  # not exposed
    with pytest.raises(RuntimeError, match="not installed"):
        FakeProvider(ast_doc("test_fullapi_cpp"), impl).bridge  # noqa: B018
    with pytest.raises(RuntimeError, match="no call is being handled"):
        provider.context  # noqa: B018


def test_unknown_methods_answer_null_and_emits_are_checked() -> None:
    provider = FakeProvider(ast_doc("mini_module"), {})
    from logos_bridge.testing import CallContext

    ctx = CallContext(None, "mini_module", "nope", [], [], 1, "ws", None)  # type: ignore[arg-type]
    assert provider.handle(ctx).value is None
    with ThreadedFakeBridge() as fake:
        provider.install(fake.fake)
        with pytest.raises(Exception, match="expected bytes at arg0.body"):
            provider.emit("added", {"id": "n", "body": "text"})
        with pytest.raises(ValueError, match="declares no event"):
            provider.emit("nope")
        assert provider.emit("added", {"id": "n", "body": b""}) == 0
    assert provider.lidl_text == lidl_text("mini_module")


def test_a_blocking_generated_client_on_a_fake_provider(tmp_path: Path) -> None:
    mini = fixture_client(tmp_path, "mini_module")
    notes: dict[str, Any] = {}

    class Notes:
        def put(self, note: Any) -> Any:
            notes[note.id] = note
            provider.emit("added", note)
            return note

        def find(self, id: str, prefix: str | None) -> LogosResult[Any]:
            if id in notes:
                return LogosResult(True, {"id": id, "prefix": prefix})
            return LogosResult(False, None, f"no note {id}")

        def clear(self) -> None:
            notes.clear()

    provider = FakeProvider(mini.INTERFACE, Notes(), records=mini.RECORD_TYPES)
    with ThreadedFakeBridge() as fake:
        fake.portal.call(_install, provider, fake.fake)
        with BridgeClient(fake.url) as bridge:
            client = mini.MiniModuleClient(bridge)
            with client.on_added() as added:
                note = mini.Note(id="a", body=b"x", tag="t")
                assert client.put(note) == note
                assert added.get(timeout=5).note == note
            assert client.find("a").unwrap() == {"id": "a", "prefix": None}
            assert client.find("b").error == "no note b"
            assert client.clear() is None and notes == {}
            assert client.lidl() == Interface.from_shape(mini.INTERFACE).to_lidl()


async def _install(provider: FakeProvider, fake: FakeBridge) -> None:
    provider.install(fake)


def test_the_interface_is_identity_complete() -> None:
    provider = FakeProvider(interface("mini_module").shape(), {})
    assert provider.interface.has_identity and provider.name == "mini_module"
    assert provider.plans.methods["lidl"].method.is_identity


@async_test
async def test_the_stricter_cases_are_accepted_by_the_fake_provider() -> None:
    async with FakeBridge() as fake:
        FakeProvider(ast_doc("test_fullapi_ext_cpp"), FullApi()).install(fake)
        async with AsyncBridgeClient(fake.url) as client:
            padded = TABLES["ext-cases"][1].case("bstr/padded-base64")
            echoed = await client.call_encoded("test_fullapi_ext_cpp", padded.method, padded.wire_args())
            assert echoed == wire(padded.expectations()[None])
            plain = TABLES["ext-cases"][1].case("[bstr]/lenient-plain-string")
            echoed = await client.call_encoded("test_fullapi_ext_cpp", plain.method, plain.wire_args())
            assert echoed == plain.expectations()["test_fullapi_ext_cpp"]  # the C++ provider's answer
