"""Does the module a bridge serves match the contract a client expects?

Levels, strictest first:

* ``exact``: the served ``interface_sha256`` is the expected one;
* ``shape``: same module, and every expected method and event is served with the
  same :meth:`~logos_bridge.lidl.Interface.shape` signature (descriptions,
  metadata and the optional spelling may differ; extra members are fine);
* ``structural``: every expected member is served with the same structural
  signature (module, parameter and record names may differ);
* ``names``: the module is ``untyped``/``invalid`` (or the bridge predates
  ``lidl()`` discovery); calls go untyped. Only with ``allow_untyped=True``.

``pending`` is waited out first. ``not_exposed`` lists members of both contracts
that this bridge's policy does not let a client use.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from .errors import BridgeWarning, IncompatibleModule, UntypedModule
from .lidl import Interface
from .lidl_sources import interface_from_module_info
from .models import ModuleInfo

if TYPE_CHECKING:
    from .client import AsyncBridgeClient

LEVELS: Final = ("exact", "shape", "structural", "names")
TYPED_LEVELS: Final = frozenset({"exact", "shape", "structural"})

Kind = Literal["method", "event"]


@dataclass(frozen=True)
class CompatMembers:
    """Method and event names."""

    methods: tuple[str, ...] = ()
    events: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.methods or self.events)

    def describe(self) -> str:
        parts = []
        if self.methods:
            parts.append("methods " + ", ".join(self.methods))
        if self.events:
            parts.append("events " + ", ".join(self.events))
        return "; ".join(parts)


@dataclass(frozen=True)
class CompatReport:
    """The outcome of comparing an expected contract with a bridge's view of a module."""

    module: str
    status: str | None
    level: str | None
    expected_interface_sha256: str
    expected_shape_sha256: str
    served_interface_sha256: str | None = None
    served_shape_sha256: str | None = None
    served_contract_sha256: str | None = None
    missing: CompatMembers = CompatMembers()
    mismatched: CompatMembers = CompatMembers()
    extra: CompatMembers = CompatMembers()
    not_exposed: CompatMembers = CompatMembers()
    interface_error: str | None = None
    notes: tuple[str, ...] = ()
    info: ModuleInfo | None = field(default=None, compare=False, repr=False)
    served: Interface | None = field(default=None, compare=False, repr=False)

    @property
    def compatible(self) -> bool:
        return self.level is not None

    @property
    def typed(self) -> bool:
        return self.level in TYPED_LEVELS

    def describe(self) -> str:
        """A one-line summary."""
        status = self.status or "no lidl discovery"
        if self.level is None:
            head = f"{self.module}: incompatible (status {status})"
        else:
            head = f"{self.module}: {self.level} (status {status})"
        details = []
        for label, members in (("missing", self.missing), ("different", self.mismatched),
                               ("not exposed", self.not_exposed)):
            if members:
                details.append(f"{label}: {members.describe()}")
        if self.interface_error:
            details.append(f"interface_error: {self.interface_error}")
        return head + ("; " + "; ".join(details) if details else "")

    def require(self, methods: Iterable[str] = (), events: Iterable[str] = ()) -> CompatReport:
        """Raise :class:`IncompatibleModule` unless every named member is usable.

        Usable means: in the served contract (or among the live names, for
        ``names``), with a matching signature, and exposed by this bridge.
        """
        problems = []
        for kind, names, missing, mismatched, hidden in (
            ("method", list(methods), self.missing.methods, self.mismatched.methods, self.not_exposed.methods),
            ("event", list(events), self.missing.events, self.mismatched.events, self.not_exposed.events),
        ):
            for name in names:
                if name in missing:
                    problems.append(f"{kind} {name} is not served")
                elif name in mismatched:
                    problems.append(f"{kind} {name} has a different signature")
                elif name in hidden:
                    problems.append(f"{kind} {name} is not exposed by this bridge")
        if problems:
            raise IncompatibleModule(f"{self.module}: " + "; ".join(problems), self)
        return self

    def raise_if_incompatible(self) -> CompatReport:
        if self.level is not None:
            return self
        if self.status != "ok":
            detail = f": {self.interface_error}" if self.interface_error else ""
            raise UntypedModule(
                f"{self.module} does not expose a valid lidl() contract "
                f"(status: {self.status or 'no lidl discovery'}{detail}); calls would be untyped, "
                "pass allow_untyped=True to accept that",
                self,
            )
        raise IncompatibleModule(
            f"{self.module} serves a contract this client was not generated for: "
            f"{self.describe()}; regenerate the client from the module's lidl()",
            self,
        )


def _members(iface: Interface) -> dict[Kind, tuple[str, ...]]:
    return {"method": iface.method_names, "event": iface.event_names}


