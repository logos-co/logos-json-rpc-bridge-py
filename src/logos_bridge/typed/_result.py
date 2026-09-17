"""``LogosResult``: a module's structured answer (``{success, value, error}``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from ..errors import ModuleResultError

T = TypeVar("T")


@dataclass(frozen=True)
class LogosResult(Generic[T]):
    """A LIDL ``result``. ``success: false`` is an answer, not a failed call.

    Frozen, without ``__slots__`` (a generic dataclass with slots does not
    subscript cleanly on Python 3.10).
    """

    success: bool
    value: T
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.success

    def unwrap(self) -> T:
        """``value``, or :class:`~logos_bridge.errors.ModuleResultError` for ``success: false``."""
        if self.success:
            return self.value
        raise ModuleResultError(self.error, self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {"success": self.success, "value": self.value, "error": self.error}
