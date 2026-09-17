"""``logos-bridge-codegen python``: a typed client module for one contract.

The module holds records (frozen dataclasses), one event class per event (with
``meta``, the raw :class:`~logos_bridge.models.Event`), a union and a ``Literal``
alias over the events, ``EVENT_NAMES``, the embedded ``INTERFACE`` (its shape,
checked against ``SHAPE_SHA256`` at import), ``INTERFACE_SHA256``,
``CONTRACT_SHA256``, and two clients: ``Async<Base>Client`` and the blocking
``<Base>Client``. Every call goes through ``rpc.call``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from .._version import __version__
from ..digest import shape_sha256
from ..lidl import Event, Interface, Method, Record, Slot, TypeRef
from ..typed import CODEGEN_FORMAT, rejection_ambiguous
from .naming import CodegenError, Names
from .text import RawCode, docstring, py_literal, py_str, rest_text, summary_and_body

PYTHON_PROVENANCE: Final = ("# Source: ", "# Generator: ")
DEFAULT_REGEN_HINT: Final = "run logos-bridge-codegen python on the module's contract (see --help)"
_BUILTIN_TYPES: Final = frozenset({"str", "bytes", "int", "float", "bool", "list", "dict", "tuple", "type",
                                   "property"})
_ARITY: Final = {"primitive": 0, "named": 0, "array": 1, "map": 2, "optional": 1}
_WIDTH: Final = 110
_PRIMITIVE_TYPES: Final = {"tstr": "str", "bstr": "bytes", "int": "int", "uint": "int", "float64": "float",
                           "bool": "bool"}


@dataclass(frozen=True)
class Contract:
    """What a generator needs: the interface and where it came from."""

    interface: Interface
    contract_sha256: str | None = None
    source: str = "(unknown)"
    reader: str | None = None
    notes: tuple[str, ...] = field(default=())


def unmappable_nodes(type_: TypeRef) -> list[TypeRef]:
    """Nodes the runtime codecs degrade to ``any`` (see ``CodecBuilder.build``)."""
    out = []
    for node in type_.walk():
        if _ARITY.get(node.kind) != len(node.elements):
            out.append(node)
        elif node.kind == "primitive" and node.name not in _PRIMITIVE_TYPES and node.name not in ("any", "result"):
            out.append(node)
        elif node.kind == "map" and not (node.elements[0].kind == "primitive" and node.elements[0].name == "tstr"):
            out.append(node)
    return out


class _TypeSpeller:
    def __init__(self, names: Names, shadowed: frozenset[str]) -> None:
        self.names = names
        self.shadowed = shadowed

    def builtin(self, name: str) -> str:
        return f"_builtins.{name}" if name in self.shadowed else name

    def spell(self, type_: TypeRef, *, param: bool) -> str:
        kind = type_.kind
        if _ARITY.get(kind) != len(type_.elements):
            return "_typing.Any"
        if kind == "primitive":
            if type_.name == "result":
                return "_t.LogosResult[_typing.Any]"
            python = _PRIMITIVE_TYPES.get(type_.name)
            return self.builtin(python) if python else "_typing.Any"
        if kind == "named":
            return self.names.records[type_.name]
        if kind == "array":
            inner = self.spell(type_.elements[0], param=param)
            return f"_abc.Sequence[{inner}]" if param else f"{self.builtin('list')}[{inner}]"
        if kind == "map":
            key = type_.elements[0]
            if not (key.kind == "primitive" and key.name == "tstr"):
                return "_typing.Any"
            inner = self.spell(type_.elements[1], param=param)
            container = "_abc.Mapping" if param else self.builtin("dict")
            return f"{container}[{self.builtin('str')}, {inner}]"
        inner = self.spell(type_.value_type, param=param)
        return inner if inner == "_typing.Any" else f"{inner} | None"

    def slot(self, slot: Slot, *, param: bool) -> str:
        spelled = self.spell(slot.value_type, param=param)
        if slot.optional and spelled != "_typing.Any":
            return f"{spelled} | None"
        return spelled


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str = "", indent: int = 0) -> None:
        if not text:
            self.lines.append("")
            return
        for line in text.split("\n"):
            self.lines.append((" " * indent + line) if line else "")

    def blank(self, count: int = 1) -> None:
        for _ in range(count):
            self.add()

    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def _def(w: _Writer, indent: int, head: str, params: Sequence[str], returns: str) -> None:
    """``head(params) -> returns:``, one parameter per line when it does not fit."""
    line = f"{head}({', '.join(params)}) -> {returns}:"
    if indent + len(line) <= _WIDTH:
        w.add(line, indent)
        return
    w.add(f"{head}(", indent)
    for param in params:
        w.add(f"{param},", indent + 4)
    w.add(f") -> {returns}:", indent)


def _sequence(w: _Writer, head: str, opener: str, items: Sequence[str], closer: str) -> None:
    line = f"{head}{opener}{', '.join(items)}{',' if len(items) == 1 and opener == '(' else ''}{closer}"
    if len(line) <= _WIDTH:
        w.add(line)
        return
    w.add(f"{head}{opener}")
    for item in items:
        w.add(f"{item},", 4)
    w.add(closer)


def _return_call(w: _Writer, function: str, arguments: str) -> None:
    line = f"return {function}({arguments})"
    if 8 + len(line) <= _WIDTH:
        w.add(line, 8)
    else:
        w.add(f"return {function}(", 8)
        w.add(arguments, 12)
        w.add(")", 8)


def _lidl_type(slot: Slot) -> str:
    return slot.type.spell()


def _method_doc(module: str, method: Method, names: Names, interface: Interface) -> str:
    summary, body = summary_and_body(method.description)
    parts = [summary or f"Call ``{module}.{method.name}``."]
    if body:
        parts.append(rest_text(body))
    parts.append(f"LIDL: ``method {method.signature()}``" + (" (derived built-in)" if method.derived else ""))
    if method.params:
        lines = ["Args:"]
        for py, param in zip(names.params[method.name], method.params):
            optional = " (optional)" if param.optional else ""
            lines.append(f"    {py}: ``{_lidl_type(param)}``{optional}.")
        lines.append("    timeout: seconds to wait for the answer (default: the bridge client's).")
        parts.append("\n".join(lines))
    else:
        parts.append("Args:\n    timeout: seconds to wait for the answer (default: the bridge client's).")
    if method.returns is None:
        parts.append("Returns:\n    Nothing: the method has no return value (the provider answers ``true``).")
    elif method.returns.value_type.kind == "primitive" and method.returns.value_type.name == "result":
        parts.append("Returns:\n    The ``result``. ``success=False`` is an answer, not an exception;\n"
                     "    :meth:`LogosResult.unwrap` turns it into one.")
    else:
        parts.append(f"Returns:\n    ``{method.returns.type.spell()}``.")
    raises = [
        "Raises:",
        "    ArgumentError: an argument does not match the contract (nothing is sent).",
        "    ProviderRejection: the provider refused the call.",
    ]
    if method.returns is not None and rejection_ambiguous(method.returns.type, interface):
        raises.append("        This return type can hold a value shaped like a refusal; such a value is")
        raises.append("        reported as a rejection too.")
    raises.append("    ResultDecodeError: the result does not match the contract.")
    raises.append("    MethodNotFound: this bridge does not expose the method.")
    parts.append("\n".join(raises))
    return "\n\n".join(parts)


def _record_doc(record: Record, attrs: dict[str, str]) -> str:
    lines = [f"LIDL record ``{record.name}``.", "", "Attributes:"]
    for fd in record.fields:
        optional = " (optional; omitted from the wire when ``None``)" if fd.optional else ""
        lines.append(f"    {attrs[fd.name]}: ``{fd.spell()}``{optional}")
    return "\n".join(lines)


def _event_doc(event: Event, attrs: dict[str, str]) -> str:
    summary, body = summary_and_body(event.description)
    parts = [f"The ``{event.name}`` event" + (f": {summary}" if summary else ".")]
    if body:
        parts.append(rest_text(body))
    parts.append(f"LIDL: ``event {event.signature()}``")
    lines = ["Attributes:"]
    for param in event.params:
        lines.append(f"    {attrs[param.name]}: ``{_lidl_type(param)}``")
    lines.append("    meta: the raw event (subscription, generation, ts, data).")
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _class_members(names: Names, interface: Interface, *, sync: bool) -> frozenset[str]:
    members = set(names.methods.values()) | {"events", "check_compat", "bridge", "module"}
    for event in interface.events:
        members |= {names.subscriber(event.name), names.decoder(event.name)}
    if sync:
        members |= {"aio", "portal"}
    return frozenset(members)


def generate_python(contract: Contract, names: Names, *, regen_hint: str | None = None,
                    allow_unknown_types: bool = False) -> str:
    """The module's text. Raises :class:`CodegenError`."""
    iface = contract.interface
    unmappable = sorted({node.spell() for _, slot in iface.slots() for node in unmappable_nodes(slot.type)})
    if unmappable and not allow_unknown_types:
        raise CodegenError(f"the contract uses types this generator cannot map: {', '.join(unmappable)} "
                           "(pass --allow-unknown-types to generate them as Any)")
    shape = iface.shape()  # declaration order; the digest canonicalizes it
    w = _Writer()
    version = iface.version or "(no version)"
    w.add("# GENERATED by logos-bridge-codegen. Do not edit: regenerate instead.")
    w.add(f"# Contract: {iface.name} {version}")
    w.add(f"# INTERFACE_SHA256: {iface.interface_sha256()}")
    w.add(f"# CONTRACT_SHA256: {contract.contract_sha256 or 'unknown (the source had no contract text)'}")
    w.add(f"# SHAPE_SHA256: {shape_sha256(shape)}")
    w.add(f"# Codegen format: {CODEGEN_FORMAT}")
    w.add(f"# Regenerate: {regen_hint or DEFAULT_REGEN_HINT}")
    if unmappable:
        w.add(f"# Typed as Any (unknown to this generator): {', '.join(unmappable)}")
    w.add(f"# Source: {contract.source}")
    w.add(f"# Generator: logos-bridge {__version__}" + (f"; reader: {contract.reader}" if contract.reader else ""))
    base, aio, sync = names.base, names.async_client, names.sync_client
    module_doc = [f"Typed client for the ``{iface.name}`` module (LIDL contract {version}).", ""]
    if iface.description:
        module_doc += [rest_text(iface.description), ""]
    module_doc.append(
        f"Clients: :class:`{aio}` (asyncio, on a connected\n"
        f":class:`logos_bridge.AsyncBridgeClient`) and :class:`{sync}` (blocking, on a\n"
        ":class:`logos_bridge.BridgeClient`). Every call goes through ``rpc.call``.\n"
        "Run ``check_compat()`` after connecting, and again after a subscription ends\n"
        "with ``provider_changed``."
    )
    w.add(docstring("\n".join(module_doc), 0))
    w.blank()
    w.add("from __future__ import annotations")
    w.blank()
    w.add("import builtins as _builtins")
    w.add("import dataclasses as _dataclasses")
    w.add("import typing as _typing")
    w.add("from collections import abc as _abc")
    w.blank()
    w.add("from logos_bridge import typed as _t")
    w.add("from logos_bridge.client import AsyncBridgeClient as _AsyncBridgeClient")
    w.add("from logos_bridge.compat import CompatReport as _CompatReport")
    w.add("from logos_bridge.models import Event as _Event")
    w.add("from logos_bridge.portal import BlockingPortal as _BlockingPortal")
    w.add("from logos_bridge.sync import BridgeClient as _BridgeClient")
    w.blank()
    w.add(f"_t.require_codegen_format({CODEGEN_FORMAT})")
    w.blank()
    w.add(f"MODULE_NAME: _typing.Final = {py_str(names.module_default)}")
    w.add(f"INTERFACE_SHA256: _typing.Final = {py_str(iface.interface_sha256())}")
    contract_literal = py_str(contract.contract_sha256) if contract.contract_sha256 else "None"
    if contract.contract_sha256:
        w.add("CONTRACT_SHA256: _typing.Final[str | None] = (")
        w.add(contract_literal, 4)
        w.add(")")
    else:
        w.add(f"CONTRACT_SHA256: _typing.Final[str | None] = {contract_literal}")
    w.add(f"SHAPE_SHA256: _typing.Final = {py_str(shape_sha256(shape))}")
    w.blank()
    w.add("# The contract's shape (see logos_bridge.lidl.Interface.shape), checked at import.")
    prefix = "INTERFACE: _typing.Final[_abc.Mapping[str, _typing.Any]] = "
    w.add(prefix + py_literal(shape, 0, len(prefix), _WIDTH))

    for record in iface.types:
        attrs = names.fields[record.name]
        speller = _TypeSpeller(names, frozenset(attrs.values()) & _BUILTIN_TYPES)
        w.blank(2)
        w.add("@_dataclasses.dataclass(frozen=True, slots=True, kw_only=True)")
        w.add(f"class {names.records[record.name]}:")
        w.add(docstring(_record_doc(record, attrs), 4))
        if record.fields:
            w.blank()
        for fd in record.fields:
            default = "default=None, " if fd.optional else ""
            w.add(f"{attrs[fd.name]}: {speller.slot(fd, param=False)} = "
                  f"_dataclasses.field({default}metadata={{{py_str('lidl')}: {py_str(fd.name)}}})", 4)

    for event in iface.events:
        attrs = names.event_fields[event.name]
        speller = _TypeSpeller(names, (frozenset(attrs.values()) | {"meta"}) & _BUILTIN_TYPES)
        w.blank(2)
        w.add("@_dataclasses.dataclass(frozen=True, slots=True, kw_only=True)")
        w.add(f"class {names.event_classes[event.name]}:")
        w.add(docstring(_event_doc(event, attrs), 4))
        w.blank()
        for param in event.params:
            w.add(f"{attrs[param.name]}: {speller.slot(param, param=False)} = "
                  f"_dataclasses.field(metadata={{{py_str('lidl')}: {py_str(param.name)}}})", 4)
        w.add("meta: _Event", 4)

    w.blank(2)
    classes = [names.event_classes[e.name] for e in iface.events]
    event_names = [e.name for e in iface.events]
    if not classes:
        w.add(f"{names.event_union}: _typing.TypeAlias = _typing.NoReturn")
        w.add(f"{names.event_literal}: _typing.TypeAlias = _typing.NoReturn")
        w.add("EVENT_NAMES: _typing.Final[tuple[str, ...]] = ()")
    else:
        if len(classes) == 1:
            w.add(f"{names.event_union}: _typing.TypeAlias = {classes[0]}")
        else:
            _sequence(w, f"{names.event_union}: _typing.TypeAlias = _typing.Union", "[", classes, "]")
        quoted = [py_str(e) for e in event_names]
        _sequence(w, f"{names.event_literal}: _typing.TypeAlias = _typing.Literal", "[", quoted, "]")
        _sequence(w, f"EVENT_NAMES: _typing.Final[tuple[{names.event_literal}, ...]] = ", "(", quoted, ")")
    for label, table in (("RECORD_TYPES", {r.name: RawCode(names.records[r.name]) for r in iface.types}),
                         ("EVENT_TYPES", {e.name: RawCode(names.event_classes[e.name]) for e in iface.events})):
        head = f"{label}: _typing.Final[_abc.Mapping[str, type]] = "
        w.add(head + py_literal(table, 0, len(head), _WIDTH))
    w.blank()
    w.add("_BINDING: _typing.Final = _t.Binding(")
    w.add("INTERFACE, SHAPE_SHA256, INTERFACE_SHA256, records=RECORD_TYPES, events=EVENT_TYPES", 4)
    w.add(")")

    _write_async_client(w, contract, names)
    _write_sync_client(w, contract, names)
    text = w.text()
    try:
        compile(text, f"<generated {iface.name}>", "exec")
    except SyntaxError as exc:  # a generator bug, not a contract problem
        raise CodegenError(f"generated code does not compile: {exc}", 70) from None
    return text


