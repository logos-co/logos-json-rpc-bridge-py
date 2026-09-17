"""The LIDL interface model, read from logos-lidl's JSON AST.

The AST is what ``lidl json --identity`` prints and what a bridge serves as
``interface``. Reading rules:

* slots use the derived ``isOptional``/``valueType`` (and ``returnIsOptional``/
  ``returnValueType``) keys; a method without return keys returns nothing, and a
  legacy ``void`` return means the same;
* ``jsonReturn``/``resultReturn`` are kept but carry no meaning here;
* unknown keys are kept, so :meth:`Interface.to_json` reproduces the document and
  its digest exactly;
* unknown type kinds are kept and flagged by :meth:`Interface.validate`, never coerced.

:meth:`Interface.validate` mirrors logos-lidl's validator (same messages) and adds
Python-specific checks; :meth:`Interface.with_identity` mirrors
``injectIdentityMethods``; :meth:`Interface.to_lidl` mirrors the canonical serializer.
"""

from __future__ import annotations

import copy
import functools
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from .digest import interface_sha256
from .errors import BridgeError

PRIMITIVES: Final = frozenset({"tstr", "bstr", "int", "uint", "float64", "bool", "result", "any"})
KINDS: Final = ("primitive", "array", "map", "optional", "named")
IDENTITY_METHODS: Final = ("name", "version", "lidl")
IDENTITY_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "name": "The module's name, as declared in its metadata.",
    "version": "The module's version, as declared in its metadata.",
    "lidl": "The module's canonical LIDL interface document.",
}
SHAPE_FORMAT: Final = 1

#: Issue codes for types this reader cannot map; consumers may degrade them to ``any``.
UNMAPPABLE_CODES: Final = frozenset({"unknown_kind", "unknown_primitive", "malformed_type", "map_key_not_string"})

_IDENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ELEMENT_COUNT: Final[Mapping[str, int]] = {"primitive": 0, "named": 0, "array": 1, "map": 2, "optional": 1}

_MODULE_KEYS: Final = frozenset(
    {"name", "version", "description", "category", "depends", "optional_depends", "types", "methods", "events"}
)
_TYPE_KEYS: Final = frozenset({"kind", "name", "elements"})


# -- errors ------------------------------------------------------------------


class LidlError(BridgeError):
    """Root of the LIDL model's errors."""


class LidlFormatError(LidlError, ValueError):
    """The JSON is not a LIDL AST. ``pointer`` is an RFC 6901 pointer into it."""

    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        self.reason = message
        super().__init__(f"{pointer or '(root)'}: {message}")


class IdentityError(LidlError, ValueError):
    """``name``/``version``/``lidl`` could not be injected (logos-lidl's wording)."""


class InterfaceInvalid(LidlError, ValueError):
    """Validation found errors; ``issues`` holds all of them (warnings included)."""

    def __init__(self, module: str, issues: Sequence[Issue]) -> None:
        self.module = module
        self.issues = tuple(issues)
        errors = [i for i in self.issues if i.severity == "error"]
        lines = "\n".join(f"  {i}" for i in errors)
        super().__init__(f"contract {module or '(unnamed)'} is invalid:\n{lines}")


