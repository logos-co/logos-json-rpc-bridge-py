"""Where contracts come from: ``.lidl`` files, AST files, LGX packages, a running bridge.

``.lidl`` text is parsed only by logos-lidl's ``lidl`` CLI (``LOGOS_LIDL_CLI``, then
``PATH``). LGX packages are opened only by logos-package's ``lgx`` CLI
(``LOGOS_LGX_CLI``, then ``PATH``): ``lgx verify``, ``lgx manifest --json``, then
``lgx extract`` (``--assets-only`` when the CLI has it, otherwise one variant), and
the contract is ``assets/lidl/<module>.lidl``. A package also carries its
dependencies' contracts, so the module is always selected by name.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .codec import loads
from .digest import contract_sha256, interface_sha256
from .errors import BridgeError, BridgeWarning
from .lidl import IdentityError, Interface, InterfaceInvalid, LidlFormatError
from .models import ModuleInfo

LIDL_CLI_ENV: Final = "LOGOS_LIDL_CLI"
LGX_CLI_ENV: Final = "LOGOS_LGX_CLI"
CLI_TIMEOUT: Final = 120.0

# lidl CLI exit codes (logos-lidl src/cli.cpp)
LIDL_EXIT_USAGE: Final = 1
LIDL_EXIT_UNREADABLE: Final = 2
LIDL_EXIT_PARSE: Final = 3
LIDL_EXIT_IDENTITY: Final = 4
LIDL_EXIT_INVALID: Final = 5


class ContractSourceError(BridgeError):
    """A contract could not be obtained. ``exit_code`` is the codegen CLI's."""

    exit_code: int = 3


class SourceUnavailable(ContractSourceError):
    """The source is missing, unreadable, untyped, or a tool is not installed."""

    exit_code = 3


class CliNotFound(SourceUnavailable):
    """A required command-line tool is not installed."""


class ContractRejected(ContractSourceError):
    """The contract was found and refused: unparseable, invalid, or inconsistent."""

    exit_code = 4


# -- tools -------------------------------------------------------------------


def _find_tool(explicit: str | os.PathLike[str] | None, env: str, name: str, package: str) -> str:
    candidates = [("argument", os.fspath(explicit))] if explicit is not None else []
    if os.environ.get(env):
        candidates.append((env, os.environ[env]))
    for origin, value in candidates:
        path = Path(value)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise CliNotFound(f"the {name} CLI given by {origin} ({value}) is not an executable file")
    found = shutil.which(name)
    if found is None:
        raise CliNotFound(
            f"the {name} CLI is not installed: set {env} or put {name} on PATH "
            f"(nix: {package})"
        )
    return found


def find_lidl_cli(explicit: str | os.PathLike[str] | None = None) -> str:
    """The ``lidl`` executable: ``explicit``, then ``$LOGOS_LIDL_CLI``, then ``PATH``."""
    return _find_tool(explicit, LIDL_CLI_ENV, "lidl", "logos-lidl#lidl-cli")


def find_lgx_cli(explicit: str | os.PathLike[str] | None = None) -> str:
    """The ``lgx`` executable: ``explicit``, then ``$LOGOS_LGX_CLI``, then ``PATH``."""
    return _find_tool(explicit, LGX_CLI_ENV, "lgx", "logos-package#lgx")


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace").strip()


