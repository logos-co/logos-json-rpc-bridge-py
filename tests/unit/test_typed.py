"""The typed layer: codecs, plans, rejection folding, typed subscriptions."""

from __future__ import annotations

import dataclasses
import enum
import math
from collections.abc import Callable
from typing import Any

import pytest
from fixture_util import edge_doc, interface

from logos_bridge import (
    AsyncBridgeClient,
    BridgeClient,
    Event,
    ModuleResultError,
    ProviderRejection,
    SubscriptionTerminated,
)
from logos_bridge.lidl import Interface, TypeRef
from logos_bridge.testing import FakeBridge, ThreadedFakeBridge, async_test
from logos_bridge.typed import (
    CODEGEN_FORMAT,
    INT64_MAX,
    INT64_MIN,
    UINT64_MAX,
    ArgumentError,
    ArityError,
    BlockingTypedSubscription,
    ClientRejection,
    CodecBuilder,
    CodecError,
    CodegenFormatError,
    DecodedEvent,
    EventDecodeError,
    InterfacePlans,
    LogosResult,
    ResultDecodeError,
    arity_message,
    encode_json_value,
    python_type_name,
    rejection_ambiguous,
    require_codegen_format,
    wire_names,
)

MINI = interface("mini_module")
EXT = interface("test_fullapi_ext_cpp")


def codec(spelled: str, iface: Interface = EXT) -> Any:
    from logos_bridge.lidl import parse_shape_type

    return CodecBuilder(iface).build(parse_shape_type(spelled))


def rejects(fn: Callable[[], Any], message: str) -> None:
    with pytest.raises(CodecError) as excinfo:
        fn()
    assert str(excinfo.value) == message


# ------------------------------------------------------------------ scalars


def test_tstr() -> None:
    class Name(str, enum.Enum):
        A = "alpha"

    c = codec("tstr")
    assert c.encode("é", "arg0") == "é" and c.decode("x", "arg0") == "x"
    assert type(c.encode(Name.A, "arg0")) is str and c.encode(Name.A, "arg0") == "alpha"
    rejects(lambda: c.encode(b"x", "arg0"), "expected string at arg0, got bytes")
    rejects(lambda: c.decode({"_bytes": "aGk"}, "result"), "expected string at result, got object")
    rejects(lambda: c.encode(None, "arg1"), "expected string at arg1, got null")


def test_bstr_is_tagged_only_at_bstr_slots() -> None:
    c = codec("bstr")
    for value in (b"\xfb\xff", bytearray(b"\xfb\xff"), memoryview(b"\xfb\xff")):
        assert c.encode(value, "arg0") == {"_bytes": "-_8"}
    rejects(lambda: c.encode("hi", "arg0"), "expected bytes at arg0, got string")
    rejects(lambda: c.encode({"_bytes": "aGk"}, "arg0"), "expected bytes at arg0, got object")
    assert c.decode({"_bytes": "-_8"}, "result") == b"\xfb\xff"
    assert c.decode({"_bytes": "Zm9vYg=="}, "result") == b"foob"  # padding tolerated, as logos-protocol
    rejects(lambda: c.decode({"_bytes": "a b"}, "result"),
            "expected bytes at result, got object "
            "(characters outside the base64url alphabet (A-Z a-z 0-9 - _, no padding))")
    rejects(lambda: c.decode("aGk", "result"), "expected bytes at result, got string")
    rejects(lambda: c.decode({"_bytes": "aGk", "x": 1}, "result"), "expected bytes at result, got object")
    # A provider decodes leniently (logos_codec.h bytesFromJsonLenient).
    assert c.decode("hi", "arg0", lenient=True) == b"hi"
    assert c.decode(12, "arg0", lenient=True) == b"12"
    assert c.decode(1.5, "arg0", lenient=True) == b"1.5"
    assert c.decode([104, 105, "x", True, 256 + 33], "arg0", lenient=True) == b"hi!"
    assert c.decode({"_bytes": "a G k"}, "arg0", lenient=True) == b"hi"
    rejects(lambda: c.decode(True, "arg0", lenient=True), "expected bytes at arg0, got boolean")
    # Bytes elsewhere: tagged inside `any`, refused by `tstr`, left as data by a typed map.
    assert codec("any").encode({"k": [b"\x00"]}, "arg0") == {"k": [{"_bytes": "AA"}]}
    assert codec("any").decode({"k": {"_bytes": "AA"}}, "result") == {"k": {"_bytes": "AA"}}
    assert codec("{tstr:bstr}").decode({"k": {"_bytes": "AA"}}, "r") == {"k": b"\x00"}


