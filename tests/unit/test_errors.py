from __future__ import annotations

import logging
import pickle
import warnings
from typing import Any

import pytest

from logos_bridge import _protocol as proto
from logos_bridge import errors
from logos_bridge.errors import (
    CLOSE_CODE_HINTS,
    DROPPED_UPGRADE_HINTS,
    HTTP_STATUS_HINTS,
    RPC_ERROR_CLASSES,
    BridgeError,
    BytesDecodeError,
    CallNotDispatched,
    Cancelled,
    ClientTimeout,
    ConnectError,
    ConnectionClosed,
    HttpStatusError,
    InvalidParams,
    InvalidRequest,
    MethodNotFound,
    ModuleResultError,
    ModuleUnavailable,
    NotAuthorised,
    Overloaded,
    ParseError,
    PortalStopped,
    ProviderRejection,
    RequestTooLarge,
    RpcError,
    ShuttingDown,
    SubscriptionOverflow,
    SubscriptionTerminated,
    UpstreamCallFailed,
    UpstreamTimeout,
    UpstreamTransportError,
)

# code -> (class, message, logos code, logos name), from the bridge's error_map.h
BRIDGE_ERRORS: dict[int, tuple[type[RpcError], str, int, str]] = {
    -32700: (ParseError, "parse error", 2, "INVALID_PARAMS"),
    -32600: (InvalidRequest, "invalid request", 2, "INVALID_PARAMS"),
    -32601: (MethodNotFound, "method not found", 1, "METHOD_NOT_FOUND"),
    -32602: (InvalidParams, "invalid params", 2, "INVALID_PARAMS"),
    -32603: (UpstreamCallFailed, "upstream call failed", 3, "MODULE_ERROR"),
    -32000: (CallNotDispatched, "call could not be dispatched", 3, "MODULE_ERROR"),
    -32001: (ModuleUnavailable, "module unavailable", 8, "NOT_READY"),
    -32002: (UpstreamTimeout, "upstream call timed out", 6, "TIMEOUT"),
    -32003: (UpstreamTransportError, "transport error", 5, "TRANSPORT_ERROR"),
    -32004: (NotAuthorised, "not authorised", 4, "NOT_AUTHORISED"),
    -32005: (Cancelled, "cancelled", 9, "CANCELLED"),
    -32006: (ShuttingDown, "shutting down", 8, "NOT_READY"),
    -32029: (Overloaded, "overloaded", 8, "NOT_READY"),
}


def test_the_error_table_matches_the_bridge() -> None:
    assert set(proto.ERROR_TABLE) == set(BRIDGE_ERRORS)
    for code, (_cls, message, logos_code, logos_name) in BRIDGE_ERRORS.items():
        spec = proto.ERROR_TABLE[code]
        assert (spec.code, spec.message, spec.logos_error_code, spec.logos_error_name) == (
            code, message, logos_code, logos_name)
        assert spec.to_error() == {"code": code, "message": message,
                                   "data": {"logos_error_code": logos_code, "logos_error_name": logos_name}}


def test_every_code_has_its_own_class() -> None:
    assert set(RPC_ERROR_CLASSES) == set(BRIDGE_ERRORS)
    assert len(set(RPC_ERROR_CLASSES.values())) == len(BRIDGE_ERRORS)
    for code, (cls, *_rest) in BRIDGE_ERRORS.items():
        assert RPC_ERROR_CLASSES[code] is cls
        assert issubclass(cls, RpcError) and issubclass(cls, BridgeError)
        assert cls.code == code


@pytest.mark.parametrize("code", sorted(BRIDGE_ERRORS))
def test_from_error_builds_the_subclass(code: int) -> None:
    cls, message, logos_code, logos_name = BRIDGE_ERRORS[code]
    error = RpcError.from_error(proto.ERROR_TABLE[code].to_error(), request_id=7, module="m", method="f")
    assert type(error) is cls
    assert (error.code, error.message, error.request_id) == (code, message, 7)
    assert (error.logos_error_code, error.logos_error_name) == (logos_code, logos_name)
    assert str(error) == f"{message} (json-rpc {code}, {logos_name}) calling m.f"


def test_bridge_messages_are_the_constant_strings() -> None:
    assert "batch too large" in proto.ERROR_TABLE[-32029].bridge_messages
    assert "too many subscriptions" in proto.ERROR_TABLE[-32029].bridge_messages
    assert proto.ERROR_TABLE[-32005].bridge_messages == ()  # reserved, never sent


def test_unknown_and_malformed_errors() -> None:
    unknown = RpcError.from_error({"code": -32099, "message": "custom", "data": [1]}, op="rpc.x")
    assert type(unknown) is RpcError
    assert (unknown.code, unknown.message, unknown.data, unknown.logos_error_code) == (-32099, "custom", [1], None)
    assert str(unknown) == "custom (json-rpc -32099) in rpc.x"
    malformed = RpcError.from_error("oops")
    assert type(malformed) is RpcError
    assert malformed.code == -32603 and "malformed" in malformed.message
    no_message = RpcError.from_error({"code": -32001})
    assert isinstance(no_message, ModuleUnavailable) and no_message.message == "module unavailable"
    assert isinstance(RpcError.from_error({"code": True}), UpstreamCallFailed)