def _signature(method: Method, names: Names, speller: _TypeSpeller) -> tuple[list[str], str]:
    params = ["self"]
    for index, (py, param) in enumerate(zip(names.params[method.name], method.params)):
        default = " = None" if index >= method.min_args else ""
        params.append(f"{py}: {speller.slot(param, param=True)}{default}")
    params += ["*", f"timeout: {speller.builtin('float')} | None = None"]
    returns = "None" if method.returns is None else speller.slot(method.returns, param=False)
    return params, returns


def _options(speller: _TypeSpeller) -> list[str]:
    return [f"timeout: {speller.builtin('float')} | None = None",
            f"max_pending: {speller.builtin('int')} | None = None"]


def _compat_params(speller: _TypeSpeller) -> list[str]:
    return ["self", "*", f"allow_untyped: {speller.builtin('bool')} = False",
            f"discovery_wait: {speller.builtin('float')} | None = 10.0"]


def _args_tuple(values: Sequence[str]) -> str:
    if not values:
        return "()"
    return "(" + ", ".join(values) + ("," if len(values) == 1 else "") + ")"


def _write_async_client(w: _Writer, contract: Contract, names: Names) -> None:
    iface = contract.interface
    speller = _TypeSpeller(names, _class_members(names, iface, sync=False) & _BUILTIN_TYPES)
    prop = f"@{speller.builtin('property')}"
    w.blank(2)
    w.add(f"class {names.async_client}:")
    w.add(docstring(
        f"``{iface.name}`` over an :class:`logos_bridge.AsyncBridgeClient`.\n\n"
        f"``module`` is the bridge module name to call (default ``{names.module_default}``).", 4))
    w.blank()
    w.add(f"def __init__(self, bridge: _AsyncBridgeClient, module: {speller.builtin('str')} = MODULE_NAME) "
          "-> None:", 4)
    w.add("self._bridge = bridge", 8)
    w.add("self._module = module", 8)
    w.blank()
    w.add(f"def __repr__(self) -> {speller.builtin('str')}:", 4)
    w.add(f'return f"<{names.async_client} {{self._module}} on {{self._bridge.url}}>"', 8)
    w.blank()
    w.add(prop, 4)
    w.add("def bridge(self) -> _AsyncBridgeClient:", 4)
    w.add("return self._bridge", 8)
    w.blank()
    w.add(prop, 4)
    w.add(f"def module(self) -> {speller.builtin('str')}:", 4)
    w.add("return self._module", 8)
    for method in iface.methods:
        py = names.methods[method.name]
        w.blank()
        params, returns = _signature(method, names, speller)
        _def(w, 4, f"async def {py}", params, returns)
        w.add(docstring(_method_doc(iface.name, method, names, iface), 8))
        arguments = (f"self._bridge, self._module, {py_str(method.name)}, "
                     f"{_args_tuple(names.params[method.name])}, timeout")
        lead = "" if method.returns is None else ("return " if returns == "_typing.Any" else "result = ")
        line = f"{lead}await _BINDING.call({arguments})"
        if 8 + len(line) <= _WIDTH:
            w.add(line, 8)
        else:
            w.add(f"{lead}await _BINDING.call(", 8)
            w.add(arguments, 12)
            w.add(")", 8)
        if method.returns is not None and returns != "_typing.Any":
            w.add(f"return _typing.cast({py_str(returns)}, result)", 8)
    union, literal = names.event_union, names.event_literal
    for event in iface.events:
        cls = names.event_classes[event.name]
        w.blank()
        _def(w, 4, f"def {names.subscriber(event.name)}", ["self", "*", *_options(speller)],
             f"_t.TypedSubscribeRequest[{cls}]")
        w.add(docstring(f"Subscribe to ``{event.name}``: ``await`` it, or use ``async with``.\n\n"
                        f"Items are :class:`{cls}`; one that does not match the contract raises\n"
                        ":class:`logos_bridge.EventDecodeError` (``results()`` yields it instead).", 8))
        _return_call(w, "_BINDING.subscribe", f"self._bridge, self._module, ({py_str(event.name)},), timeout, "
                     "max_pending")
        w.blank()
        w.add(f"def {names.decoder(event.name)}(self, event: _Event) -> {cls}:", 4)
        w.add(docstring(f"Decode a raw ``{event.name}`` event (``EventDecodeError`` if it does not match).", 8))
        w.add(f"decoded = _BINDING.decode_event({py_str(event.name)}, event)", 8)
        w.add(f"return _typing.cast({py_str(cls)}, decoded)", 8)
    w.blank()
    _def(w, 4, "def events", ["self", f"*names: {literal}", *_options(speller)], f"_t.TypedSubscribeRequest[{union}]")
    w.add(docstring("One ordered stream of the named events (every event when none are named).", 8))
    w.add("return _BINDING.subscribe(self._bridge, self._module, names, timeout, max_pending)", 8)
    w.blank()
    _def(w, 4, "async def check_compat", _compat_params(speller), "_CompatReport")
    w.add(docstring(
        "Compare the served contract with this client's (exact, shape, structural, or names).\n\n"
        "Waits out ``pending``; raises :class:`logos_bridge.IncompatibleModule` (or\n"
        ":class:`logos_bridge.UntypedModule` without ``allow_untyped``) when no level applies.\n"
        "Use ``report.require(methods=..., events=...)`` for the members you need.", 8))
    w.add("return await _BINDING.check_compat(", 8)
    w.add("self._bridge, self._module, allow_untyped=allow_untyped, discovery_wait=discovery_wait", 12)
    w.add(")", 8)


