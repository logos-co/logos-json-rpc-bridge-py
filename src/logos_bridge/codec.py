"""JSON policy, the ``{"_bytes": ...}`` tag, provider rejections and ``LogosResult``.

* :func:`dumps` is the only encoder on the wire: compact, raw UTF-8, no NaN, ``str`` keys only.
* ``bstr`` travels as ``{"_bytes": "<base64url, unpadded>"}``.
* A provider that refuses a call answers ``{"code", "message", "origin"}`` as the *result*;
  :func:`as_provider_rejection` is the exact predicate of ``logos-rust-sdk`` ``as_dispatch_rejection``.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from .errors import BytesDecodeError, ModuleResultError, ProviderRejection

BYTES_KEY: Final = "_bytes"
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]*")

# The closed set of provider refusal codes (logos-rust-sdk src/args.rs REJECTION_CODES).
REJECTION_CODES: Final[tuple[str, ...]] = ("dispatch_failed", "invalid_args", "unknown_method")

_RESULT_KEYS: Final = frozenset({"success", "value", "error"})


# -- JSON --------------------------------------------------------------------


def _check_keys(value: Any) -> None:
    # json.dumps silently stringifies int/float/bool/None keys; the wire must not.
    stack: list[tuple[Any, str]] = [(value, "$")]
    seen: set[int] = set()
    while stack:
        item, path = stack.pop()
        if isinstance(item, dict):
            if id(item) in seen:
                continue  # json.dumps reports real cycles itself
            seen.add(id(item))
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError(f"JSON object keys must be str, got {type(key).__name__} at {path}")
                if isinstance(child, (dict, list, tuple)):
                    stack.append((child, f"{path}.{key}"))
        elif isinstance(item, (list, tuple)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            for index, child in enumerate(item):
                if isinstance(child, (dict, list, tuple)):
                    stack.append((child, f"{path}[{index}]"))


def dumps(value: Any) -> bytes:
    """Encode ``value`` as compact, strict UTF-8 JSON.

    Raises ``TypeError`` for non-``str`` object keys and unsupported types, and
    ``ValueError`` for NaN/Infinity and for strings that are not valid Unicode
    (lone surrogates).
    """
    _check_keys(value)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"value is not encodable as UTF-8 JSON: {exc.reason} at offset {exc.start}") from None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def loads(data: str | bytes | bytearray) -> Any:
    """Decode one JSON document, refusing NaN/Infinity."""
    return json.loads(data, parse_constant=_reject_constant)


# -- bytes -------------------------------------------------------------------


def encode_bytes_tag(data: bytes | bytearray | memoryview) -> dict[str, str]:
    """``b"\\xfb\\xff"`` -> ``{"_bytes": "-_8"}``."""
    return {BYTES_KEY: base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")}


def is_bytes_tag(value: Any) -> bool:
    """The canonical tag shape: an object with exactly one key, ``_bytes``, holding a string."""
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get(BYTES_KEY), str)


def _check_b64url(text: str) -> None:
    if _B64URL.fullmatch(text) is None:
        raise BytesDecodeError("characters outside the base64url alphabet (A-Z a-z 0-9 - _, no padding)", text)
    if len(text) % 4 == 1:
        raise BytesDecodeError(f"impossible base64url length {len(text)}", text)


def decode_bytes_tag(tag: Any) -> bytes:
    """Decode one tag strictly: exact shape, base64url alphabet, possible length.

    Trailing ``=`` padding is tolerated, as logos-protocol's checked decoder does.
    """
    if not is_bytes_tag(tag):
        raise BytesDecodeError("not a {\"_bytes\": \"...\"} object", tag)
    text: str = tag[BYTES_KEY]
    body = text.rstrip("=")
    _check_b64url(body)
    try:
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - guarded above
        raise BytesDecodeError(str(exc), text) from None


def decode_bytes_tags(value: Any) -> Any:
    """Return ``value`` with every tag, at any depth, replaced by ``bytes``.

    Containers are copied; objects that merely contain a ``_bytes`` key among others
    are data and left alone.
    """
    if isinstance(value, dict):
        if is_bytes_tag(value):
            return decode_bytes_tag(value)
        return {k: decode_bytes_tags(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_bytes_tags(v) for v in value]
    return value


def encode_value(value: Any) -> Any:
    """Prepare one argument for the wire.

    bytes-like -> tag; tuples -> lists; mappings -> dicts with ``str`` keys. A
    pre-encoded tag is validated. Any other object with a ``_bytes`` key is refused:
    the name is reserved for the tag.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{value!r} is not representable in JSON")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return encode_bytes_tag(value)
    if isinstance(value, Mapping):
        if BYTES_KEY in value:
            tag = value[BYTES_KEY]
            if len(value) == 1 and isinstance(tag, str):
                _check_b64url(tag)
                return {BYTES_KEY: tag}
            raise ValueError('the object key "_bytes" is reserved for the bytes tag {"_bytes": "<base64url>"}')
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be str, got {type(key).__name__}")
            out[key] = encode_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    raise TypeError(f"cannot send {type(value).__name__} as JSON")


def encode_args(args: Iterable[Any]) -> list[Any]:
    """Positional arguments for ``rpc.call``."""
    return [encode_value(a) for a in args]


# -- Provider rejections -----------------------------------------------------


def as_provider_rejection(value: Any) -> str | None:
    """The rejection's message if ``value`` is a provider refusal, else ``None``.

    Exactly ``logos-rust-sdk`` ``as_dispatch_rejection``: an object of exactly three
    keys, ``code``/``message``/``origin`` all strings, ``code`` in :data:`REJECTION_CODES`.
    Anything else, including a method legitimately returning such a map with another
    code, stays data.
    """
    if not isinstance(value, dict):
        return None
    if len(value) != 3:
        return None
    code = value.get("code")
    message = value.get("message")
    origin = value.get("origin")
    if not isinstance(code, str) or not isinstance(message, str) or not isinstance(origin, str):
        return None
    if code not in REJECTION_CODES:
        return None
    return message


def rejection_from_result(
    value: Any, *, module: str | None = None, method: str | None = None
) -> ProviderRejection | None:
    """A :class:`ProviderRejection` for a refusal result, else ``None``."""
    message = as_provider_rejection(value)
    if message is None:
        return None
    return ProviderRejection(value["code"], message, value["origin"], module=module, method=method)


# -- LogosResult -------------------------------------------------------------


def is_logos_result(value: Any) -> bool:
    """The canonical ``{"success": bool, "value": any, "error": str|null}`` object."""
    return (
        isinstance(value, dict)
        and len(value) == 3
        and _RESULT_KEYS.issuperset(value)
        and isinstance(value["success"], bool)
        and (value["error"] is None or isinstance(value["error"], str))
    )


def unwrap_result(value: Any) -> Any:
    """``value["value"]`` of a successful ``LogosResult``.

    Raises :class:`ModuleResultError` for ``success: false`` and ``TypeError`` for
    anything that is not a ``LogosResult``.
    """
    if not is_logos_result(value):
        raise TypeError(f"not a LogosResult: {value!r:.200}")
    if value["success"]:
        return value["value"]
    raise ModuleResultError(value["error"], value)
