"""Every exception this package raises on purpose derives from :class:`BridgeError`."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ._protocol import ERROR_TABLE
from .models import InvalidParamsDetail

_logger = logging.getLogger("logos_bridge")


def _restore(cls: type[BaseException], args: tuple[Any, ...], state: dict[str, Any]) -> BaseException:
    obj = cls.__new__(cls)
    obj.args = args
    obj.__dict__.update(state)
    return obj


class BridgeError(Exception):
    """Root of the logos-bridge exception hierarchy."""

    def __reduce__(self) -> tuple[Any, ...]:
        # Subclasses take keyword-only arguments, which default exception pickling cannot replay.
        return (_restore, (type(self), self.args, dict(self.__dict__)))


class BridgeWarning(UserWarning):
    """Runtime diagnostics: uncorrelated server errors, queue growth, risky headers."""


def warn(message: str) -> None:
    """Emit a :class:`BridgeWarning` from background code.

    An ``error`` warnings filter must not turn this into an exception inside the
    reader task, so a raised warning is logged instead.
    """
    try:
        warnings.warn(message, BridgeWarning, stacklevel=2)
    except Warning:
        _logger.warning("%s", message)


# -- JSON-RPC errors answered by the bridge ----------------------------------


class RpcError(BridgeError):
    """A JSON-RPC ``error`` the bridge answered. Subclassed per code."""

    code: int = 0

    def __init__(
        self,
        message: str | None = None,
        *,
        code: int | None = None,
        data: Any = None,
        request_id: int | str | None = None,
        op: str | None = None,
        module: str | None = None,
        method: str | None = None,
    ) -> None:
        self.code = type(self).code if code is None else code
        spec = ERROR_TABLE.get(self.code)
        self.message = message if message is not None else (spec.message if spec else "error")
        self.data = data
        self.request_id = request_id
        self.op = op
        self.module = module
        self.method = method
        super().__init__(self._render())

    @property
    def logos_error_code(self) -> int | None:
        value = self.data.get("logos_error_code") if isinstance(self.data, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def logos_error_name(self) -> str | None:
        value = self.data.get("logos_error_name") if isinstance(self.data, dict) else None
        return value if isinstance(value, str) else None

    def _render(self) -> str:
        parts = [f"{self.message} (json-rpc {self.code}"]
        name = self.logos_error_name
        if name:
            parts.append(f", {name}")
        parts.append(")")
        if self.module and self.method:
            parts.append(f" calling {self.module}.{self.method}")
        elif self.op:
            parts.append(f" in {self.op}")
        return "".join(parts)

    @staticmethod
    def from_error(
        error: Any,
        *,
        request_id: int | str | None = None,
        op: str | None = None,
        module: str | None = None,
        method: str | None = None,
    ) -> RpcError:
        """Build the subclass matching ``error["code"]`` (base class for unknown codes)."""
        if not isinstance(error, Mapping):
            return RpcError(f"malformed error member: {error!r}", code=-32603, request_id=request_id,
                            op=op, module=module, method=method)
        code = error.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            code = -32603
        message = error.get("message")
        cls = RPC_ERROR_CLASSES.get(code, RpcError)
        return cls(
            message if isinstance(message, str) else None,
            code=code,
            data=error.get("data"),
            request_id=request_id,
            op=op,
            module=module,
            method=method,
        )


class ParseError(RpcError):
    code = -32700


class InvalidRequest(RpcError):
    code = -32600


class MethodNotFound(RpcError):
    """Unknown module or method, or one this bridge does not expose. Deliberately indistinct."""

    code = -32601


class MethodNotExposed(MethodNotFound):
    """The contract declares the member, but this bridge does not expose it. Raised locally."""

    def __init__(self, module: str, member: str, *, kind: str = "method") -> None:
        self.member = member
        self.kind = kind
        super().__init__(
            f"{module} declares {kind} {member!r}, but this bridge does not expose it "
            "(its policy restricts calls, not knowledge)",
            module=module, method=member if kind == "method" else None,
        )


class InvalidParams(RpcError):
    code = -32602

    @property
    def detail(self) -> InvalidParamsDetail | None:
        if not isinstance(self.data, dict):
            return None
        return InvalidParamsDetail.from_json(self.data.get("invalid_params_detail"))


class UpstreamCallFailed(RpcError):
    code = -32603


class CallNotDispatched(RpcError):
    code = -32000


class ModuleUnavailable(RpcError):
    code = -32001


class UpstreamTimeout(RpcError):
    """The bridge's own deadline elapsed. Not a ``TimeoutError``: the call may still run."""

    code = -32002