def json_type_name(value: Any) -> str:
    """nlohmann::json's ``type_name()`` for a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _pointer(base: str, *steps: str | int) -> str:
    out = base
    for step in steps:
        out += "/" + str(step).replace("~", "~0").replace("/", "~1")
    return out


class _Reader:
    """Typed access to AST objects; C++ ``j.value(key, default)`` semantics."""

    @staticmethod
    def obj(value: Any, ptr: str, what: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise LidlFormatError(ptr, f"expected {what} (an object), got {json_type_name(value)}")
        return value

    @staticmethod
    def string(obj: Mapping[str, Any], key: str, ptr: str) -> str:
        value = obj.get(key, "")
        if not isinstance(value, str):
            raise LidlFormatError(_pointer(ptr, key), f"expected string, got {json_type_name(value)}")
        return value

    @staticmethod
    def boolean(obj: Mapping[str, Any], key: str, ptr: str) -> bool:
        value = obj.get(key, False)
        if not isinstance(value, bool):
            raise LidlFormatError(_pointer(ptr, key), f"expected boolean, got {json_type_name(value)}")
        return value

    @staticmethod
    def array(obj: Mapping[str, Any], key: str, ptr: str) -> list[Any]:
        value = obj.get(key, [])
        if not isinstance(value, list):
            raise LidlFormatError(_pointer(ptr, key), f"expected array, got {json_type_name(value)}")
        return value

    @classmethod
    def names(cls, obj: Mapping[str, Any], key: str, ptr: str) -> tuple[str, ...]:
        items = cls.array(obj, key, ptr)
        for index, item in enumerate(items):
            if not isinstance(item, str):
                raise LidlFormatError(_pointer(ptr, key, index), f"expected string, got {json_type_name(item)}")
        return tuple(items)


def _extra(raw: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in known}


# -- types -------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRef:
    """A type expression: ``kind`` is primitive, array, map, optional or named.

    An unknown ``kind`` is kept as written; :attr:`known` is false for it.
    """

    kind: str
    name: str = ""
    elements: tuple[TypeRef, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def primitive(cls, name: str) -> TypeRef:
        return cls("primitive", name)

    @classmethod
    def named(cls, name: str) -> TypeRef:
        return cls("named", name)

    @classmethod
    def array(cls, element: TypeRef) -> TypeRef:
        return cls("array", "", (element,))

    @classmethod
    def map(cls, key: TypeRef, value: TypeRef) -> TypeRef:
        return cls("map", "", (key, value))

    @classmethod
    def optional(cls, inner: TypeRef) -> TypeRef:
        return cls("optional", "", (inner,))

    @classmethod
    def from_json(cls, value: Any, pointer: str = "") -> TypeRef:
        obj = _Reader.obj(value, pointer, "a type")
        kind = obj.get("kind", "primitive")
        if not isinstance(kind, str):
            raise LidlFormatError(_pointer(pointer, "kind"), f"expected string, got {json_type_name(kind)}")
        elements = tuple(
            cls.from_json(item, _pointer(pointer, "elements", index))
            for index, item in enumerate(_Reader.array(obj, "elements", pointer))
        )
        return cls(kind, _Reader.string(obj, "name", pointer), elements, _extra(obj, _TYPE_KEYS))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(copy.deepcopy(self.extra))
        out.update({"elements": [e.to_json() for e in self.elements], "kind": self.kind, "name": self.name})
        return out

    @property
    def is_optional(self) -> bool:
        return self.kind == "optional"

    @property
    def value_type(self) -> TypeRef:
        """Every leading optional stripped (``??T`` -> ``T``), as ``optionalValueType``."""
        current = self
        while current.kind == "optional" and current.elements:
            current = current.elements[0]
        return current

    @property
    def well_formed(self) -> bool:
        """A known kind with the right number of elements, recursively."""
        expected = _ELEMENT_COUNT.get(self.kind)
        return expected is not None and len(self.elements) == expected and all(e.well_formed for e in self.elements)

    @property
    def known(self) -> bool:
        """Well formed, primitives named, and map keys ``tstr``: this reader can map it."""
        if not self.well_formed:
            return False
        if self.kind == "primitive":
            return self.name in PRIMITIVES
        if self.kind == "map":
            key = self.elements[0]
            if not (key.kind == "primitive" and key.name == "tstr"):
                return False
        return all(e.known for e in self.elements)

    def walk(self) -> Iterator[TypeRef]:
        """This node and every nested one, depth first."""
        yield self
        for element in self.elements:
            yield from element.walk()

    def spell(self) -> str:
        """The canonical serializer's spelling (``[tstr]``, ``{tstr: any}``, ``? tstr``)."""
        if self.kind in ("primitive", "named"):
            return self.name
        if self.kind == "array" and len(self.elements) == 1:
            return f"[{self.elements[0].spell()}]"
        if self.kind == "map" and len(self.elements) == 2:
            return f"{{{self.elements[0].spell()}: {self.elements[1].spell()}}}"
        if self.kind == "optional" and len(self.elements) == 1:
            return f"? {self.elements[0].spell()}"
        inner = ", ".join(e.spell() for e in self.elements)
        return f"<{self.kind}{' ' + self.name if self.name else ''}{'(' + inner + ')' if inner else ''}>"

    def __str__(self) -> str:
        return self.spell()


@dataclass(frozen=True)
class Slot:
    """A record field, a parameter, or a method's return.

    ``optional``/``value_type`` are the AST's derived answer. ``flag`` is a field's
    ``? name:`` spelling, kept verbatim; it is never the answer to "is it optional".
    """

    name: str
    type: TypeRef
    optional: bool
    value_type: TypeRef
    flag: bool = False
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def make(cls, name: str, type: TypeRef, *, flag: bool = False) -> Slot:
        return cls(name, type, flag or type.is_optional, type.value_type, flag)

    @property
    def derived_consistent(self) -> bool:
        return self.optional == (self.flag or self.type.is_optional) and self.value_type == self.type.value_type

    def spell(self) -> str:
        if self.flag:
            return f"? {self.name}: {self.type.spell()}"
        return f"{self.name}: {self.type.spell()}"


def _slot_from_json(value: Any, ptr: str, *, is_field: bool) -> Slot:
    obj = _Reader.obj(value, ptr, "a field" if is_field else "a parameter")
    if "type" not in obj:
        raise LidlFormatError(ptr, 'missing "type"')
    type_ = TypeRef.from_json(obj["type"], _pointer(ptr, "type"))
    flag = _Reader.boolean(obj, "optional", ptr) if is_field else False
    optional = obj.get("isOptional")
    if optional is None:
        optional = flag or type_.is_optional
    elif not isinstance(optional, bool):
        raise LidlFormatError(_pointer(ptr, "isOptional"), f"expected boolean, got {json_type_name(optional)}")
    value_type = (TypeRef.from_json(obj["valueType"], _pointer(ptr, "valueType"))
                  if "valueType" in obj else type_.value_type)
    return Slot(_Reader.string(obj, "name", ptr), type_, optional, value_type, flag, obj)


def _slot_to_json(slot: Slot, *, is_field: bool) -> dict[str, Any]:
    if slot.raw is not None:
        return copy.deepcopy(dict(slot.raw))
    out: dict[str, Any] = {
        "isOptional": slot.optional,
        "name": slot.name,
        "type": slot.type.to_json(),
        "valueType": slot.value_type.to_json(),
    }
    if is_field:
        out["optional"] = slot.flag
    return out


