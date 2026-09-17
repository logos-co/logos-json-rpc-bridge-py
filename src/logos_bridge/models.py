"""Value objects decoded from bridge answers. Unknown keys are kept in ``raw``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_or(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _require_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Event:
    """One ``rpc.event`` notification. ``data`` is the upstream payload, verbatim."""

    subscription: str | int
    module: str
    event: str
    data: Any
    generation: int
    ts: int

    def decoded_data(self) -> Any:
        """``data`` with ``{"_bytes": ...}`` tags decoded to ``bytes`` (strict)."""
        from .codec import decode_bytes_tags  # models stays a leaf module

        return decode_bytes_tags(self.data)


@dataclass(frozen=True)
class Exposure:
    """What this bridge lets a client call and subscribe to (bridges with ``lidl()`` discovery)."""

    methods: tuple[str, ...]
    events: tuple[str, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> Exposure:
        data = _require_object(obj, "exposure")
        return cls(_str_tuple(data.get("methods")), _str_tuple(data.get("events")), data)


@dataclass(frozen=True)
class CrossCheckFinding:
    """One finding of the bridge's contract-vs-live cross-check."""

    severity: str
    code: str
    detail: str
    member: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> CrossCheckFinding:
        data = _require_object(obj, "cross_check finding")
        return cls(
            severity=_str_or(data.get("severity"), "info"),
            code=_str_or(data.get("code"), ""),
            detail=_str_or(data.get("detail"), ""),
            member=_opt_str(data.get("member")),
            raw=data,
        )


@dataclass(frozen=True)
class CrossCheck:
    """``cross_check``: ``state`` is ``consistent``, ``warnings`` or ``inconsistent``."""

    state: str
    findings: tuple[CrossCheckFinding, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> CrossCheck:
        data = _require_object(obj, "cross_check")
        findings = data.get("findings")
        items = findings if isinstance(findings, list) else []
        return cls(
            state=_str_or(data.get("state"), "unknown"),
            findings=tuple(CrossCheckFinding.from_json(f) for f in items if isinstance(f, dict)),
            raw=data,
        )

    def by_severity(self, severity: str) -> tuple[CrossCheckFinding, ...]:
        return tuple(f for f in self.findings if f.severity == severity)

    @property
    def errors(self) -> tuple[CrossCheckFinding, ...]:
        return self.by_severity("error")

    @property
    def warnings(self) -> tuple[CrossCheckFinding, ...]:
        return self.by_severity("warning")


_MODULE_INFO_KEYS = frozenset(
    {
        "module", "resolved", "events_declared", "methods", "events", "source", "authoritative",
        "interface_status", "stale", "interface", "exposure", "interface_sha256", "contract_sha256",
        "interface_error", "cross_check",
    }
)

#: The discovery statuses of a bridge that reads contracts through ``lidl()``.
INTERFACE_STATUSES = ("pending", "ok", "untyped", "invalid")


@dataclass(frozen=True)
class ModuleInfo:
    """A module's bridge-derived view (``rpc.schema`` / ``GET /modules/{m}``).

    ``methods``/``events`` are the live names this bridge's policy permits. The
    ``interface_*``, ``stale``, ``exposure``, digest and ``cross_check`` fields exist
    only on bridges that discover contracts through ``lidl()``; they are ``None``
    otherwise. ``interface`` (the full contract) is served by ``rpc.schema`` for
    ``ok`` modules only, never by ``rpc.list_modules``.
    """

    module: str
    resolved: bool
    events_declared: bool
    methods: tuple[str, ...]
    events: tuple[str, ...]
    source: str | None
    authoritative: bool
    interface_status: str | None = None
    interface: Mapping[str, Any] | None = None
    exposure: Exposure | None = None
    interface_sha256: str | None = None
    contract_sha256: str | None = None
    interface_error: str | None = None
    cross_check: CrossCheck | None = None
    stale: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> ModuleInfo:
        data = _require_object(obj, "module info")
        module = data.get("module")
        if not isinstance(module, str):
            raise TypeError('module info needs a string "module"')
        interface = data.get("interface")
        cross_check = data.get("cross_check")
        exposure = data.get("exposure")
        error = data.get("interface_error")
        stale = data.get("stale")
        return cls(
            module=module,
            resolved=data.get("resolved") is True,
            events_declared=data.get("events_declared") is True,
            methods=_str_tuple(data.get("methods")),
            events=_str_tuple(data.get("events")),
            source=_opt_str(data.get("source")),
            authoritative=data.get("authoritative") is True,
            interface_status=_opt_str(data.get("interface_status")),
            interface=interface if isinstance(interface, dict) else None,
            exposure=Exposure.from_json(exposure) if isinstance(exposure, dict) else None,
            interface_sha256=_opt_str(data.get("interface_sha256")),
            contract_sha256=_opt_str(data.get("contract_sha256")),
            interface_error=error if isinstance(error, str) or error is None else str(error),
            cross_check=CrossCheck.from_json(cross_check) if isinstance(cross_check, dict) else None,
            stale=stale if isinstance(stale, bool) else None,
            raw=data,
        )

    @property
    def typed(self) -> bool:
        """True when the bridge serves a validated ``lidl()`` contract for this module."""
        return self.interface_status == "ok"

    @property
    def pending(self) -> bool:
        return self.interface_status == "pending"

    @property
    def callable_methods(self) -> tuple[str, ...]:
        """``exposure.methods`` when the bridge reports it, else the live names."""
        return self.exposure.methods if self.exposure is not None else self.methods

    @property
    def subscribable_events(self) -> tuple[str, ...]:
        return self.exposure.events if self.exposure is not None else self.events

    @property
    def extra(self) -> dict[str, Any]:
        """Keys this version does not model."""
        return {k: v for k, v in self.raw.items() if k not in _MODULE_INFO_KEYS}


@dataclass(frozen=True)
class Health:
    """``GET /healthz``."""

    status: str
    uptime_seconds: int | None
    protocol: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> Health:
        data = _require_object(obj, "health")
        status = data.get("status")
        uptime = data.get("uptime_seconds")
        return cls(
            status=status if isinstance(status, str) else "unknown",
            uptime_seconds=uptime if isinstance(uptime, int) and not isinstance(uptime, bool) else None,
            protocol=_opt_str(data.get("protocol")),
            raw=data,
        )

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def draining(self) -> bool:
        return self.status == "draining"


@dataclass(frozen=True)
class InvalidParamsDetail:
    """``error.data.invalid_params_detail`` of a -32602."""

    reason: str
    path: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> InvalidParamsDetail | None:
        if not isinstance(obj, dict) or not isinstance(obj.get("reason"), str):
            return None
        return cls(reason=obj["reason"], path=_opt_str(obj.get("path")), raw=obj)
