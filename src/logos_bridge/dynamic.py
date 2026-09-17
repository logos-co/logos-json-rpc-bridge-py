"""A module as a bridge describes it, callable without generated code.

``await bridge.module(name)`` gives a :class:`DynamicModule`:

* typed when the bridge serves a valid contract (``interface_status: ok``):
  arguments are checked and encoded locally, results and events are decoded,
  records are dicts, and a type this reader cannot map degrades to ``any``;
  a declared method this bridge does not expose raises
  :class:`~logos_bridge.errors.MethodNotExposed` without a request;
* names-only otherwise (``untyped``/``invalid``, or a bridge without ``lidl()``
  discovery): plain ``rpc.call`` with a :class:`~logos_bridge.errors.BridgeWarning`.

Attributes belong to the module: ``proxy.put(...)``, and always the identity built-ins
``proxy.name()``, ``proxy.version()`` and ``proxy.lidl()``. The proxy's own API is
:data:`PROXY_ATTRIBUTES`: compound names, plus ``call``, ``on`` and ``events``. A LIDL
method named like one of those, or starting with ``_``, is reached by key:
``proxy["events"]()``.

Both end a subscription on :class:`~logos_bridge.errors.SubscriptionTerminated`;
after ``provider_changed``, :meth:`DynamicModule.refreshed` reads the new contract.
"""

from __future__ import annotations

import inspect
import keyword
import warnings
from typing import TYPE_CHECKING, Any, Final

from .errors import BridgeWarning, MethodNotExposed, MethodNotFound, UntypedModule
from .lidl import UNMAPPABLE_CODES, Interface
from .lidl_sources import interface_from_module_info
from .models import Event, ModuleInfo
from .subscription import SubscribeRequest
from .sync import Subscription
from .typed import BlockingTypedSubscription, DecodedEvent, InterfacePlans, MethodPlan, TypedSubscribeRequest

if TYPE_CHECKING:
    from .client import AsyncBridgeClient
    from .sync import BridgeClient

#: The public attributes of :class:`DynamicModule`. None is ``name``, ``version`` or ``lidl``.
PROXY_ATTRIBUTES: Final = frozenset({
    "bridge_client", "call", "declared_events", "declared_methods", "decode_event", "events",
    "exposed_events", "exposed_methods", "from_module_info", "interface_status", "is_typed",
    "module_info", "module_name", "on", "refreshed", "served_interface", "typed_plans", "untyped_reason",
})
#: The public attributes of :class:`BlockingDynamicModule`.
BLOCKING_PROXY_ATTRIBUTES: Final = (PROXY_ATTRIBUTES - {"from_module_info"}) | {"aio", "portal"}


def _py_name(name: str) -> str:
    # Keywords cannot be parameters, and `timeout` is the proxy's own.
    return name + "_" if keyword.iskeyword(name) or name == "timeout" else name