class UpstreamTransportError(RpcError):
    code = -32003


class NotAuthorised(RpcError):
    code = -32004


class Cancelled(RpcError):
    code = -32005


class ShuttingDown(RpcError):
    code = -32006


class Overloaded(RpcError):
    code = -32029


RPC_ERROR_CLASSES: Final[Mapping[int, type[RpcError]]] = {
    cls.code: cls
    for cls in (
        ParseError, InvalidRequest, MethodNotFound, InvalidParams, UpstreamCallFailed,
        CallNotDispatched, ModuleUnavailable, UpstreamTimeout, UpstreamTransportError,
        NotAuthorised, Cancelled, ShuttingDown, Overloaded,
    )
}


# -- Results that mean failure -----------------------------------------------


class ProviderRejection(BridgeError):
    """The provider ran and refused the call; the refusal arrived as the call's *result*."""

    def __init__(
        self,
        code: str,
        message: str,
        origin: str,
        *,
        module: str | None = None,
        method: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.origin = origin
        self.module = module
        self.method = method
        target = f" calling {module}.{method}" if module and method else ""
        super().__init__(f"{code}: {message} (origin {origin}){target}")


class ModuleResultError(BridgeError):
    """``unwrap_result`` on a ``{"success": false, ...}`` result."""

    def __init__(self, error: Any, result: Mapping[str, Any]) -> None:
        self.error = error
        self.result = result
        text = error if isinstance(error, str) and error else "module reported failure without an error message"
        super().__init__(text)


# -- Typed calls -------------------------------------------------------------


class ClientRejection(BridgeError, TypeError):
    """The contract says the provider would refuse this call, so it was not sent.

    ``code`` is the refusal a provider would have answered (``dispatch_failed`` or
    ``invalid_args``) and ``message`` its wording.
    """

    code: str = "dispatch_failed"

    def __init__(
        self,
        message: str,
        *,
        module: str | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        self.message = message
        self.module = module
        self.method = method
        self.path = path
        target = f" calling {module}.{method}" if module and method else ""
        super().__init__(f"{self.code}: {message} (refused by this client){target}")


class ArgumentError(ClientRejection):
    """An argument does not match its declared type (a provider answers ``dispatch_failed``)."""

    code = "dispatch_failed"


class ArityError(ClientRejection):
    """The wrong number of arguments (a provider answers ``invalid_args``)."""

    code = "invalid_args"

    def __init__(
        self,
        message: str,
        *,
        expected: int,
        got: int,
        module: str | None = None,
        method: str | None = None,
    ) -> None:
        self.expected = expected
        self.got = got
        super().__init__(message, module=module, method=method)


class DecodeError(BridgeError, ValueError):
    """A value from the bridge does not match its declared type."""

    def __init__(self, message: str, *, path: str | None = None, value: Any = None) -> None:
        self.reason = message
        self.path = path
        self.value = value
        super().__init__(message)


class ResultDecodeError(DecodeError):
    """A call's result does not match the method's declared return type."""

    def __init__(self, message: str, *, module: str, method: str, path: str | None = None,
                 value: Any = None) -> None:
        self.module = module
        self.method = method
        super().__init__(message, path=path, value=value)
        self.args = (f"{module}.{method} returned a value that does not match its contract: {message}",)


class EventDecodeError(DecodeError):
    """One event's payload does not match its declaration. ``event`` is the raw event."""

    def __init__(self, message: str, *, event: Any, path: str | None = None) -> None:
        self.event = event
        self.module = getattr(event, "module", None)
        self.name = getattr(event, "event", None)
        super().__init__(message, path=path, value=getattr(event, "data", None))
        self.args = (f"event {self.module}.{self.name} does not match its contract: {message}",)


class IncompatibleModule(BridgeError):
    """The module a bridge serves does not match the contract a client expects.

    ``report`` is the :class:`~logos_bridge.compat.CompatReport` that says why.
    """

    def __init__(self, message: str, report: Any = None) -> None:
        self.report = report
        super().__init__(message)


class UntypedModule(IncompatibleModule):
    """The bridge serves no valid ``lidl()`` contract for the module (untyped, invalid, or an old bridge)."""


class CodegenFormatError(BridgeError, ImportError):
    """Generated code needs a codegen format this logos-bridge does not support."""


class BindingIntegrityError(BridgeError, ImportError):
    """A generated module's embedded contract does not match its recorded digest."""


# -- Transport ---------------------------------------------------------------


class HttpStatusError(BridgeError):
    """A non-200 HTTP answer: a transport-level refusal, not a JSON-RPC error."""

    def __init__(
        self,
        status: int,
        url: str,
        *,
        body: bytes = b"",
        hint: str | None = None,
        error: RpcError | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.body = body
        self.hint = hint
        self.error = error
        text = f"HTTP {status} from {url}"
        if hint:
            text += f": {hint}"
        super().__init__(text)


HTTP_STATUS_HINTS: Final[Mapping[int, str]] = {
    401: "the bridge runs auth.mode 'bearer', which this client does not support",
    403: (
        "Host or Origin refused: the bridge accepts only Host 127.0.0.1, localhost or [::1] "
        "(optionally with its own port) and refuses any Origin not in http.allowed_origins; "
        "through a tunnel pass host_header='127.0.0.1:<bridge port>'"
    ),
    404: "no such route (GET /healthz, /modules, /modules/{module}; POST /rpc, /modules/{module}/{method})",
    415: "POST requires Content-Type: application/json",
}

# Why a WebSocket upgrade can be dropped without an HTTP answer (lws FILTER refusal).
DROPPED_UPGRADE_HINTS: Final[tuple[str, ...]] = (
    "Host: only the literals 127.0.0.1, localhost and [::1] (optionally with the bridge's port) are "
    "accepted; through an SSH tunnel or port forward pass host_header='127.0.0.1:<bridge port>'",
    "Origin: any Origin header is refused unless listed in http.allowed_origins",
    "per-peer cap: at most 8 connections per client address (limits.max_connections_per_peer); "
    "bridges without the keep-alive fix leak one slot per kept-alive HTTP request, so a pooled "
    "HTTP client can lock WebSocket clients out until the bridge restarts",
    "limits.max_connections (default 128) is reached",
    "auth.mode 'bearer': current bridges cannot verify tokens, so non-browser upgrades are refused",
    "subprotocol: the bridge speaks 'jsonrpc-bridge.v1' and refuses any other requested subprotocol",
)


class ConnectError(BridgeError, ConnectionError):
    """The WebSocket connection could not be opened."""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        hints: Sequence[str] = (),
        status: int | None = None,
    ) -> None:
        self.url = url
        self.hints = tuple(hints)
        self.status = status
        self.message = message
        text = f"cannot connect to {url}: {message}"
        if self.hints:
            text += "\npossible causes:\n" + "\n".join(f"  - {h}" for h in self.hints)
        super().__init__(text)


CLOSE_CODE_NAMES: Final[Mapping[int, str]] = {
    1000: "NORMAL_CLOSURE",
    1001: "GOING_AWAY",
    1002: "PROTOCOL_ERROR",
    1003: "UNSUPPORTED_DATA",
    1005: "NO_STATUS_RCVD",
    1006: "ABNORMAL_CLOSURE",
    1007: "INVALID_DATA",
    1008: "POLICY_VIOLATION",
    1009: "MESSAGE_TOO_BIG",
    1010: "MANDATORY_EXTENSION",
    1011: "INTERNAL_ERROR",
    1012: "SERVICE_RESTART",
    1013: "TRY_AGAIN_LATER",
    1014: "BAD_GATEWAY",
    1015: "TLS_HANDSHAKE",
}

CLOSE_CODE_HINTS: Final[Mapping[int, str]] = {
    1000: "the connection was closed normally",
    1001: "the peer is going away",
    1003: "the bridge accepts text frames only; a binary frame closes the connection",
    1006: (
        "the connection dropped without a close frame: the bridge was stopped or unloaded, its "
        "host process exited, or the network path failed; this client does not reconnect"
    ),
    1007: "a text frame was not valid UTF-8",
    1008: (
        "the bridge closed a slow reader: more than limits.max_queued_frames_per_connection "
        "(default 256) frames were queued for this connection, e.g. while this process or its "
        "event loop was blocked"
    ),
    1009: (
        "a request exceeded the bridge's limits.max_frame_bytes (default 1 MiB); keep "
        "max_request_size at or below it"
    ),
    1011: "internal error",
}

_KEEPALIVE_HINT: Final = (
    "no pong within ping_timeout: the bridge's service thread is stalled or the network path is broken"
)


def close_code_name(code: int) -> str:
    name = CLOSE_CODE_NAMES.get(code)
    if name:
        return name
    if 3000 <= code <= 3999:
        return "REGISTERED"
    if 4000 <= code <= 4999:
        return "PRIVATE"
    return "UNKNOWN"


def close_hint(code: int, *, initiated_by: str, reason: str = "", server_error: RpcError | None = None) -> str | None:
    """A human hint for a close code, as this bridge uses them."""
    if isinstance(server_error, ShuttingDown):
        return "the bridge is shutting down"
    if code == 1011 and initiated_by == "client" and "keepalive" in reason:
        return _KEEPALIVE_HINT
    if code == 1000 and initiated_by == "server":
        return "the bridge closed the connection"
    return CLOSE_CODE_HINTS.get(code)


class ConnectionClosed(BridgeError, ConnectionError):
    """The WebSocket connection ended. Raised fresh to every waiter.

    ``initiated_by`` is ``"server"`` or ``"client"`` (whoever sent the first close frame),
    or ``"transport"`` when no close frame was exchanged (code 1006).
    ``server_error`` carries an unattributable bridge error seen before the close.
    """

    def __init__(
        self,
        code: int,
        reason: str = "",
        *,
        initiated_by: str,
        server_error: RpcError | None = None,
        hint: str | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.code_name = close_code_name(code)
        self.initiated_by = initiated_by
        self.server_error = server_error
        self.hint = hint if hint is not None else close_hint(
            code, initiated_by=initiated_by, reason=reason, server_error=server_error
        )
        text = f"connection closed: {code} {self.code_name}"
        if reason:
            text += f" ({reason})"
        text += f", initiated by {initiated_by}"
        if self.hint:
            text += f": {self.hint}"
        if server_error is not None:
            text += f"; last bridge error: {server_error}"
        super().__init__(text)


class ClientTimeout(BridgeError, TimeoutError):
    """This client's own deadline elapsed. The bridge may still run the call."""

    def __init__(
        self,
        timeout: float,
        *,
        op: str | None = None,
        module: str | None = None,
        method: str | None = None,
        request_id: int | str | None = None,
        what: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.op = op
        self.module = module
        self.method = method
        self.request_id = request_id
        target = what or (f"{module}.{method}" if module and method else (op or "request"))
        super().__init__(f"{target} got no answer within {timeout:g}s")


class DiscoveryPending(ClientTimeout):
    """The bridge was still discovering the module's contract when the wait ended."""

    def __init__(self, module: str, waited: float) -> None:
        super().__init__(waited, module=module, what=f"contract discovery of {module} (interface_status: pending)")


class SubscriptionTerminated(BridgeError):
    """The bridge ended the subscription (``rpc.subscription_terminated``).

    ``reason`` is ``provider_unavailable`` (the provider went away) or
    ``provider_changed`` (the bridge found a different build of the module; a typed
    consumer should re-run its compatibility check before subscribing again).
    """

    def __init__(
        self,
        subscription: str | int,
        module: str,
        event: str,
        reason: str,
        *,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        self.subscription = subscription
        self.module = module
        self.event = event
        self.reason = reason
        self.raw = dict(raw or {})
        super().__init__(
            f"subscription {subscription} to {module}.{event} terminated: {reason}; "
            "events in between are lost, subscribe again for a fresh stream"
        )


class SubscriptionOverflow(BridgeError):
    """More than ``max_pending`` events were queued and unconsumed."""

    def __init__(self, max_pending: int, subscriptions: Sequence[str]) -> None:
        self.max_pending = max_pending
        self.subscriptions = tuple(subscriptions)
        super().__init__(
            f"more than max_pending={max_pending} events queued on {', '.join(self.subscriptions)}; "
            "the subscription was dropped"
        )


class SubscriptionClosed(BridgeError):
    """The subscription was closed by this client and has no queued events left."""


class RequestTooLarge(BridgeError, ValueError):
    """The encoded request exceeds ``max_request_size``; nothing was sent."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"request is {size} bytes, over max_request_size={limit}; nothing was sent")


class BytesDecodeError(BridgeError, ValueError):
    """A ``{"_bytes": ...}`` tag is not unpadded base64url."""

    def __init__(self, reason: str, value: Any = None) -> None:
        self.reason = reason
        self.value = value
        super().__init__(f"invalid _bytes tag: {reason}")


class PortalStopped(BridgeError, RuntimeError):
    """The blocking portal was stopped."""


class BlockingCallInEventLoop(BridgeError, RuntimeError):
    """A blocking call was made from an event loop thread, where it would stall or deadlock."""
