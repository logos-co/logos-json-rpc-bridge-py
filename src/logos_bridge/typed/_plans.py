"""How to call one method and read one event of a contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..codec import rejection_from_result
from ..errors import ArgumentError, ArityError, EventDecodeError, MethodNotFound, ResultDecodeError
from ..lidl import Event as EventDecl
from ..lidl import Interface, Method, json_type_name
from ..models import Event
from ._codecs import Codec, CodecBuilder, CodecError, rejection_ambiguous, wire_names
from ._subscription import TypedSubscribeRequest

if TYPE_CHECKING:
    from ..client import AsyncBridgeClient


def arity_message(min_args: int, max_args: int, got: int) -> str:
    """The provider's ``invalid_args`` wording (logos-cpp-sdk cdylib dispatch)."""
    if got < min_args:
        return f"expected {min_args} arguments, got {got}"
    return f"expected {'' if min_args == max_args else 'at most '}{max_args} arguments, got {got}"


@dataclass(frozen=True)
class ParamPlan:
    name: str
    codec: Codec
    optional: bool


class MethodPlan:
    """Encode arguments, fold rejections and decode the result for one method."""

    def __init__(self, method: Method, builder: CodecBuilder) -> None:
        self.method = method
        self.name = method.name
        self.params = tuple(ParamPlan(p.name, builder.slot(p.optional, p.value_type), p.optional)
                            for p in method.params)
        returns = method.returns
        self.returns: Codec | None = None if returns is None else builder.slot(returns.optional, returns.value_type)
        self.rejection_ambiguous = returns is not None and rejection_ambiguous(returns.type, builder.interface)
        self.min_args = method.min_args
        self.max_args = method.max_args

    def __repr__(self) -> str:
        return f"<MethodPlan {self.method.signature()}>"

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)

    def check_arity(self, count: int, *, module: str | None = None) -> None:
        if not self.min_args <= count <= self.max_args:
            raise ArityError(arity_message(self.min_args, self.max_args, count), expected=self.max_args,
                             got=count, module=module, method=self.name)

    def encode_args(self, args: Sequence[Any], *, module: str | None = None) -> list[Any]:
        """The wire arguments. Omitted trailing optionals are sent as ``null``."""
        self.check_arity(len(args), module=module)
        wire = []
        for index, param in enumerate(self.params):
            value = args[index] if index < len(args) else None
            try:
                wire.append(param.codec.encode(value, f"arg{index}"))
            except CodecError as exc:
                raise ArgumentError(str(exc), module=module, method=self.name, path=exc.path) from None
        return wire

    def bind(self, args: Sequence[Any], kwargs: Mapping[str, Any], *, module: str | None = None) -> list[Any]:
        """Positional values from a Python call that may name parameters."""
        values = list(args)
        positional = len(values)
        if positional > self.max_args:
            self.check_arity(positional, module=module)
        names = self.param_names
        for key, value in kwargs.items():
            if key not in names:
                raise TypeError(f"{self.name}() got an unexpected keyword argument {key!r}")
            index = names.index(key)
            if index < positional:
                raise TypeError(f"{self.name}() got multiple values for argument {key!r}")
            values.extend([_MISSING] * (index + 1 - len(values)))
            values[index] = value
        while values and values[-1] is _MISSING and self.params[len(values) - 1].optional:
            values.pop()
        for index, value in enumerate(values):
            if value is _MISSING:
                if not self.params[index].optional:
                    self.check_arity(index, module=module)
                values[index] = None
        return values

    def decode_result(self, value: Any, *, module: str) -> Any:
        """Fold a provider rejection into an exception, then decode per the contract."""
        rejection = rejection_from_result(value, module=module, method=self.name)
        if rejection is not None:
            raise rejection
        if self.returns is None:
            return None  # the wire says `true`
        try:
            return self.returns.decode(value, "result")
        except CodecError as exc:
            raise ResultDecodeError(str(exc), module=module, method=self.name, path=exc.path,
                                    value=value) from None

    async def call(self, bridge: AsyncBridgeClient, module: str, args: Sequence[Any], *,
                   timeout: float | None = None) -> Any:
        wire = self.encode_args(args, module=module)
        result = await bridge.call_encoded(module, self.name, wire, timeout=timeout)
        return self.decode_result(result, module=module)


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING: Any = _Missing()


@dataclass(frozen=True)
class DecodedEvent:
    """An event decoded against its declaration, for callers without generated classes."""

    name: str
    args: tuple[Any, ...]
    values: Mapping[str, Any]
    meta: Event

    def __getitem__(self, key: str | int) -> Any:
        return self.args[key] if isinstance(key, int) else self.values[key]


