"""Integration tests: a real json_rpc_bridge under a logoscore daemon.

They need a built stack (see ``harness.Stack``). Without one they skip, unless
``LOGOS_BRIDGE_INTEGRATION=required`` (set by the nix checks): then a missing piece
fails, and so does any skip whose reason does not start with ``[optional]``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import DENIED, NOTES, PROVIDERS, UNTYPED_CANDIDATE, NodeFactory, Stack, required_mode

from logos_bridge.testing.live import (
    LiveBridge,
    LiveBridgeUnavailable,
    bridge_config,
    interrupt_on_sigterm,
    short_tmpdir_env,
)

OPTIONAL = "[optional]"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_unexpected_skips: list[str] = []
_nodes: list[LiveBridge] = []


def pytest_configure(config: pytest.Config) -> None:
    # pytest runs its finalizers (and so stops the daemons) for KeyboardInterrupt only.
    interrupt_on_sigterm()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not (report.skipped and required_mode()) or hasattr(report, "wasxfail"):
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    if not reason.removeprefix("Skipped: ").startswith(OPTIONAL):
        _unexpected_skips.append(f"{report.nodeid}: {reason}")


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if NOTES:
        terminalreporter.section("integration stack")
        for note in NOTES:
            terminalreporter.write_line(note)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    write: Callable[[str], Any] = reporter.write_line if reporter is not None else print
    if _unexpected_skips:
        write(f"integration: {len(_unexpected_skips)} test(s) skipped in required mode:")
        for line in _unexpected_skips:
            write(f"  {line}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if session.exitstatus != 0:
        for node in _nodes:
            _out, err = node.logs()
            tail = [line for line in err.strip().splitlines() if "__lws_lc_" not in line][-40:]
            if tail:
                write(f"--- {node.label}: daemon stderr (last {len(tail)} lines) ---")
                for line in tail:
                    write(line)


@pytest.fixture(scope="session", autouse=True)
def short_tmpdir() -> Iterator[Path]:
    # The daemon's sockets live under TMPDIR, and macOS caps their paths at 104 bytes.
    with short_tmpdir_env("lbpy") as path:
        yield path


@pytest.fixture(scope="session")
def stack() -> Stack:
    try:
        return Stack.from_env()
    except LiveBridgeUnavailable as exc:
        if required_mode():
            pytest.fail(f"the integration stack is unavailable: {exc}", pytrace=False)
        pytest.skip(str(exc))


def _factory(stack: Stack, request: pytest.FixtureRequest) -> NodeFactory:
    def start(*, name: str, config: dict[str, Any] | None = None, providers: tuple[str, ...] = PROVIDERS,
              **options: Any) -> LiveBridge:
        node = LiveBridge(stack.live, providers, label=name, **options)
        _nodes.append(node)
        request.addfinalizer(node.stop)
        return node.start(config)
    return start


@pytest.fixture(scope="session")
def session_node_factory(stack: Stack, request: pytest.FixtureRequest) -> NodeFactory:
    return _factory(stack, request)


@pytest.fixture(scope="module")
def module_node_factory(stack: Stack, request: pytest.FixtureRequest) -> NodeFactory:
    return _factory(stack, request)


@pytest.fixture
def node_factory(stack: Stack, request: pytest.FixtureRequest) -> NodeFactory:
    return _factory(stack, request)


@pytest.fixture(scope="session")
def node(session_node_factory: NodeFactory) -> LiveBridge:
    """The shared node: both providers, plus the daemon's own untyped modules_state."""
    shared = session_node_factory(name="shared", also_expose=(UNTYPED_CANDIDATE,))
    info = shared.info()
    views = shared.views()
    statuses = ", ".join(f"{m}={views[m]['interface_status']}" for m in shared.exposed)
    NOTES.append(f"bridge {info.get('version')}: protocol_version={info.get('protocol_version')} "
             f"subscription_continuity={info.get('subscription_continuity')} "
             f"lidl_reader={info.get('lidl_reader')!r}; {statuses}")
    return shared


@pytest.fixture(scope="session")
def policy_node(session_node_factory: NodeFactory) -> LiveBridge:
    config = bridge_config({"name": m, "methods": {"deny": [method]}, "events": {"deny": [event]}}
                           for m, (method, event) in DENIED.items())
    return session_node_factory(name="policy", config=config)


@pytest.fixture(scope="session")
def typed_views(node: LiveBridge) -> dict[str, dict[str, Any]]:
    """The providers' views; skips unless both are typed (built with a lidl()-deriving builder)."""
    views = node.views()
    untyped = {m: views[m]["interface_status"] for m in PROVIDERS if views[m]["interface_status"] != "ok"}
    if untyped:
        pytest.skip(f"the providers are not typed ({untyped}); they need a module-builder that derives lidl()")
    return views


@pytest.fixture(scope="session")
def untyped_module(node: LiveBridge) -> str:
    views = node.views()
    candidates = [m for m in (UNTYPED_CANDIDATE, *node.exposed) if views[m]["interface_status"] == "untyped"]
    if not candidates:
        pytest.skip(f"{OPTIONAL} no exposed module is untyped (modules_state gained lidl())")
    return candidates[0]


@pytest.fixture
def fresh_node(node_factory: NodeFactory) -> Iterator[LiveBridge]:
    """A node of its own, for a test that disturbs the daemon."""
    yield node_factory(name="fresh")