@pytest.mark.parametrize(("value", "message"), [
    (True, "expected integer at arg0, got boolean"),
    (3.0, "expected integer at arg0, got number"),
    ("3", "expected integer at arg0, got string"),
    (INT64_MAX + 1, "expected signed integer in range at arg0, got number"),
    (INT64_MIN - 1, "expected signed integer in range at arg0, got number"),
])
def test_int_encoding_is_strict(value: Any, message: str) -> None:
    rejects(lambda: codec("int").encode(value, "arg0"), message)


def test_int_and_uint() -> None:
    i, u = codec("int"), codec("uint")
    assert i.encode(INT64_MIN, "a") == INT64_MIN and i.encode(INT64_MAX, "a") == INT64_MAX
    assert u.encode(UINT64_MAX, "a") == UINT64_MAX and u.encode(0, "a") == 0

    class Level(enum.IntEnum):
        HIGH = 3

    assert type(i.encode(Level.HIGH, "a")) is int
    rejects(lambda: u.encode(-1, "arg2"), "expected unsigned integer at arg2, got number")
    rejects(lambda: u.encode(UINT64_MAX + 1, "arg2"), "expected unsigned integer in range at arg2, got number")
    rejects(lambda: i.decode(1.0, "result"), "expected integer at result, got number")
    rejects(lambda: i.decode(False, "result"), "expected integer at result, got boolean")
    rejects(lambda: u.decode(-5, "result[0]"), "expected unsigned integer at result[0], got number")
    # The provider side accepts a whole-valued float (logos_codec.h), within range.
    assert i.decode(3.0, "arg0", lenient=True) == 3
    rejects(lambda: i.decode(3.5, "arg0", lenient=True), "expected integer at arg0, got number")
    rejects(lambda: u.decode(-1.0, "arg0", lenient=True), "expected unsigned integer in range at arg0, got number")
    rejects(lambda: i.decode(2.0**63, "arg0", lenient=True), "expected signed integer in range at arg0, got number")
    assert u.decode(2.0**63, "arg0", lenient=True) == 2**63
    rejects(lambda: u.decode(2.0**64, "arg0", lenient=True), "expected unsigned integer in range at arg0, got number")


def test_float64_and_bool() -> None:
    f, b = codec("float64"), codec("bool")
    assert f.encode(2, "a") == 2.0 and type(f.encode(2, "a")) is float
    assert f.decode(7, "r") == 7.0 and f.decode(-0.5, "r") == -0.5
    rejects(lambda: f.encode(True, "a"), "expected number at a, got boolean")
    rejects(lambda: f.encode(math.nan, "a"), "expected finite number at a, got number")
    rejects(lambda: f.encode(10**400, "a"), "expected number in range at a, got number")
    rejects(lambda: f.decode("1", "r"), "expected number at r, got string")
    assert b.encode(False, "a") is False and b.decode(True, "r") is True
    rejects(lambda: b.encode(1, "a"), "expected bool at a, got number")
    rejects(lambda: b.decode(0, "r"), "expected bool at r, got number")


def test_any_is_a_json_tree() -> None:
    c = codec("any")
    assert c.encode((1, "a", None, [2.5]), "arg0") == [1, "a", None, [2.5]]
    assert c.encode({"_bytes": "aGk"}, "arg0") == {"_bytes": "aGk"}  # a pre-encoded tag passes
    assert c.encode(UINT64_MAX, "arg0") == UINT64_MAX
    rejects(lambda: c.encode(UINT64_MAX + 1, "arg0"), "expected integer in range at arg0, got number")
    rejects(lambda: c.encode(math.inf, "arg0"), "expected finite number at arg0, got number")
    rejects(lambda: c.encode({"_bytes": "a=b"}, "arg0"), "expected unpadded base64url at arg0._bytes, got string")
    rejects(lambda: c.encode({"_bytes": "x", "y": 1}, "arg0"),
            'expected an object without the reserved "_bytes" key at arg0, got object')
    rejects(lambda: c.encode({1: 2}, "arg0"), "expected string keys at arg0, got a number key")
    rejects(lambda: c.encode({"k": {1, 2}}, "arg0"), "expected a JSON value at arg0.k, got set")
    rejects(lambda: c.encode(LogosResult(True, 1), "arg0"), "expected a JSON value at arg0, got LogosResult")
    assert c.decode({"_bytes": "aGk"}, "r") == {"_bytes": "aGk"} and c.accepts_null
    assert encode_json_value(None, "x") is None


