"""Contract digests, byte-compatible with the bridge.

``interface_sha256`` is the lowercase hex SHA-256 of the served interface object's
canonical JSON: sorted keys, ``,``/``:`` separators, raw UTF-8, no NaN. Sorting by
code point equals the bridge's sort by UTF-8 bytes. ``contract_sha256`` hashes the
exact ``lidl()`` text. ``shape_sha256`` hashes the spelling-free form
(:meth:`logos_bridge.lidl.Interface.shape`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .lidl import Interface


def canonical_json(value: Any) -> bytes:
    """``json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` as UTF-8."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def interface_sha256(doc: Any) -> str:
    """SHA-256 of :func:`canonical_json` of an interface document."""
    return hashlib.sha256(canonical_json(doc)).hexdigest()


def contract_sha256(text: str | bytes) -> str:
    """SHA-256 of a contract's exact UTF-8 bytes."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    return hashlib.sha256(data).hexdigest()


def shape_sha256(value: Interface | Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of an interface's shape.

    ``value`` is an :class:`~logos_bridge.lidl.Interface`, a JSON AST, or a shape
    document itself.
    """
    from .lidl import Interface

    if isinstance(value, Interface):
        shape = value.shape()
    elif isinstance(value, Mapping) and "shape" in value and isinstance(value.get("methods"), Mapping):
        shape = dict(value)
    else:
        shape = Interface.from_json(dict(value)).shape()
    return hashlib.sha256(canonical_json(shape)).hexdigest()
