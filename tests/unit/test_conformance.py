"""Every logos-test-modules conformance case through the typed codecs.

* Value cases: the encoded arguments are the wire the table sends, and the
  expected results decode to the materialized expectation.
* Rejections: refused locally, with the exact wording a provider answers
  (pinned below), and the provider-side decode of the raw wire agrees.
* M3 guard: a one-key ``_bytes`` map at an ``any``/``{tstr: any}`` slot stays a map.
* Strictness registry: the cases where this client is deliberately stricter
  than a provider. An entry that stops being stricter fails, like an xpass.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fixture_util import FIXTURES, interface

from logos_bridge import Event, MethodNotFound, ProviderRejection
from logos_bridge.lidl import Interface, TypeRef
from logos_bridge.testing.conformance import (
    ConformanceCase,
    ConformanceTable,
    ConformanceTableError,
    materialize,
    wire,
)
from logos_bridge.typed import (
    ArgumentError,
    ArityError,
    ClientRejection,
    CodecError,
    DecodedEvent,
    InterfacePlans,
    LogosResult,
    MethodPlan,
    ResultDecodeError,
)

TABLES = {
    "cases": ("test_fullapi_cpp", ConformanceTable.load(FIXTURES / "conformance" / "cases.json")),
    "ext-cases": ("test_fullapi_ext_cpp", ConformanceTable.load(FIXTURES / "conformance" / "ext-cases.json")),
}
PLANS = {name: InterfacePlans(interface(contract)) for name, (contract, _) in TABLES.items()}

# The wording a provider answers for each rejection case (logos-protocol logos_codec.h
# and the cdylib dispatch), pinned: this client must refuse with exactly these words.
PINNED: dict[tuple[str, str], str] = {
    ("cases", "hostile/uint/negative"): "expected unsigned integer at arg0, got number",
    ("cases", "hostile/int/fractional"): "expected integer at arg0, got number",
    ("cases", "hostile/bool/one"): "expected bool at arg0, got number",
    ("cases", "hostile/[tstr]/mixed-elements"): "expected string at arg0[1], got number",
    ("cases", "hostile/[any]/scalar"): "expected array at arg0, got string",
    ("cases", "hostile/{tstr:any}/scalar"): "expected object at arg0, got number",
    ("cases", "hostile/[uint]/negative-element"): "expected unsigned integer at arg0[1], got number",
    ("cases", "hostile/[uint]/fractional-element"): "expected integer at arg0[1], got number",
    ("cases", "hostile/[int]/fractional-element"): "expected integer at arg0[1], got number",
    ("cases", "failure/B/arity/too-few"): "expected 1 arguments, got 0",
    ("cases", "failure/B/arity/too-many"): "expected 1 arguments, got 2",
    ("cases", "failure/B/arity/too-many-untyped-extra"): "expected 1 arguments, got 2",
    ("cases", "failure/B/arity/too-many-zero-parameter"): "expected 0 arguments, got 1",
    ("cases", "failure/B/arity/too-many-lidl-builtin"): "expected 0 arguments, got 1",
    ("cases", "failure/C/identity-arity-is-a-bare-null"): "expected 0 arguments, got 1",
    ("ext-cases", "[[int]]/out-of-signed-range"): "expected signed integer in range at arg0[0][0], got number",
    ("ext-cases", "hostile/Optional/scalar/wrong-type"): "expected string at arg0, got number",
    ("ext-cases", "hostile/Optional/record/wrong-type-in-optional-field"):
        "expected string at arg0.maybe, got number",
    ("ext-cases", "hostile/Optional/record/required-absent"): "expected string at arg0.required, got null",
    ("ext-cases", "hostile/Optional/record/required-null"): "expected string at arg0.required, got null",
    ("ext-cases", "failure/B/arity/too-few"): "expected 1 arguments, got 0",
    ("ext-cases", "failure/B/arity/too-many"): "expected 1 arguments, got 2",
    ("ext-cases", "failure/B/arity/too-many-zero-parameter"): "expected 0 arguments, got 1",
    ("ext-cases", "failure/B/arity/too-many-lidl-builtin"): "expected 0 arguments, got 1",
}

# Where this client refuses what a provider accepts. Each entry: the local refusal,
# and whether a provider's decode accepts the same wire.
STRICTER: dict[tuple[str, str], str] = {
    # A typed bstr takes bytes; a pre-encoded (and padded) tag is a caller's JSON, not bytes.
    ("ext-cases", "bstr/padded-base64"): "expected bytes at arg0.payload, got object",
    # A plain string is not bytes: the C++ provider converts it, the Rust one echoes the string.
    ("ext-cases", "[bstr]/lenient-plain-string"): "expected bytes at arg0[0], got string",
}

# The Rust provider's echo breaks the contract; this client refuses to decode it.
STRICTER_RESULTS: dict[tuple[str, str, str], str] = {
    ("ext-cases", "[bstr]/lenient-plain-string", "test_fullapi_ext_rust"): "expected bytes at result[0], got string",
}

# The canonical encoder spells an empty record field by omission, so the wire differs.
CANONICALIZED: dict[tuple[str, str], list[Any]] = {
    ("ext-cases", "Optional/record/explicit-null"): [{"required": "r"}],
    ("ext-cases", "Optional/[Record]/mixed-presence"): [[
        {"required": "a", "maybe": "m", "count": 1, "blob": {"_bytes": "aGk"}},
        {"required": "b"},
        {"required": "c", "count": 2},
    ]],
}

# Not a codec question: class A names a module that is not loaded.
TRANSPORT = {("cases", "failure/A/module-not-loaded"), ("ext-cases", "failure/A/module-not-loaded")}
# Class C: the name is not in the contract, so the typed layer refuses it before sending.
UNDECLARED = {("cases", "failure/C/unknown-method"), ("ext-cases", "failure/C/unknown-method")}

M3_CASES = {("cases", "adversarial/any/_bytes-key"), ("cases", "adversarial/{tstr:any}/_bytes-key")}


def all_cases() -> list[tuple[str, ConformanceCase]]:
    return [(name, case) for name, (_, table) in TABLES.items() for case in table.cases]


def case_id(item: tuple[str, ConformanceCase]) -> str:
    return f"{item[0]}:{item[1].id}"


def value_cases() -> list[tuple[str, ConformanceCase]]:
    skip = set(PINNED) | set(STRICTER) | TRANSPORT | UNDECLARED
    return [(n, c) for n, c in all_cases() if (n, c.id) not in skip]


def by_type(type_: TypeRef, value: Any, iface: Interface) -> Any:
    """The expectation as a typed caller sees it: bytes only at bstr slots, `any` verbatim."""
    t = type_.value_type
    if value is None:
        return None
    if t.kind == "primitive":
        if t.name == "bstr":
            return materialize(value)
        if t.name == "result":
            return LogosResult(value["success"], value["value"], value["error"])
        return value
    if t.kind == "array":
        return [by_type(t.elements[0], v, iface) for v in value]
    if t.kind == "map":
        return {k: by_type(t.elements[1], v, iface) for k, v in value.items()}
    record = iface.record(t.name)
    assert record is not None
    out = {}
    for f in record.fields:
        item = by_type(f.type, value.get(f.name), iface)
        if item is not None or not f.optional:
            out[f.name] = item
    return out


def typed_expectation(plan: MethodPlan, expected: Any, iface: Interface) -> Any:
    returns = plan.method.returns
    return None if returns is None else by_type(returns.type, wire(expected), iface)


def provider_decode(plan: MethodPlan, args: list[Any]) -> str | None:
    """What a C++ provider answers for these wire arguments, or ``None`` if it accepts them."""
    try:
        plan.check_arity(len(args))
        for index, (param, value) in enumerate(zip(plan.params, args)):
            param.codec.decode(value, f"arg{index}", lenient=True)
    except ClientRejection as exc:
        return exc.message
    except CodecError as exc:
        return str(exc)
    return None


def test_the_tables_are_the_vendored_revision() -> None:
    cpp, ext = TABLES["cases"][1], TABLES["ext-cases"][1]
    assert (cpp.contract, len(cpp.cases), len(cpp.events)) == ("full_api", 77, 20)
    assert (ext.contract, len(ext.cases), len(ext.events)) == ("full_api_ext", 45, 1)
    assert cpp.providers == ("test_fullapi_cpp", "test_fullapi_rust")
    assert "THE FAILURE CLASSES" in cpp.comment


def test_every_case_is_classified_once() -> None:
    keys = {(n, c.id) for n, c in all_cases()}
    for registry in (set(PINNED), set(STRICTER), set(CANONICALIZED), TRANSPORT, UNDECLARED, M3_CASES):
        assert registry <= keys, registry - keys
    rejections = {(n, c.id) for n, c in all_cases() if c.expected_error in ("dispatch_failed", "invalid_args")}
    assert rejections == set(PINNED), "every provider rejection case needs pinned wording"
    assert {(n, c.id) for n, c in all_cases() if c.failure_class == "A"} == TRANSPORT
    assert {(n, c.id) for n, c in all_cases() if c.failure_class == "C"} == UNDECLARED


@pytest.mark.parametrize("item", value_cases(), ids=case_id)
def test_value_cases_round_trip(item: tuple[str, ConformanceCase]) -> None:
    table, case = item
    plan = PLANS[table].method(case.method)
    encoded = plan.encode_args(case.call_args())
    assert encoded == CANONICALIZED.get((table, case.id), case.wire_args())
    assert provider_decode(plan, case.wire_args()) is None
    for provider, expected in case.expectations().items():
        key = (table, case.id, provider or "")
        if key in STRICTER_RESULTS:
            continue
        decoded = plan.decode_result(wire(expected), module="m")
        assert decoded == typed_expectation(plan, expected, PLANS[table].interface), provider


@pytest.mark.parametrize("key", sorted(PINNED), ids=lambda k: f"{k[0]}:{k[1]}")
def test_rejections_are_local_and_worded_like_the_provider(key: tuple[str, str]) -> None:
    table, cid = key
    case = TABLES[table][1].case(cid)
    plan = PLANS[table].method(case.method)
    expected_class = ArityError if case.expected_error == "invalid_args" else ArgumentError
    with pytest.raises(expected_class) as excinfo:
        plan.encode_args(case.call_args(), module="m")
    assert excinfo.value.message == PINNED[key]
    assert excinfo.value.code == case.expected_error
    assert str(excinfo.value) == f"{case.expected_error}: {PINNED[key]} (refused by this client) calling m.{plan.name}"
    # Parity: a provider decoding the raw wire refuses with the same words.
    assert provider_decode(plan, case.wire_args()) == PINNED[key]


@pytest.mark.parametrize("key", sorted(STRICTER), ids=lambda k: f"{k[0]}:{k[1]}")
def test_the_strictness_registry(key: tuple[str, str]) -> None:
    table, cid = key
    case = TABLES[table][1].case(cid)
    plan = PLANS[table].method(case.method)
    with pytest.raises(ArgumentError) as excinfo:
        plan.encode_args(case.call_args())
    assert excinfo.value.message == STRICTER[key]
    assert provider_decode(plan, case.wire_args()) is None, "a provider accepts this wire"


def test_stricter_results() -> None:
    for (table, cid, provider), message in STRICTER_RESULTS.items():
        case = TABLES[table][1].case(cid)
        plan = PLANS[table].method(case.method)
        with pytest.raises(ResultDecodeError) as excinfo:
            plan.decode_result(wire(case.expectations()[provider]), module="m")
        assert excinfo.value.reason == message
        assert f"m.{plan.name} returned a value that does not match its contract" in str(excinfo.value)


def test_undeclared_methods_are_refused_before_sending() -> None:
    for table, cid in UNDECLARED:
        case = TABLES[table][1].case(cid)
        with pytest.raises(MethodNotFound, match=f"declares no method '{case.method}'"):
            PLANS[table].method(case.method, module="m")


def test_the_m3_guard() -> None:
    # A caller's one-key `_bytes` map is sent as written, and comes back as a map.
    for table, cid in sorted(M3_CASES):
        case = TABLES[table][1].case(cid)
        plan = PLANS[table].method(case.method)
        assert case.raw
        assert plan.encode_args(case.call_args()) == [{"_bytes": "aGk"}]
        decoded = plan.decode_result({"_bytes": "aGk"}, module="m")
        assert decoded == {"_bytes": "aGk"} and not isinstance(decoded, bytes)
    # Only a bstr slot reads the same object as bytes.
    echo_bytes = PLANS["cases"].method("echoBytes")
    assert echo_bytes.decode_result({"_bytes": "aGk"}, module="m") == b"hi"
    # Nested inside a typed map, the tag is still data for `any` values.
    echo_map = PLANS["cases"].method("echoMap")
    assert echo_map.decode_result({"k": {"_bytes": "aGk"}}, module="m") == {"k": {"_bytes": "aGk"}}


def test_rejections_fold_whatever_the_return_type() -> None:
    refusal = {"code": "dispatch_failed", "message": "expected integer at arg0, got string", "origin": "p"}
    for table, plans in PLANS.items():
        for plan in plans.methods.values():
            with pytest.raises(ProviderRejection) as excinfo:
                plan.decode_result(refusal, module="p")
            assert excinfo.value.message == refusal["message"], (table, plan.name)
    ambiguous = {n for n, p in PLANS["cases"].methods.items() if p.rejection_ambiguous}
    assert ambiguous == {"echoAny", "echoMap"}
    assert {n for n, p in PLANS["ext-cases"].methods.items() if p.rejection_ambiguous} == {"echoStringMap"}


def all_events() -> list[tuple[str, Any]]:
    return [(name, event) for name, (_, table) in TABLES.items() for event in table.events]


@pytest.mark.parametrize("item", all_events(), ids=lambda i: f"{i[0]}:{i[1].id}")
def test_event_cases_decode(item: tuple[str, Any]) -> None:
    table, case = item
    plans = PLANS[table]
    raw = Event("s1", "m", case.event, wire(case.values), 1, 0)
    decoded = plans.decode_event(raw)
    assert isinstance(decoded, DecodedEvent) and decoded.meta is raw
    assert list(decoded.args) == materialize(case.values)
    fire = plans.method(case.fire)
    assert fire.encode_args(materialize(case.values)) == wire(case.values)
    assert fire.decode_result(True, module="m") is True


def test_the_tables_cover_the_contracts() -> None:
    for table, (contract, conformance) in TABLES.items():
        declared = {m.name for m in interface(contract).methods if not m.derived}
        exercised = {c.method for c in conformance.cases} | {e.fire for e in conformance.events}
        assert declared <= exercised, (table, declared - exercised)
        events = {e.name for e in interface(contract).events}
        assert events == {e.event for e in conformance.events}, table


# ----------------------------------------------------------------- the loader


def minimal(**case: Any) -> dict[str, Any]:
    base = {"id": "x", "type": "tstr", "position": "method_arg", "method": "m", "args": [], "expect": 1}
    return {"schema": 1, "contract": "c", "providers": ["p"], "cases": [{**base, **case}], "events": []}


def test_the_loader_refuses_what_no_driver_reads() -> None:
    table = ConformanceTable.from_json(minimal())
    assert table.case("x").expectations() == {None: 1} and table.path is None
    with pytest.raises(KeyError):
        table.case("y")
    with pytest.raises(ConformanceTableError, match="keys no driver reads"):
        ConformanceTable.from_json(minimal(expect_error="dispatch_failed"))
    doc = minimal()
    del doc["cases"][0]["expect"]
    with pytest.raises(ConformanceTableError, match="has no expectation"):
        ConformanceTable.from_json(doc)
    with pytest.raises(ConformanceTableError, match="unsupported table schema"):
        ConformanceTable.from_json({**minimal(), "schema": 2})
    with pytest.raises(ConformanceTableError, match="unknown table keys"):
        ConformanceTable.from_json({**minimal(), "extra": 1})
    with pytest.raises(ConformanceTableError, match="a JSON object"):
        ConformanceTable.from_json([])
    doc = minimal()
    doc["cases"].append(dict(doc["cases"][0]))
    with pytest.raises(ConformanceTableError, match="duplicate case ids"):
        ConformanceTable.from_json(doc)
    doc = minimal()
    doc["events"] = [{"id": "e", "type": "tstr", "position": "event_param", "event": "e", "fire": "f",
                      "value": 1, "bogus": 2}]
    with pytest.raises(ConformanceTableError, match="event e: keys no driver reads"):
        ConformanceTable.from_json(doc)


def test_materialize_and_wire() -> None:
    all_bytes: Mapping[str, Any] = {"_bytes": "__ALL_BYTES__"}
    assert materialize([all_bytes]) == [bytes(range(256))]
    assert wire({"k": all_bytes})["k"]["_bytes"].startswith("AAECAwQF")
    assert materialize({"_bytes": "aGk"}, raw=True) == {"_bytes": "aGk"}
    assert materialize({"k": [{"_bytes": "aGk"}]}) == {"k": [b"hi"]}
    case = TABLES["cases"][1].case("result/ok")
    assert set(case.expectations()) == {"test_fullapi_cpp", "test_fullapi_rust"}
    assert case.expected_error is None and case.failure_class is None
    assert TABLES["cases"][1].case("hostile/uint/negative").failure_class == "E"
    no_expectation = ConformanceCase("i", "t", "p", "m", [])
    with pytest.raises(ConformanceTableError):
        no_expectation.expectations()
