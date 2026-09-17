"""Python names for a contract's members.

``snake``/``pascal`` are logos-rust-sdk's ``rustgen.rs`` rules. A name that is a
Python keyword (a frozen list, so output does not depend on the running Python),
or one the generated code already uses, gets a trailing ``_``; one that starts
with ``__`` (name mangling, dataclass internals) gets a ``lidl`` prefix. Two
members that end up with the same name are an error: rename one with
``--rename KIND:NAME=PYTHON_NAME``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from ..lidl import Interface

# Python 3.13's keyword.kwlist, frozen: generated code must not vary with the interpreter.
PY_KEYWORDS: Final = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in",
    "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
})

#: Members of the generated clients besides the contract's methods, with the private
#: attributes an instance sets (they would hide a method of the same name).
CLIENT_MEMBERS: Final = frozenset({
    "bridge", "module", "events", "check_compat", "aio", "portal", "_bridge", "_module", "_portal", "_aio",
})
#: Keyword-only parameters of every generated method.
METHOD_PARAMS: Final = frozenset({"self", "timeout"})
#: Module-level constants of a generated module.
MODULE_CONSTANTS: Final = frozenset({
    "MODULE_NAME", "INTERFACE", "INTERFACE_SHA256", "CONTRACT_SHA256", "SHAPE_SHA256", "EVENT_NAMES",
    "RECORD_TYPES", "EVENT_TYPES",
})
RENAME_KINDS: Final = ("method", "event", "type", "field", "param", "eparam")

_IDENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class CodegenError(Exception):
    """Generation failed. ``exit_code`` is the CLI's (2 usage, 4 contract, 70 internal)."""

    def __init__(self, message: str, exit_code: int = 4) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def pascal(name: str) -> str:
    """``storage_module`` -> ``StorageModule``; ``storageStart`` -> ``StorageStart``."""
    return "".join(part[0].upper() + part[1:] for part in name.split("_") if part)


def snake(name: str) -> str:
    """``echoBlobMap`` -> ``echo_blob_map``; ``whoAmI`` -> ``who_am_i``."""
    out = []
    for index, char in enumerate(name):
        if char.isupper():
            if index > 0:
                out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def escape(name: str, reserved: frozenset[str] = frozenset()) -> str:
    """A usable identifier: keywords and ``reserved`` get ``_``, ``__x`` gets ``lidl``."""
    if _IDENT.fullmatch(name) is None:  # e.g. pascal("_1x") == "1x"
        return "lidl_" + name
    if name.startswith("__"):
        return "lidl" + name
    if name in PY_KEYWORDS or name in reserved:
        return name + "_"
    return name


@dataclass(frozen=True)
class Rename:
    """``--rename KIND:NAME=PYTHON_NAME``; ``NAME`` is ``Owner.member`` for fields and parameters."""

    kind: str
    target: str
    name: str

    @classmethod
    def parse(cls, text: str) -> Rename:
        kind, sep, rest = text.partition(":")
        target, eq, name = rest.partition("=")
        if not sep or not eq or kind not in RENAME_KINDS or not target:
            raise CodegenError(
                f"--rename {text!r}: expected KIND:NAME=PYTHON_NAME with KIND one of {', '.join(RENAME_KINDS)}", 2)
        if kind in ("field", "param", "eparam") and target.count(".") != 1:
            raise CodegenError(f"--rename {text!r}: a {kind} is named Owner.member", 2)
        if _IDENT.fullmatch(name) is None or name in PY_KEYWORDS:
            raise CodegenError(f"--rename {text!r}: {name!r} is not a usable Python identifier", 2)
        return cls(kind, target, name)


@dataclass
class Names:
    """Every Python name the generators use, keyed by the contract's names."""

    base: str
    module_default: str
    async_client: str
    sync_client: str
    event_union: str
    event_literal: str
    records: dict[str, str] = field(default_factory=dict)
    fields: dict[str, dict[str, str]] = field(default_factory=dict)
    methods: dict[str, str] = field(default_factory=dict)
    params: dict[str, list[str]] = field(default_factory=dict)
    events: dict[str, str] = field(default_factory=dict)
    event_classes: dict[str, str] = field(default_factory=dict)
    event_fields: dict[str, dict[str, str]] = field(default_factory=dict)

    def subscriber(self, event: str) -> str:
        return f"on_{self.events[event]}"

    def decoder(self, event: str) -> str:
        return f"decode_{self.events[event]}"