class EventPlan:
    """Decode one event's positional payload (and encode one, for fakes)."""

    def __init__(self, event: EventDecl, builder: CodecBuilder, cls: type | None = None) -> None:
        self.event = event
        self.name = event.name
        self.params = tuple(ParamPlan(p.name, builder.slot(p.optional, p.value_type), p.optional)
                            for p in event.params)
        required = [i + 1 for i, p in enumerate(event.params) if not p.optional]
        self.min_args = required[-1] if required else 0
        self.cls = cls
        self._attrs: Mapping[str, str] = wire_names(cls) if cls is not None else {}

    def __repr__(self) -> str:
        return f"<EventPlan {self.event.signature()}>"

    def decode_values(self, data: Any) -> tuple[Any, ...]:
        """The payload's values. Raises :class:`CodecError`."""
        if not isinstance(data, list):
            raise CodecError("an array of event arguments", "data", json_type_name(data))
        count, most = len(data), len(self.params)
        if not self.min_args <= count <= most:
            wanted = f"{most}" if self.min_args == most else f"{self.min_args} to {most}"
            raise CodecError(f"{wanted} event arguments", "data", f"{count}")
        values = []
        for index, param in enumerate(self.params):
            item = data[index] if index < len(data) else None
            values.append(param.codec.decode(item, f"arg{index}"))
        return tuple(values)

    def decode(self, event: Event) -> Any:
        """A generated event instance (or :class:`DecodedEvent`); :class:`EventDecodeError` otherwise."""
        try:
            values = self.decode_values(event.data)
        except CodecError as exc:
            raise EventDecodeError(str(exc), event=event, path=exc.path) from None
        if self.cls is not None:
            kwargs = {self._attrs.get(p.name, p.name): v for p, v in zip(self.params, values)}
            return self.cls(meta=event, **kwargs)
        return DecodedEvent(self.name, values, {p.name: v for p, v in zip(self.params, values)}, event)

    def encode_values(self, values: Sequence[Any]) -> list[Any]:
        if not self.min_args <= len(values) <= len(self.params):
            raise ArityError(arity_message(self.min_args, len(self.params), len(values)),
                             expected=len(self.params), got=len(values), method=self.name)
        out = []
        for index, param in enumerate(self.params):
            value = values[index] if index < len(values) else None
            try:
                out.append(param.codec.encode(value, f"arg{index}"))
            except CodecError as exc:
                raise ArgumentError(str(exc), method=self.name, path=exc.path) from None
        return out


class InterfacePlans:
    """Every method and event plan of a contract.

    ``records``/``events`` map LIDL names to generated classes; without them,
    records decode to dicts and events to :class:`DecodedEvent`.
    """

    def __init__(self, interface: Interface, *, records: Mapping[str, type] | None = None,
                 events: Mapping[str, type] | None = None) -> None:
        self.interface = interface
        builder = CodecBuilder(interface, records=records, dynamic=records is None)
        self.methods: dict[str, MethodPlan] = {m.name: MethodPlan(m, builder) for m in interface.methods}
        classes = dict(events or {})
        unknown = set(classes) - set(interface.event_names)
        if unknown:
            raise ValueError(f"event classes for undeclared events: {sorted(unknown)}")
        self.events: dict[str, EventPlan] = {
            e.name: EventPlan(e, builder, classes.get(e.name)) for e in interface.events
        }

    def method(self, name: str, *, module: str | None = None) -> MethodPlan:
        plan = self.methods.get(name)
        if plan is None:
            raise MethodNotFound(f"{self.interface.name} declares no method {name!r}", module=module, method=name)
        return plan

    def event(self, name: str) -> EventPlan:
        plan = self.events.get(name)
        if plan is None:
            raise ValueError(f"{self.interface.name} declares no event {name!r}")
        return plan

    def decode_event(self, event: Event) -> Any:
        plan = self.events.get(event.event)
        if plan is None:
            raise EventDecodeError(f"undeclared event {event.event!r}", event=event)
        return plan.decode(event)

    async def call(self, bridge: AsyncBridgeClient, module: str, name: str, args: Sequence[Any], *,
                   timeout: float | None = None) -> Any:
        return await self.method(name, module=module).call(bridge, module, args, timeout=timeout)

    def subscribe(self, bridge: AsyncBridgeClient, module: str, names: Sequence[str], *,
                  timeout: float | None = None, max_pending: int | None = None,
                  decode: Callable[[Event], Any] | None = None) -> TypedSubscribeRequest[Any]:
        """One ordered, typed stream of ``names`` (every event when empty)."""
        chosen = list(names) if names else list(self.events)
        for name in chosen:
            self.event(name)
        if not chosen:
            raise ValueError(f"{self.interface.name} declares no events")
        if len(chosen) == 1:
            raw = bridge.subscribe(module, chosen[0], timeout=timeout, max_pending=max_pending)
        else:
            raw = bridge.subscribe_many(module, chosen, timeout=timeout, max_pending=max_pending)
        return TypedSubscribeRequest(raw, decode or self.decode_event)