def _run(argv: Sequence[str], *, stdin: bytes | None = None, timeout: float = CLI_TIMEOUT) -> CliResult:
    try:
        proc = subprocess.run(list(argv), input=stdin, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise CliNotFound(f"cannot run {argv[0]}: {exc}") from None
    except PermissionError as exc:
        raise CliNotFound(f"cannot run {argv[0]}: {exc}") from None
    except subprocess.TimeoutExpired:
        raise SourceUnavailable(f"{' '.join(argv)} did not finish within {timeout:g}s") from None
    return CliResult(proc.returncode, proc.stdout, proc.stderr)


class LidlCli:
    """logos-lidl's ``lidl`` command."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = find_lidl_cli(path)
        self._version: str | None = None

    def __repr__(self) -> str:
        return f"<LidlCli {self.path}>"

    def version(self) -> str:
        """``lidl --version``, e.g. ``lidl 0.1.0 (2043d8b)``."""
        if self._version is None:
            result = _run([self.path, "--version"])
            if result.returncode != 0:
                raise SourceUnavailable(f"{self.path} --version failed: {result.err}")
            self._version = result.stdout.decode("utf-8", "replace").strip()
        return self._version

    def _fail(self, result: CliResult, what: str) -> ContractSourceError:
        message = result.err or f"exit status {result.returncode}"
        if result.returncode == LIDL_EXIT_UNREADABLE:
            return SourceUnavailable(message)
        if result.returncode in (LIDL_EXIT_PARSE, LIDL_EXIT_IDENTITY, LIDL_EXIT_INVALID):
            return ContractRejected(message)
        return SourceUnavailable(f"{self.path} {what} failed: {message}")

    def json(self, source: str | os.PathLike[str], *, identity: bool = True) -> dict[str, Any]:
        """``lidl json [--identity] FILE``: the JSON AST."""
        argv = [self.path, "json"] + (["--identity"] if identity else []) + ["--", os.fspath(source)]
        result = _run(argv)
        if result.returncode != 0:
            raise self._fail(result, "json")
        try:
            doc = loads(result.stdout)
        except ValueError as exc:
            raise ContractRejected(f"{self.path} json printed something that is not JSON: {exc}") from None
        if not isinstance(doc, dict):
            raise ContractRejected(f"{self.path} json printed a {type(doc).__name__}, not an AST object")
        return doc

    def check(self, source: str | os.PathLike[str], *, identity: bool = False) -> tuple[int, dict[str, list[str]]]:
        """``lidl check --json``: the exit status and ``{errors, warnings}``."""
        argv = [self.path, "check", "--json"] + (["--identity"] if identity else []) + ["--", os.fspath(source)]
        result = _run(argv)
        if result.returncode not in (0, LIDL_EXIT_INVALID):
            raise self._fail(result, "check")
        report = loads(result.stdout)
        if not isinstance(report, dict):
            raise ContractRejected(f"{self.path} check --json printed {report!r}")
        return result.returncode, report

    def fmt(self, source: str | os.PathLike[str]) -> str:
        """``lidl fmt FILE``: the canonical text."""
        result = _run([self.path, "fmt", "--", os.fspath(source)])
        if result.returncode != 0:
            raise self._fail(result, "fmt")
        return result.stdout.decode("utf-8")


class LgxCli:
    """logos-package's ``lgx`` command. No ``.lgx`` is ever opened any other way."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = find_lgx_cli(path)
        self._assets_only: bool | None = None

    def __repr__(self) -> str:
        return f"<LgxCli {self.path}>"

    def verify(self, package: str | os.PathLike[str]) -> None:
        result = _run([self.path, "verify", os.fspath(package)])
        if result.returncode != 0:
            detail = result.err or result.stdout.decode("utf-8", "replace").strip()
            raise ContractRejected(f"{package} failed lgx verify: {detail}")

    def manifest(self, package: str | os.PathLike[str]) -> dict[str, Any]:
        result = _run([self.path, "manifest", os.fspath(package), "--json"])
        if result.returncode != 0:
            raise ContractRejected(f"lgx manifest {package} failed: {result.err}")
        try:
            doc = loads(result.stdout)
        except ValueError as exc:
            raise ContractRejected(f"lgx manifest {package} printed invalid JSON: {exc}") from None
        if not isinstance(doc, dict):
            raise ContractRejected(f"lgx manifest {package} printed {type(doc).__name__}, not an object")
        return doc

    def supports_assets_only(self) -> bool:
        """Whether ``lgx extract`` knows ``--assets-only`` (logos-package from d759f34, #42, on)."""
        if self._assets_only is None:
            result = _run([self.path, "extract", "--help"])
            text = result.stdout + result.stderr
            self._assets_only = b"--assets-only" in text
        return self._assets_only

    def extract_assets(self, package: str | os.PathLike[str], into: str | os.PathLike[str],
                       manifest: Mapping[str, Any] | None = None) -> Path:
        """Extract the root ``assets/`` tree; returns the directory that holds it."""
        target = Path(into)
        if self.supports_assets_only():
            result = _run([self.path, "extract", os.fspath(package), "--assets-only", "--output", str(target)])
            if result.returncode != 0:
                raise ContractRejected(f"lgx extract --assets-only {package} failed: {result.err}")
            return target / "assets"
        variants = sorted(variant_names(manifest if manifest is not None else self.manifest(package)))
        if not variants:
            raise ContractRejected(f"{package} has no variants, so its assets cannot be extracted")
        variant = variants[0]  # assets are the same for every variant
        result = _run([self.path, "extract", os.fspath(package), "--variant", variant, "--output", str(target)])
        if result.returncode != 0:
            raise ContractRejected(f"lgx extract --variant {variant} {package} failed: {result.err}")
        return target / variant / "assets"


def variant_names(manifest: Mapping[str, Any]) -> list[str]:
    """The variants a manifest declares (the keys of ``main``)."""
    main = manifest.get("main")
    return [str(k) for k in main] if isinstance(main, Mapping) else []


# -- sources -----------------------------------------------------------------


@dataclass(frozen=True)
class ContractSource:
    """A contract and where it came from.

    ``interface`` has the identity built-ins unless loaded with ``identity=False``.
    ``text`` is the contract's exact text when the source had one (not for ``--ast``).
    """

    interface: Interface
    kind: str
    location: str
    text: str | None = None
    reader: str | None = None
    info: ModuleInfo | None = field(default=None, compare=False)
    notes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.interface.name

    @property
    def interface_sha256(self) -> str:
        return self.interface.interface_sha256()

    @property
    def contract_sha256(self) -> str | None:
        return contract_sha256(self.text) if self.text is not None else None

    @property
    def shape_sha256(self) -> str:
        return self.interface.shape_sha256()

    def describe(self) -> str:
        """One line for a generated file's provenance header."""
        return f"{self.kind} {self.location}"


def _finish(doc: Mapping[str, Any], *, identity: bool, where: str, module: str | None,
            allow_unknown_types: bool = True) -> Interface:
    try:
        interface = Interface.from_json(dict(doc))
        if identity:
            interface = interface.with_identity()
    except (LidlFormatError, IdentityError) as exc:
        raise ContractRejected(f"{where}: {exc}") from None
    if module is not None and interface.name != module:
        raise ContractRejected(f"{where} declares module '{interface.name}', not '{module}'")
    try:
        interface.check(allow_unknown_types=allow_unknown_types)
    except InterfaceInvalid as exc:
        raise ContractRejected(f"{where}: {exc}") from None
    return interface


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SourceUnavailable(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractRejected(f"{path} is not UTF-8: {exc}") from None


def load_lidl(path: str | os.PathLike[str], *, module: str | None = None, identity: bool = True,
              lidl_cli: str | os.PathLike[str] | LidlCli | None = None,
              allow_unknown_types: bool = True) -> ContractSource:
    """A ``.lidl`` file, parsed by ``lidl json [--identity]``."""
    source = Path(path)
    cli = lidl_cli if isinstance(lidl_cli, LidlCli) else LidlCli(lidl_cli)
    text = _read_text(source)
    doc = cli.json(source, identity=identity)
    interface = _finish(doc, identity=False, where=str(source), module=module,
                        allow_unknown_types=allow_unknown_types)
    notes: tuple[str, ...] = ()
    if cli.fmt(source) != text:
        notes = (f"{source} is not in canonical form (lidl fmt differs), so its contract_sha256 "
                 "will not match a module's lidl()",)
    return ContractSource(interface, "lidl", str(path), text=text, reader=cli.version(), notes=notes)


def load_ast(path: str | os.PathLike[str], *, module: str | None = None, identity: bool = True,
             allow_unknown_types: bool = True) -> ContractSource:
    """A JSON AST file (``lidl json`` output)."""
    source = Path(path)
    text = _read_text(source)
    try:
        doc = loads(text)
    except ValueError as exc:
        raise ContractRejected(f"{source} is not JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise ContractRejected(f"{source} holds a {type(doc).__name__}, not an AST object")
    interface = _finish(doc, identity=identity, where=str(source), module=module,
                        allow_unknown_types=allow_unknown_types)
    return ContractSource(interface, "ast", str(path))


def lgx_contract_names(assets: Path) -> list[str]:
    folder = assets / "lidl"
    return sorted(p.stem for p in folder.glob("*.lidl")) if folder.is_dir() else []


def load_lgx(path: str | os.PathLike[str], module: str, *, identity: bool = True,
             lgx_cli: str | os.PathLike[str] | LgxCli | None = None,
             lidl_cli: str | os.PathLike[str] | LidlCli | None = None,
             allow_unknown_types: bool = True) -> ContractSource:
    """``assets/lidl/<module>.lidl`` of an LGX package, read through the ``lgx`` CLI."""
    package = Path(path)
    if not package.is_file():
        raise SourceUnavailable(f"no such package: {package}")
    lgx = lgx_cli if isinstance(lgx_cli, LgxCli) else LgxCli(lgx_cli)
    lidl = lidl_cli if isinstance(lidl_cli, LidlCli) else LidlCli(lidl_cli)
    lgx.verify(package)
    manifest = lgx.manifest(package)
    with tempfile.TemporaryDirectory(prefix="logos-bridge-lgx-") as tmp:
        assets = lgx.extract_assets(package, Path(tmp) / "out", manifest)
        contract = assets / "lidl" / f"{module}.lidl"
        if not contract.is_file():
            carried = lgx_contract_names(assets)
            listing = ", ".join(carried) if carried else "none"
            raise SourceUnavailable(
                f"{package} has no contract for '{module}' (assets/lidl/{module}.lidl); "
                f"it carries: {listing}"
            )
        source = load_lidl(contract, module=module, identity=identity, lidl_cli=lidl,
                           allow_unknown_types=allow_unknown_types)
    location = f"{path} (module {module}, package {manifest.get('name', '?')} {manifest.get('version', '?')})"
    return ContractSource(source.interface, "lgx", location, text=source.text, reader=source.reader,
                          notes=source.notes)


def interface_from_module_info(info: ModuleInfo) -> Interface:
    """The served contract of a typed module, digest-checked (a mismatch only warns)."""
    if info.interface_status != "ok" or info.interface is None:
        raise SourceUnavailable(
            f"module {info.module} does not expose LIDL (status: {info.interface_status or 'no lidl discovery'})"
            + (f": {info.interface_error}" if info.interface_error else "")
        )
    computed = interface_sha256(info.interface)
    if info.interface_sha256 is not None and computed != info.interface_sha256:
        warnings.warn(
            f"{info.module}: the served interface hashes to {computed}, but the bridge reports "
            f"interface_sha256 {info.interface_sha256}",
            BridgeWarning,
            stacklevel=2,
        )
    return Interface.from_json(dict(info.interface))


async def load_from_bridge(url: str, module: str, *, identity: bool = True,
                           lidl_cli: str | os.PathLike[str] | LidlCli | None = None,
                           discovery_wait: float = 10.0, allow_unknown_types: bool = True,
                           **client_options: Any) -> ContractSource:
    """The contract a running bridge serves for ``module``, via ``<module>.lidl()``.

    Requires ``interface_status: ok``; the text must hash to ``contract_sha256``.
    Without a ``lidl`` CLI the digest-checked served ``interface`` is used instead.
    """
    from .client import AsyncBridgeClient

    async with AsyncBridgeClient(url, **client_options) as bridge:
        info = await bridge.wait_for_module(module, discovery_wait)
        if info.interface_status != "ok":
            detail = f": {info.interface_error}" if info.interface_error else ""
            raise SourceUnavailable(
                f"module {module} does not expose LIDL (status: {info.interface_status or 'no lidl discovery'})"
                + detail
            )
        text = await bridge.call(module, "lidl", decode_bytes=False)
    if not isinstance(text, str):
        raise ContractRejected(f"{module}.lidl() answered {type(text).__name__}, not a string")
    if info.contract_sha256 is not None and contract_sha256(text) != info.contract_sha256:
        raise ContractRejected(
            f"{module}.lidl() hashes to {contract_sha256(text)}, but the bridge reports "
            f"contract_sha256 {info.contract_sha256}"
        )
    location = f"{url} (module {module})"
    try:
        cli = lidl_cli if isinstance(lidl_cli, LidlCli) else LidlCli(lidl_cli)
    except CliNotFound:
        if lidl_cli is not None:
            raise
        interface = _finish(interface_from_module_info(info).to_json(), identity=identity,
                            where=location, module=module, allow_unknown_types=allow_unknown_types)
        return ContractSource(interface, "bridge", location, text=text, reader="bridge", info=info,
                              notes=("no lidl CLI: used the served interface",))
    with tempfile.TemporaryDirectory(prefix="logos-bridge-contract-") as tmp:
        path = Path(tmp) / f"{module}.lidl"
        path.write_text(text, encoding="utf-8", newline="")
        doc = cli.json(path, identity=identity)
    interface = _finish(doc, identity=False, where=location, module=module,
                        allow_unknown_types=allow_unknown_types)
    notes: list[str] = []
    if identity and info.interface_sha256 is not None and interface.interface_sha256() != info.interface_sha256:
        notes.append(f"{cli.version()} reads this contract differently from the bridge's reader "
                     "(interface_sha256 differs)")
    return ContractSource(interface, "bridge", location, text=text, reader=cli.version(), info=info,
                          notes=tuple(notes))


def load_from_bridge_sync(url: str, module: str, **options: Any) -> ContractSource:
    """:func:`load_from_bridge` for code without an event loop."""
    return asyncio.run(load_from_bridge(url, module, **options))
