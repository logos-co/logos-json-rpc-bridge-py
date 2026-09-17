"""Wire vocabulary of logos-json-rpc-bridge: operations, envelopes, frames, error table.

Mirrors ``src/rpc_dispatcher.h`` and ``src/error_map.h`` of the bridge. Pure: no I/O.
The error table is shared by the client (error classes) and ``logos_bridge.testing``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, TypeGuard

JSONRPC_VERSION: Final = "2.0"
DEFAULT_URL: Final = "ws://127.0.0.1:8645/ws"
DEFAULT_HTTP_URL: Final = "http://127.0.0.1:8645"
SUBPROTOCOL: Final = "jsonrpc-bridge.v1"

OP_CALL: Final = "rpc.call"
OP_SUBSCRIBE: Final = "rpc.subscribe"
OP_UNSUBSCRIBE: Final = "rpc.unsubscribe"
OP_SCHEMA: Final = "rpc.schema"
OP_LIST_MODULES: Final = "rpc.list_modules"
OP_CANCEL: Final = "rpc.cancel"
OP_PING: Final = "rpc.ping"
OP_AUTHENTICATE: Final = "rpc.authenticate"

NOTIFY_EVENT: Final = "rpc.event"
NOTIFY_TERMINATED: Final = "rpc.subscription_terminated"

BRIDGE_OPS: Final = frozenset(
    {OP_CALL, OP_SUBSCRIBE, OP_UNSUBSCRIBE, OP_SCHEMA, OP_LIST_MODULES, OP_CANCEL, OP_PING}
)
# Ops that need the WebSocket client's subscription routing; raw request paths refuse them.
STREAM_OPS: Final = frozenset({OP_SUBSCRIBE, OP_UNSUBSCRIBE})

# Termination reasons: the provider went away, or revalidation found a different build.
REASON_PROVIDER_UNAVAILABLE: Final = "provider_unavailable"
REASON_PROVIDER_CHANGED: Final = "provider_changed"

_MISSING: Any = object()


def make_request(req_id: int | str | None, method: str, params: Any = _MISSING) -> dict[str, Any]:
    """A JSON-RPC request object; ``params`` is omitted when not given."""
    out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method}
    if params is not _MISSING and params is not None:
        out["params"] = params
    return out


def make_notification(method: str, params: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}


def make_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def make_error(req_id: Any, error: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": dict(error)}


# -- Error table -------------------------------------------------------------

LOGOS_ERROR_NAMES: Final[Mapping[int, str]] = {
    1: "METHOD_NOT_FOUND",
    2: "INVALID_PARAMS",
    3: "MODULE_ERROR",
    4: "NOT_AUTHORISED",
    5: "TRANSPORT_ERROR",
    6: "TIMEOUT",
    7: "VERSION_MISMATCH",
    8: "NOT_READY",
    9: "CANCELLED",
}


@dataclass(frozen=True)
class ErrorSpec:
    """One JSON-RPC code as the bridge answers it."""

    code: int
    name: str
    message: str
    logos_error_code: int
    logos_error_name: str
    # Every constant message the bridge sends with this code (it never interpolates).
    bridge_messages: tuple[str, ...] = field(default=())

    def data(self) -> dict[str, Any]:
        return {"logos_error_code": self.logos_error_code, "logos_error_name": self.logos_error_name}

    def to_error(self, message: str | None = None, data: Any = _MISSING) -> dict[str, Any]:
        """The ``error`` member the bridge would send for this code."""
        return {
            "code": self.code,
            "message": self.message if message is None else message,
            "data": self.data() if data is _MISSING else data,
        }


def _spec(
    code: int, name: str, message: str, logos: int, *messages: str
) -> tuple[int, ErrorSpec]:
    return code, ErrorSpec(code, name, message, logos, LOGOS_ERROR_NAMES[logos], messages)


ERROR_TABLE: Final[Mapping[int, ErrorSpec]] = dict(
    [
        _spec(-32700, "PARSE_ERROR", "parse error", 2, "parse error"),
        _spec(
            -32600, "INVALID_REQUEST", "invalid request", 2,
            "request must be an object",
            'jsonrpc must be "2.0"',
            "method must be a string",
            "id must be a string, number or null",
            "empty batch",
            "notifications are not accepted for rpc.call: every module call has a reply",
        ),
        _spec(-32601, "METHOD_NOT_FOUND", "method not found", 1, "method not found"),
        _spec(
            -32602, "INVALID_PARAMS", "invalid params", 2,
            "invalid params",
            "params must be an object or array",
            'rpc.call params must be an object with "module" and "method"',
            'rpc.call requires string "module" and "method"',
            '"module" and "method" must be non-empty',
            "rpc.call inner params must be an object or array",
            "params must be an object",
            '"subscription" (a caller-assigned id) is required',
            'rpc.subscribe requires string "module" and "event"',
            '"module" and "event" must be non-empty',
            'rpc.schema requires a string "module"',
        ),
        _spec(-32603, "INTERNAL_ERROR", "upstream call failed", 3, "upstream call failed"),
        _spec(-32000, "MODULE_ERROR", "call could not be dispatched", 3, "call could not be dispatched"),
        _spec(-32001, "UNAVAILABLE", "module unavailable", 8, "module unavailable"),
        _spec(-32002, "TIMEOUT", "upstream call timed out", 6, "upstream call timed out"),
        _spec(-32003, "TRANSPORT_ERROR", "transport error", 5, "transport error"),
        _spec(-32004, "UNAUTHORIZED", "not authorised", 4, "not authorised"),
        # Reserved by the bridge; no current build sends it.
        _spec(-32005, "CANCELLED", "cancelled", 9),
        _spec(-32006, "SHUTTING_DOWN", "shutting down", 8, "shutting down"),
        _spec(-32029, "OVERLOADED", "overloaded", 8, "batch too large", "too many subscriptions"),
    ]
)

# logos::CallError codes -> JSON-RPC code (error_map.h mapCallError); unknown -> -32603.
CALL_ERROR_CODES: Final[Mapping[str, int]] = {
    "timeout": -32002,
    "object_unavailable": -32001,
    "transport_error": -32003,
    "unauthorized": -32004,
    "call_failed": -32000,
    "invalid_args": -32602,
    "invalid_arg": -32602,
}


def error_spec(code: int) -> ErrorSpec | None:
    return ERROR_TABLE.get(code)


def map_call_error(upstream_code: str) -> ErrorSpec:
    """The bridge's answer for an upstream ``logos::CallError`` code."""
    return ERROR_TABLE[CALL_ERROR_CODES.get(upstream_code, -32603)]


