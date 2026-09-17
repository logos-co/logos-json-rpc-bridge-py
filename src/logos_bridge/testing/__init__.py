"""Test support shipped with logos-bridge.

:class:`FakeBridge` (and :class:`ThreadedFakeBridge`) imitate a bridge;
:class:`FakeProvider` answers for a module from its contract; :func:`async_test`
runs async tests without a pytest plugin. :mod:`logos_bridge.testing.conformance`
reads logos-test-modules' conformance tables, :mod:`logos_bridge.testing.docs`
checks the bridge's API documents (with the ``docs-test`` extra), and
:mod:`logos_bridge.testing.live` runs a real bridge under a logoscore daemon
(with logos-logoscore-py installed).
"""

from __future__ import annotations

from ._fake import (
    BUILTIN_METHODS,
    CallContext,
    Delay,
    FakeBridge,
    FakeConnection,
    FakeError,
    FakeModule,
    FakeSubscription,
    Handshake,
    HttpExchange,
    NoResponse,
    RecordedRequest,
    Reject,
    SubscribeContext,
    Wire,
)
from ._provider import FakeProvider
from ._helpers import async_test, free_port
from ._threaded import ThreadedFakeBridge

__all__ = [
    "BUILTIN_METHODS",
    "CallContext",
    "Delay",
    "FakeBridge",
    "FakeConnection",
    "FakeError",
    "FakeModule",
    "FakeProvider",
    "FakeSubscription",
    "Handshake",
    "HttpExchange",
    "NoResponse",
    "RecordedRequest",
    "Reject",
    "SubscribeContext",
    "ThreadedFakeBridge",
    "Wire",
    "async_test",
    "free_port",
]
