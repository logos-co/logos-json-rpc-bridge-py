"""The goldens' annotations at runtime, for environments without mypy (the pip CI job).

Every annotation must evaluate, the blocking client must mirror the async one, and
record, event, parameter and return types must follow the LIDL mapping table.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import importlib.util
import inspect
import sys
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from logos_bridge.codegen import plan_names
from logos_bridge.lidl import Interface, Slot, TypeRef
from logos_bridge.models import Event
from logos_bridge.typed import BlockingTypedSubscription, LogosResult, TypedSubscribeRequest

GOLDENS = Path(__file__).resolve().parents[1] / "goldens"
NAMES = sorted(p.name.removesuffix("_client.py") for p in GOLDENS.glob("*_client.py"))
PRIMITIVES: dict[str, Any] = {"tstr": str, "bstr": bytes, "int": int, "uint": int, "float64": float,
                              "bool": bool, "any": Any, "result": LogosResult[Any]}


def load(name: str) -> ModuleType:
    module_name = f"golden_typing_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, GOLDENS / f"{name}_client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def expected(type_: TypeRef, records: dict[str, type], *, param: bool) -> Any:
    """The mapping table: inputs take abstract containers, values are concrete."""
    if type_.kind == "primitive":
        return PRIMITIVES[type_.name]
    if type_.kind == "named":
        return records[type_.name]
    if type_.kind == "array":
        inner = expected(type_.elements[0], records, param=param)
        return (collections.abc.Sequence if param else list)[inner]
    if type_.kind == "map":
        inner = expected(type_.elements[1], records, param=param)
        return (collections.abc.Mapping if param else dict)[str, inner]
    inner = expected(type_.elements[0], records, param=param)
    return inner if inner is Any else typing.Optional[inner]


def expected_slot(slot: Slot, records: dict[str, type], *, param: bool) -> Any:
    inner = expected(slot.value_type, records, param=param)
    return typing.Optional[inner] if slot.optional and inner is not Any else inner


@pytest.fixture(params=NAMES)
def golden(request: pytest.FixtureRequest) -> tuple[ModuleType, Interface]:
    module = load(request.param)
    return module, Interface.coerce(module.INTERFACE)


def test_there_is_a_golden_per_contract() -> None:
    assert NAMES == ["mini_module", "storage_module", "test_fullapi_cpp", "test_fullapi_ext_cpp"]


def test_every_annotation_evaluates(golden: tuple[ModuleType, Interface]) -> None:
    module, _ = golden
    checked = 0
    for value in list(vars(module).values()):
        if not (inspect.isclass(value) and value.__module__ == module.__name__):
            continue
        typing.get_type_hints(value)
        for member in vars(value).values():
            function = member.fget if isinstance(member, property) else member
            if inspect.isfunction(function):
                typing.get_type_hints(function)
                checked += 1
    assert checked > 10


def test_records_and_events_follow_the_mapping_table(golden: tuple[ModuleType, Interface]) -> None:
    module, iface = golden
    records: dict[str, type] = dict(module.RECORD_TYPES)
    assert sorted(records) == sorted(r.name for r in iface.types)
    for record in iface.types:
        cls = records[record.name]
        hints = typing.get_type_hints(cls)
        wire = {f.metadata["lidl"]: f.name for f in dataclasses.fields(cls)}
        assert list(wire) == [f.name for f in record.fields]
        for slot in record.fields:
            assert hints[wire[slot.name]] == expected_slot(slot, records, param=False), (record.name, slot.name)
    assert tuple(module.EVENT_NAMES) == iface.event_names
    for event in iface.events:
        cls = module.EVENT_TYPES[event.name]
        hints = typing.get_type_hints(cls)
        wire = {f.metadata["lidl"]: f.name for f in dataclasses.fields(cls) if "lidl" in f.metadata}
        assert list(wire) == [p.name for p in event.params]
        for slot in event.params:
            assert hints[wire[slot.name]] == expected_slot(slot, records, param=False), (event.name, slot.name)
        assert hints["meta"] is Event
    union = getattr(module, plan_names(iface).event_union)
    members = set(typing.get_args(union)) if typing.get_origin(union) is typing.Union else {union}
    assert members == set(module.EVENT_TYPES.values())


def test_methods_follow_the_mapping_table(golden: tuple[ModuleType, Interface]) -> None:
    module, iface = golden
    names = plan_names(iface)
    records: dict[str, type] = dict(module.RECORD_TYPES)
    for client in (names.async_client, names.sync_client):
        cls = getattr(module, client)
        for method in iface.methods:
            function = getattr(cls, names.methods[method.name])
            hints = typing.get_type_hints(function)
            params = list(inspect.signature(function).parameters)
            assert params[: len(method.params) + 1] == ["self", *names.params[method.name]]
            for slot, python in zip(method.params, names.params[method.name]):
                assert hints[python] == expected_slot(slot, records, param=True), (client, method.name, slot.name)
            returns = None if method.returns is None else expected_slot(method.returns, records, param=False)
            assert hints["return"] == (type(None) if returns is None else returns), (client, method.name)
            assert hints["timeout"] == typing.Optional[float]


def test_the_blocking_client_mirrors_the_async_one(golden: tuple[ModuleType, Interface]) -> None:
    module, iface = golden
    names = plan_names(iface)
    aio, blocking = getattr(module, names.async_client), getattr(module, names.sync_client)
    public = sorted(n for n in vars(aio) if not n.startswith("_"))
    assert sorted(n for n in vars(blocking) if not n.startswith("_")) == sorted({*public, "aio", "portal"})
    for name in public:
        member = vars(aio)[name]
        if isinstance(member, property):
            assert isinstance(vars(blocking)[name], property)
            continue
        a_sig, b_sig = inspect.signature(member), inspect.signature(vars(blocking)[name])
        assert [(p.name, p.kind, p.default) for p in a_sig.parameters.values()] == [
            (p.name, p.kind, p.default) for p in b_sig.parameters.values()], name
        a_hints, b_hints = typing.get_type_hints(member), typing.get_type_hints(vars(blocking)[name])
        a_return, b_return = a_hints.pop("return"), b_hints.pop("return")
        assert a_hints == b_hints, name
        if typing.get_origin(a_return) is TypedSubscribeRequest:
            assert typing.get_origin(b_return) is BlockingTypedSubscription, name
            assert typing.get_args(a_return) == typing.get_args(b_return), name
        else:
            assert a_return == b_return, name
    for event in iface.events:
        assert callable(getattr(aio, names.subscriber(event.name)))
        assert callable(getattr(blocking, names.decoder(event.name)))
