"""Builds the arguments of ``websockets.asyncio.client.connect`` for the bridge."""

from __future__ import annotations

import platform
import re
import urllib.parse
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias

from websockets.version import version as websockets_version

from ._version import __version__
from .errors import BridgeWarning

USER_AGENT: Final = (
    f"logos-bridge-py/{__version__} websockets/{websockets_version} "
    f"Python/{platform.python_version()}"
)

HeadersLike: TypeAlias = "Mapping[str, str] | Sequence[tuple[str, str]]"

# Headers websockets writes itself; a second copy breaks the handshake.
_MANAGED_HEADERS: Final = frozenset(
    {
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-protocol",
        "sec-websocket-extensions",
    }
)
_HOST_HEADER: Final = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?")


@dataclass(frozen=True)
class ConnectPlan:
    """The handshake URI plus keyword arguments for ``connect()``."""

    uri: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    dial_host: str = ""
    dial_port: int = 0


def normalize_headers(extra_headers: HeadersLike | None) -> list[tuple[str, str]]:
    """Validate user headers: ``Host`` is refused (use ``host_header``), ``Origin`` warns."""
    if extra_headers is None:
        return []
    items = list(extra_headers.items()) if isinstance(extra_headers, Mapping) else list(extra_headers)
    out: list[tuple[str, str]] = []
    for item in items:
        if not (isinstance(item, tuple) and len(item) == 2):
            raise TypeError("extra_headers must be a mapping or a sequence of (name, value) pairs")
        name, value = item
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("header names and values must be str")
        if any(c in name + value for c in "\r\n\0"):
            raise ValueError(f"header {name!r} contains a line break or NUL")
        lowered = name.strip().lower()
        if lowered == "host":
            raise ValueError("set the Host header with host_header=..., not extra_headers")
        if lowered in _MANAGED_HEADERS:
            raise ValueError(f"{name} is managed by the WebSocket handshake and cannot be overridden")
        if lowered == "origin":
            warnings.warn(
                "an Origin header makes the bridge refuse the connection unless the origin is "
                "listed in http.allowed_origins",
                BridgeWarning,
                stacklevel=4,
            )
        out.append((name, value))
    return out


def validate_host_header(host_header: str) -> str:
    if not isinstance(host_header, str) or _HOST_HEADER.fullmatch(host_header) is None:
        raise ValueError(f"host_header must look like 'host' or 'host:port', got {host_header!r}")
    port = host_header.rsplit(":", 1)[1] if host_header.rfind(":") > host_header.rfind("]") else None
    if port is not None and not 0 < int(port) < 65536:
        raise ValueError(f"host_header port out of range: {host_header!r}")
    return host_header


def build_connect_plan(
    url: str,
    *,
    subprotocol: str,
    open_timeout: float | None,
    ping_interval: float | None,
    ping_timeout: float | None,
    close_timeout: float | None,
    max_message_size: int | None,
    host_header: str | None = None,
    extra_headers: HeadersLike | None = None,
    user_agent: str = USER_AGENT,
) -> ConnectPlan:
    """Arguments that make websockets behave like a well-mannered bridge client.

    No Origin, no compression (the bridge negotiates none), no proxy from the
    environment, an unbounded incoming queue (the client's reader never stops
    reading, so the bridge never sees a slow reader), and the configured
    subprotocol. ``host_header`` is spelled into the handshake URI while the real
    target is dialled through ``host``/``port``.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError(f"bridge URL must be ws:// or wss://, got {url!r}")
    if parts.hostname is None:
        raise ValueError(f"bridge URL has no host: {url!r}")
    if parts.fragment:
        raise ValueError(f"bridge URL must not have a fragment: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("bridge URLs carry no credentials")
    try:
        dial_port = parts.port or (443 if parts.scheme == "wss" else 80)
    except ValueError as exc:
        raise ValueError(f"bad port in bridge URL {url!r}: {exc}") from None
    dial_host = parts.hostname
    if not subprotocol:
        raise ValueError("subprotocol must be a non-empty string")

    headers = normalize_headers(extra_headers)
    kwargs: dict[str, Any] = {
        "origin": None,
        "extensions": None,
        "compression": None,
        "proxy": None,
        "subprotocols": [subprotocol],
        "additional_headers": headers or None,
        "user_agent_header": user_agent,
        "open_timeout": open_timeout,
        "ping_interval": ping_interval,
        "ping_timeout": ping_timeout,
        "close_timeout": close_timeout,
        "max_size": max_message_size,
        "max_queue": None,
    }
    uri = url
    if host_header is not None:
        validate_host_header(host_header)
        uri = urllib.parse.urlunsplit((parts.scheme, host_header, parts.path, parts.query, ""))
        kwargs["host"] = dial_host
        kwargs["port"] = dial_port
        if parts.scheme == "wss":
            kwargs["server_hostname"] = dial_host
    return ConnectPlan(uri=uri, kwargs=kwargs, dial_host=dial_host, dial_port=dial_port)