def test_invalid_params_detail() -> None:
    error = RpcError.from_error(proto.invalid_params_error("schema-mismatch", "who"))
    assert isinstance(error, InvalidParams)
    assert error.detail is not None
    assert (error.detail.reason, error.detail.path) == ("schema-mismatch", "who")
    assert InvalidParams().detail is None
    no_path = RpcError.from_error(proto.invalid_params_error("malformed-cbor"))
    assert isinstance(no_path, InvalidParams) and no_path.detail is not None and no_path.detail.path is None


def test_call_error_mapping() -> None:
    assert proto.map_call_error("timeout").code == -32002
    assert proto.map_call_error("object_unavailable").code == -32001
    assert proto.map_call_error("transport_error").code == -32003
    assert proto.map_call_error("unauthorized").code == -32004
    assert proto.map_call_error("call_failed").code == -32000
    assert proto.map_call_error("invalid_args").code == -32602
    assert proto.map_call_error("invalid_arg").code == -32602
    assert proto.map_call_error("something_new").code == -32603
    assert proto.not_found() == proto.ERROR_TABLE[-32601].to_error()


def test_timeouts_are_told_apart() -> None:
    assert issubclass(ClientTimeout, TimeoutError)
    assert not issubclass(UpstreamTimeout, TimeoutError)
    timeout = ClientTimeout(1.5, module="m", method="f", request_id=3)
    assert str(timeout) == "m.f got no answer within 1.5s"
    assert ClientTimeout(2, op="rpc.ping").args == ("rpc.ping got no answer within 2s",)


def test_exception_bases() -> None:
    assert issubclass(ConnectError, ConnectionError) and issubclass(ConnectError, BridgeError)
    assert issubclass(ConnectionClosed, ConnectionError) and issubclass(ConnectionClosed, BridgeError)
    assert issubclass(RequestTooLarge, ValueError)
    assert issubclass(BytesDecodeError, ValueError)
    assert issubclass(PortalStopped, RuntimeError)
    assert issubclass(errors.BlockingCallInEventLoop, RuntimeError)
    for cls in (ProviderRejection, ModuleResultError, HttpStatusError, SubscriptionTerminated,
                SubscriptionOverflow, errors.SubscriptionClosed):
        assert issubclass(cls, BridgeError)


@pytest.mark.parametrize("code", [1000, 1001, 1003, 1006, 1007, 1008, 1009, 1011])
def test_close_codes_have_names_and_hints(code: int) -> None:
    assert errors.close_code_name(code) != "UNKNOWN"
    assert CLOSE_CODE_HINTS[code]


def test_close_code_names() -> None:
    assert errors.close_code_name(1008) == "POLICY_VIOLATION"
    assert errors.close_code_name(1009) == "MESSAGE_TOO_BIG"
    assert errors.close_code_name(1006) == "ABNORMAL_CLOSURE"
    assert errors.close_code_name(3001) == "REGISTERED"
    assert errors.close_code_name(4001) == "PRIVATE"
    assert errors.close_code_name(2000) == "UNKNOWN"


def test_connection_closed_fields() -> None:
    closed = ConnectionClosed(1008, initiated_by="server")
    assert (closed.code, closed.code_name, closed.initiated_by, closed.reason) == (
        1008, "POLICY_VIOLATION", "server", "")
    assert closed.hint is not None and "max_queued_frames_per_connection" in closed.hint
    assert "1008 POLICY_VIOLATION, initiated by server" in str(closed)
    too_big = ConnectionClosed(1009, initiated_by="server")
    assert too_big.hint is not None and "max_frame_bytes" in too_big.hint
    dropped = ConnectionClosed(1006, initiated_by="transport")
    assert dropped.hint is not None and "does not reconnect" in dropped.hint


def test_keepalive_and_shutdown_hints() -> None:
    keepalive = ConnectionClosed(1011, "keepalive ping timeout", initiated_by="client")
    assert keepalive.hint is not None and "pong" in keepalive.hint
    shutdown = ConnectionClosed(1006, initiated_by="transport", server_error=ShuttingDown())
    assert shutdown.hint == "the bridge is shutting down"
    assert "last bridge error: shutting down" in str(shutdown)
    assert ConnectionClosed(1000, initiated_by="server").hint == "the bridge closed the connection"


def test_dropped_upgrade_hints_name_every_cause() -> None:
    text = "\n".join(DROPPED_UPGRADE_HINTS)
    for needle in ("Host", "host_header", "Origin", "8 connections", "keep-alive", "max_connections",
                   "bearer", "jsonrpc-bridge.v1"):
        assert needle in text
    error = ConnectError("dropped", url="ws://x", hints=DROPPED_UPGRADE_HINTS)
    assert str(error).startswith("cannot connect to ws://x: dropped\npossible causes:\n  - Host")


