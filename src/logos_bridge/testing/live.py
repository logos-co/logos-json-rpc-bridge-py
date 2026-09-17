"""A real json_rpc_bridge under a logoscore daemon, for integration tests.

::

    from logos_bridge import AsyncBridgeClient
    from logos_bridge.testing.live import live_bridge

    with live_bridge(["storage_module"]) as node:      # LiveStack.from_env()
        async with AsyncBridgeClient(node.ws_url) as bridge:
            ...

:func:`live_bridge` starts a daemon over the stack's modules directories, loads the
providers and then ``json_rpc_bridge``, starts the bridge on a free port, and waits until
discovery has settled and the served documents show it. On exit it stops the daemon and
kills whatever module host outlived it. ``python -m logos_bridge.testing.live --reap DIR``
is the backstop for a test process that was killed: it kills every process whose command
line names a path under ``DIR`` (use ``LOGOS_LIVE_RUN_DIR``).

The daemon is logos-logoscore-py's ``LogoscoreDaemon`` (the ``logoscore`` package). It is
not a dependency of this package, and is imported on first use.

Environment (:meth:`LiveStack.from_env`):

``LOGOS_LOGOSCORE_BIN``
    The ``logoscore`` executable (then ``LOGOSCORE_BIN``, then ``PATH``).
``LOGOS_BRIDGE_INSTALL_DIR``
    The bridge's installed modules: a directory holding ``json_rpc_bridge/``, or an
    install output holding ``modules/``.
``LOGOS_LIVE_MODULES_DIRS``
    More modules directories (the providers'), separated by ``os.pathsep``.
``LOGOS_LIVE_RUN_DIR``
    Where daemons get their config directories (default: the temporary directory).
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import functools
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, Final, TypeVar

from ._helpers import free_port

BRIDGE_MODULE: Final = "json_rpc_bridge"
LOGOSCORE_ENV: Final = "LOGOS_LOGOSCORE_BIN"
LOGOSCORE_ENV_FALLBACK: Final = "LOGOSCORE_BIN"  # logos-logoscore-py's own name
BRIDGE_INSTALL_ENV: Final = "LOGOS_BRIDGE_INSTALL_DIR"
MODULES_DIRS_ENV: Final = "LOGOS_LIVE_MODULES_DIRS"
RUN_DIR_ENV: Final = "LOGOS_LIVE_RUN_DIR"
#: The daemon's sockets are $TMPDIR/logos_<module>_<id>, and macOS caps AF_UNIX paths at 104 bytes.
SHORT_TMPDIR_LIMIT: Final = 40

LOGOSCORE_HINT: Final = (
    "pip install git+https://github.com/logos-co/logos-logoscore-py, or put its src/ on PYTHONPATH"
)

T = TypeVar("T")


class LiveBridgeUnavailable(RuntimeError):
    """Something a live bridge needs is missing: an executable, a directory, or the logoscore package."""


class LiveBridgeError(RuntimeError):
    """The daemon or the bridge did not do what was asked."""


def require_logoscore() -> ModuleType:
    """The ``logoscore`` package (logos-logoscore-py), or :class:`LiveBridgeUnavailable`."""
    try:
        import logoscore
    except ImportError as exc:
        raise LiveBridgeUnavailable(
            f"the logoscore package (logos-logoscore-py) is not importable ({exc}): {LOGOSCORE_HINT}"
        ) from None
    module: ModuleType = logoscore
    return module


# -- the stack --------------------------------------------------------------


def _executable(value: str, origin: str) -> str:
    if not (Path(value).is_file() and os.access(value, os.X_OK)):
        raise LiveBridgeUnavailable(f"{origin} ({value}) is not an executable file")
    return value


def find_logoscore(env: Mapping[str, str] | None = None) -> str:
    """``$LOGOS_LOGOSCORE_BIN``, then ``$LOGOSCORE_BIN``, then ``logoscore`` on ``PATH``."""
    env = os.environ if env is None else env
    for name in (LOGOSCORE_ENV, LOGOSCORE_ENV_FALLBACK):
        if env.get(name):
            return _executable(env[name], name)
    found = shutil.which("logoscore", path=env.get("PATH", ""))
    if found is None:
        raise LiveBridgeUnavailable(
            f"the logoscore CLI is not available: set {LOGOSCORE_ENV} or put logoscore on PATH "
            "(nix: github:logos-co/logos-logoscore-cli)"
        )
    return found


def modules_dir(value: str | os.PathLike[str], origin: str = "modules directory") -> str:
    """A modules directory, given it or an install output that holds one in ``modules/``."""
    path = Path(value)
    if (path / "modules").is_dir() and not (path / BRIDGE_MODULE).is_dir():
        path = path / "modules"
    if not path.is_dir():
        raise LiveBridgeUnavailable(f"{origin} names a missing directory: {os.fspath(value)}")
    return str(path)


@dataclass(frozen=True)
class LiveStack:
    """What a live bridge runs: the ``logoscore`` CLI and the modules directories its daemon scans.

    One directory must hold ``json_rpc_bridge/``. ``run_dir`` is where daemons get their
    config directories (``None``: the temporary directory).
    """

    logoscore: str
    modules_dirs: tuple[str, ...]
    run_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *,
                 modules_dirs: Iterable[str | os.PathLike[str]] = ()) -> LiveStack:
        """The stack the environment describes (see the module docstring), plus ``modules_dirs``."""
        env = os.environ if env is None else env
        logoscore = find_logoscore(env)
        dirs = [modules_dir(d, "modules_dirs") for d in modules_dirs]
        dirs += [modules_dir(d, MODULES_DIRS_ENV) for d in env.get(MODULES_DIRS_ENV, "").split(os.pathsep) if d]
        install = env.get(BRIDGE_INSTALL_ENV)
        if not install:
            raise LiveBridgeUnavailable(
                f"{BRIDGE_INSTALL_ENV} is not set: point it at json_rpc_bridge's installed modules "
                "(nix: the modules/ of github:logos-co/logos-json-rpc-bridge#install)"
            )
        dirs.append(modules_dir(install, BRIDGE_INSTALL_ENV))
        run_dir = env.get(RUN_DIR_ENV)
        stack = cls(logoscore, tuple(dict.fromkeys(dirs)), Path(run_dir) if run_dir else None)
        return stack.check()

    def check(self) -> LiveStack:
        """Raise :class:`LiveBridgeUnavailable` unless every piece is there."""
        _executable(self.logoscore, "logoscore")
        for directory in self.modules_dirs:
            if not Path(directory).is_dir():
                raise LiveBridgeUnavailable(f"a modules directory is missing: {directory}")
        if not any((Path(d) / BRIDGE_MODULE).is_dir() for d in self.modules_dirs):
            raise LiveBridgeUnavailable(
                f"no modules directory holds {BRIDGE_MODULE}/ ({', '.join(self.modules_dirs) or 'none given'}); "
                f"set {BRIDGE_INSTALL_ENV}"
            )
        require_logoscore()
        return self


def bridge_config(modules: Iterable[str | Mapping[str, Any]], *, revalidate_ms: int | None = 1000,
                  **sections: Mapping[str, Any]) -> dict[str, Any]:
    """A ``json_rpc_bridge`` config exposing ``modules``: names, or ``{"name", "methods", "events"}``.

    ``revalidate_ms`` sets ``discovery.revalidate_ms`` (``None``: the bridge's default);
    ``sections`` adds top-level sections, e.g. ``limits={"call_timeout_ms": 5000}``.
    ``http.port`` is set when the bridge starts.
    """
    exposed: list[Any] = []
    for entry in modules:
        if isinstance(entry, str):
            exposed.append(entry)
        elif isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            exposed.append(copy.deepcopy(dict(entry)))
        else:
            raise TypeError(f"a module is a name or a mapping with a 'name', got {entry!r}")
    if not exposed:
        raise ValueError("a bridge config exposes at least one module")
    config: dict[str, Any] = {"expose": {"modules": exposed}}
    if revalidate_ms is not None:
        config["discovery"] = {"revalidate_ms": revalidate_ms}
    for key, value in sections.items():
        if key in config:
            raise ValueError(f"the {key!r} section comes from the other arguments")
        config[key] = copy.deepcopy(dict(value))
    return config


def exposed_modules(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The module names a bridge config exposes, in order."""
    modules = config.get("expose", {}).get("modules", [])
    return tuple(m if isinstance(m, str) else m["name"] for m in modules)


# -- temporary directories --------------------------------------------------


def short_tmpdir(prefix: str = "lb") -> Path:
    """A new directory with a short path: under ``/tmp`` on macOS, else in the temporary directory."""
    base = "/tmp" if sys.platform == "darwin" else None
    return Path(tempfile.mkdtemp(prefix=f"{prefix}.", dir=base))


@contextmanager
def short_tmpdir_env(prefix: str = "lb", *, force: bool = False) -> Iterator[Path]:
    """Point ``TMPDIR`` at a :func:`short_tmpdir` for the block, then restore it and remove the directory.

    The daemon, its module hosts and every ``logoscore`` call must share one ``TMPDIR``,
    so this is process-wide. It applies only on macOS when the current temporary directory
    is longer than :data:`SHORT_TMPDIR_LIMIT`, unless ``force``; otherwise it yields that
    directory and changes nothing.
    """
    current = tempfile.gettempdir()
    if not force and (sys.platform != "darwin" or len(current) <= SHORT_TMPDIR_LIMIT):
        yield Path(current)
        return
    short = short_tmpdir(prefix)
    saved_env, saved_tempdir = os.environ.get("TMPDIR"), tempfile.tempdir
    os.environ["TMPDIR"] = str(short)
    tempfile.tempdir = str(short)
    try:
        yield short
    finally:
        if saved_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = saved_env
        tempfile.tempdir = saved_tempdir
        shutil.rmtree(short, ignore_errors=True)


# -- raw HTTP ---------------------------------------------------------------


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def http_request(port: int, method: str, path: str, body: bytes | None = None, *,
                 headers: Mapping[str, str] | None = None, timeout: float = 30.0) -> HttpAnswer:
    """One request to ``127.0.0.1:port`` on a fresh connection, headers exactly as given.

    ``Host`` defaults to ``127.0.0.1:port``; ``Connection: close`` and ``Content-Length`` are added.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        given = {k.lower() for k in (headers or {})}
        if "host" not in given:
            conn.putheader("Host", f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.putheader("Connection", "close")
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return HttpAnswer(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
    finally:
        conn.close()


def rpc_http(port: int, payload: Any, *, headers: Mapping[str, str] | None = None,
             timeout: float = 30.0) -> HttpAnswer:
    """``POST /rpc`` with ``payload`` as JSON."""
    return http_request(port, "POST", "/rpc", json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json", **(headers or {})}, timeout=timeout)


# -- processes --------------------------------------------------------------


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    ppid: int
    pgid: int
    command: str


# sysctl(3) on macOS: struct kinfo_proc is 648 bytes; p_pid at 40, e_ppid and e_pgid at 560 and 564.
_CTL_KERN, _KERN_ARGMAX, _KERN_PROC, _KERN_PROCARGS2 = 1, 8, 14, 49
_KERN_PROC_ALL, _KERN_PROC_PID = 0, 1
_KINFO_SIZE, _KINFO_PID, _KINFO_PPID_PGID = 648, 40, 560


def _darwin_process_table() -> list[ProcessEntry] | None:
    """The process table from sysctl (a nix build cannot run the setuid /bin/ps); ``None`` if unusable."""
    import ctypes
    import errno
    import struct

    try:
        sysctl = ctypes.CDLL(None, use_errno=True).sysctl
    except (OSError, AttributeError):
        return None
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int

    def read(*mib: int) -> bytes | None:
        name = (ctypes.c_int * len(mib))(*mib)
        for _ in range(4):
            size = ctypes.c_size_t(0)
            if sysctl(name, len(mib), None, ctypes.byref(size), None, 0) != 0:
                return None
            buf = ctypes.create_string_buffer(size.value + size.value // 4 + 4096)  # the table may grow
            size.value = len(buf)
            if sysctl(name, len(mib), buf, ctypes.byref(size), None, 0) == 0:
                return ctypes.string_at(buf, size.value)
            if ctypes.get_errno() != errno.ENOMEM:
                return None
        return None

    def ids(data: bytes, at: int) -> tuple[int, int, int]:
        (pid,) = struct.unpack_from("i", data, at + _KINFO_PID)
        ppid, pgid = struct.unpack_from("ii", data, at + _KINFO_PPID_PGID)
        return pid, ppid, pgid

    mine = read(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, os.getpid())
    if mine is None or len(mine) != _KINFO_SIZE or ids(mine, 0) != (os.getpid(), os.getppid(), os.getpgrp()):
        return None  # not the layout this reads
    table = read(_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, 0)
    argmax = read(_CTL_KERN, _KERN_ARGMAX)
    if table is None or len(table) % _KINFO_SIZE or argmax is None:
        return None
    (limit,) = struct.unpack("i", argmax[:4])
    args = ctypes.create_string_buffer(limit)

    def command(pid: int) -> str:
        # KERN_PROCARGS2: argc, the executable path, NUL padding, then argc arguments (own user only).
        name = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(limit)
        if sysctl(name, 3, args, ctypes.byref(size), None, 0) != 0 or size.value < 4:
            return ""
        raw = ctypes.string_at(args, size.value)
        (argc,) = struct.unpack_from("i", raw, 0)
        start = raw.find(b"\0", 4)
        if start < 0:
            return ""
        while start < len(raw) and raw[start] == 0:
            start += 1
        return " ".join(a.decode("utf-8", "replace") for a in raw[start:].split(b"\0")[:argc])

    return [ProcessEntry(pid, ppid, pgid, command(pid))
            for pid, ppid, pgid in (ids(table, at) for at in range(0, len(table), _KINFO_SIZE))]


def process_table() -> list[ProcessEntry]:
    """Every visible process: from ``/proc`` (Linux), sysctl (macOS) or ``ps``; empty if none works.

    Command lines are those of this user's processes (others may show as empty).
    """
    if sys.platform == "darwin":
        found = _darwin_process_table()
        if found is not None:
            return found
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        entries = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            fields = stat[stat.rfind(")") + 2:].split()  # state, ppid, pgrp, ...
            command = cmdline.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            entries.append(ProcessEntry(int(entry.name), int(fields[1]), int(fields[2]), command))
        return entries
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run([ps, "-A", "-ww", "-o", "pid=,ppid=,pgid=,command="], capture_output=True,
                             text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    entries = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and all(p.lstrip("-").isdigit() for p in parts[:3]):
            entries.append(ProcessEntry(int(parts[0]), int(parts[1]), int(parts[2]),
                                        parts[3] if len(parts) > 3 else ""))
    return entries


def _spared(table: Sequence[ProcessEntry]) -> set[int]:
    """This process, its ancestors, and its process group: never killed."""
    by_pid = {e.pid: e for e in table}
    spared = {os.getpid()}
    pid = os.getpid()
    while pid in by_pid and by_pid[pid].ppid > 0 and by_pid[pid].ppid not in spared:
        pid = by_pid[pid].ppid
        spared.add(pid)
    own_group = os.getpgrp()
    return spared | {e.pid for e in table if e.pgid == own_group}


def processes_under(directory: str | os.PathLike[str],
                    table: Sequence[ProcessEntry] | None = None) -> list[ProcessEntry]:
    """Processes whose command line names a path inside ``directory`` (see :func:`_spared`)."""
    entries = process_table() if table is None else list(table)
    path = os.fspath(directory)
    markers = {os.path.abspath(path).rstrip(os.sep) + os.sep, os.path.realpath(path).rstrip(os.sep) + os.sep}
    spared = _spared(entries)
    return [e for e in entries if e.pid not in spared and any(m in e.command for m in markers)]


def kill_processes(entries: Iterable[ProcessEntry]) -> list[int]:
    """SIGKILL each entry, and the whole group of a group leader. Returns the pids signalled."""
    own_group = os.getpgrp()
    killed = []
    for entry in entries:
        try:
            if entry.pgid == entry.pid and entry.pgid != own_group:
                os.killpg(entry.pgid, signal.SIGKILL)
            else:
                os.kill(entry.pid, signal.SIGKILL)
        except OSError:  # already gone, or not ours
            continue
        killed.append(entry.pid)
    return killed


def reap(directory: str | os.PathLike[str], *, dry_run: bool = False,
         table: Sequence[ProcessEntry] | None = None) -> list[ProcessEntry]:
    """Kill every process that names a path under ``directory``; returns them (``dry_run``: only find)."""
    found = processes_under(directory, table)
    if not dry_run:
        kill_processes(found)
    return found


# -- the node ---------------------------------------------------------------


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def interrupt_on_sigterm() -> None:
    """Turn SIGTERM into KeyboardInterrupt (if nothing handles it yet), so ``finally`` blocks and
    pytest's finalizers stop the daemons. Main thread only."""
    if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
        signal.signal(signal.SIGTERM, _raise_interrupt)


class LiveBridge:
    """A logoscore daemon with ``providers`` and ``json_rpc_bridge`` loaded, and the bridge started.

    ``also_expose`` names modules the default config exposes without loading them (the
    daemon's own, such as ``modules_state``). The methods block; from asyncio, use
    :func:`async_live_bridge` and ``asyncio.to_thread`` (or ``loop.run_in_executor``).
    """

    _live: set[LiveBridge] = set()
    _atexit_registered = False

    def __init__(self, stack: LiveStack, providers: Sequence[str] = (), *, also_expose: Sequence[str] = (),
                 label: str = "live", env: Mapping[str, str] | None = None, startup_timeout: float = 60.0,
                 cli_timeout: float = 60.0) -> None:
        self.stack = stack
        self.providers = tuple(providers)
        self.also_expose = tuple(also_expose)
        self.label = label
        self.startup_timeout = startup_timeout
        self.cli_timeout = cli_timeout
        self.port = 0
        self.config: dict[str, Any] = {}
        self.started: dict[str, Any] = {}
        self._env = dict(env or {})
        self._daemon: Any = None
        self._client: Any = None
        self._config_dir: Path | None = None
        self._last_logs = ("", "")

    def __repr__(self) -> str:
        return f"<LiveBridge {self.label} port={self.port} config_dir={self._config_dir}>"

    def __enter__(self) -> LiveBridge:
        return self if self._daemon is not None else self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------ description

    @property
    def running(self) -> bool:
        return self._daemon is not None

    @property
    def daemon(self) -> Any:
        """The ``logoscore.LogoscoreDaemon``."""
        if self._daemon is None:
            raise LiveBridgeError(f"{self.label} is not running")
        return self._daemon

    @property
    def client(self) -> Any:
        """A ``logoscore.LogoscoreClient`` for the daemon."""
        if self._client is None:
            raise LiveBridgeError(f"{self.label} is not running")
        return self._client

    @property
    def config_dir(self) -> Path | None:
        """The daemon's ``--config-dir``: ``logoscore --config-dir DIR ...`` talks to it."""
        return self._config_dir

    @property
    def exposed(self) -> tuple[str, ...]:
        return exposed_modules(self.config)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------- lifecycle

    def start(self, config: Mapping[str, Any] | None = None, *, port: int | None = None,
              settle: float | None = 60.0) -> LiveBridge:
        """Start the daemon, load the modules, start the bridge (``config``: :func:`bridge_config` of the
        providers and ``also_expose``), and wait up to ``settle`` seconds (``None``: don't) for discovery."""
        if self._daemon is not None:
            raise LiveBridgeError(f"{self.label} is already running")
        logoscore = require_logoscore()
        self.stack.check()
        base = self.stack.run_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        self._config_dir = Path(tempfile.mkdtemp(prefix="lb-", dir=base))
        env = {"QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"), **self._env}
        self._daemon = logoscore.LogoscoreDaemon(list(self.stack.modules_dirs), binary=self.stack.logoscore,
                                                 config_dir=self._config_dir, env=env,
                                                 startup_timeout=self.startup_timeout)
        LiveBridge._live.add(self)
        if not LiveBridge._atexit_registered:
            atexit.register(LiveBridge.stop_all)
            LiveBridge._atexit_registered = True
        try:
            self._daemon.start()
            self._client = self._daemon.client(timeout=self.cli_timeout)
            for module in (*self.providers, BRIDGE_MODULE):
                self.load(module)
            self.start_bridge(config if config is not None
                              else bridge_config((*self.providers, *self.also_expose)), port=port)
            if settle is not None:
                self.wait_settled(settle)
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        """Stop the daemon and kill any module host that outlived it. Idempotent."""
        daemon, self._daemon, self._client = self._daemon, None, None
        config_dir = self._config_dir
        LiveBridge._live.discard(self)
        if daemon is None:
            return
        watched: list[ProcessEntry] = []
        if config_dir is not None:
            table = process_table()
            watched = [e for e in table if e.ppid == daemon.pid] + processes_under(config_dir, table)
        try:
            self._last_logs = daemon.logs()
            daemon.stop(timeout=20.0)
        finally:
            seen = {e.pid: e.command for e in watched}
            leftovers = [e for e in process_table() if seen.get(e.pid) == e.command]  # same pid, same process
            killed = kill_processes(leftovers)
            if killed:
                print(f"logos_bridge.testing.live: killed {len(killed)} process(es) left by {self.label}: {killed}",
                      file=sys.stderr)
            if config_dir is not None:
                shutil.rmtree(config_dir, ignore_errors=True)

    @classmethod
    def stop_all(cls) -> None:
        """Stop every running node (registered with ``atexit`` on first start)."""
        for node in list(cls._live):
            node.stop()

    def logs(self) -> tuple[str, str]:
        """The daemon's (stdout, stderr), also after :meth:`stop`."""
        if self._daemon is None:
            return self._last_logs
        logs: tuple[str, str] = self._daemon.logs()
        return logs

    # --------------------------------------------------------------- daemon

    def load(self, module: str) -> None:
        self.client.load_module(module)

    def unload(self, module: str) -> None:
        self.client.unload_module(module)

    def reload(self, module: str) -> None:
        self.client.reload_module(module)

    def daemon_call(self, module: str, method: str, *args: Any, timeout: float | None = None) -> Any:
        """``module.method(*args)`` through the daemon's CLI, not the bridge (the CLI's argument typing applies)."""
        return self.client.call(module, method, *args, timeout=timeout)

    # ---------------------------------------------------------------- bridge

    def start_bridge(self, config: Mapping[str, Any], *, port: int | None = None) -> dict[str, Any]:
        """``json_rpc_bridge.start(json.dumps(config))`` on ``port`` (default: a free one); returns its value."""
        last: Any = None
        for _ in range(1 if port is not None else 3):
            effective = copy.deepcopy(dict(config))
            effective.setdefault("http", {})["port"] = port or free_port()
            answer = self.daemon_call(BRIDGE_MODULE, "start", json.dumps(effective))
            if isinstance(answer, dict) and answer.get("success") is True:
                self.port, self.config = effective["http"]["port"], effective
                self.started = dict(answer.get("value") or {})
                return self.started
            last = answer
        raise LiveBridgeError(f"{BRIDGE_MODULE}.start refused {dict(config)}: {last}")

    def stop_bridge(self) -> None:
        self.daemon_call(BRIDGE_MODULE, "stop")

    def info(self) -> dict[str, Any]:
        """``json_rpc_bridge.getInfo()``, decoded (it answers JSON text)."""
        answer = self.daemon_call(BRIDGE_MODULE, "getInfo")
        info = json.loads(answer) if isinstance(answer, str) else answer
        if not isinstance(info, dict):
            raise LiveBridgeError(f"unexpected getInfo answer: {answer!r}")
        return info

    def views(self) -> dict[str, dict[str, Any]]:
        """``GET /modules``, by module name."""
        answer = http_request(self.port, "GET", "/modules")
        if answer.status != 200:
            raise LiveBridgeError(f"GET /modules answered {answer.status}: {answer.body[:200]!r}")
        return {view["module"]: view for view in answer.json()}

    def view(self, module: str) -> dict[str, Any]:
        """``rpc.schema`` (the view with ``interface``)."""
        answer = rpc_http(self.port, {"jsonrpc": "2.0", "id": 1, "method": "rpc.schema",
                                      "params": {"module": module}}).json()
        if "result" not in answer:
            raise LiveBridgeError(f"rpc.schema {module}: {answer}")
        result: dict[str, Any] = answer["result"]
        return result

    def published(self, views: Mapping[str, Mapping[str, Any]]) -> bool:
        """Whether the served documents show ``views``: they are rebuilt asynchronously (25 ms coalescing)."""
        answer = rpc_http(self.port, {"jsonrpc": "2.0", "id": 1, "method": "rpc.discover"}).json()
        listed = {entry.get("name"): entry for entry in answer["result"].get("x-logos-modules", [])}

        def same(module: str) -> bool:
            entry, view = listed.get(module, {}), views[module]
            keys = ["exposure"]
            if view.get("interface_status") == "ok":  # only typed entries carry the digests
                keys += ["interface_sha256", "contract_sha256"]
            return entry.get("status") == view.get("interface_status") and all(
                entry.get(k) == view.get(k) for k in keys)
        return all(same(m) for m in self.exposed)

    def wait_until(self, predicate: Callable[[], T | None], timeout: float, what: str,
                   interval: float = 0.1) -> T:
        """Poll ``predicate`` until it returns something truthy; :class:`LiveBridgeError` after ``timeout``."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while True:
            try:
                last = predicate()
            except (OSError, http.client.HTTPException, LiveBridgeError, ValueError, KeyError) as exc:
                last = exc
            else:
                if last:
                    result: T = last
                    return result
            if time.monotonic() >= deadline:
                raise LiveBridgeError(
                    f"{self.label}: timed out after {timeout:g}s waiting for {what} (last: {last!r:.300})")
            time.sleep(interval)

    def wait_settled(self, timeout: float = 60.0) -> dict[str, dict[str, Any]]:
        """Every exposed module discovered (not ``pending``), not stale, and shown by the documents."""
        def settled() -> dict[str, dict[str, Any]] | None:
            views = self.views()
            ready = all(m in views and views[m].get("interface_status") not in (None, "pending")
                        and views[m].get("stale") is False for m in self.exposed)
            return views if ready and self.published(views) else None
        return self.wait_until(settled, timeout, "discovery to settle")

    def wait_status(self, module: str, status: str, timeout: float = 60.0) -> dict[str, Any]:
        """``module``'s view once it has ``status`` and is not stale."""
        def reached() -> dict[str, Any] | None:
            view = self.views().get(module)
            return view if view and view.get("interface_status") == status and view.get("stale") is False else None
        return self.wait_until(reached, timeout, f"{module} to be {status}")