def test_containers() -> None:
    lst, mp = codec("[int]"), codec("{tstr:[bstr]}")
    assert lst.encode((1, 2), "arg0") == [1, 2] and lst.encode(range(2), "arg0") == [0, 1]
    rejects(lambda: lst.encode("12", "arg0"), "expected array at arg0, got string")
    rejects(lambda: lst.encode(b"12", "arg0"), "expected array at arg0, got bytes")
    rejects(lambda: lst.encode({"a": 1}, "arg0"), "expected array at arg0, got object")
    rejects(lambda: lst.encode([1, "x"], "arg1"), "expected integer at arg1[1], got string")
    rejects(lambda: lst.decode((1,), "result"), "expected array at result, got tuple")
    assert mp.encode({"_bytes": [b"\x01"]}, "arg0") == {"_bytes": [{"_bytes": "AQ"}]}  # keys are data
    assert codec("{tstr:tstr}").encode({"_bytes": "not base64!"}, "a") == {"_bytes": "not base64!"}
    rejects(lambda: mp.encode({1: []}, "arg0"), "expected string keys at arg0, got a number key")
    rejects(lambda: mp.encode([], "arg0"), "expected object at arg0, got array")
    rejects(lambda: mp.decode({"k": ["x"]}, "result"), "expected bytes at result.k[0], got string")
    nested = codec("[[?int]]")
    assert nested.encode([[1, None]], "a") == [[1, None]] and nested.decode([[None, 2]], "r") == [[None, 2]]
    rejects(lambda: nested.decode([[1], ["x"]], "r"), "expected integer at r[1][0], got string")


def test_optionals() -> None:
    c = codec("?tstr")
    assert c.encode(None, "a") is None and c.decode(None, "r") is None and c.accepts_null
    rejects(lambda: c.encode(42, "arg0"), "expected string at arg0, got number")
    assert codec("?any").lidl == "any"  # ?any is any
    assert codec("??tstr").lidl == "? tstr"
    assert codec("?[?int]").lidl == "? [? int]"


# ------------------------------------------------------------------ records


def test_records_in_dynamic_mode_are_dicts() -> None:
    opt = codec("Opt")
    assert opt.encode({"required": "r", "maybe": None, "count": 0}, "arg0") == {"required": "r", "count": 0}
    rejects(lambda: opt.encode({"required": "r", "bogus": 1}, "arg0"),
            "expected Opt at arg0, got object (no field 'bogus')")
    rejects(lambda: opt.encode({"maybe": "m"}, "arg0"), "expected string at arg0.required, got null")
    rejects(lambda: opt.encode(["r"], "arg0"), "expected Opt at arg0, got array")
    assert opt.decode({"required": "r", "maybe": None, "extra": [1]}, "result") == {"required": "r"}
    assert opt.decode({"required": "r", "blob": {"_bytes": "AA"}}, "result") == {"required": "r", "blob": b"\x00"}
    rejects(lambda: opt.decode({"required": 1}, "result"), "expected string at result.required, got number")
    rejects(lambda: opt.decode("x", "result"), "expected object at result, got string")
    wrapper = codec("Wrapper")
    rejects(lambda: wrapper.decode({"inner": {"id": "i", "n": -1, "payload": {"_bytes": ""}}, "tags": [],
                                    "blobs": []}, "result"),
            "expected unsigned integer at result.inner.n, got number")
    rejects(lambda: wrapper.encode({"inner": None, "tags": [], "blobs": []}, "arg0"),
            "expected object at arg0.inner, got null")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Blob:
    id_: str = dataclasses.field(metadata={"lidl": "id"})
    n: int
    payload: bytes


