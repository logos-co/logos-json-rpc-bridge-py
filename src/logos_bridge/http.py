"""Blocking HTTP client for the bridge (``POST /rpc`` and the read-only GET routes).

Standard library only. Environment proxies are ignored, every request carries
``Connection: close`` (a kept-alive connection holds one of the bridge's eight
per-peer slots), and no ``Origin`` is ever sent. HTTP has no subscriptions.
"""

from __future__ import annotations

import http.client
import itertools
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from . import _protocol as proto
from ._transport import USER_AGENT, HeadersLike, normalize_headers, validate_host_header
from .codec import decode_bytes_tags, dumps, encode_args, loads, rejection_from_result
from .errors import (
    HTTP_STATUS_HINTS,
    BridgeError,
    ClientTimeout,
    ConnectError,
    ConnectionClosed,
    HttpStatusError,
    MethodNotFound,
    RequestTooLarge,
    RpcError,
)
from .models import Health, ModuleInfo

_DROPPED_HINT: Final = (
    "the bridge dropped the request without an answer: a body over limits.max_body_bytes, "
    "or the bridge stopped"
)


def _parse_error_body(body: bytes) -> RpcError | None:
    try:
        parsed = loads(body)
    except ValueError:
        return None
    if isinstance(parsed, dict) and proto.is_json_int(parsed.get("code")):
        return RpcError.from_error(parsed)
    return None


def status_error(status: int, url: str, body: bytes = b"") -> HttpStatusError:
    """The :class:`~logos_bridge.errors.HttpStatusError` for a non-200 answer, with a hint."""
    return HttpStatusError(status, url, body=body, hint=HTTP_STATUS_HINTS.get(status),
                           error=_parse_error_body(body))