@contextmanager
def live_bridge(providers: Sequence[str] = (), config: Mapping[str, Any] | None = None, *,
                stack: LiveStack | None = None, also_expose: Sequence[str] = (), port: int | None = None,
                settle: float | None = 60.0, label: str = "live") -> Iterator[LiveBridge]:
    """A started :class:`LiveBridge` for the block (``stack``: :meth:`LiveStack.from_env`)."""
    node = LiveBridge(stack or LiveStack.from_env(), providers, also_expose=also_expose, label=label)
    node.start(config, port=port, settle=settle)
    try:
        yield node
    finally:
        node.stop()


async def _in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args, **kwargs))


@asynccontextmanager
async def async_live_bridge(providers: Sequence[str] = (), config: Mapping[str, Any] | None = None, *,
                            stack: LiveStack | None = None, also_expose: Sequence[str] = (),
                            port: int | None = None, settle: float | None = 60.0,
                            label: str = "live") -> AsyncIterator[LiveBridge]:
    """:func:`live_bridge` for asyncio: start and stop run in a worker thread."""
    node = LiveBridge(stack or LiveStack.from_env(), providers, also_expose=also_expose, label=label)
    starting = asyncio.ensure_future(_in_thread(node.start, config, port=port, settle=settle))
    try:
        await asyncio.shield(starting)
        yield node
    finally:
        if not starting.done():
            await asyncio.wait({starting})  # a cancelled start still finishes before teardown
        await _in_thread(node.stop)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m logos_bridge.testing.live --reap DIR``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--reap":
        print("usage: python -m logos_bridge.testing.live --reap DIR", file=sys.stderr)
        return 2
    found = reap(args[1])
    if found:
        print(f"logos_bridge.testing.live: reaped {len(found)} process(es) under {args[1]}: "
              f"{[e.pid for e in found]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