def test_records_in_class_mode() -> None:
    builder = CodecBuilder(EXT, records={"Blob": Blob})
    blob = builder.build(TypeRef.named("Blob"))
    wire = {"id": "x", "n": 1, "payload": {"_bytes": "aGk"}}
    assert blob.encode(Blob(id_="x", n=1, payload=b"hi"), "arg0") == wire
    assert blob.decode(wire, "result") == Blob(id_="x", n=1, payload=b"hi")
    rejects(lambda: blob.encode(wire, "arg0"), "expected Blob at arg0, got object")
    assert blob.annotation() is Blob
    wrapper = builder.build(TypeRef.named("Wrapper"))  # no class: a dict holding Blob instances
    assert wrapper.decode({"inner": wire, "tags": [], "blobs": [wire]}, "r")["blobs"] == [
        Blob(id_="x", n=1, payload=b"hi")]
    with pytest.raises(ValueError, match="undeclared records"):
        CodecBuilder(EXT, records={"Nope": Blob})
    with pytest.raises(ValueError, match="no record named 'Nope'"):
        builder.build(TypeRef.named("Nope"))
    with pytest.raises(TypeError, match="is not a dataclass"):
        CodecBuilder(EXT, records={"Blob": dict}).build(TypeRef.named("Blob"))
    assert wire_names(Blob) == {"id": "id_"}


def test_recursive_records() -> None:
    recursive = Interface.from_json(edge_doc("recursive"))
    node = CodecBuilder(recursive).build(TypeRef.named("Node"))
    tree = {"value": 1, "children": [{"value": 2, "children": []}]}
    assert node.encode(tree, "arg0") == tree and node.decode(tree, "r") == tree
    rejects(lambda: node.decode({"value": 1, "children": [{"value": "x", "children": []}]}, "r"),
            "expected integer at r.children[0].value, got string")


def test_unmappable_types_degrade_per_node() -> None:
    forward = Interface.from_json(edge_doc("forward"))
    plans = InterfacePlans(forward)
    put, scores, keep = plans.method("put"), plans.method("scores"), plans.method("keep")
    assert [p.codec.lidl for p in put.params] == ["Item", "float32"]
    assert put.encode_args([{"id": "i", "tags": []}, {"anything": [1]}]) == [
        {"id": "i", "tags": []}, {"anything": [1]}]
    assert scores.params[0].codec.lidl == "<set(tstr)>"
    assert scores.returns is not None and scores.returns.lidl == "{tstr: float64}"
    assert keep.returns is not None and keep.returns.lidl == "{int: tstr}"
    assert keep.decode_result({"1": "x"}, module="m") == {"1": "x"}
    changed = plans.event("changed")
    assert [p.codec.lidl for p in changed.params] == ["Item", "<array>"]


def test_results() -> None:
    c = codec("result")
    ok = c.decode({"success": True, "value": {"_bytes": "AA"}, "error": None}, "result")
    assert ok == LogosResult(True, {"_bytes": "AA"}, None) and ok.ok and ok.unwrap() == {"_bytes": "AA"}
    failed = c.decode({"success": False, "value": None, "error": "nope"}, "result")
    with pytest.raises(ModuleResultError, match="nope"):
        failed.unwrap()
    rejects(lambda: c.decode({"success": True, "value": 1}, "result"),
            'expected result {"success": bool, "value": any, "error": string|null} at result, got object '
            "(keys ['success', 'value'])")
    rejects(lambda: c.decode({"success": 1, "value": 1, "error": None}, "result"),
            "expected bool at result.success, got number")
    rejects(lambda: c.decode({"success": True, "value": 1, "error": 5}, "result"),
            "expected string or null at result.error, got number")
    rejects(lambda: c.decode([], "result"),
            'expected result {"success": bool, "value": any, "error": string|null} at result, got array')
    assert c.encode(LogosResult(False, b"\x00", "x"), "arg0") == {"success": False, "value": {"_bytes": "AA"},
                                                                  "error": "x"}
    rejects(lambda: c.encode({"success": True, "value": 1, "error": None}, "arg0"),
            "expected LogosResult at arg0, got object")
    dynamic = CodecBuilder(EXT, dynamic=True).build(TypeRef.primitive("result"))
    assert dynamic.encode({"success": True, "value": 1, "error": None}, "a")["value"] == 1
    assert LogosResult(True, 1).to_json() == {"success": True, "value": 1, "error": None}
    with pytest.raises(dataclasses.FrozenInstanceError):
        ok.success = False  # type: ignore[misc]