# -- declarations ------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """A ``type Name { ... }`` declaration."""

    name: str
    fields: tuple[Slot, ...] = ()
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    def field(self, name: str) -> Slot | None:
        return next((f for f in self.fields if f.name == name), None)

    def to_json(self) -> dict[str, Any]:
        if self.raw is not None:
            return copy.deepcopy(dict(self.raw))
        return {"fields": [_slot_to_json(f, is_field=True) for f in self.fields], "name": self.name}


def _params_spelling(params: Sequence[Slot]) -> str:
    return ", ".join(p.spell() for p in params)


@dataclass(frozen=True)
class Method:
    """A method. ``returns`` is ``None`` for a method that returns nothing."""

    name: str
    params: tuple[Slot, ...] = ()
    returns: Slot | None = None
    description: str = ""
    derived: bool = False
    json_return: bool = False
    result_return: bool = False
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def min_args(self) -> int:
        """Arguments a provider requires: through the last non-optional parameter."""
        required = [i + 1 for i, p in enumerate(self.params) if not p.optional]
        return required[-1] if required else 0

    @property
    def max_args(self) -> int:
        return len(self.params)

    @property
    def is_identity(self) -> bool:
        return self.derived and self.name in IDENTITY_METHODS

    def signature(self) -> str:
        """``name(p: T, q: ? T) -> R`` in LIDL spelling."""
        text = f"{self.name}({_params_spelling(self.params)})"
        return text + (f" -> {self.returns.type.spell()}" if self.returns is not None else "")

    def to_json(self) -> dict[str, Any]:
        if self.raw is not None:
            return copy.deepcopy(dict(self.raw))
        out: dict[str, Any] = {
            "derived": self.derived,
            "description": self.description,
            "jsonReturn": self.json_return,
            "name": self.name,
            "params": [_slot_to_json(p, is_field=False) for p in self.params],
            "resultReturn": self.result_return,
        }
        if self.returns is not None:
            out["returnIsOptional"] = self.returns.optional
            out["returnType"] = self.returns.type.to_json()
            out["returnValueType"] = self.returns.value_type.to_json()
        return out


def _is_void(type_: TypeRef) -> bool:
    return type_.kind in ("primitive", "named") and type_.name == "void" and not type_.elements


def _method_from_json(value: Any, ptr: str) -> Method:
    obj = _Reader.obj(value, ptr, "a method")
    params = tuple(
        _slot_from_json(p, _pointer(ptr, "params", i), is_field=False)
        for i, p in enumerate(_Reader.array(obj, "params", ptr))
    )
    returns: Slot | None = None
    if obj.get("returnType") is not None:
        type_ = TypeRef.from_json(obj["returnType"], _pointer(ptr, "returnType"))
        if not _is_void(type_):  # the legacy spelling of "no return"
            optional = obj.get("returnIsOptional")
            if optional is None:
                optional = type_.is_optional
            elif not isinstance(optional, bool):
                raise LidlFormatError(_pointer(ptr, "returnIsOptional"),
                                      f"expected boolean, got {json_type_name(optional)}")
            value_type = (TypeRef.from_json(obj["returnValueType"], _pointer(ptr, "returnValueType"))
                          if "returnValueType" in obj else type_.value_type)
            returns = Slot("", type_, optional, value_type)
    return Method(
        name=_Reader.string(obj, "name", ptr),
        params=params,
        returns=returns,
        description=_Reader.string(obj, "description", ptr),
        derived=_Reader.boolean(obj, "derived", ptr),
        json_return=_Reader.boolean(obj, "jsonReturn", ptr),
        result_return=_Reader.boolean(obj, "resultReturn", ptr),
        raw=obj,
    )


@dataclass(frozen=True)
class Event:
    """An event declaration."""

    name: str
    params: tuple[Slot, ...] = ()
    description: str = ""
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    def signature(self) -> str:
        return f"{self.name}({_params_spelling(self.params)})"

    def to_json(self) -> dict[str, Any]:
        if self.raw is not None:
            return copy.deepcopy(dict(self.raw))
        return {
            "description": self.description,
            "name": self.name,
            "params": [_slot_to_json(p, is_field=False) for p in self.params],
        }


def _event_from_json(value: Any, ptr: str) -> Event:
    obj = _Reader.obj(value, ptr, "an event")
    params = tuple(
        _slot_from_json(p, _pointer(ptr, "params", i), is_field=False)
        for i, p in enumerate(_Reader.array(obj, "params", ptr))
    )
    return Event(_Reader.string(obj, "name", ptr), params, _Reader.string(obj, "description", ptr), obj)


def _record_from_json(value: Any, ptr: str) -> Record:
    obj = _Reader.obj(value, ptr, "a type declaration")
    fields = tuple(
        _slot_from_json(f, _pointer(ptr, "fields", i), is_field=True)
        for i, f in enumerate(_Reader.array(obj, "fields", ptr))
    )
    return Record(_Reader.string(obj, "name", ptr), fields, obj)


# -- validation --------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One validation finding. ``origin`` is ``lidl`` for logos-lidl's own rules."""

    severity: Literal["error", "warning"]
    code: str
    pointer: str
    message: str
    origin: Literal["lidl", "python"] = "lidl"

    def __str__(self) -> str:
        return f"{self.pointer or '(root)'}: {self.severity}: {self.message}"


