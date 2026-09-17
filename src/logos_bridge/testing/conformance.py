"""Reading logos-test-modules' LIDL conformance tables (``conformance/*.json``).

A table has ``cases`` (a method call and its expectation) and ``events`` (a fired
event and its payload). Bytes are ``{"_bytes": ...}``; ``"__ALL_BYTES__"`` stands
for all 256 byte values; ``raw: true`` means tags are data, not bytes. A rejection
is ``{"__error__": "<class>"}``. Unknown keys are refused, as the table's own
driver (``logos-logoscore-py/conformance/run_matrix.py``) does.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ..codec import BYTES_KEY, decode_bytes_tag, is_bytes_tag

ALL_BYTES: Final = "__ALL_BYTES__"
ERROR_KEY: Final = "__error__"
CASE_KEYS: Final = frozenset({
    "id", "type", "position", "method", "args", "expect", "expect_by_provider", "raw", "module",
    "isolate", "timeout_ms", "tags", "why", "cells",
})
EVENT_KEYS: Final = frozenset({"id", "type", "position", "event", "fire", "value", "values", "cells", "why"})
TABLE_KEYS: Final = frozenset({"schema", "contract", "comment", "providers", "cases", "events"})

#: The README's failure classes, by the code a case expects.
FAILURE_CLASSES: Final[Mapping[str, str]] = {
    "MODULE_NOT_LOADED": "A",
    "invalid_args": "B",
    "METHOD_NOT_FOUND": "C",
    "dispatch_failed": "E",
}

_ABSENT: Any = object()


class ConformanceTableError(ValueError):
    """The table does not have the shape its driver reads."""


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        if is_bytes_tag(value) and value[BYTES_KEY] == ALL_BYTES:
            return {BYTES_KEY: base64.urlsafe_b64encode(bytes(range(256))).rstrip(b"=").decode("ascii")}
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def wire(value: Any) -> Any:
    """``value`` as JSON on the wire: tags stay tags, ``__ALL_BYTES__`` expanded."""
    return _expand(value)


def materialize(value: Any, *, raw: bool = False) -> Any:
    """``value`` as a Python caller holds it: tags become ``bytes`` unless ``raw``."""
    expanded = _expand(value)
    return expanded if raw else _to_bytes(expanded)


def _to_bytes(value: Any) -> Any:
    if isinstance(value, dict):
        if is_bytes_tag(value):
            return decode_bytes_tag(value)
        return {k: _to_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_bytes(v) for v in value]
    return value


@dataclass(frozen=True)
class ConformanceCase:
    """One method case."""

    id: str
    type: str
    position: str
    method: str
    args: list[Any]
    expect: Any = _ABSENT
    expect_by_provider: Mapping[str, Any] | None = None
    raw: bool = False
    module: str | None = None
    isolate: bool = False
    timeout_ms: int | None = None
    tags: tuple[str, ...] = ()
    why: str = ""
    cells: tuple[tuple[str, str], ...] = ()
    source: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def expected_error(self) -> str | None:
        """The failure code a rejection case names (``dispatch_failed``, ...)."""
        values = [self.expect] if self.expect is not _ABSENT else list((self.expect_by_provider or {}).values())
        for value in values:
            if isinstance(value, dict) and set(value) == {ERROR_KEY}:
                code: str = value[ERROR_KEY]
                return code
        return None

    @property
    def failure_class(self) -> str | None:
        code = self.expected_error
        return FAILURE_CLASSES.get(code) if code is not None else None

    def expectations(self) -> dict[str | None, Any]:
        """``{provider: expected}``; the key is ``None`` for a shared expectation."""
        if self.expect_by_provider is not None:
            return {provider: value for provider, value in self.expect_by_provider.items()}
        if self.expect is _ABSENT:
            raise ConformanceTableError(f"case {self.id} has no expectation")
        return {None: self.expect}

    def call_args(self) -> list[Any]:
        """The arguments as a Python caller passes them."""
        result: list[Any] = materialize(self.args, raw=self.raw)
        return result

    def wire_args(self) -> list[Any]:
        result: list[Any] = wire(self.args)
        return result


@dataclass(frozen=True)
class ConformanceEvent:
    """One event case: firing ``fire`` emits ``event`` with ``values``."""

    id: str
    type: str
    position: str
    event: str
    fire: str
    values: list[Any]
    cells: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConformanceTable:
    contract: str
    providers: tuple[str, ...]
    cases: tuple[ConformanceCase, ...]
    events: tuple[ConformanceEvent, ...]
    schema: int = 1
    comment: str = ""
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> ConformanceTable:
        location = Path(path)
        doc = json.loads(location.read_bytes())
        return cls.from_json(doc, location)

    @classmethod
    def from_json(cls, doc: Any, path: Path | None = None) -> ConformanceTable:
        if not isinstance(doc, dict):
            raise ConformanceTableError("a conformance table is a JSON object")
        unknown = set(doc) - TABLE_KEYS
        if unknown:
            raise ConformanceTableError(f"unknown table keys: {sorted(unknown)}")
        if doc.get("schema") != 1:
            raise ConformanceTableError(f"unsupported table schema {doc.get('schema')!r}")
        cases = []
        for raw in doc.get("cases", []):
            extra = set(raw) - CASE_KEYS
            if extra:
                raise ConformanceTableError(f"case {raw.get('id')}: keys no driver reads: {sorted(extra)}")
            if "expect" not in raw and "expect_by_provider" not in raw:
                raise ConformanceTableError(f"case {raw.get('id')} has no expectation")
            cases.append(ConformanceCase(
                id=raw["id"], type=raw["type"], position=raw["position"], method=raw["method"],
                args=list(raw.get("args", [])), expect=raw.get("expect", _ABSENT),
                expect_by_provider=raw.get("expect_by_provider"), raw=bool(raw.get("raw", False)),
                module=raw.get("module"), isolate=bool(raw.get("isolate", False)),
                timeout_ms=raw.get("timeout_ms"), tags=tuple(raw.get("tags", ())), why=raw.get("why", ""),
                cells=tuple(tuple(c) for c in raw.get("cells", ())), source=raw,
            ))
        events = []
        for raw in doc.get("events", []):
            extra = set(raw) - EVENT_KEYS
            if extra:
                raise ConformanceTableError(f"event {raw.get('id')}: keys no driver reads: {sorted(extra)}")
            values = raw["values"] if "values" in raw else [raw["value"]]
            events.append(ConformanceEvent(
                id=raw["id"], type=raw["type"], position=raw["position"], event=raw["event"],
                fire=raw["fire"], values=list(values), cells=tuple(tuple(c) for c in raw.get("cells", ())),
            ))
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ConformanceTableError("duplicate case ids")
        comment = doc.get("comment", "")
        return cls(
            contract=doc.get("contract", ""),
            providers=tuple(doc.get("providers", ())),
            cases=tuple(cases),
            events=tuple(events),
            schema=doc["schema"],
            comment="\n".join(comment) if isinstance(comment, list) else str(comment),
            path=path,
        )

    def case(self, case_id: str) -> ConformanceCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)