def _write_sync_client(w: _Writer, contract: Contract, names: Names) -> None:
    iface = contract.interface
    speller = _TypeSpeller(names, _class_members(names, iface, sync=True) & _BUILTIN_TYPES)
    prop = f"@{speller.builtin('property')}"
    aio, sync = names.async_client, names.sync_client
    w.blank(2)
    w.add(f"class {sync}:")
    w.add(docstring(f"The blocking twin of :class:`{aio}`, over a :class:`logos_bridge.BridgeClient`.", 4))
    w.blank()
    w.add(f"def __init__(self, bridge: _BridgeClient, module: {speller.builtin('str')} = MODULE_NAME) -> None:", 4)
    w.add("self._bridge = bridge", 8)
    w.add("self._portal = bridge.portal", 8)
    w.add(f"self._aio = {aio}(bridge.aio, module)", 8)
    w.blank()
    w.add(f"def __repr__(self) -> {speller.builtin('str')}:", 4)
    w.add(f'return f"<{sync} {{self._aio.module}} on {{self._bridge.url}}>"', 8)
    for name, annotation, value in (("aio", aio, "self._aio"), ("bridge", "_BridgeClient", "self._bridge"),
                                    ("module", speller.builtin("str"), "self._aio.module"),
                                    ("portal", "_BlockingPortal", "self._portal")):
        w.blank()
        w.add(prop, 4)
        w.add(f"def {name}(self) -> {annotation}:", 4)
        w.add(f"return {value}", 8)
    for method in iface.methods:
        py = names.methods[method.name]
        w.blank()
        params, returns = _signature(method, names, speller)
        _def(w, 4, f"def {py}", params, returns)
        w.add(docstring(_method_doc(iface.name, method, names, iface), 8))
        args = "".join(f", {p}" for p in names.params[method.name])
        call = f"self._portal.call(self._aio.{py}{args}, timeout=timeout)"
        line = call if method.returns is None else f"return {call}"
        if 8 + len(line) <= _WIDTH:
            w.add(line, 8)
        else:
            w.add(("" if method.returns is None else "return ") + "self._portal.call(", 8)
            w.add(f"self._aio.{py}{args}, timeout=timeout", 12)
            w.add(")", 8)
    union, literal = names.event_union, names.event_literal
    for event in iface.events:
        cls = names.event_classes[event.name]
        sub, dec = names.subscriber(event.name), names.decoder(event.name)
        w.blank()
        _def(w, 4, f"def {sub}", ["self", "*", *_options(speller)], f"_t.BlockingTypedSubscription[{cls}]")
        w.add(docstring(f"Subscribe to ``{event.name}``; iterate the result, and close it (or use ``with``).", 8))
        w.add(f"request = self._aio.{sub}(timeout=timeout, max_pending=max_pending)", 8)
        w.add("return _t.BlockingTypedSubscription.open(request, self._portal)", 8)
        w.blank()
        w.add(f"def {dec}(self, event: _Event) -> {cls}:", 4)
        w.add(docstring(f"Decode a raw ``{event.name}`` event.", 8))
        w.add(f"return self._aio.{dec}(event)", 8)
    w.blank()
    _def(w, 4, "def events", ["self", f"*names: {literal}", *_options(speller)],
         f"_t.BlockingTypedSubscription[{union}]")
    w.add(docstring("One ordered, blocking stream of the named events (every event when none are named).", 8))
    w.add("request = self._aio.events(*names, timeout=timeout, max_pending=max_pending)", 8)
    w.add("return _t.BlockingTypedSubscription.open(request, self._portal)", 8)
    w.blank()
    _def(w, 4, "def check_compat", _compat_params(speller), "_CompatReport")
    w.add(docstring(f"Blocking :meth:`{aio}.check_compat`.", 8))
    w.add("return self._portal.call(", 8)
    w.add("self._aio.check_compat, allow_untyped=allow_untyped, discovery_wait=discovery_wait", 12)
    w.add(")", 8)


def strip_provenance(text: str, prefixes: Sequence[str] = PYTHON_PROVENANCE) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith(tuple(prefixes)))