class _Validator:
    def __init__(self, interface: Interface) -> None:
        self.interface = interface
        self.declared = {t.name for t in interface.types}
        self.issues: list[Issue] = []

    def error(self, code: str, ptr: str, message: str, origin: Literal["lidl", "python"] = "lidl") -> None:
        self.issues.append(Issue("error", code, ptr, message, origin))

    def warning(self, code: str, ptr: str, message: str, origin: Literal["lidl", "python"] = "lidl") -> None:
        self.issues.append(Issue("warning", code, ptr, message, origin))

    def ident(self, name: str, ptr: str, what: str) -> None:
        if name and _IDENT.fullmatch(name) is None:
            self.error("invalid_identifier", ptr, f"{what} '{name}' is not an identifier ([A-Za-z_][A-Za-z0-9_]*)",
                       "python")

    def slot(self, slot: Slot, ptr: str) -> None:
        if slot.raw is not None and not slot.derived_consistent:
            self.error("derived_key_mismatch", ptr,
                       f"'{slot.name}': isOptional/valueType disagree with its type and optional flag", "python")

    def run(self) -> list[Issue]:
        iface = self.interface
        if not iface.name:
            self.error("module_name_empty", "/name", "Module name is empty")
        self.ident(iface.name, "/name", "module name")
        seen_types: set[str] = set()
        for ti, td in enumerate(iface.types):
            tp = f"/types/{ti}"
            if td.name in PRIMITIVES:
                self.error("type_shadows_builtin", tp + "/name", f"Type '{td.name}' shadows a builtin type")
            if td.name in seen_types:
                self.error("duplicate_type", tp + "/name", f"Duplicate type definition '{td.name}'")
            seen_types.add(td.name)
            self.ident(td.name, tp + "/name", "type name")
            for fi, fd in enumerate(td.fields):
                fp = f"{tp}/fields/{fi}"
                where = f"field '{fd.name}' of type '{td.name}'"
                self.ident(fd.name, fp + "/name", "field name")
                self.slot(fd, fp)
                if fd.flag and fd.type.is_optional:
                    self.warning(
                        "optional_twice", fp,
                        f"Field '{fd.name}' of type '{td.name}' is marked optional twice ('? {fd.name}: ?T'); "
                        "the field flag and the optional type are equivalent, one suffices",
                    )
                if fd.flag and not fd.type.is_optional:
                    self.optional_any(fd.type, where, fp + "/type")
                self.type_expr(fd.type, where, fp + "/type", in_map_key=False, under_optional=False)
        seen_methods: set[str] = set()
        for mi, md in enumerate(iface.methods):
            mp = f"/methods/{mi}"
            if md.name in seen_methods:
                self.error("duplicate_method", mp + "/name", f"Duplicate method definition '{md.name}'")
            seen_methods.add(md.name)
            self.ident(md.name, mp + "/name", "method name")
            if md.returns is not None:
                self.slot(md.returns, mp)
                self.type_expr(md.returns.type, f"return type of method '{md.name}'", mp + "/returnType",
                               in_map_key=False, under_optional=False)
            seen_params: set[str] = set()
            for pi, pd in enumerate(md.params):
                pp = f"{mp}/params/{pi}"
                self.ident(pd.name, pp + "/name", "parameter name")
                self.slot(pd, pp)
                self.type_expr(pd.type, f"parameter '{pd.name}' of method '{md.name}'", pp + "/type",
                               in_map_key=False, under_optional=False)
                if pd.name in seen_params:
                    self.error("duplicate_param", pp + "/name",
                               f"Duplicate parameter '{pd.name}' in method '{md.name}'")
                seen_params.add(pd.name)
        seen_events: set[str] = set()
        for ei, ed in enumerate(iface.events):
            ep = f"/events/{ei}"
            if ed.name in seen_events:
                self.error("duplicate_event", ep + "/name", f"Duplicate event definition '{ed.name}'")
            seen_events.add(ed.name)
            self.ident(ed.name, ep + "/name", "event name")
            seen_event_params: set[str] = set()
            for pi, pd in enumerate(ed.params):
                pp = f"{ep}/params/{pi}"
                self.ident(pd.name, pp + "/name", "parameter name")
                self.slot(pd, pp)
                self.type_expr(pd.type, f"parameter '{pd.name}' of event '{ed.name}'", pp + "/type",
                               in_map_key=False, under_optional=False)
                if pd.name in seen_event_params:
                    self.error("duplicate_event_param", pp + "/name",
                               f"duplicate parameter '{pd.name}' in event '{ed.name}'", "python")
                seen_event_params.add(pd.name)
        self.infinite_records()
        return self.issues

    def optional_any(self, inner: TypeRef, where: str, ptr: str) -> None:
        if inner.kind == "primitive" and inner.name == "any":
            self.warning("optional_any", ptr,
                         f"Optional 'any' in {where} is redundant: 'any' already admits the empty value; use 'any'")

    def type_expr(self, te: TypeRef, where: str, ptr: str, *, in_map_key: bool, under_optional: bool) -> None:
        expected = _ELEMENT_COUNT.get(te.kind)
        if expected is None:
            self.error("unknown_kind", ptr, f"unknown type kind '{te.kind}' in {where}", "python")
            return
        if len(te.elements) != expected:
            self.error("malformed_type", ptr,
                       f"a '{te.kind}' type needs {expected} element(s), has {len(te.elements)} (in {where})",
                       "python")
        if te.kind == "primitive":
            if te.name not in PRIMITIVES:
                self.error("unknown_primitive", ptr, f"unknown primitive type '{te.name}' in {where}", "python")
        elif te.kind == "named":
            if te.name not in self.declared:
                self.error("unknown_type", ptr, f"Unknown type '{te.name}'")
        elif te.kind == "array":
            if te.elements:
                self.type_expr(te.elements[0], where, ptr + "/elements/0", in_map_key=False, under_optional=False)
        elif te.kind == "map":
            if len(te.elements) >= 2:
                self.type_expr(te.elements[0], where, ptr + "/elements/0", in_map_key=True, under_optional=False)
                self.type_expr(te.elements[1], where, ptr + "/elements/1", in_map_key=False, under_optional=False)
                key = te.elements[0]
                if not (key.kind == "primitive" and key.name == "tstr"):
                    self.error("map_key_not_string", ptr + "/elements/0",
                               f"map keys must be tstr, got '{key.spell()}' (in {where})", "python")
        else:  # optional
            if in_map_key:
                self.error("optional_map_key", ptr, f"Optional is not allowed in a map key position (in {where})")
            if under_optional:
                self.warning("redundant_optional", ptr,
                             f"Redundant nested optional in {where}: '??T' denotes the same two states as '?T'")
            if te.elements:
                self.optional_any(te.elements[0], where, ptr + "/elements/0")
                self.type_expr(te.elements[0], where, ptr + "/elements/0", in_map_key=False, under_optional=True)

    def infinite_records(self) -> None:
        # A record reaching itself through required record fields only has no finite value.
        records = {t.name: t for t in self.interface.types}

        def required_refs(record: Record) -> set[str]:
            return {f.type.name for f in record.fields
                    if not f.optional and f.type.kind == "named" and f.type.name in records}

        for ti, record in enumerate(self.interface.types):
            stack, seen = list(required_refs(record)), set()
            while stack:
                name = stack.pop()
                if name == record.name:
                    self.warning("infinite_record", f"/types/{ti}",
                                 f"record '{record.name}' contains itself through required fields only, "
                                 "so no finite value exists", "python")
                    break
                if name not in seen:
                    seen.add(name)
                    stack.extend(required_refs(records[name]))


