"""Per-slot codecs between Python values and the bridge's JSON.

Encoding and decoding are strict (see the table in the README); ``lenient=True``
decodes as a C++ provider does (logos-protocol ``logos_codec.h``), which is what
:class:`~logos_bridge.testing.FakeProvider` needs. Errors read like a provider's
``dispatch_failed``: ``expected integer at arg1[3].n, got string``.
"""

from __future__ import annotations

import base64
import dataclasses
import math
import re
import typing
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ..codec import BYTES_KEY, decode_bytes_tag, encode_bytes_tag, is_bytes_tag
from ..errors import BytesDecodeError
from ..lidl import Interface, TypeRef, json_type_name
from ._result import LogosResult

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
UINT64_MAX: Final = 2**64 - 1
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]*")
_ARITY: Final[Mapping[str, int]] = {"primitive": 0, "named": 0, "array": 1, "map": 2, "optional": 1}


class CodecError(ValueError):
    """A value that does not match its declared type, with the provider-style path."""

    def __init__(self, expected: str, path: str, got: str, detail: str | None = None) -> None:
        self.expected = expected
        self.path = path
        self.got = got
        self.detail = detail
        text = f"expected {expected} at {path or 'value'}, got {got}"
        super().__init__(text + (f" ({detail})" if detail else ""))


def python_type_name(value: Any) -> str:
    """The JSON type a Python value would become, or its class name when none fits."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bytes"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _plain_str(value: str) -> str:
    return value if type(value) is str else str.__str__(value)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview))


def encode_json_value(value: Any, path: str) -> Any:
    """``any``: a JSON tree; bytes-like values become tags, tuples lists."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _plain_str(value)
    if isinstance(value, int):
        if not INT64_MIN <= value <= UINT64_MAX:
            raise CodecError("integer in range", path, "number")
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("finite number", path, "number")
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return encode_bytes_tag(value)
    if isinstance(value, Mapping):
        if BYTES_KEY in value:
            tag = value[BYTES_KEY]
            if len(value) == 1 and isinstance(tag, str):
                if _B64URL.fullmatch(tag) is None or len(tag) % 4 == 1:
                    raise CodecError("unpadded base64url", f"{path}.{BYTES_KEY}", "string")
                return {BYTES_KEY: _plain_str(tag)}  # a pre-encoded tag
            raise CodecError('an object without the reserved "_bytes" key', path, "object")
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CodecError("string keys", path, f"a {python_type_name(key)} key")
            out[_plain_str(key)] = encode_json_value(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [encode_json_value(item, f"{path}[{i}]") for i, item in enumerate(value)]
    raise CodecError("a JSON value", path, python_type_name(value))


def _lenient_b64(text: str) -> bytes:
    # logos_codec.h b64UrlDecode: characters outside the alphabet are skipped.
    body = "".join(ch for ch in text if ch.isascii() and (ch.isalnum() or ch in "-_"))
    body = body[: len(body) - (len(body) % 4 == 1)]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def _number_text(value: int | float) -> str:
    return str(value) if isinstance(value, int) else repr(value)  # nlohmann's dump, closely enough


class Codec:
    """One LIDL type. ``expected`` is the noun a provider uses for it in errors."""

    lidl: str = "any"
    expected: str = "value"
    accepts_null: bool = False

    def encode(self, value: Any, path: str) -> Any:
        raise NotImplementedError

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        raise NotImplementedError

    def annotation(self) -> Any:
        """The Python type this codec produces (for signatures)."""
        return Any

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.lidl}>"


class StrCodec(Codec):
    lidl = "tstr"
    expected = "string"

    def encode(self, value: Any, path: str) -> Any:
        if isinstance(value, str):
            return _plain_str(value)
        raise CodecError("string", path, python_type_name(value))

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if isinstance(value, str):
            return value
        raise CodecError("string", path, json_type_name(value))

    def annotation(self) -> Any:
        return str


class BytesCodec(Codec):
    lidl = "bstr"
    expected = "bytes"

    def encode(self, value: Any, path: str) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return encode_bytes_tag(value)
        raise CodecError("bytes", path, python_type_name(value))

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if is_bytes_tag(value):
            try:
                return decode_bytes_tag(value)
            except BytesDecodeError as exc:
                if lenient:
                    return _lenient_b64(value[BYTES_KEY])
                raise CodecError("bytes", path, "object", exc.reason) from None
        if lenient:  # logos_codec.h bytesFromJsonLenient
            if isinstance(value, str):
                return value.encode("utf-8")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return _number_text(value).encode("ascii")
            if isinstance(value, list):
                return bytes(v & 0xFF for v in value if isinstance(v, int) and not isinstance(v, bool))
        raise CodecError("bytes", path, json_type_name(value))

    def annotation(self) -> Any:
        return bytes