def test_python_type_names() -> None:
    assert [python_type_name(v) for v in (None, True, 1, 1.5, "s", b"", {}, [], (), Blob(id_="", n=0, payload=b""))] == [
        "null", "boolean", "number", "number", "string", "bytes", "object", "array", "array", "Blob"]


def test_rejection_ambiguity() -> None:
    doc = {"name": "m", "types": [
        {"name": "Refusal", "fields": [
            {"name": n, "type": {"kind": "primitive", "name": "tstr", "elements": []}} for n in
            ("code", "message", "origin")]},
        {"name": "Other", "fields": [
            {"name": n, "type": {"kind": "primitive", "name": "tstr", "elements": []}} for n in ("code", "message")]},
    ]}
    iface = Interface.from_json(doc)
    from logos_bridge.lidl import parse_shape_type

    for spelled, expected in (("any", True), ("?any", True), ("{tstr:tstr}", True), ("{tstr:?tstr}", True),
                              ("{tstr:any}", True), ("{tstr:int}", False), ("[any]", False), ("tstr", False),
                              ("result", False), ("Refusal", True), ("?Refusal", True), ("Other", False),
                              ("Missing", False)):
        assert rejection_ambiguous(parse_shape_type(spelled), iface) is expected, spelled


# -------------------------------------------------------------------- plans


def test_arity_messages_match_the_cdylib_dispatch() -> None:
    assert arity_message(1, 1, 0) == "expected 1 arguments, got 0"
    assert arity_message(1, 1, 2) == "expected 1 arguments, got 2"
    assert arity_message(1, 2, 3) == "expected at most 2 arguments, got 3"
    assert arity_message(0, 0, 1) == "expected 0 arguments, got 1"
    fold = InterfacePlans(Interface.from_json(edge_doc("noreturn"))).method("fold")
    assert fold.encode_args([]) == [None, None]  # an empty positional slot is null; arity never changes
    assert fold.encode_args([1]) == [1, None]
    with pytest.raises(ArityError) as excinfo:
        fold.encode_args([1, b"", 3], module="m")
    assert (excinfo.value.message, excinfo.value.expected, excinfo.value.got) == (
        "expected at most 2 arguments, got 3", 2, 3)
    assert isinstance(excinfo.value, TypeError) and isinstance(excinfo.value, ClientRejection)


def test_bind_maps_keywords_to_positions() -> None:
    find = InterfacePlans(MINI).method("find")
    assert find.param_names == ("id", "prefix")
    assert find.bind(["x"], {}) == ["x"]
    assert find.bind([], {"id": "x"}) == ["x"]
    assert find.bind([], {"prefix": "p", "id": "x"}) == ["x", "p"]
    with pytest.raises(ArityError, match="expected 1 arguments, got 0"):
        find.bind([], {"prefix": "p"})
    with pytest.raises(TypeError, match="unexpected keyword argument 'nope'"):
        find.bind([], {"nope": 1})
    with pytest.raises(TypeError, match="multiple values for argument 'id'"):
        find.bind(["x"], {"id": "y"})
    with pytest.raises(ArityError, match="expected at most 2 arguments, got 3"):
        find.bind(["x", "p", "q"], {})
    assert repr(find).startswith("<MethodPlan find(id: tstr")


def test_argument_errors_carry_context() -> None:
    put = InterfacePlans(MINI).method("put")
    with pytest.raises(ArgumentError) as excinfo:
        put.encode_args([{"id": "n", "body": "text"}], module="notes")
    error = excinfo.value
    assert (error.code, error.path, error.module, error.method) == ("dispatch_failed", "arg0.body", "notes", "put")
    assert str(error) == ("dispatch_failed: expected bytes at arg0.body, got string (refused by this client) "
                          "calling notes.put")


def test_no_return_results() -> None:
    clear = InterfacePlans(MINI).method("clear")
    assert clear.returns is None and not clear.rejection_ambiguous
    for wire in (True, None, "anything"):
        assert clear.decode_result(wire, module="m") is None
    with pytest.raises(ProviderRejection):
        clear.decode_result({"code": "invalid_args", "message": "m", "origin": "o"}, module="m")


