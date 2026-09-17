"""logos_bridge.testing.live without a daemon: the stack, configs, temporary directories, the reaper."""

from __future__ import annotations

import builtins
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from logos_bridge.testing import live
from logos_bridge.testing.live import (
    LiveBridge,
    LiveBridgeError,
    LiveBridgeUnavailable,
    LiveStack,
    ProcessEntry,
    bridge_config,
    exposed_modules,
)


@pytest.fixture
def logoscore_package(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake = types.ModuleType("logoscore")
    monkeypatch.setitem(sys.modules, "logoscore", fake)
    return fake


def executable(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def installed(root: Path, *modules: str) -> Path:
    for module in modules:
        (root / module).mkdir(parents=True)
    return root


def test_the_module_imports_without_logoscore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] == "logoscore":
            raise ImportError("No module named 'logoscore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "logoscore", raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(LiveBridgeUnavailable, match="logos-logoscore-py.*pip install git\\+https"):
        live.require_logoscore()
    with pytest.raises(LiveBridgeUnavailable, match="logoscore package"):
        LiveStack("/bin/sh", (str(installed(tmp_path, "json_rpc_bridge")),)).check()


def test_from_env_names_what_is_missing(tmp_path: Path, logoscore_package: types.ModuleType) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    logoscore = executable(bin_dir / "logoscore")
    bridge = installed(tmp_path / "bridge" / "modules", "json_rpc_bridge").parent
    providers = installed(tmp_path / "providers", "storage_module")
    with pytest.raises(LiveBridgeUnavailable, match="set LOGOS_LOGOSCORE_BIN or put logoscore on PATH"):
        LiveStack.from_env({"PATH": str(tmp_path / "nothing")})
    with pytest.raises(LiveBridgeUnavailable, match=r"LOGOS_LOGOSCORE_BIN \(.*\) is not an executable file"):
        LiveStack.from_env({"LOGOS_LOGOSCORE_BIN": str(tmp_path / "missing")})
    with pytest.raises(LiveBridgeUnavailable, match="LOGOS_BRIDGE_INSTALL_DIR is not set"):
        LiveStack.from_env({"LOGOS_LOGOSCORE_BIN": logoscore})
    with pytest.raises(LiveBridgeUnavailable, match="LOGOS_BRIDGE_INSTALL_DIR names a missing directory"):
        LiveStack.from_env({"LOGOS_LOGOSCORE_BIN": logoscore, "LOGOS_BRIDGE_INSTALL_DIR": str(tmp_path / "x")})
    with pytest.raises(LiveBridgeUnavailable, match="no modules directory holds json_rpc_bridge/"):
        LiveStack.from_env({"LOGOS_LOGOSCORE_BIN": logoscore, "LOGOS_BRIDGE_INSTALL_DIR": str(providers)})
    with pytest.raises(LiveBridgeUnavailable, match="LOGOS_LIVE_MODULES_DIRS names a missing directory"):
        LiveStack.from_env({"LOGOS_LOGOSCORE_BIN": logoscore, "LOGOS_BRIDGE_INSTALL_DIR": str(bridge),
                            "LOGOS_LIVE_MODULES_DIRS": str(tmp_path / "gone")})

    extra = installed(tmp_path / "extra", "other_module")
    stack = LiveStack.from_env({
        "PATH": str(bin_dir), "LOGOS_BRIDGE_INSTALL_DIR": str(bridge),
        "LOGOS_LIVE_MODULES_DIRS": os.pathsep.join([str(providers), "", str(providers)]),
        "LOGOS_LIVE_RUN_DIR": str(tmp_path / "run"),
    }, modules_dirs=[extra])
    assert stack.logoscore == logoscore and stack.run_dir == tmp_path / "run"
    # Explicit dirs first, then the environment's, then the bridge's (an install output's modules/).
    assert stack.modules_dirs == (str(extra), str(providers), str(bridge / "modules"))
    fallback = LiveStack.from_env({"LOGOSCORE_BIN": logoscore, "LOGOS_BRIDGE_INSTALL_DIR": str(bridge / "modules")})
    assert fallback.modules_dirs == (str(bridge / "modules"),) and fallback.run_dir is None


def test_bridge_configs() -> None:
    policy = {"name": "storage_module", "methods": {"deny": ["destroy"]}}
    config = bridge_config(["a", policy], limits={"call_timeout_ms": 5000})
    assert config == {
        "expose": {"modules": ["a", {"name": "storage_module", "methods": {"deny": ["destroy"]}}]},
        "discovery": {"revalidate_ms": 1000},
        "limits": {"call_timeout_ms": 5000},
    }
    assert config["expose"]["modules"][1] is not policy
    assert exposed_modules(config) == ("a", "storage_module")
    assert bridge_config(["a"], revalidate_ms=None) == {"expose": {"modules": ["a"]}}
    with pytest.raises(ValueError, match="at least one module"):
        bridge_config([])
    with pytest.raises(TypeError, match="a mapping with a 'name'"):
        bridge_config([{"methods": {}}])
    with pytest.raises(ValueError, match="'discovery' section"):
        bridge_config(["a"], discovery={"revalidate_ms": 0})
    assert exposed_modules({}) == ()


def test_short_temporary_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    made = live.short_tmpdir("lbt")
    try:
        assert made.is_dir() and made.name.startswith("lbt.")
        if sys.platform == "darwin":
            assert str(made).startswith("/tmp/")
    finally:
        made.rmdir()
    before = tempfile.gettempdir()
    with live.short_tmpdir_env("lbt", force=True) as short:
        assert os.environ["TMPDIR"] == str(short) and tempfile.gettempdir() == str(short)
        assert len(str(short)) <= live.SHORT_TMPDIR_LIMIT or sys.platform != "darwin"
    assert not short.exists() and tempfile.gettempdir() == before
    monkeypatch.setattr(sys, "platform", "linux")
    with live.short_tmpdir_env("lbt") as unchanged:
        assert str(unchanged) == before and tempfile.gettempdir() == before


def test_a_node_that_never_started() -> None:
    node = LiveBridge(LiveStack("/bin/sh", ()), ["m"], label="idle")
    assert not node.running and node.config_dir is None and node.logs() == ("", "")
    node.stop()
    with pytest.raises(LiveBridgeError, match="idle is not running"):
        node.daemon  # noqa: B018
    with pytest.raises(LiveBridgeError, match="idle is not running"):
        node.load("m")
    assert repr(node) == "<LiveBridge idle port=0 config_dir=None>"


def test_main_usage(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert live.main([]) == 2 and "usage: python -m logos_bridge.testing.live --reap DIR" in capsys.readouterr().err
    assert live.main(["--reap", str(tmp_path)]) == 0
    done = subprocess.run([sys.executable, "-m", "logos_bridge.testing.live", "--reap", str(tmp_path)],
                          capture_output=True, text=True, timeout=60, check=False,
                          env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert done.returncode == 0, done.stderr


# ----------------------------------------------------------------- the reaper


def test_the_process_table_lists_this_process() -> None:
    if sys.platform == "darwin":  # sysctl, not the setuid /bin/ps a nix build cannot run
        assert live._darwin_process_table() is not None, "an unexpected kinfo_proc layout"
    table = {e.pid: e for e in live.process_table()}
    me = table[os.getpid()]
    assert me.ppid == os.getppid() and me.pgid == os.getpgrp()
    assert me.command.split()[1:] == sys.orig_argv[1:] or "pytest" in me.command


def test_the_reaper_selects_by_directory_and_spares_its_callers(tmp_path: Path) -> None:
    run = tmp_path / "run"
    me, parent, group = os.getpid(), os.getppid(), os.getpgrp()
    table = [
        ProcessEntry(me, parent, group, f"python -m logos_bridge.testing.live --reap {run}/"),
        ProcessEntry(parent, 1, group, f"sh -c reap {run}/x"),
        ProcessEntry(9_000_001, me, group, f"sleep 1 {run}/in-my-group"),
        ProcessEntry(9_000_002, 1, 9_000_002, f"logoscore -D --config-dir {run}/lb-1 -m /x"),
        ProcessEntry(9_000_003, 9_000_002, 9_000_003, f"logos_host --instance-persistence-path {run}/lb-1/data/m"),
        ProcessEntry(9_000_004, 9_000_003, 9_000_003, f"child {run}/lb-1/data/m"),
        ProcessEntry(9_000_005, 1, 9_000_005, f"logos_host --path {run}x/elsewhere"),
        ProcessEntry(9_000_006, 1, 9_000_006, f"logos_host --path {run}"),
    ]
    found = live.reap(run, dry_run=True, table=table)
    assert [e.pid for e in found] == [9_000_002, 9_000_003, 9_000_004]
    assert [e.pid for e in live.processes_under(f"{run}/", table)] == [9_000_002, 9_000_003, 9_000_004]


SPAWNER = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", os.environ["MARKER"]])
print(child.pid, flush=True)
child.wait()
print("child gone", flush=True)
time.sleep(60)
"""


def wait_gone(process: subprocess.Popen[Any], timeout: float = 15.0) -> int:
    return process.wait(timeout=timeout)


@pytest.fixture
def run_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="lbreap-"))
    yield path
    live.reap(path)
    path.rmdir()


def test_the_reaper_kills_real_processes(run_dir: Path) -> None:
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"{run_dir}/lb-1"],
                              start_new_session=True)
    spawner = subprocess.Popen([sys.executable, "-c", SPAWNER], start_new_session=True, stdout=subprocess.PIPE,
                               text=True, env={**os.environ, "MARKER": f"{run_dir}/lb-2/data"})
    try:
        assert spawner.stdout is not None
        child = int(spawner.stdout.readline())
        deadline = time.monotonic() + 15
        while not any(e.pid == child for e in live.processes_under(run_dir)):
            assert time.monotonic() < deadline, "the child never showed up"
            time.sleep(0.05)
        found = {e.pid for e in live.reap(run_dir)}
        assert {leader.pid, child} <= found and spawner.pid not in found
        assert wait_gone(leader) == -9  # a group leader: its group is killed
        assert spawner.stdout.readline().strip() == "child gone"  # a member: only itself
        assert spawner.poll() is None
    finally:
        for process in (leader, spawner):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
        if spawner.stdout is not None:
            spawner.stdout.close()