class IntCodec(Codec):
    expected = "integer"

    def __init__(self, unsigned: bool) -> None:
        self.unsigned = unsigned
        self.lidl = "uint" if unsigned else "int"

    def _check_range(self, value: int, path: str) -> int:
        if self.unsigned:
            if value < 0:
                raise CodecError("unsigned integer", path, "number")
            if value > UINT64_MAX:
                raise CodecError("unsigned integer in range", path, "number")
        elif not INT64_MIN <= value <= INT64_MAX:
            raise CodecError("signed integer in range", path, "number")
        return int(value)

    def encode(self, value: Any, path: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError("integer", path, python_type_name(value))
        return self._check_range(value, path)

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if isinstance(value, bool):
            raise CodecError("integer", path, "boolean")
        if isinstance(value, int):
            return self._check_range(value, path)
        if isinstance(value, float) and lenient:
            # A whole-valued float is an integer to logos_codec.h; 3.7 is not.
            if not value.is_integer():
                raise CodecError("integer", path, "number")
            if self.unsigned and not 0 <= value < 2.0**64:
                raise CodecError("unsigned integer in range", path, "number")
            if not self.unsigned and not -(2.0**63) <= value < 2.0**63:
                raise CodecError("signed integer in range", path, "number")
            return int(value)
        raise CodecError("integer", path, json_type_name(value))

    def annotation(self) -> Any:
        return int


class FloatCodec(Codec):
    lidl = "float64"
    expected = "number"

    def _convert(self, value: Any, path: str, got: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError("number", path, got)
        try:
            out = float(value)
        except OverflowError:
            raise CodecError("number in range", path, "number") from None
        if not math.isfinite(out):
            raise CodecError("finite number", path, "number")
        return out

    def encode(self, value: Any, path: str) -> Any:
        return self._convert(value, path, python_type_name(value))

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        return self._convert(value, path, json_type_name(value))

    def annotation(self) -> Any:
        return float


class BoolCodec(Codec):
    lidl = "bool"
    expected = "bool"

    def encode(self, value: Any, path: str) -> Any:
        if isinstance(value, bool):
            return value
        raise CodecError("bool", path, python_type_name(value))

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if isinstance(value, bool):
            return value
        raise CodecError("bool", path, json_type_name(value))

    def annotation(self) -> Any:
        return bool


class AnyCodec(Codec):
    """``any``: encoded as a JSON tree, decoded verbatim (tags stay tags)."""

    lidl = "any"
    expected = "value"
    accepts_null = True

    def __init__(self, lidl: str = "any") -> None:
        self.lidl = lidl

    def encode(self, value: Any, path: str) -> Any:
        return encode_json_value(value, path)

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        return value


class OptionalCodec(Codec):
    accepts_null = True

    def __init__(self, inner: Codec) -> None:
        self.inner = inner
        self.lidl = f"? {inner.lidl}"
        self.expected = inner.expected

    def encode(self, value: Any, path: str) -> Any:
        return None if value is None else self.inner.encode(value, path)

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        return None if value is None else self.inner.decode(value, path, lenient=lenient)

    def annotation(self) -> Any:
        return typing.Optional[self.inner.annotation()]  # noqa: UP045  (a runtime object on 3.10)


class ListCodec(Codec):
    expected = "array"

    def __init__(self, element: Codec) -> None:
        self.element = element
        self.lidl = f"[{element.lidl}]"

    def encode(self, value: Any, path: str) -> Any:
        if not _is_sequence(value) or isinstance(value, Mapping):
            raise CodecError("array", path, python_type_name(value))
        return [self.element.encode(item, f"{path}[{i}]") for i, item in enumerate(value)]

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if not isinstance(value, list):
            raise CodecError("array", path, json_type_name(value))
        return [self.element.decode(item, f"{path}[{i}]", lenient=lenient) for i, item in enumerate(value)]

    def annotation(self) -> Any:
        return list[self.element.annotation()]  # type: ignore[misc]


class MapCodec(Codec):
    """``{tstr: V}``: keys are data, including one spelled ``_bytes``."""

    expected = "object"

    def __init__(self, value: Codec) -> None:
        self.value = value
        self.lidl = f"{{tstr: {value.lidl}}}"

    def encode(self, value: Any, path: str) -> Any:
        if not isinstance(value, Mapping):
            raise CodecError("object", path, python_type_name(value))
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CodecError("string keys", path, f"a {python_type_name(key)} key")
            out[_plain_str(key)] = self.value.encode(item, f"{path}.{key}")
        return out

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if not isinstance(value, dict):
            raise CodecError("object", path, json_type_name(value))
        return {k: self.value.decode(v, f"{path}.{k}", lenient=lenient) for k, v in value.items()}

    def annotation(self) -> Any:
        return dict[str, self.value.annotation()]  # type: ignore[misc]


@dataclasses.dataclass(frozen=True)
class RecordField:
    wire: str
    attr: str
    codec: Codec
    optional: bool


class RecordCodec(Codec):
    """A record: a generated dataclass (``cls``), or a ``dict`` when ``cls`` is ``None``."""

    expected = "object"

    def __init__(self, name: str, cls: type | None) -> None:
        self.name = name
        self.lidl = name
        self.cls = cls
        self.fields: tuple[RecordField, ...] = ()
        self._wire_names: frozenset[str] = frozenset()

    def bind(self, fields: Sequence[RecordField]) -> None:
        self.fields = tuple(fields)
        self._wire_names = frozenset(f.wire for f in self.fields)

    def encode(self, value: Any, path: str) -> Any:
        if self.cls is not None:
            if not isinstance(value, self.cls):
                raise CodecError(self.name, path, python_type_name(value))
            items = {f.wire: getattr(value, f.attr) for f in self.fields}
        else:
            if not isinstance(value, Mapping):
                raise CodecError(self.name, path, python_type_name(value))
            unknown = [k for k in value if k not in self._wire_names]
            if unknown:
                raise CodecError(self.name, path, "object", f"no field {unknown[0]!r}")
            items = {f.wire: value.get(f.wire) for f in self.fields}
        out: dict[str, Any] = {}
        for f in self.fields:
            item = items[f.wire]
            where = f"{path}.{f.wire}"
            if item is None:
                if f.optional:
                    continue  # a named slot spells empty by omission
                if not f.codec.accepts_null:
                    raise CodecError(f.codec.expected, where, "null")
            out[f.wire] = f.codec.encode(item, where)
        return out

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        if not isinstance(value, dict):
            raise CodecError("object", path, json_type_name(value))
        decoded: dict[str, Any] = {}
        for f in self.fields:
            item = value.get(f.wire)
            where = f"{path}.{f.wire}"
            if item is None and not (f.optional or f.codec.accepts_null):
                raise CodecError(f.codec.expected, where, "null")
            decoded[f.wire] = None if item is None else f.codec.decode(item, where, lenient=lenient)
        if self.cls is not None:
            return self.cls(**{f.attr: decoded[f.wire] for f in self.fields})
        return {f.wire: decoded[f.wire] for f in self.fields if not (f.optional and decoded[f.wire] is None)}

    def annotation(self) -> Any:
        return self.cls if self.cls is not None else dict[str, Any]


_RESULT_KEYS: Final = frozenset({"success", "value", "error"})
_RESULT_NOUN: Final = 'result {"success": bool, "value": any, "error": string|null}'


class ResultCodec(Codec):
    lidl = "result"
    expected = "object"

    def __init__(self, dynamic: bool = False) -> None:
        self.dynamic = dynamic

    def encode(self, value: Any, path: str) -> Any:
        if isinstance(value, LogosResult):
            obj = value.to_json()
        elif self.dynamic and isinstance(value, Mapping):
            obj = dict(value)
        else:
            raise CodecError("LogosResult", path, python_type_name(value))
        self._check(obj, path, python_type_name)
        return {"success": obj["success"], "value": encode_json_value(obj["value"], f"{path}.value"),
                "error": obj["error"]}

    @staticmethod
    def _check(obj: Any, path: str, namer: Any) -> None:
        if not isinstance(obj, Mapping):
            raise CodecError(_RESULT_NOUN, path, namer(obj))
        keys = set(obj)
        if keys != _RESULT_KEYS:
            raise CodecError(_RESULT_NOUN, path, "object", f"keys {sorted(keys)}")
        if not isinstance(obj["success"], bool):
            raise CodecError("bool", f"{path}.success", namer(obj["success"]))
        if obj["error"] is not None and not isinstance(obj["error"], str):
            raise CodecError("string or null", f"{path}.error", namer(obj["error"]))

    def decode(self, value: Any, path: str, *, lenient: bool = False) -> Any:
        self._check(value, path, json_type_name)
        return LogosResult(value["success"], value["value"], value["error"])

    def annotation(self) -> Any:
        return LogosResult[Any]


_PRIMITIVE_CODECS: Final[Mapping[str, Codec]] = {
    "tstr": StrCodec(),
    "bstr": BytesCodec(),
    "int": IntCodec(unsigned=False),
    "uint": IntCodec(unsigned=True),
    "float64": FloatCodec(),
    "bool": BoolCodec(),
    "any": AnyCodec(),
}


LIDL_FIELD: Final = "lidl"


def wire_names(cls: type) -> dict[str, str]:
    """``{wire name: attribute}`` of a dataclass whose fields carry ``metadata={"lidl": name}``."""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls.__name__} is not a dataclass")
    return {f.metadata[LIDL_FIELD]: f.name for f in dataclasses.fields(cls) if LIDL_FIELD in f.metadata}


class CodecBuilder:
    """Codecs for one interface's types.

    ``records`` maps a record name to its generated dataclass, whose fields name
    their wire key in ``metadata={"lidl": ...}`` (a field without it uses its own
    name); records without a class decode to dicts. A type this reader cannot map
    (unknown kind or primitive, malformed, non-``tstr`` map key) becomes ``any``:
    degradation is per type node.
    """

    def __init__(self, interface: Interface, *, records: Mapping[str, type] | None = None,
                 dynamic: bool = False) -> None:
        self.interface = interface
        self.classes = dict(records or {})
        self.dynamic = dynamic
        self._records: dict[str, RecordCodec] = {}
        unknown = set(self.classes) - {r.name for r in interface.types}
        if unknown:
            raise ValueError(f"record classes for undeclared records: {sorted(unknown)}")

    def build(self, type_: TypeRef) -> Codec:
        kind = type_.kind
        if _ARITY.get(kind) != len(type_.elements):
            return AnyCodec(type_.spell())
        if kind == "primitive":
            if type_.name == "result":
                return ResultCodec(self.dynamic)
            codec = _PRIMITIVE_CODECS.get(type_.name)
            return codec if codec is not None else AnyCodec(type_.name)
        if kind == "named":
            return self.record(type_.name)
        if kind == "array":
            return ListCodec(self.build(type_.elements[0]))
        if kind == "map":
            key = type_.elements[0]
            if not (key.kind == "primitive" and key.name == "tstr"):
                return AnyCodec(type_.spell())
            return MapCodec(self.build(type_.elements[1]))
        inner = self.build(type_.value_type)
        return inner if inner.accepts_null else OptionalCodec(inner)

    def slot(self, optional: bool, value_type: TypeRef) -> Codec:
        """A positional slot (parameter, return, event parameter)."""
        codec = self.build(value_type)
        return OptionalCodec(codec) if optional and not codec.accepts_null else codec

    def record(self, name: str) -> RecordCodec:
        existing = self._records.get(name)
        if existing is not None:
            return existing
        declared = self.interface.record(name)
        if declared is None:
            raise ValueError(f"the contract has no record named {name!r}")
        cls = self.classes.get(name)
        codec = RecordCodec(name, cls)
        self._records[name] = codec  # registered first: records may be recursive
        attrs: Mapping[str, str] = wire_names(cls) if cls is not None else {}
        codec.bind([
            RecordField(f.name, attrs.get(f.name, f.name), self.build(f.value_type), f.optional)
            for f in declared.fields
        ])
        return codec


def string_capable(type_: TypeRef) -> bool:
    value = type_.value_type
    return value.kind == "primitive" and value.name in ("tstr", "any")


def rejection_ambiguous(type_: TypeRef, interface: Interface) -> bool:
    """Whether a legitimate value of ``type_`` can look like a provider rejection.

    ``any``, a map whose values can be strings, or a record whose fields are
    exactly ``code``/``message``/``origin`` (all string-capable). Such results
    are still folded into :class:`~logos_bridge.errors.ProviderRejection`.
    """
    value = type_.value_type
    if value.kind == "primitive":
        return value.name == "any"
    if value.kind == "map" and len(value.elements) == 2:
        return string_capable(value.elements[1])
    if value.kind == "named":
        record = interface.record(value.name)
        if record is None:
            return False
        names = {f.name for f in record.fields}
        return names == {"code", "message", "origin"} and all(string_capable(f.type) for f in record.fields)
    return False