class DynamicMethod:
    """One callable method. ``__signature__`` and ``__doc__`` come from the contract."""

    def __init__(self, module: DynamicModule, name: str) -> None:
        self._module = module
        self.__name__ = name
        self.__qualname__ = f"{module.module_name}.{name}"
        plans = module.typed_plans
        plan = plans.methods.get(name) if plans is not None else None
        self.plan: MethodPlan | None = plan
        self._aliases: dict[str, str] = {}
        if plan is None:
            self.__doc__ = f"{module.module_name}.{name} (untyped: positional arguments, raw result)"
            self.__signature__ = inspect.Signature([
                inspect.Parameter("args", inspect.Parameter.VAR_POSITIONAL),
                inspect.Parameter("timeout", inspect.Parameter.KEYWORD_ONLY, default=None),
            ])
            return
        method = plan.method
        doc = method.description or f"{module.module_name}.{name}"
        self.__doc__ = f"{doc}\n\nLIDL: method {method.signature()}"
        params = []
        for index, param in enumerate(plan.params):
            py = _py_name(param.name)
            self._aliases[py] = param.name
            default: Any = None if index >= plan.min_args else inspect.Parameter.empty
            params.append(inspect.Parameter(py, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
                                            annotation=param.codec.annotation()))
        params.append(inspect.Parameter("timeout", inspect.Parameter.KEYWORD_ONLY, default=None))
        returns = plan.returns.annotation() if plan.returns is not None else None
        self.__signature__ = inspect.Signature(params, return_annotation=returns)

    def __repr__(self) -> str:
        return f"<DynamicMethod {self.__qualname__}{self.__signature__}>"

    @property
    def exposed(self) -> bool:
        return self.__name__ in self._module.exposed_methods

    async def __call__(self, /, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        module = self._module
        if self.plan is None:
            if kwargs:
                raise TypeError(f"{self.__qualname__} is untyped: pass arguments by position")
            return await module.bridge_client.call(module.module_name, self.__name__, *args, timeout=timeout)
        if not self.exposed:
            raise MethodNotExposed(module.module_name, self.__name__)
        named = {self._aliases.get(k, k): v for k, v in kwargs.items()}
        values = self.plan.bind(args, named, module=module.module_name)
        return await self.plan.call(module.bridge_client, module.module_name, values, timeout=timeout)


class DynamicModule:
    """See the module docstring. Build it with :meth:`AsyncBridgeClient.module`."""

    def __init__(self, bridge: AsyncBridgeClient, info: ModuleInfo, interface: Interface | None,
                 reason: str | None = None) -> None:
        self._bridge = bridge
        self._info = info
        self._interface = interface
        self._plans = InterfacePlans(interface) if interface is not None else None
        self._reason = reason
        self._methods: dict[str, DynamicMethod] = {}

    @classmethod
    def from_module_info(cls, bridge: AsyncBridgeClient, info: ModuleInfo, *,
                         require_typed: bool = False) -> DynamicModule:
        """Typed if ``info`` carries a usable contract; otherwise names-only (or :class:`UntypedModule`)."""
        interface: Interface | None = None
        reason: str | None = None
        if info.typed and info.interface is not None:
            candidate = interface_from_module_info(info)
            blocking = [i for i in candidate.errors if i.code not in UNMAPPABLE_CODES]
            if blocking:
                reason = f"the served contract fails local validation: {blocking[0]}"
            else:
                interface = candidate
        else:
            status = info.interface_status or "no lidl discovery"
            reason = f"status {status}" + (f": {info.interface_error}" if info.interface_error else "")
        if interface is None:
            if require_typed:
                raise UntypedModule(f"{info.module} has no usable lidl() contract ({reason})")
            warnings.warn(f"{info.module}: calls are untyped ({reason})", BridgeWarning, stacklevel=3)
        return cls(bridge, info, interface, reason)

    def __repr__(self) -> str:
        mode = "typed" if self.is_typed else "untyped"
        return f"<DynamicModule {self.module_name} {mode} status={self.interface_status}>"

    # ------------------------------------------------------------ description

    @property
    def bridge_client(self) -> AsyncBridgeClient:
        return self._bridge

    @property
    def module_name(self) -> str:
        return self._info.module

    @property
    def module_info(self) -> ModuleInfo:
        """The bridge's view the proxy was built from."""
        return self._info

    @property
    def interface_status(self) -> str | None:
        return self._info.interface_status

    @property
    def is_typed(self) -> bool:
        return self._interface is not None

    @property
    def untyped_reason(self) -> str | None:
        return self._reason

    @property
    def served_interface(self) -> Interface | None:
        """The contract the bridge serves, or ``None`` for a names-only proxy."""
        return self._interface

    @property
    def typed_plans(self) -> InterfacePlans | None:
        return self._plans

    @property
    def exposed_methods(self) -> tuple[str, ...]:
        """What this bridge lets a client call."""
        return self._info.callable_methods

    @property
    def exposed_events(self) -> tuple[str, ...]:
        """What this bridge lets a client subscribe to."""
        return self._info.subscribable_events

    @property
    def declared_methods(self) -> tuple[str, ...]:
        return self._interface.method_names if self._interface is not None else self.exposed_methods

    @property
    def declared_events(self) -> tuple[str, ...]:
        return self._interface.event_names if self._interface is not None else self.exposed_events

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {n for n in self.declared_methods if n.isidentifier()})

    # ------------------------------------------------------------------ calls

    def __getattr__(self, name: str) -> DynamicMethod:
        # Reached only for names the proxy itself does not define (and never before __init__ ran).
        if name.startswith("_") or "_interface" not in self.__dict__ or name not in self.declared_methods:
            raise AttributeError(f"{type(self).__name__} has no attribute or declared method {name!r}")
        return self[name]

    def __getitem__(self, name: str) -> DynamicMethod:
        """The method ``name``, whatever it is called; :class:`MethodNotFound` if the contract lacks it."""
        existing = self._methods.get(name)
        if existing is not None:
            return existing
        if self._interface is not None and self._interface.method(name) is None:
            raise MethodNotFound(f"{self.module_name} declares no method {name!r}", module=self.module_name,
                                 method=name)
        method = DynamicMethod(self, name)
        self._methods[name] = method
        return method

    async def call(self, method: str, /, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        return await self[method](*args, timeout=timeout, **kwargs)

    # ----------------------------------------------------------------- events

    def _check_events(self, names: tuple[str, ...]) -> None:
        if self._interface is None:
            return
        for name in names:
            if self._interface.event(name) is None:
                raise MethodNotFound(f"{self.module_name} declares no event {name!r}", module=self.module_name)
            if name not in self.exposed_events:
                raise MethodNotExposed(self.module_name, name, kind="event")

    def on(self, event: str, *, timeout: float | None = None,
           max_pending: int | None = None) -> TypedSubscribeRequest[DecodedEvent] | SubscribeRequest:
        """Subscribe to one event: typed (:class:`DecodedEvent`) or raw (:class:`Event`)."""
        return self.events(event, timeout=timeout, max_pending=max_pending)

    def events(self, *names: str, timeout: float | None = None,
               max_pending: int | None = None) -> TypedSubscribeRequest[DecodedEvent] | SubscribeRequest:
        """One ordered stream of ``names`` (every subscribable event when none are given)."""
        chosen = names or self.exposed_events
        if not chosen:
            raise ValueError(f"{self.module_name} has no events this bridge lets a client subscribe to")
        self._check_events(tuple(chosen))
        if self._plans is not None:
            return self._plans.subscribe(self._bridge, self.module_name, list(chosen), timeout=timeout,
                                         max_pending=max_pending)
        if len(chosen) == 1:
            return self._bridge.subscribe(self.module_name, chosen[0], timeout=timeout, max_pending=max_pending)
        return self._bridge.subscribe_many(self.module_name, list(chosen), timeout=timeout,
                                           max_pending=max_pending)

    def decode_event(self, event: Event) -> DecodedEvent | Event:
        """Decode a raw event against the contract (a names-only module returns it as is)."""
        if self._plans is None:
            return event
        decoded: DecodedEvent = self._plans.decode_event(event)
        return decoded

    async def refreshed(self, *, discovery_wait: float | None = 10.0, require_typed: bool = False) -> DynamicModule:
        """A new proxy from the bridge's current view (after ``provider_changed``, say)."""
        info = await self._bridge.wait_for_module(self.module_name, discovery_wait)
        return DynamicModule.from_module_info(self._bridge, info, require_typed=require_typed)


class BlockingDynamicMethod:
    def __init__(self, module: BlockingDynamicModule, aio: DynamicMethod) -> None:
        self._module = module
        self.aio = aio
        self.__name__ = aio.__name__
        self.__qualname__ = aio.__qualname__
        self.__doc__ = aio.__doc__
        self.__signature__ = aio.__signature__

    def __repr__(self) -> str:
        return f"<BlockingDynamicMethod {self.__qualname__}{self.__signature__}>"

    def __call__(self, /, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        return self._module.portal.call(self.aio, *args, timeout=timeout, **kwargs)


async def _open_raw(request: SubscribeRequest) -> Any:
    return await request


class BlockingDynamicModule:
    """The blocking twin of :class:`DynamicModule`, from :meth:`BridgeClient.module`.

    Its own API is :data:`BLOCKING_PROXY_ATTRIBUTES` (the async proxy's, plus ``aio`` and
    ``portal``); the same naming rule applies.
    """

    def __init__(self, bridge: BridgeClient, aio: DynamicModule) -> None:
        self._bridge = bridge
        self.aio = aio

    def __repr__(self) -> str:
        return f"<BlockingDynamicModule {self.aio!r}>"

    @property
    def portal(self) -> Any:
        return self._bridge.portal

    @property
    def bridge_client(self) -> BridgeClient:
        return self._bridge

    @property
    def module_name(self) -> str:
        return self.aio.module_name

    @property
    def module_info(self) -> ModuleInfo:
        return self.aio.module_info

    @property
    def interface_status(self) -> str | None:
        return self.aio.interface_status

    @property
    def is_typed(self) -> bool:
        return self.aio.is_typed

    @property
    def untyped_reason(self) -> str | None:
        return self.aio.untyped_reason

    @property
    def served_interface(self) -> Interface | None:
        return self.aio.served_interface

    @property
    def typed_plans(self) -> InterfacePlans | None:
        return self.aio.typed_plans

    @property
    def exposed_methods(self) -> tuple[str, ...]:
        return self.aio.exposed_methods

    @property
    def exposed_events(self) -> tuple[str, ...]:
        return self.aio.exposed_events

    @property
    def declared_methods(self) -> tuple[str, ...]:
        return self.aio.declared_methods

    @property
    def declared_events(self) -> tuple[str, ...]:
        return self.aio.declared_events

    def __getattr__(self, name: str) -> BlockingDynamicMethod:
        aio = self.__dict__.get("aio")
        if name.startswith("_") or aio is None or name not in aio.declared_methods:
            raise AttributeError(f"{type(self).__name__} has no attribute or declared method {name!r}")
        return self[name]

    def __getitem__(self, name: str) -> BlockingDynamicMethod:
        return BlockingDynamicMethod(self, self.aio[name])

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {n for n in self.declared_methods if n.isidentifier()})

    def call(self, method: str, /, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        return self[method](*args, timeout=timeout, **kwargs)

    def on(self, event: str, *, timeout: float | None = None, max_pending: int | None = None) -> Any:
        return self.events(event, timeout=timeout, max_pending=max_pending)

    def events(self, *names: str, timeout: float | None = None, max_pending: int | None = None) -> Any:
        """A :class:`BlockingTypedSubscription` (typed) or a :class:`Subscription` (names-only)."""
        request = self.aio.events(*names, timeout=timeout, max_pending=max_pending)
        if isinstance(request, TypedSubscribeRequest):
            return BlockingTypedSubscription.open(request, self.portal)
        return Subscription(self.portal.call(_open_raw, request), self.portal)

    def decode_event(self, event: Event) -> Any:
        return self.aio.decode_event(event)

    def refreshed(self, *, discovery_wait: float | None = 10.0, require_typed: bool = False) -> BlockingDynamicModule:
        fresh: DynamicModule = self.portal.call(self.aio.refreshed, discovery_wait=discovery_wait,
                                                require_typed=require_typed)
        return BlockingDynamicModule(self._bridge, fresh)