# -- the interface -----------------------------------------------------------


def _identity_signature_ok(method: Method) -> bool:
    returns = method.returns
    return (not method.params and returns is not None
            and returns.type.kind == "primitive" and returns.type.name == "tstr")


def _describe_signature(method: Method) -> str:
    # identity.cpp describeSignature: names only, and the return's raw `name`.
    text = f"{method.name}({', '.join(p.name for p in method.params)})"
    if method.returns is not None:
        text += f" -> {method.returns.type.name}"
    return text


def identity_method(name: str) -> Method:
    """The derived built-in ``name``/``version``/``lidl``, as logos-lidl builds it."""
    if name not in IDENTITY_METHODS:
        raise ValueError(f"{name!r} is not an identity method")
    tstr = TypeRef.primitive("tstr")
    return Method(name, (), Slot("", tstr, False, tstr), IDENTITY_DESCRIPTIONS[name], derived=True)


def _escape_lidl(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


@dataclass(frozen=True)
class Interface:
    """A module contract. Build it with :meth:`from_json` (or :meth:`from_shape`)."""

    name: str
    version: str = ""
    description: str = ""
    category: str = ""
    depends: tuple[str, ...] = ()
    optional_depends: tuple[str, ...] = ()
    types: tuple[Record, ...] = ()
    methods: tuple[Method, ...] = ()
    events: tuple[Event, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    # ---------------------------------------------------------------- reading

    @classmethod
    def from_json(cls, doc: Any) -> Interface:
        """Read a JSON AST (a decoded object). Raises :class:`LidlFormatError`."""
        obj = _Reader.obj(doc, "", "a module")
        return cls(
            name=_Reader.string(obj, "name", ""),
            version=_Reader.string(obj, "version", ""),
            description=_Reader.string(obj, "description", ""),
            category=_Reader.string(obj, "category", ""),
            depends=_Reader.names(obj, "depends", ""),
            optional_depends=_Reader.names(obj, "optional_depends", ""),
            types=tuple(_record_from_json(t, f"/types/{i}") for i, t in enumerate(_Reader.array(obj, "types", ""))),
            methods=tuple(_method_from_json(m, f"/methods/{i}")
                          for i, m in enumerate(_Reader.array(obj, "methods", ""))),
            events=tuple(_event_from_json(e, f"/events/{i}") for i, e in enumerate(_Reader.array(obj, "events", ""))),
            extra=_extra(obj, _MODULE_KEYS),
            raw=obj,
        )

    @classmethod
    def loads(cls, text: str | bytes) -> Interface:
        """Read a JSON AST from text."""
        from .codec import loads

        try:
            doc = loads(text)
        except ValueError as exc:
            raise LidlFormatError("", f"not JSON: {exc}") from None
        return cls.from_json(doc)

    @classmethod
    def coerce(cls, value: Interface | Mapping[str, Any]) -> Interface:
        """An :class:`Interface`, a JSON AST, or a :meth:`shape` document."""
        if isinstance(value, Interface):
            return value
        if isinstance(value, Mapping) and "shape" in value and isinstance(value.get("methods"), Mapping):
            return cls.from_shape(value)
        return cls.from_json(dict(value))

    # ---------------------------------------------------------------- writing

    def to_json(self) -> dict[str, Any]:
        """The AST; for a document read with :meth:`from_json`, exactly that document."""
        if self.raw is not None:
            return copy.deepcopy(dict(self.raw))
        out: dict[str, Any] = copy.deepcopy(dict(self.extra))
        out.update({
            "category": self.category,
            "depends": list(self.depends),
            "description": self.description,
            "events": [e.to_json() for e in self.events],
            "methods": [m.to_json() for m in self.methods],
            "name": self.name,
            "optional_depends": list(self.optional_depends),
            "types": [t.to_json() for t in self.types],
            "version": self.version,
        })
        return out

    def interface_sha256(self) -> str:
        return interface_sha256(self.to_json())

    def to_lidl(self) -> str:
        """The canonical ``.lidl`` text (logos-lidl ``serialize``; derived methods omitted)."""
        lines = [f"module {self.name} {{"]
        if self.version:
            lines.append(f'  version "{self.version}"')
        if self.description:
            lines.append(f'  description "{_escape_lidl(self.description)}"')
        if self.category:
            lines.append(f'  category "{self.category}"')
        lines.append(f"  depends [{', '.join(self.depends)}]")
        if self.optional_depends:
            lines.append(f"  optional_depends [{', '.join(self.optional_depends)}]")
        for record in self.types:
            lines.append("")
            lines.append(f"  type {record.name} {{")
            for fd in record.fields:
                lines.append(f"    {'? ' if fd.flag else ''}{fd.name}: {_checked_spell(fd.type)}")
            lines.append("  }")
        authored = [m for m in self.methods if not m.derived]
        if authored:
            lines.append("")
        for method in authored:
            text = f"  method {method.name}({_checked_params(method.params)})"
            if method.returns is not None:
                text += f" -> {_checked_spell(method.returns.type)}"
            if method.description:
                text += f' description "{_escape_lidl(method.description)}"'
            lines.append(text)
        if self.events:
            lines.append("")
        for event in self.events:
            text = f"  event {event.name}({_checked_params(event.params)})"
            if event.description:
                text += f' description "{_escape_lidl(event.description)}"'
            lines.append(text)
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------------- lookup

    def method(self, name: str) -> Method | None:
        return next((m for m in self.methods if m.name == name), None)

    def event(self, name: str) -> Event | None:
        return next((e for e in self.events if e.name == name), None)

    def record(self, name: str) -> Record | None:
        return next((t for t in self.types if t.name == name), None)

    @property
    def method_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.methods)

    @property
    def event_names(self) -> tuple[str, ...]:
        return tuple(e.name for e in self.events)

    @property
    def has_identity(self) -> bool:
        """``name``, ``version`` and ``lidl`` are all declared."""
        return all(self.method(n) is not None for n in IDENTITY_METHODS)

    def slots(self) -> Iterator[tuple[str, Slot]]:
        """Every slot with its RFC 6901 pointer."""
        for ti, record in enumerate(self.types):
            for fi, fd in enumerate(record.fields):
                yield f"/types/{ti}/fields/{fi}", fd
        for mi, method in enumerate(self.methods):
            for pi, param in enumerate(method.params):
                yield f"/methods/{mi}/params/{pi}", param
            if method.returns is not None:
                yield f"/methods/{mi}", method.returns
        for ei, event in enumerate(self.events):
            for pi, param in enumerate(event.params):
                yield f"/events/{ei}/params/{pi}", param

    # ------------------------------------------------------------- validation

    @functools.cached_property
    def issues(self) -> tuple[Issue, ...]:
        """Every finding of :meth:`validate`, in logos-lidl's order."""
        return tuple(_Validator(self).run())

    def validate(self) -> tuple[Issue, ...]:
        return self.issues

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    def check(self, *, allow_unknown_types: bool = False) -> Interface:
        """Raise :class:`InterfaceInvalid` on errors; unmappable types pass with ``allow_unknown_types``."""
        blocking = [i for i in self.errors if not (allow_unknown_types and i.code in UNMAPPABLE_CODES)]
        if blocking:
            raise InterfaceInvalid(self.name, self.issues)
        return self

    def lidl_report(self) -> dict[str, list[str]]:
        """``lidl check --json``'s ``{errors, warnings}`` for this document."""
        return {
            "errors": [i.message for i in self.errors if i.origin == "lidl"],
            "warnings": [i.message for i in self.warnings if i.origin == "lidl"],
        }

    # --------------------------------------------------------------- identity

    def with_identity(self) -> Interface:
        """Append the derived ``name()``, ``version()`` and ``lidl()`` (idempotent).

        Mirrors logos-lidl ``injectIdentityMethods``, including its error messages.
        """
        for wanted in IDENTITY_METHODS:
            existing = self.method(wanted)
            if existing is None or existing.derived:
                continue
            if wanted == "lidl":
                raise IdentityError(
                    f"module '{self.name}' declares 'lidl()', but 'lidl' is a generator-owned "
                    "built-in that returns the canonical interface document"
                )
            if not _identity_signature_ok(existing):
                raise IdentityError(
                    f"module '{self.name}' declares '{_describe_signature(existing)}', but '{wanted}' "
                    f"is reserved for module identity and must be '{wanted}() -> tstr'"
                )
        added = [identity_method(n) for n in IDENTITY_METHODS if self.method(n) is None]
        if not added:
            return self
        return replace(self, methods=self.methods + tuple(added), raw=None)

    # ------------------------------------------------------------------ shape

    def shape(self) -> dict[str, Any]:
        """The spelling-free form: what a positional client sends and receives.

        Keeps the module name, member names, parameter names and types, record
        names and field names/types. Drops descriptions, metadata, the optional
        spelling (``? f: T`` = ``f: ?T``; ``??T`` = ``?T``; ``?any`` = ``any``),
        ``derived`` and the legacy return flags, and declaration order except
        for parameters. Methods, events and records are keyed by name.
        """
        return {
            "shape": SHAPE_FORMAT,
            "module": self.name,
            "records": {r.name: {f.name: _shape_slot(f) for f in r.fields} for r in self.types},
            "methods": {
                m.name: {
                    "params": [[p.name, _shape_slot(p)] for p in m.params],
                    "returns": _shape_slot(m.returns) if m.returns is not None else None,
                }
                for m in self.methods
            },
            "events": {e.name: [[p.name, _shape_slot(p)] for p in e.params] for e in self.events},
        }

    def shape_sha256(self) -> str:
        from .digest import shape_sha256

        return shape_sha256(self)

    @classmethod
    def from_shape(cls, shape: Mapping[str, Any]) -> Interface:
        """Rebuild an interface from :meth:`shape` output (descriptions and metadata are gone)."""
        if not isinstance(shape, Mapping) or shape.get("shape") != SHAPE_FORMAT:
            raise LidlFormatError("/shape", f"not a shape document of format {SHAPE_FORMAT}")
        module = shape.get("module")
        if not isinstance(module, str):
            raise LidlFormatError("/module", "expected string")

        def slots(items: Any, ptr: str) -> tuple[Slot, ...]:
            if not isinstance(items, list):
                raise LidlFormatError(ptr, f"expected array, got {json_type_name(items)}")
            out = []
            for i, item in enumerate(items):
                if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
                    raise LidlFormatError(_pointer(ptr, i), "expected [name, type]")
                out.append(Slot.make(item[0], parse_shape_type(item[1], _pointer(ptr, i, 1))))
            return tuple(out)

        records = []
        for rname, fields in _shape_section(shape, "records").items():
            if not isinstance(fields, Mapping):
                raise LidlFormatError(_pointer("/records", rname), "expected object")
            records.append(Record(rname, tuple(
                Slot.make(fname, parse_shape_type(ftype, _pointer("/records", rname, fname)))
                for fname, ftype in fields.items()
            )))
        methods = []
        for mname, spec in _shape_section(shape, "methods").items():
            ptr = _pointer("/methods", mname)
            if not isinstance(spec, Mapping):
                raise LidlFormatError(ptr, "expected object")
            ret = spec.get("returns")
            returns = None if ret is None else Slot.make("", parse_shape_type(ret, _pointer(ptr, "returns")))
            params = slots(spec.get("params", []), _pointer(ptr, "params"))
            method = Method(mname, params, returns)
            if mname in IDENTITY_METHODS and _identity_signature_ok(method):
                method = identity_method(mname)  # shapes drop `derived`; the built-ins are always derived
            methods.append(method)
        events = [Event(ename, slots(params, _pointer("/events", ename)))
                  for ename, params in _shape_section(shape, "events").items()]
        return cls(module, types=tuple(records), methods=tuple(methods), events=tuple(events))

    def structural(self) -> dict[str, Any]:
        """:meth:`shape` without the module name, parameter names and record names.

        Records are renamed ``R0, R1, ...`` in first-reference order (methods by
        name, then events by name), and unreferenced records are dropped. Two
        contracts that differ only in those names have equal structural forms.
        """
        namer = _RecordNamer(self)
        methods = {}
        for method in sorted(self.methods, key=lambda m: m.name):
            params = [namer.slot(p) for p in method.params]
            methods[method.name] = {
                "params": params,
                "returns": namer.slot(method.returns) if method.returns is not None else None,
            }
        events = {e.name: [namer.slot(p) for p in e.params] for e in sorted(self.events, key=lambda e: e.name)}
        return {"structural": SHAPE_FORMAT, "records": namer.records, "methods": methods, "events": events}

    def member_signature(self, kind: Literal["method", "event"], name: str,
                         *, structural: bool = False) -> dict[str, Any] | None:
        """One member's shape (or structural form) with the records it reaches."""
        member: Method | Event | None = self.method(name) if kind == "method" else self.event(name)
        if member is None:
            return None
        if structural:
            namer = _RecordNamer(self)
            sig: dict[str, Any] = {"params": [namer.slot(p) for p in member.params]}
            if isinstance(member, Method):
                sig["returns"] = namer.slot(member.returns) if member.returns is not None else None
            return {"signature": sig, "records": namer.records}
        reached: dict[str, Any] = {}
        sig = {"params": [[p.name, _shape_slot(p)] for p in member.params]}
        if isinstance(member, Method):
            sig["returns"] = _shape_slot(member.returns) if member.returns is not None else None
        stack = [s.type for s in member.params] + (
            [member.returns.type] if isinstance(member, Method) and member.returns is not None else [])
        while stack:
            for node in stack.pop().walk():
                record = self.record(node.name) if node.kind == "named" else None
                if record is not None and record.name not in reached:
                    reached[record.name] = {f.name: _shape_slot(f) for f in record.fields}
                    stack.extend(f.type for f in record.fields)
        return {"signature": sig, "records": reached}


def _shape_section(shape: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = shape.get(key, {})
    if not isinstance(section, Mapping):
        raise LidlFormatError("/" + key, "expected object")
    return section


def _checked_spell(type_: TypeRef) -> str:
    if not type_.well_formed:
        raise ValueError(f"cannot serialize the type {type_.spell()}: not a well-formed LIDL type")
    return type_.spell()


def _checked_params(params: Sequence[Slot]) -> str:
    return ", ".join(f"{p.name}: {_checked_spell(p.type)}" for p in params)


def _compactable(type_: TypeRef) -> bool:
    if not type_.well_formed:
        return False
    for node in type_.walk():
        if node.kind == "primitive" and node.name not in PRIMITIVES:
            return False
        if node.kind == "named" and (node.name in PRIMITIVES or _IDENT.fullmatch(node.name) is None):
            return False
    return True


def shape_type(type_: TypeRef, *, optional: bool = False) -> Any:
    """A type in shape form: a compact string (``?[tstr]``, ``{tstr:Blob}``), or the
    AST object when the tree holds a node the compact form cannot spell."""
    if not _compactable(type_):
        node = type_.to_json()
        return {"kind": node["kind"], "name": node["name"], "elements": node["elements"]}
    value = type_.value_type
    body = _compact(value)
    if value.kind == "primitive" and value.name == "any":
        return body  # ?any denotes the same values as any
    return "?" + body if (optional or type_.is_optional) else body


def _compact(type_: TypeRef) -> str:
    if type_.kind in ("primitive", "named"):
        return type_.name
    if type_.kind == "array":
        return "[" + _compact_inner(type_.elements[0]) + "]"
    if type_.kind == "map":
        return "{" + _compact_inner(type_.elements[0]) + ":" + _compact_inner(type_.elements[1]) + "}"
    return _compact_inner(type_)


def _compact_inner(type_: TypeRef) -> str:
    spelled = shape_type(type_)
    assert isinstance(spelled, str)  # a compactable tree has compactable children
    return spelled


def _shape_slot(slot: Slot) -> Any:
    if not (_compactable(slot.type) and _compactable(slot.value_type)):
        return shape_type(slot.type)
    value = slot.value_type
    body = _compact(value)
    if slot.optional and not (value.kind == "primitive" and value.name == "any"):
        return "?" + body
    return body


class _RecordNamer:
    def __init__(self, interface: Interface) -> None:
        self.interface = interface
        self.names: dict[str, str] = {}
        self.records: dict[str, Any] = {}

    def slot(self, slot: Slot) -> Any:
        return self.rename(_shape_slot(slot), slot)

    def rename(self, spelled: Any, slot: Slot) -> Any:
        for node in slot.type.walk():
            if node.kind == "named":
                self.visit(node.name)
        if not isinstance(spelled, str):
            return spelled
        return re.sub(r"[A-Za-z_][A-Za-z0-9_]*", lambda m: self.names.get(m.group(0), m.group(0)), spelled)

    def visit(self, name: str) -> None:
        record = self.interface.record(name)
        if record is None or name in self.names:
            return
        new = f"R{len(self.names)}"
        self.names[name] = new
        self.records[new] = None  # reserve the position before recursing
        fields = {}
        for fd in sorted(record.fields, key=lambda f: f.name):
            fields[fd.name] = self.rename(_shape_slot(fd), fd)
        self.records[new] = fields


# -- shape type parsing ------------------------------------------------------


def parse_shape_type(value: Any, pointer: str = "") -> TypeRef:
    """Invert :func:`shape_type`."""
    if isinstance(value, Mapping):
        return TypeRef.from_json(dict(value), pointer)
    if not isinstance(value, str):
        raise LidlFormatError(pointer, f"expected a type, got {json_type_name(value)}")
    parser = _ShapeTypeParser(value, pointer)
    result = parser.parse()
    if parser.pos != len(value):
        raise LidlFormatError(pointer, f"unexpected {value[parser.pos:]!r} in type {value!r}")
    return result


class _ShapeTypeParser:
    def __init__(self, text: str, pointer: str) -> None:
        self.text = text
        self.pointer = pointer
        self.pos = 0

    def fail(self, what: str) -> LidlFormatError:
        return LidlFormatError(self.pointer, f"{what} at offset {self.pos} of type {self.text!r}")

    def expect(self, char: str) -> None:
        if not self.text.startswith(char, self.pos):
            raise self.fail(f"expected {char!r}")
        self.pos += 1

    def parse(self) -> TypeRef:
        text = self.text
        if self.pos >= len(text):
            raise self.fail("unexpected end")
        char = text[self.pos]
        if char == "?":
            self.pos += 1
            return TypeRef.optional(self.parse())
        if char == "[":
            self.pos += 1
            inner = self.parse()
            self.expect("]")
            return TypeRef.array(inner)
        if char == "{":
            self.pos += 1
            key = self.parse()
            self.expect(":")
            value = self.parse()
            self.expect("}")
            return TypeRef.map(key, value)
        match = _IDENT.match(text, self.pos)
        if match is None:
            raise self.fail("expected a type name")
        self.pos = match.end()
        name = match.group(0)
        return TypeRef.primitive(name) if name in PRIMITIVES else TypeRef.named(name)
