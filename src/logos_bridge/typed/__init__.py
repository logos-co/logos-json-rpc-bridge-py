"""Typed calls and events against a LIDL contract.

The runtime that generated clients (``logos-bridge-codegen python``) and the
dynamic proxy (:meth:`logos_bridge.AsyncBridgeClient.module`) are built on.
"""

from __future__ import annotations

from typing import Final

from ..errors import (
    ArgumentError,
    ArityError,
    BindingIntegrityError,
    ClientRejection,
    CodegenFormatError,
    DecodeError,
    EventDecodeError,
    ResultDecodeError,
)
from ._binding import Binding
from ._codecs import (
    INT64_MAX,
    INT64_MIN,
    LIDL_FIELD,
    UINT64_MAX,
    Codec,
    CodecBuilder,
    CodecError,
    encode_json_value,
    python_type_name,
    rejection_ambiguous,
    wire_names,
)
from ._plans import DecodedEvent, EventPlan, InterfacePlans, MethodPlan, arity_message
from ._result import LogosResult
from ._subscription import BlockingTypedSubscription, TypedSubscribeRequest, TypedSubscription

#: The generated-code format this runtime writes and reads.
CODEGEN_FORMAT: Final = 1
SUPPORTED_CODEGEN_FORMATS: Final = frozenset({1})


def require_codegen_format(version: int) -> None:
    """Called at import by generated modules: refuse a format this runtime cannot run."""
    if version not in SUPPORTED_CODEGEN_FORMATS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_CODEGEN_FORMATS))
        raise CodegenFormatError(
            f"this module was generated for codegen format {version}, but this logos-bridge supports "
            f"format {supported}; install a logos-bridge that matches the generator, or regenerate"
        )


__all__ = [
    "CODEGEN_FORMAT",
    "INT64_MAX",
    "INT64_MIN",
    "LIDL_FIELD",
    "SUPPORTED_CODEGEN_FORMATS",
    "UINT64_MAX",
    "ArgumentError",
    "ArityError",
    "Binding",
    "BindingIntegrityError",
    "BlockingTypedSubscription",
    "ClientRejection",
    "Codec",
    "CodecBuilder",
    "CodecError",
    "CodegenFormatError",
    "DecodeError",
    "DecodedEvent",
    "EventDecodeError",
    "EventPlan",
    "InterfacePlans",
    "LogosResult",
    "MethodPlan",
    "ResultDecodeError",
    "TypedSubscribeRequest",
    "TypedSubscription",
    "arity_message",
    "encode_json_value",
    "python_type_name",
    "rejection_ambiguous",
    "require_codegen_format",
    "wire_names",
]