class BridgeHttpClient:
    """One-shot HTTP requests to a bridge.

    ``call_timeout`` (default ``None``: wait for the bridge's own deadline) applies to
    :meth:`call`; ``op_timeout`` to everything else. Both are socket timeouts.
    """

    def __init__(
        self,
        url: str = proto.DEFAULT_HTTP_URL,
        *,
        call_timeout: float | None = None,
        op_timeout: float | None = 30.0,
        host_header: str | None = None,
        max_request_size: int | None = 2**20,
        max_response_size: int = 64 * 2**20,
        extra_headers: HeadersLike | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"bridge HTTP URL must be http(s)://host:port, got {url!r}")
        self._base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        self._call_timeout = call_timeout
        self._op_timeout = op_timeout
        self._host_header = validate_host_header(host_header) if host_header is not None else None
        self._max_request_size = max_request_size
        self._max_response_size = max_response_size
        self._extra_headers = normalize_headers(extra_headers)
        self._user_agent = user_agent
        self._ids = itertools.count(1)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:
        return f"<BridgeHttpClient {self._base}>"

    @property
    def url(self) -> str:
        return self._base

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, body: bytes | None) -> urllib.request.Request:
        req = urllib.request.Request(self._base + path, data=body, method=method)
        for name, value in self._extra_headers:
            req.add_header(name, value)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self._user_agent)
        req.add_header("Connection", "close")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self._host_header is not None:
            req.add_header("Host", self._host_header)
        return req

    def _open(self, req: urllib.request.Request, timeout: float | None, what: str) -> tuple[int, bytes]:
        limit = self._max_response_size
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status = int(resp.status)
                body = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(limit + 1)
            finally:
                exc.close()
            return exc.code, body
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise ClientTimeout(timeout or 0.0, what=what) from None
            if isinstance(reason, ConnectionRefusedError):
                raise ConnectError("connection refused", url=self._base, hints=(
                    "nothing listens there: is json_rpc_bridge loaded and started, and is http.port "
                    "the port in the URL?",
                )) from exc
            if isinstance(reason, ConnectionError):  # reset or broken pipe while sending
                raise ConnectionClosed(1006, str(reason), initiated_by="transport", hint=_DROPPED_HINT) from exc
            raise ConnectError(str(reason), url=self._base) from exc
        except TimeoutError:
            raise ClientTimeout(timeout or 0.0, what=what) from None
        except (ConnectionError, http.client.HTTPException) as exc:
            raise ConnectionClosed(1006, str(exc), initiated_by="transport", hint=_DROPPED_HINT) from exc
        if len(body) > limit:
            raise BridgeError(f"response to {what} exceeds max_response_size={limit}")
        return status, body

    def _get(self, path: str, what: str) -> tuple[int, bytes]:
        return self._open(self._request("GET", path, None), self._op_timeout, what)

    def _rpc(
        self,
        op: str,
        params: Any,
        *,
        timeout: float | None,
        module: str | None = None,
        method: str | None = None,
    ) -> Any:
        req_id = next(self._ids)
        body = dumps(proto.make_request(req_id, op, params))
        if self._max_request_size is not None and len(body) > self._max_request_size:
            raise RequestTooLarge(len(body), self._max_request_size)
        what = f"{module}.{method}" if module and method else op
        status, answer = self._open(self._request("POST", "/rpc", body), timeout, what)
        if status != 200:
            raise status_error(status, self._base + "/rpc", answer)
        try:
            decoded = loads(answer)
        except ValueError:
            raise BridgeError(f"unparseable answer to {what}: {answer[:200]!r}") from None
        frame = proto.classify(decoded)
        if frame.kind in (proto.FrameKind.ERROR, proto.FrameKind.UNCORRELATED_ERROR):
            # Over HTTP an id:null error still belongs to this request.
            raise RpcError.from_error(frame.error, request_id=frame.id, op=op, module=module, method=method)
        if frame.kind is proto.FrameKind.RESULT:
            if frame.id != req_id:
                raise BridgeError(f"answer to {what} carries id {frame.id!r}, expected {req_id}")
            return frame.result
        raise BridgeError(f"unexpected answer to {what}: {answer[:200]!r}")

    # ------------------------------------------------------------- public

    def healthz(self) -> Health:
        """``GET /healthz``."""
        status, body = self._get("/healthz", "GET /healthz")
        if status != 200:
            raise status_error(status, self._base + "/healthz", body)
        return Health.from_json(loads(body))

    def list_modules(self) -> list[ModuleInfo]:
        """``GET /modules``."""
        status, body = self._get("/modules", "GET /modules")
        if status != 200:
            raise status_error(status, self._base + "/modules", body)
        decoded = loads(body)
        if not isinstance(decoded, list):
            raise BridgeError(f"unexpected GET /modules answer: {body[:200]!r}")
        return [ModuleInfo.from_json(item) for item in decoded]

    def schema(self, module: str) -> ModuleInfo:
        """``GET /modules/{module}``; a 404 raises :class:`~logos_bridge.errors.MethodNotFound`."""
        if not isinstance(module, str) or not module:
            raise ValueError("module must be a non-empty string")
        path = "/modules/" + urllib.parse.quote(module, safe="")
        status, body = self._get(path, f"GET {path}")
        if status == 404:
            error = _parse_error_body(body)
            if isinstance(error, MethodNotFound):
                error.op = proto.OP_SCHEMA
                error.module = module
                raise error
            raise MethodNotFound(op=proto.OP_SCHEMA, module=module)
        if status != 200:
            raise status_error(status, self._base + path, body)
        return ModuleInfo.from_json(loads(body))

    def ping(self) -> float:
        """``rpc.ping``; returns the round-trip time in seconds."""
        started = time.monotonic()
        result = self._rpc(proto.OP_PING, None, timeout=self._op_timeout)
        if result != "pong":
            raise BridgeError(f"unexpected rpc.ping answer: {result!r}")
        return time.monotonic() - started

    def request(self, op: str, params: Any = None, *, timeout: float | None = None) -> Any:
        """A raw bridge operation over ``POST /rpc``. Subscriptions need the WebSocket client."""
        if op in proto.STREAM_OPS:
            raise ValueError(f"{op} is not available over HTTP; use AsyncBridgeClient/BridgeClient")
        return self._rpc(op, params, timeout=self._op_timeout if timeout is None else timeout)

    def call(
        self,
        module: str,
        method: str,
        *args: Any,
        timeout: float | None = None,
        decode_bytes: bool = True,
        detect_rejection: bool = True,
    ) -> Any:
        """``rpc.call`` over ``POST /rpc``; same value rules as the WebSocket client."""
        if not isinstance(module, str) or not module or not isinstance(method, str) or not method:
            raise ValueError("module and method must be non-empty strings")
        params = {"module": module, "method": method, "params": encode_args(args)}
        result = self._rpc(
            proto.OP_CALL, params, timeout=self._call_timeout if timeout is None else timeout,
            module=module, method=method,
        )
        if detect_rejection:
            rejection = rejection_from_result(result, module=module, method=method)
            if rejection is not None:
                raise rejection
        return decode_bytes_tags(result) if decode_bytes else result


def healthz(url: str = proto.DEFAULT_HTTP_URL, *, timeout: float | None = 5.0, host_header: str | None = None) -> Health:
    """One ``GET /healthz``."""
    return BridgeHttpClient(url, op_timeout=timeout, host_header=host_header).healthz()


def call(
    module: str,
    method: str,
    *args: Any,
    url: str = proto.DEFAULT_HTTP_URL,
    timeout: float | None = None,
    host_header: str | None = None,
    decode_bytes: bool = True,
    detect_rejection: bool = True,
) -> Any:
    """One ``rpc.call`` over HTTP."""
    client = BridgeHttpClient(url, call_timeout=timeout, host_header=host_header)
    return client.call(module, method, *args, decode_bytes=decode_bytes, detect_rejection=detect_rejection)