def compare(expected: Interface, info: ModuleInfo, *, expected_interface_sha256: str | None = None,
            allow_untyped: bool = False) -> CompatReport:
    """Classify ``info`` against ``expected`` (no I/O, never raises for a mismatch)."""
    exposure = info.exposure
    expected_digest = expected_interface_sha256 or expected.interface_sha256()
    expected_shape = expected.shape_sha256()

    def report(level: str | None, **fields: Any) -> CompatReport:
        return CompatReport(
            module=info.module, status=info.interface_status, level=level,
            expected_interface_sha256=expected_digest, expected_shape_sha256=expected_shape,
            served_contract_sha256=info.contract_sha256, interface_error=info.interface_error,
            info=info, **fields,
        )
    if info.interface_status == "ok" and info.interface is not None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", BridgeWarning)
            served = interface_from_module_info(info)
        notes = tuple(str(w.message) for w in caught)
        served_digest = info.interface_sha256 or served.interface_sha256()
        missing: dict[Kind, list[str]] = {"method": [], "event": []}
        shape_diff: dict[Kind, list[str]] = {"method": [], "event": []}
        structural_diff: dict[Kind, list[str]] = {"method": [], "event": []}
        served_names = _members(served)
        for kind, names in _members(expected).items():
            for name in names:
                if name not in served_names[kind]:
                    missing[kind].append(name)
                    continue
                if expected.member_signature(kind, name) != served.member_signature(kind, name):
                    shape_diff[kind].append(name)
                if (expected.member_signature(kind, name, structural=True)
                        != served.member_signature(kind, name, structural=True)):
                    structural_diff[kind].append(name)
        level: str | None
        if served_digest == expected_digest:
            level = "exact"
        elif any(missing.values()) or any(structural_diff.values()):
            level = None
        elif expected.name == served.name and not any(shape_diff.values()):
            level = "shape"
        else:
            level = "structural"
        expected_names = _members(expected)
        extra = {k: tuple(n for n in served_names[k] if n not in expected_names[k]) for k in served_names}
        both = {k: [n for n in expected_names[k] if n in served_names[k]] for k in expected_names}
        hidden = _not_exposed(both, exposure)
        return report(
            level,
            served_interface_sha256=served_digest,
            served_shape_sha256=served.shape_sha256(),
            missing=CompatMembers(tuple(missing["method"]), tuple(missing["event"])),
            mismatched=CompatMembers(tuple(structural_diff["method"]), tuple(structural_diff["event"])),
            extra=CompatMembers(extra["method"], extra["event"]),
            not_exposed=hidden,
            notes=notes,
            served=served,
        )
    if info.interface_status == "pending":
        return report(None, notes=("discovery is still pending",))
    # Names only: what the live report and the exposure say.
    live: dict[Kind, tuple[str, ...]] = {"method": info.methods, "event": info.events}
    if exposure is not None:
        live = {"method": tuple(dict.fromkeys(info.methods + exposure.methods)),
                "event": tuple(dict.fromkeys(info.events + exposure.events))}
    known: dict[Kind, bool] = {"method": info.resolved and bool(live["method"]),
                               "event": info.resolved and info.events_declared}
    missing_names: dict[Kind, tuple[str, ...]] = {
        k: tuple(n for n in _members(expected)[k] if known[k] and n not in live[k]) for k in live
    }
    present = {k: [n for n in _members(expected)[k] if n not in missing_names[k]] for k in live}
    return report(
        "names" if allow_untyped else None,
        missing=CompatMembers(missing_names["method"], missing_names["event"]),
        not_exposed=_not_exposed(present, exposure),
    )


def _not_exposed(members: dict[Kind, list[str]], exposure: object) -> CompatMembers:
    if exposure is None:
        return CompatMembers()
    methods = getattr(exposure, "methods", ())
    events = getattr(exposure, "events", ())
    return CompatMembers(
        tuple(n for n in members["method"] if n not in methods),
        tuple(n for n in members["event"] if n not in events),
    )


async def check_compat(bridge: AsyncBridgeClient, module: str, expected: Interface, *,
                       expected_interface_sha256: str | None = None, allow_untyped: bool = False,
                       discovery_wait: float | None = 10.0) -> CompatReport:
    """Wait out ``pending``, compare, and raise :class:`IncompatibleModule` (or
    :class:`UntypedModule`) unless a level applies.

    Re-run it after a subscription ends with ``provider_changed``: the bridge found
    a different build of the module.
    """
    info = await bridge.wait_for_module(module, discovery_wait)
    report = compare(expected, info, expected_interface_sha256=expected_interface_sha256,
                     allow_untyped=allow_untyped)
    return report.raise_if_incompatible()