class _Scope:
    def __init__(self, what: str) -> None:
        self.what = what
        self.taken: dict[str, str] = {}
        self.problems: list[str] = []

    def claim(self, name: str, owner: str, hint: str) -> None:
        other = self.taken.get(name)
        if other is not None:
            self.problems.append(f"{self.what}: {other} and {owner} both become {name!r} ({hint})")
        else:
            self.taken[name] = owner


def plan_names(interface: Interface, *, class_name: str | None = None, module_name: str | None = None,
               renames: tuple[Rename, ...] = ()) -> Names:
    """Name every member, or raise :class:`CodegenError` listing every collision."""
    wanted: dict[tuple[str, str], str] = {}
    for rename in renames:
        wanted[(rename.kind, rename.target)] = rename.name
    used: set[tuple[str, str]] = set()

    def pick(kind: str, target: str, default: str) -> str:
        key = (kind, target)
        if key in wanted:
            used.add(key)
            return wanted[key]
        return default

    base = class_name or pascal(interface.name)
    if _IDENT.fullmatch(base) is None or base in PY_KEYWORDS:
        raise CodegenError(f"cannot derive a class name from module {interface.name!r}; pass --class-name", 2)
    names = Names(
        base=base,
        module_default=module_name or interface.name,
        async_client=f"Async{base}Client",
        sync_client=f"{base}Client",
        event_union=f"{base}Event",
        event_literal=f"{base}EventName",
    )
    problems: list[str] = []
    module_scope = _Scope("module-level names")
    for constant in sorted(MODULE_CONSTANTS):
        module_scope.claim(constant, f"the constant {constant}", "reserved")
    for generated in (names.async_client, names.sync_client, names.event_union, names.event_literal):
        module_scope.claim(generated, f"the generated {generated}", "--class-name")

    for record in interface.types:
        py = pick("type", record.name, escape(pascal(record.name) or record.name))
        names.records[record.name] = py
        module_scope.claim(py, f"record {record.name}", f"--rename type:{record.name}=NAME")
        scope = _Scope(f"fields of record {record.name}")
        attrs: dict[str, str] = {}
        for fd in record.fields:
            attr = pick("field", f"{record.name}.{fd.name}", escape(snake(fd.name)))
            attrs[fd.name] = attr
            scope.claim(attr, f"field {fd.name}", f"--rename field:{record.name}.{fd.name}=NAME")
        names.fields[record.name] = attrs
        problems += scope.problems

    client_scope = _Scope("client members")
    for member in sorted(CLIENT_MEMBERS):
        client_scope.claim(member, f"the client's {member}", "reserved")
    for method in interface.methods:
        py = pick("method", method.name, escape(snake(method.name), CLIENT_MEMBERS))
        names.methods[method.name] = py
        client_scope.claim(py, f"method {method.name}", f"--rename method:{method.name}=NAME")
        scope = _Scope(f"parameters of method {method.name}")
        for member in sorted(METHOD_PARAMS):
            scope.claim(member, f"the generated {member}", "reserved")
        params = []
        for param in method.params:
            py_param = pick("param", f"{method.name}.{param.name}", escape(snake(param.name), METHOD_PARAMS))
            params.append(py_param)
            scope.claim(py_param, f"parameter {param.name}", f"--rename param:{method.name}.{param.name}=NAME")
        names.params[method.name] = params
        problems += scope.problems

    for event in interface.events:
        stem = pick("event", event.name, snake(event.name))
        names.events[event.name] = stem
        hint = f"--rename event:{event.name}=NAME"
        client_scope.claim(f"on_{stem}", f"event {event.name}'s subscriber", hint)
        client_scope.claim(f"decode_{stem}", f"event {event.name}'s decoder", hint)
        cls = escape(pascal(stem) + "Event")
        names.event_classes[event.name] = cls
        module_scope.claim(cls, f"event {event.name}'s class", hint)
        scope = _Scope(f"parameters of event {event.name}")
        scope.claim("meta", "the event's meta", "reserved")
        attrs = {}
        for param in event.params:
            attr = pick("eparam", f"{event.name}.{param.name}", escape(snake(param.name), frozenset({"meta"})))
            attrs[param.name] = attr
            scope.claim(attr, f"parameter {param.name}", f"--rename eparam:{event.name}.{param.name}=NAME")
        names.event_fields[event.name] = attrs
        problems += scope.problems

    unused = sorted(f"{k}:{t}" for k, t in wanted if (k, t) not in used)
    if unused:
        raise CodegenError(f"--rename names nothing in the contract: {', '.join(unused)}", 2)
    problems = module_scope.problems + client_scope.problems + problems
    if problems:
        raise CodegenError("Python names collide:\n" + "\n".join(f"  {p}" for p in problems))
    return names