def not_found() -> dict[str, Any]:
    """The one refusal for unknown, unexposed and denied targets alike."""
    return ERROR_TABLE[-32601].to_error()


def invalid_params_error(reason: str, path: str = "") -> dict[str, Any]:
    """-32602 with ``data.invalid_params_detail`` (error_map.h invalidParamsJson)."""
    err = ERROR_TABLE[-32602].to_error()
    detail: dict[str, Any] = {"reason": reason}
    if path:
        detail["path"] = path
    err["data"]["invalid_params_detail"] = detail
    return err


# -- Frames ------------------------------------------------------------------


class FrameKind(enum.Enum):
    RESULT = "result"
    ERROR = "error"
    # An error whose id is null: the bridge could not attribute it to a request.
    UNCORRELATED_ERROR = "uncorrelated_error"
    EVENT = "event"
    TERMINATED = "terminated"
    NOTIFICATION = "notification"
    REQUEST = "request"
    INVALID = "invalid"


@dataclass(frozen=True)
class Frame:
    kind: FrameKind
    id: Any = None
    result: Any = None
    error: Mapping[str, Any] | None = None
    method: str | None = None
    params: Any = None
    problem: str | None = None


def _invalid(problem: str) -> Frame:
    return Frame(FrameKind.INVALID, problem=problem)


def is_json_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def classify(obj: Any) -> Frame:
    """Classify one decoded server message (a batch is classified item by item)."""
    if not isinstance(obj, dict):
        return _invalid("message is not a JSON object")
    if obj.get("jsonrpc") != JSONRPC_VERSION:
        return _invalid('missing "jsonrpc": "2.0"')
    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str):
            return _invalid("method is not a string")
        params = obj.get("params")
        if "id" in obj:
            return Frame(FrameKind.REQUEST, id=obj["id"], method=method, params=params)
        if method in (NOTIFY_EVENT, NOTIFY_TERMINATED):
            if not isinstance(params, dict) or "subscription" not in params:
                return _invalid(f"{method} without a subscription")
            kind = FrameKind.EVENT if method == NOTIFY_EVENT else FrameKind.TERMINATED
            return Frame(kind, method=method, params=params)
        return Frame(FrameKind.NOTIFICATION, method=method, params=params)
    if "id" not in obj:
        return _invalid("neither a response nor a notification")
    has_result = "result" in obj
    has_error = "error" in obj
    if has_result == has_error:
        return _invalid("a response needs exactly one of result and error")
    req_id = obj["id"]
    if has_error:
        err = obj["error"]
        if not isinstance(err, dict) or not is_json_int(err.get("code")):
            return _invalid("error member without an integer code")
        kind = FrameKind.UNCORRELATED_ERROR if req_id is None else FrameKind.ERROR
        return Frame(kind, id=req_id, error=err)
    return Frame(FrameKind.RESULT, id=req_id, result=obj["result"])
