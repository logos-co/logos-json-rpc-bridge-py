"""Shared pytest configuration.

Async tests use ``logos_bridge.testing.async_test`` (a fresh loop per test, hard
timeout, leftover-task check); no pytest-asyncio.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: takes more than a second (bursts, keepalive, GC)")
    config.addinivalue_line("markers", "fidelity: checks FakeBridge against the bridge's wire behaviour")
    config.addinivalue_line("markers", "integration: needs a real bridge under logoscore (tests/integration)")