def test_event_plans() -> None:
    plans = InterfacePlans(MINI)
    added = plans.event("added")
    note = {"id": "n", "body": {"_bytes": "aGk"}}
    event = Event("s", "mini_module", "added", [note], 3, 1)
    decoded = plans.decode_event(event)
    assert isinstance(decoded, DecodedEvent)
    assert decoded.args == ({"id": "n", "body": b"hi"},) and decoded["note"] == decoded[0] and decoded.meta is event
    assert added.encode_values([{"id": "n", "body": b"hi"}]) == [note]
    for data, message in (
        ([note, 1], "expected 1 event arguments at data, got 2"),
        ([], "expected 1 event arguments at data, got 0"),
        ({"note": note}, "expected an array of event arguments at data, got object"),
        ([{"id": "n"}], "expected bytes at arg0.body, got null"),
    ):
        with pytest.raises(EventDecodeError) as excinfo:
            plans.decode_event(Event("s", "mini_module", "added", data, 1, 0))
        assert excinfo.value.reason == message
        assert excinfo.value.name == "added" and excinfo.value.event.data == data
        assert str(excinfo.value).startswith("event mini_module.added does not match its contract: ")
    with pytest.raises(EventDecodeError, match="undeclared event 'nope'"):
        plans.decode_event(Event("s", "m", "nope", [], 1, 0))
    with pytest.raises(ValueError, match="declares no event"):
        plans.event("nope")
    with pytest.raises(ArityError):
        added.encode_values([])
    with pytest.raises(ArgumentError, match="arg0.body"):
        added.encode_values([{"id": "n", "body": "x"}])
    optional_tail = InterfacePlans(Interface.from_json(edge_doc("recursive")))
    assert optional_tail.event("grown").min_args == 2 and repr(added).startswith("<EventPlan added(")
    with pytest.raises(ValueError, match="undeclared events"):
        InterfacePlans(MINI, events={"nope": DecodedEvent})


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class AddedEvent:
    note: dict[str, Any] = dataclasses.field(metadata={"lidl": "note"})
    meta: Event


def test_event_classes() -> None:
    plans = InterfacePlans(MINI, events={"added": AddedEvent})
    event = Event("s", "mini_module", "added", [{"id": "n", "body": {"_bytes": ""}}], 1, 0)
    decoded = plans.decode_event(event)
    assert decoded == AddedEvent(note={"id": "n", "body": b""}, meta=event)


def test_the_codegen_format_guard() -> None:
    require_codegen_format(CODEGEN_FORMAT)
    with pytest.raises(CodegenFormatError, match="generated for codegen format 2") as excinfo:
        require_codegen_format(2)
    assert isinstance(excinfo.value, ImportError)


# ------------------------------------------------------------ over the wire


def mini_fake(fake: FakeBridge) -> None:
    fake.module("mini_module", ["put", ("find", ["id", "prefix"]), "clear"], ["added"])


@async_test
async def test_typed_calls_use_rpc_call_and_fold(tmp_path: Any) -> None:
    plans = InterfacePlans(MINI)
    async with FakeBridge() as fake:
        mini_fake(fake)
        fake.on_call("mini_module", "put", lambda ctx: ctx.params[0])
        fake.on_call("mini_module", "find", lambda ctx: {"success": False, "value": None, "error": "no " + ctx.params[0]})
        fake.on_call("mini_module", "clear", True)
        async with AsyncBridgeClient(fake.url) as client:
            note = {"id": "n", "body": b"\x00\xff", "tag": None}
            assert await plans.call(client, "mini_module", "put", [note]) == {"id": "n", "body": b"\x00\xff"}
            record = await fake.wait_for_request("rpc.call")
            assert record.text == ('{"jsonrpc":"2.0","id":1,"method":"rpc.call","params":{"module":"mini_module",'
                                   '"method":"put","params":[{"id":"n","body":{"_bytes":"AP8"}}]}}')
            found = await plans.call(client, "mini_module", "find", ["x"])
            assert found == LogosResult(False, None, "no x")
            assert (await fake.wait_for_request("rpc.call", count=2)).params["params"] == ["x", None]
            assert await plans.call(client, "mini_module", "clear", []) is None
            with pytest.raises(ArgumentError):
                await plans.call(client, "mini_module", "put", [{"id": 1}])
            assert len([r for r in fake.requests if r.method == "rpc.call"]) == 3  # refused locally
            fake.on_call("mini_module", "put", {"id": "n"})
            with pytest.raises(ResultDecodeError, match="expected bytes at result.body, got null"):
                await plans.call(client, "mini_module", "put", [note])
            fake.on_call("mini_module", "clear",
                         {"code": "dispatch_failed", "message": "provider says no", "origin": "mini_module"})
            with pytest.raises(ProviderRejection, match="provider says no"):
                await plans.call(client, "mini_module", "clear", [])
            assert all(r.method == "rpc.call" for r in fake.requests)
            with pytest.raises(TypeError):
                await client.call_encoded("mini_module", "clear", ("not", "a list"))  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                await client.call_encoded("", "clear", [])