def test_http_status_hints() -> None:
    for status in (401, 403, 404, 415):
        assert HTTP_STATUS_HINTS[status]
    error = HttpStatusError(403, "http://x/rpc", hint=HTTP_STATUS_HINTS[403])
    assert str(error).startswith("HTTP 403 from http://x/rpc: Host or Origin refused")


EXAMPLES: list[BaseException] = [
    MethodNotFound(module="m", method="f", request_id=1, data={"logos_error_code": 1}),
    RpcError("x", code=-32099),
    ProviderRejection("dispatch_failed", "bad", "m", module="m", method="f"),
    ModuleResultError("boom", {"success": False, "value": None, "error": "boom"}),
    HttpStatusError(415, "http://x", body=b"<html>", hint="h"),
    ConnectError("refused", url="ws://x", hints=("a", "b"), status=None),
    ConnectionClosed(1008, "why", initiated_by="server", server_error=ShuttingDown()),
    ClientTimeout(1.0, op="rpc.ping"),
    SubscriptionTerminated("s1", "m", "e", "provider_unavailable", raw={"k": 1}),
    SubscriptionOverflow(5, ["s1"]),
    RequestTooLarge(10, 5),
    BytesDecodeError("bad", "x"),
    PortalStopped("stopped"),
]


@pytest.mark.parametrize("error", EXAMPLES, ids=lambda e: type(e).__name__)
def test_errors_pickle(error: BaseException) -> None:
    clone = pickle.loads(pickle.dumps(error))
    assert type(clone) is type(error)
    assert str(clone) == str(error)
    assert vars(clone).keys() == vars(error).keys()
    for key, value in vars(error).items():
        if isinstance(value, BaseException):
            assert str(getattr(clone, key)) == str(value)
        else:
            assert getattr(clone, key) == value


def test_warn_never_raises_inside_background_code(caplog: pytest.LogCaptureFixture) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with caplog.at_level(logging.WARNING, logger="logos_bridge"):
            errors.warn("queue is growing")
    assert "queue is growing" in caplog.text
    with pytest.warns(errors.BridgeWarning, match="plain"):
        errors.warn("plain")


# --------------------------------------------------------------------------- frames

FRAMES: list[tuple[Any, proto.FrameKind]] = [
    ({"jsonrpc": "2.0", "id": 1, "result": None}, proto.FrameKind.RESULT),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "m"}}, proto.FrameKind.ERROR),
    ({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "m"}}, proto.FrameKind.UNCORRELATED_ERROR),
    ({"jsonrpc": "2.0", "method": "rpc.event", "params": {"subscription": "s"}}, proto.FrameKind.EVENT),
    ({"jsonrpc": "2.0", "method": "rpc.subscription_terminated", "params": {"subscription": "s"}},
     proto.FrameKind.TERMINATED),
    ({"jsonrpc": "2.0", "method": "rpc.other", "params": {}}, proto.FrameKind.NOTIFICATION),
    ({"jsonrpc": "2.0", "id": 5, "method": "rpc.ping"}, proto.FrameKind.REQUEST),
    ({"jsonrpc": "2.0", "method": "rpc.event", "params": {}}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "method": "rpc.event", "params": []}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "id": 1}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "id": 1, "result": 1, "error": {"code": 1}}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": "x"}}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": True}}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "error": {"code": 1}}, proto.FrameKind.INVALID),
    ({"jsonrpc": "1.0", "id": 1, "result": 1}, proto.FrameKind.INVALID),
    ({"jsonrpc": "2.0", "method": 5}, proto.FrameKind.INVALID),
    ([1], proto.FrameKind.INVALID),
    ("pong", proto.FrameKind.INVALID),
]


@pytest.mark.parametrize(("obj", "kind"), FRAMES)
def test_classify(obj: Any, kind: proto.FrameKind) -> None:
    assert proto.classify(obj).kind is kind


def test_envelope_builders() -> None:
    assert proto.make_request(1, "rpc.ping") == {"jsonrpc": "2.0", "id": 1, "method": "rpc.ping"}
    assert proto.make_request(2, "rpc.ping", None) == {"jsonrpc": "2.0", "id": 2, "method": "rpc.ping"}
    assert proto.make_request(3, "rpc.schema", {"module": "m"})["params"] == {"module": "m"}
    assert proto.make_notification("rpc.event", {"a": 1}) == {"jsonrpc": "2.0", "method": "rpc.event",
                                                              "params": {"a": 1}}
    assert proto.make_result(None, "pong") == {"jsonrpc": "2.0", "id": None, "result": "pong"}
    assert proto.make_error(4, {"code": 1}) == {"jsonrpc": "2.0", "id": 4, "error": {"code": 1}}
    assert proto.STREAM_OPS == {"rpc.subscribe", "rpc.unsubscribe"}
    assert proto.is_json_int(3) and not proto.is_json_int(True) and not proto.is_json_int(3.0)
