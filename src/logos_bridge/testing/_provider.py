"""A provider module for :class:`FakeBridge`, checked against its contract."""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Awaitable, Mapping
from typing import TYPE_CHECKING, Any

from ..codegen.naming import snake
from ..lidl import Interface
from ..typed import CodecError, InterfacePlans, MethodPlan, arity_message
from ._fake import CallContext, Delay, FakeError, FakeModule, NoResponse, Reject, Wire

if TYPE_CHECKING:
    from ._fake import FakeBridge

_OUTCOMES = (FakeError, Reject, Wire, Delay)


class FakeProvider:
    """``impl`` answering as a generated provider of ``interface`` does.

    Arguments are checked the way a C++ provider checks them (logos-protocol's
    codec and the cdylib dispatch): a wrong count answers ``invalid_args``, a wrong
    value ``dispatch_failed``, in the same words, and both arrive as the call's
    result. Then ``impl``'s method runs with the decoded values (records as dicts,
    or as ``records`` classes such as a generated module's ``RECORD_TYPES``) and its
    return is encoded per the contract; a no-return method answers ``true``.
    ``name``/``version``/``lidl`` answer from the contract.

    ``impl`` is a mapping from LIDL method names to callables, or an object with
    methods named as in the contract or in snake_case. A method may be ``async``
    and may return a :class:`FakeError`, :class:`Reject`, :class:`Wire`,
    :class:`Delay` or :data:`NoResponse` instead of a value.
    """

    def __init__(self, interface: Interface | Mapping[str, Any], impl: Any, *, name: str | None = None,
                 records: Mapping[str, type] | None = None, lidl_text: str | None = None,
                 version: str | None = None) -> None:
        self.interface = Interface.coerce(interface).with_identity()
        self.name = name or self.interface.name
        self.plans = InterfacePlans(self.interface, records=records)
        self.impl = impl
        self.lidl_text = lidl_text
        self.version = version
        self._bridge: FakeBridge | None = None
        self._context: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar(
            f"fake-provider-{self.name}", default=None)

    def __repr__(self) -> str:
        return f"<FakeProvider {self.name} impl={type(self.impl).__name__}>"

    def install(self, fake: FakeBridge, *, status: str = "ok", **module_options: Any) -> FakeModule:
        """Expose the provider on ``fake``; ``module_options`` go to :meth:`FakeBridge.module`."""
        module_options.setdefault("lidl_text", self.lidl_text)
        module = fake.module(self.name, interface=self.interface, status=status, **module_options)
        if self.lidl_text is None:
            self.lidl_text = module.lidl_text
        for method in self.interface.methods:
            fake.on_call(self.name, method.name, self.handle)
        self._bridge = fake
        return module

    @property
    def bridge(self) -> FakeBridge:
        if self._bridge is None:
            raise RuntimeError(f"{self!r} is not installed on a FakeBridge")
        return self._bridge

    @property
    def context(self) -> CallContext:
        """The call being handled (inside an ``impl`` method)."""
        ctx = self._context.get()
        if ctx is None:
            raise RuntimeError("no call is being handled")
        return ctx

    def _implementation(self, method: str) -> Any:
        impl = self.impl
        if isinstance(impl, Mapping):
            found = impl.get(method)
        else:
            found = getattr(impl, method, None) or getattr(impl, snake(method), None)
        if found is None:
            raise NotImplementedError(f"{self.name}: the implementation has no {method!r}")
        return found

    def _identity(self, method: str) -> Any:
        if method == "name":
            return self.interface.name
        if method == "version":
            return self.version or self.interface.version or "1.0.0"
        return self.lidl_text if self.lidl_text is not None else self.interface.to_lidl()

    def handle(self, ctx: CallContext) -> Any:
        """The ``on_call`` handler for every declared method."""
        plan = self.plans.methods.get(ctx.method)
        if plan is None:
            return Wire(None)  # an unknown method is a bare null upstream
        count = len(ctx.params)
        if not plan.min_args <= count <= plan.max_args:
            return Reject("invalid_args", arity_message(plan.min_args, plan.max_args, count), self.name)
        values = []
        for index, param in enumerate(plan.params):
            item = ctx.params[index] if index < count else None
            try:
                values.append(param.codec.decode(item, f"arg{index}", lenient=True))
            except CodecError as exc:
                return Reject("dispatch_failed", str(exc), self.name)
        if plan.method.is_identity:
            return self._answer(plan, self._identity(ctx.method))
        function = self._implementation(ctx.method)
        token = self._context.set(ctx)
        try:
            result = function(*values)
        finally:
            self._context.reset(token)
        if inspect.isawaitable(result):
            return self._finish(plan, ctx, result)
        return self._answer(plan, result)

    async def _finish(self, plan: MethodPlan, ctx: CallContext, pending: Awaitable[Any]) -> Any:
        token = self._context.set(ctx)
        try:
            result = await pending
        finally:
            self._context.reset(token)
        return self._answer(plan, result)

    def _answer(self, plan: MethodPlan, result: Any) -> Any:
        if isinstance(result, _OUTCOMES) or result is NoResponse:
            return result
        if plan.returns is None:
            return Wire(True)  # "void must not dispatch to null"
        try:
            return Wire(plan.returns.encode(result, "result"))
        except CodecError as exc:
            raise TypeError(f"{self.name}.{plan.name} returned a value its contract refuses: {exc}") from None

    def emit(self, event: str, *values: Any, generation: int | None = None) -> int:
        """Emit ``event`` with ``values`` encoded per its declaration."""
        payload = self.plans.event(event).encode_values(values)
        return self.bridge.emit_json(self.name, event, payload, generation=generation)