@async_test
async def test_typed_subscriptions_fail_one_item_at_a_time() -> None:
    plans = InterfacePlans(MINI)
    async with FakeBridge() as fake:
        mini_fake(fake)
        async with AsyncBridgeClient(fake.url) as client:
            async with plans.subscribe(client, "mini_module", ["added"]) as sub:
                good = {"id": "a", "body": {"_bytes": "AA"}}
                fake.emit("mini_module", "added", good)
                fake.emit("mini_module", "added", {"id": 5})
                fake.emit("mini_module", "added", good)
                first = await sub.get(timeout=5)
                assert isinstance(first, DecodedEvent) and first["note"]["body"] == b"\x00"
                with pytest.raises(EventDecodeError, match="expected string at arg0.id, got number"):
                    await sub.get(timeout=5)
                assert (await sub.__anext__())["note"]["id"] == "a"
                fake.emit("mini_module", "added", {"id": 6})
                fake.emit("mini_module", "added", good)
                items = []
                async for item in sub.results():
                    items.append(item)
                    if len(items) == 2:
                        break
                assert isinstance(items[0], EventDecodeError) and isinstance(items[1], DecodedEvent)
                assert sub.ids and sub.module == "mini_module" and sub.events == ("added",)
                assert sub.pending == 0 and sub.received == 5 and sub.high_water >= 1 and not sub.ended
                assert sub.acks[0]["state"] == "registered" and repr(sub).startswith("<TypedSubscription")
                assert sub.raw.ids == sub.ids and sub.decode(first.meta) == first
            assert sub.ended
            async with plans.subscribe(client, "mini_module", []) as every:
                fake.terminate("mini_module", reason="provider_changed")
                with pytest.raises(SubscriptionTerminated) as excinfo:
                    async for _ in every.results():
                        pass
                assert excinfo.value.reason == "provider_changed"
            sub2 = await plans.subscribe(client, "mini_module", ["added"])
            await sub2.unsubscribe()
            assert [item async for item in sub2.results()] == []
            sub3 = await plans.subscribe(client, "mini_module", ["added"])
            sub3.cancel()
            await sub3.aclose()
            with pytest.raises(ValueError, match="declares no event"):
                plans.subscribe(client, "mini_module", ["nope"])
            empty = InterfacePlans(Interface.from_json(edge_doc("empty")))
            with pytest.raises(ValueError, match="declares no events"):
                empty.subscribe(client, "empty_module", [])


def test_blocking_typed_subscriptions() -> None:
    plans = InterfacePlans(MINI, events={"added": AddedEvent})
    with ThreadedFakeBridge() as fake:
        fake.module("mini_module", ["put"], ["added"])
        with BridgeClient(fake.url) as bridge:
            request = plans.subscribe(bridge.aio, "mini_module", ["added"])
            with BlockingTypedSubscription.open(request, bridge.portal) as sub:
                assert sub.ids and sub.events == ("added",) and not sub.ended and sub.aio.module == "mini_module"
                fake.emit("mini_module", "added", {"id": "x", "body": {"_bytes": ""}})
                fake.emit("mini_module", "added", "garbage")
                fake.emit("mini_module", "added", {"id": "y", "body": {"_bytes": ""}})
                assert sub.get(timeout=5).note["id"] == "x"
                results = sub.results()
                assert isinstance(next(results), EventDecodeError)
                assert next(iter(sub)).note["id"] == "y"
                assert repr(sub).startswith("<BlockingTypedSubscription")
                assert sub.pending == 0
                sub.unsubscribe()
                assert list(sub) == [] and list(sub.results()) == []
            cancelled = BlockingTypedSubscription.open(plans.subscribe(bridge.aio, "mini_module", ["added"]),
                                                       bridge.portal)
            cancelled.cancel()
            cancelled.close()
