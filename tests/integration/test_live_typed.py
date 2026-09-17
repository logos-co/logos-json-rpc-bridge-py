"""The generated clients (the committed goldens) and the dynamic proxy against typed providers."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from live_util import FIXTURES, REJECTIONS, TABLES

from logos_bridge import (
    ArgumentError,
    AsyncBridgeClient,
    ClientRejection,
    LogosResult,
    MethodNotFound,
)
from logos_bridge.codec import is_bytes_tag, rejection_from_result
from logos_bridge.codegen import plan_names
from logos_bridge.lidl import Interface, TypeRef
from logos_bridge.testing import async_test
from logos_bridge.testing.conformance import ConformanceCase, materialize, wire
from logos_bridge.testing.live import LiveBridge

pytestmark = pytest.mark.integration

GOLDENS = Path(__file__).resolve().parents[1] / "goldens"


def load_golden(name: str) -> ModuleType:
    module_name = f"live_golden_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, GOLDENS / f"{name}_client.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


# table -> the generated module for its provider
GENERATED = {table: load_golden(provider) for table, (provider, _) in TABLES.items()}

# The typed client refuses what a provider accepts (see tests/unit/test_conformance.py STRICTER).
TYPED_STRICTER = {("ext-cases", "bstr/padded-base64"), ("ext-cases", "[bstr]/lenient-plain-string")}
# Class A names a module that is not there: a transport case, not a typed one.
TRANSPORT = "MODULE_NOT_LOADED"


def interface(table: str) -> Interface:
    binding = GENERATED[table]._BINDING
    iface: Interface = binding.interface
    return iface


def typed_value(table: str, type_: TypeRef, value: Any, *, raw: bool) -> Any:
    """A table argument as a typed caller holds it: records as dataclasses, bytes at bstr slots."""
    t = type_.value_type
    if value is None:
        return None
    if t.kind == "array" and isinstance(value, list):
        return [typed_value(table, t.elements[0], v, raw=raw) for v in value]
    if t.kind == "map" and isinstance(value, dict):
        return {k: typed_value(table, t.elements[1], v, raw=raw) for k, v in value.items()}
    if t.kind == "named" and isinstance(value, dict):
        record = interface(table).record(t.name)
        assert record is not None
        module = GENERATED[table]
        attrs = plan_names(interface(table)).fields[t.name]
        return module.RECORD_TYPES[t.name](
            **{attrs[f.name]: typed_value(table, f.type, value.get(f.name), raw=raw) for f in record.fields})
    if t.kind == "primitive" and t.name == "bstr" and is_bytes_tag(value) and raw:
        return value  # a raw case hands the tag itself to a bytes slot
    return materialize(value, raw=raw)


def typed_args(table: str, case: ConformanceCase) -> list[Any]:
    method = interface(table).method(case.method)
    assert method is not None
    params = list(method.params)
    return [typed_value(table, params[i].type, arg, raw=case.raw) if i < len(params) else materialize(arg)
            for i, arg in enumerate(case.args)]


def as_dicts(value: Any) -> Any:
    """Generated records as the dynamic proxy returns them: dicts by wire name, empty optionals omitted."""
    fields = dataclasses.fields(value) if dataclasses.is_dataclass(value) and not isinstance(value, type) else ()
    if fields and all("lidl" in f.metadata for f in fields):  # a record, not LogosResult
        return {f.metadata["lidl"]: as_dicts(getattr(value, f.name)) for f in fields
                if not (getattr(value, f.name) is None and f.default is None)}
    if isinstance(value, list):
        return [as_dicts(v) for v in value]
    if isinstance(value, dict):
        return {k: as_dicts(v) for k, v in value.items()}
    return value


def typed_cases() -> list[Any]:
    return [pytest.param(table, case, id=f"{table}:{case.id}")
            for table, (_, conformance) in TABLES.items() for case in conformance.cases
            if case.expected_error != TRANSPORT and case.id != "adversarial/any/pending-call-canonical"]


@pytest.mark.parametrize(("table", "case"), typed_cases())
@async_test(timeout=60)
async def test_the_generated_client_replays_the_case(node: LiveBridge, typed_views: object, table: str,
                                                     case: ConformanceCase) -> None:
    provider = TABLES[table][0]
    module = GENERATED[table]
    binding = module._BINDING
    names = plan_names(interface(table))
    async with AsyncBridgeClient(node.ws_url) as bridge:
        client = getattr(module, names.async_client)(bridge)
        if case.expected_error == "METHOD_NOT_FOUND":
            with pytest.raises(MethodNotFound):
                binding.plans.method(case.method, module=provider)  # nothing to call, nothing sent
            return
        plan = binding.plans.method(case.method)
        args = typed_args(table, case)
        if (table, case.id) in TYPED_STRICTER:
            with pytest.raises(ArgumentError):
                await getattr(client, names.methods[case.method])(*args)
            accepted = await bridge.call_encoded(provider, case.method, case.wire_args())
            assert rejection_from_result(accepted) is None, "a provider accepts what this client refuses"
            return
        if case.expected_error in REJECTIONS:
            # Refused locally; the same wire, sent raw, is refused by the provider in the same words.
            with pytest.raises(ClientRejection) as local:
                await binding.call(bridge, provider, case.method, args, None)
            assert local.value.code == case.expected_error
            raw = await bridge.call_encoded(provider, case.method, case.wire_args())
            assert raw == {"code": case.expected_error, "message": local.value.message, "origin": provider}
            return
        result = await getattr(client, names.methods[case.method])(*args)
        expected = case.expectations()
        wire_expected = wire(expected[provider] if provider in expected else expected[None])
        assert result == plan.decode_result(wire_expected, module=provider)
        if plan.returns is None:
            assert result is None
        # The dynamic proxy, built from the served contract, answers the same.
        proxy = await bridge.module(provider, require_typed=True)
        dynamic = await proxy[case.method](*case.call_args())
        assert as_dicts(result) == dynamic


@async_test(timeout=90)
async def test_generated_event_classes(node: LiveBridge, typed_views: object) -> None:
    for table, (provider, conformance) in TABLES.items():
        module = GENERATED[table]
        names = plan_names(interface(table))
        async with AsyncBridgeClient(node.ws_url) as bridge:
            client = getattr(module, names.async_client)(bridge)
            async with client.events() as stream:
                for case in conformance.events:
                    fire = getattr(client, names.methods[case.fire])
                    event_decl = interface(table).event(case.event)
                    assert event_decl is not None
                    values = [typed_value(table, p.type, v, raw=False)
                              for p, v in zip(event_decl.params, case.values)]
                    assert await fire(*values) is True
                    event = await stream.get(timeout=15)
                    assert isinstance(event, module.EVENT_TYPES[case.event]), case.id
                    fields = names.event_fields[case.event]
                    assert [getattr(event, fields[p.name]) for p in event_decl.params] == values, case.id
                    assert event.meta.module == provider and event.meta.event == case.event


@async_test(timeout=60)
async def test_the_ext_client_is_exact_and_round_trips_records(node: LiveBridge, typed_views: object) -> None:
    ext = GENERATED["ext-cases"]
    async with AsyncBridgeClient(node.ws_url) as bridge:
        client = ext.AsyncTestFullapiExtCppClient(bridge)
        report = await client.check_compat()
        assert (report.level, report.status) == ("exact", "ok")
        assert report.served_interface_sha256 == ext.INTERFACE_SHA256
        assert report.served_contract_sha256 == ext.CONTRACT_SHA256
        assert not (report.missing or report.mismatched or report.extra or report.not_exposed)
        blob = ext.Blob(id="e", n=2**64 - 1, payload=bytes(range(256)))
        assert await client.echo_blob(blob) == blob
        wrapper = ext.Wrapper(inner=blob, tags=["a", ""], blobs=[blob, ext.Blob(id="", n=0, payload=b"")])
        assert await client.echo_wrapper(wrapper) == wrapper
        opt = ext.Opt(required="r", count=0)
        assert await client.echo_opt(opt) == opt
        assert await client.echo_optional() is None
        async with client.on_blob_event() as events:
            assert await client.fire_blob_event(blob) is True
            event = await events.get(timeout=15)
            assert isinstance(event, ext.BlobEventEvent) and event.v == blob
            assert event.meta.event == "blobEvent" and event.meta.generation >= 1


@async_test(timeout=60)
async def test_the_full_api_client_is_exact_and_lidl_is_the_fixture(node: LiveBridge, typed_views: object) -> None:
    full = GENERATED["cases"]
    async with AsyncBridgeClient(node.ws_url) as bridge:
        client = full.AsyncTestFullapiCppClient(bridge)
        report = await client.check_compat()
        assert report.level == "exact" and not report.not_exposed
        assert await client.lidl() == (FIXTURES / "lidl" / "test_fullapi_cpp.lidl").read_text(encoding="utf-8")
        assert await client.name() == "test_fullapi_cpp"
        assert await client.version() == "1.0.0"
        assert await client.make_result(True) == LogosResult(True, {"ok": True, "provider": "test_fullapi_cpp"}, None)
        assert await client.do_void() is None
