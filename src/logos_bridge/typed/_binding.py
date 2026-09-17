"""What a generated client module holds: its embedded contract, checked at import."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..digest import shape_sha256
from ..errors import BindingIntegrityError, EventDecodeError
from ..lidl import Interface, LidlFormatError
from ..models import Event
from ._plans import InterfacePlans
from ._subscription import TypedSubscribeRequest

if TYPE_CHECKING:
    from ..client import AsyncBridgeClient
    from ..compat import CompatReport


class Binding:
    """The runtime side of a generated module.

    ``interface`` is the embedded :meth:`~logos_bridge.lidl.Interface.shape`
    document; it must hash to ``shape_digest``, or the import fails with
    :class:`~logos_bridge.errors.BindingIntegrityError` (the file was edited).
    """

    def __init__(self, interface: Mapping[str, Any], shape_digest: str, interface_digest: str, *,
                 records: Mapping[str, type], events: Mapping[str, type]) -> None:
        try:
            actual = shape_sha256(interface)
            self.interface = Interface.from_shape(interface)
        except (LidlFormatError, TypeError, ValueError) as exc:
            raise BindingIntegrityError(f"the embedded INTERFACE is not a shape document: {exc}") from None
        if actual != shape_digest:
            raise BindingIntegrityError(
                f"the embedded INTERFACE hashes to {actual}, not SHAPE_SHA256 {shape_digest}: "
                "the generated module was edited; regenerate it"
            )
        self.shape_sha256 = shape_digest
        self.interface_sha256 = interface_digest
        self.plans = InterfacePlans(self.interface, records=records, events=events)

    async def call(self, bridge: AsyncBridgeClient, module: str, method: str, args: Sequence[Any],
                   timeout: float | None) -> Any:
        return await self.plans.call(bridge, module, method, args, timeout=timeout)

    def subscribe(self, bridge: AsyncBridgeClient, module: str, names: Sequence[str],
                  timeout: float | None, max_pending: int | None) -> TypedSubscribeRequest[Any]:
        return self.plans.subscribe(bridge, module, names, timeout=timeout, max_pending=max_pending)

    def decode_event(self, name: str, event: Event) -> Any:
        if event.event != name:
            raise EventDecodeError(f"expected a {name!r} event, got {event.event!r}", event=event)
        return self.plans.event(name).decode(event)

    async def check_compat(self, bridge: AsyncBridgeClient, module: str, *, allow_untyped: bool,
                           discovery_wait: float | None) -> CompatReport:
        from ..compat import check_compat

        return await check_compat(bridge, module, self.interface, expected_interface_sha256=self.interface_sha256,
                                  allow_untyped=allow_untyped, discovery_wait=discovery_wait)
