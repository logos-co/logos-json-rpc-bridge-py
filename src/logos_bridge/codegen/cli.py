"""``logos-bridge-codegen``: typed Python clients and reference pages from LIDL contracts.

Exit status: 0 ok, 1 drift (``--check``), 2 usage, 3 source unavailable (including
an untyped module), 4 contract rejected, 5 write failure, 70 internal error.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn, TextIO

from .._version import __version__
from ..errors import BridgeError, ClientTimeout, ConnectError, ConnectionClosed, RpcError
from ..lidl import InterfaceInvalid
from ..lidl_sources import (
    ContractRejected,
    ContractSource,
    ContractSourceError,
    load_ast,
    load_from_bridge_sync,
    load_lgx,
    load_lidl,
)
from ..typed import rejection_ambiguous
from .markdown import MARKDOWN_PROVENANCE, generate_markdown
from .naming import CodegenError, Rename, plan_names
from .python import PYTHON_PROVENANCE, Contract, generate_python, strip_provenance

EXIT_OK: Final = 0
EXIT_DRIFT: Final = 1
EXIT_USAGE: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_REJECTED: Final = 4
EXIT_WRITE: Final = 5
EXIT_INTERNAL: Final = 70

EPILOG: Final = """\
sources (exactly one):
  --lidl FILE                 a .lidl contract, parsed by the lidl CLI
  --ast FILE                  a JSON AST (lidl json output)
  --lgx FILE --module M       assets/lidl/M.lidl of an LGX package (via the lgx CLI)
  --from-bridge URL --module M
                              the contract a running bridge serves for M (via M.lidl())

The lidl and lgx CLIs are found through --lidl-cli/--lgx-cli, then
LOGOS_LIDL_CLI/LOGOS_LGX_CLI, then PATH.

exit status: 0 ok, 1 drift (--check), 2 usage, 3 source unavailable (including an
untyped module), 4 contract rejected, 5 write failure, 70 internal error
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _add_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("source")
    exclusive = group.add_mutually_exclusive_group(required=True)
    exclusive.add_argument("--lidl", metavar="FILE", help="a .lidl contract")
    exclusive.add_argument("--ast", metavar="FILE", help="a JSON AST file")
    exclusive.add_argument("--lgx", metavar="FILE", help="an LGX package (needs --module)")
    exclusive.add_argument("--from-bridge", metavar="URL", help="a running bridge, ws:// (needs --module)")
    group.add_argument("--module", metavar="NAME", help="the module to read (checked for --lidl/--ast)")
    group.add_argument("--lidl-cli", metavar="PATH", help="the lidl executable")
    group.add_argument("--lgx-cli", metavar="PATH", help="the lgx executable")
    group.add_argument("--no-identity", action="store_true", help="do not add name(), version() and lidl()")
    group.add_argument("--discovery-wait", type=float, default=10.0, metavar="SECONDS",
                       help="how long to wait out a pending module (--from-bridge)")
    group.add_argument("--host-header", metavar="HOST", help="Host header for --from-bridge through a tunnel")
    group.add_argument("--allow-unknown-types", action="store_true",
                       help="generate types this version does not know as Any instead of failing")


def _add_naming(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("names")
    group.add_argument("--class-name", metavar="NAME", help="base of the client names (default: the module's, "
                                                             "PascalCase)")
    group.add_argument("--module-name", metavar="NAME", help="the bridge module the clients call by default")
    group.add_argument("--rename", action="append", default=[], metavar="KIND:NAME=PYTHON_NAME",
                       help="rename one member; KIND is method, event, type, field (Type.field), param "
                            "(method.param) or eparam (event.param); repeatable")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="logos-bridge-codegen", description=__doc__.split("\n\n")[0],
                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    parser.add_argument("--version", action="version", version=f"logos-bridge-codegen {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    python = commands.add_parser("python", help="write a typed client module", epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_source(python)
    _add_naming(python)
    python.add_argument("-o", "--output", required=True, metavar="OUT", help="the file to write, or - for stdout")
    python.add_argument("--check", action="store_true", help="compare with OUT instead of writing (exit 1 on drift)")
    python.add_argument("--strict-provenance", action="store_true",
                        help="with --check, also compare the Source/Generator lines")
    python.add_argument("--regen-hint", metavar="TEXT", help="the header's 'Regenerate:' line")

    markdown = commands.add_parser("markdown", help="write a reference page", epilog=EPILOG,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_source(markdown)
    _add_naming(markdown)
    markdown.add_argument("-o", "--output", required=True, metavar="OUT", help="the file to write, or -")
    markdown.add_argument("--check", action="store_true", help="compare with OUT instead of writing")
    markdown.add_argument("--strict-provenance", action="store_true",
                          help="with --check, also compare the Source line")
    markdown.add_argument("--py-accessor", default="client", metavar="EXPR",
                          help="how examples reach the generated client (default: client)")

    digest = commands.add_parser("digest", help="print a contract digest", epilog=EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_source(digest)
    kinds = digest.add_mutually_exclusive_group()
    kinds.add_argument("--shape", action="store_true", help="shape_sha256 instead of interface_sha256")
    kinds.add_argument("--contract", action="store_true", help="contract_sha256 (needs a source with text)")
    return parser


def _load(args: argparse.Namespace) -> ContractSource:
    identity = not args.no_identity
    common = {"identity": identity, "allow_unknown_types": True}
    if args.lgx is not None or args.from_bridge is not None:
        if not args.module:
            raise _Usage("--lgx and --from-bridge need --module")
    if args.lidl is not None:
        return load_lidl(args.lidl, module=args.module, lidl_cli=args.lidl_cli, **common)
    if args.ast is not None:
        return load_ast(args.ast, module=args.module, **common)
    if args.lgx is not None:
        return load_lgx(args.lgx, args.module, lgx_cli=args.lgx_cli, lidl_cli=args.lidl_cli, **common)
    if not args.from_bridge.startswith(("ws://", "wss://")):
        raise _Usage(f"--from-bridge needs a ws:// or wss:// URL, got {args.from_bridge!r}")
    options = {"host_header": args.host_header} if args.host_header else {}
    return load_from_bridge_sync(args.from_bridge, args.module, lidl_cli=args.lidl_cli,
                                 discovery_wait=args.discovery_wait, **common, **options)


class _Usage(Exception):
    pass


def _note(stream: TextIO, message: str) -> None:
    stream.write(f"logos-bridge-codegen: note: {message}\n")


def _write(path: str, text: str, stdout: TextIO) -> None:
    if path == "-":
        stdout.write(text)
        return
    target = Path(path)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise _WriteFailed(f"cannot write {path}: {exc.strerror or exc}") from None


class _WriteFailed(Exception):
    pass


def _check(path: str, text: str, prefixes: Sequence[str], strict: bool, stderr: TextIO) -> int:
    if path == "-":
        raise _Usage("--check needs -o FILE")
    try:
        current = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        stderr.write(f"logos-bridge-codegen: {path} does not exist\n")
        return EXIT_DRIFT
    except OSError as exc:
        stderr.write(f"logos-bridge-codegen: cannot read {path}: {exc.strerror or exc}\n")
        return EXIT_DRIFT
    want, have = (text, current) if strict else (strip_provenance(text, prefixes), strip_provenance(current, prefixes))
    if want == have:
        return EXIT_OK
    diff = difflib.unified_diff(have.splitlines(keepends=True), want.splitlines(keepends=True),
                                fromfile=f"{path} (current)", tofile=f"{path} (generated)")
    stderr.write(f"logos-bridge-codegen: {path} is out of date:\n")
    stderr.writelines(diff)
    return EXIT_DRIFT


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """The CLI; returns the exit status."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        renames = tuple(Rename.parse(text) for text in getattr(args, "rename", []))
        source = _load(args)
        for note in source.notes:
            _note(err, note)
        interface = source.interface
        interface.check(allow_unknown_types=args.allow_unknown_types)
        contract = Contract(interface, source.contract_sha256, source.describe(), source.reader, source.notes)
        if args.command == "digest":
            if args.contract:
                if source.contract_sha256 is None:
                    err.write("logos-bridge-codegen: this source has no contract text (use --lidl, --lgx or "
                              "--from-bridge)\n")
                    return EXIT_UNAVAILABLE
                out.write(source.contract_sha256 + "\n")
            else:
                out.write((interface.shape_sha256() if args.shape else interface.interface_sha256()) + "\n")
            return EXIT_OK
        names = plan_names(interface, class_name=args.class_name, module_name=args.module_name, renames=renames)
        if args.command == "python":
            text = generate_python(contract, names, regen_hint=args.regen_hint,
                                   allow_unknown_types=args.allow_unknown_types)
            prefixes: Sequence[str] = PYTHON_PROVENANCE
            for method in interface.methods:
                if method.returns is not None and rejection_ambiguous(method.returns.type, interface):
                    _note(err, f"{method.name} returns {method.returns.type.spell()}: a legitimate value shaped "
                               "like a provider refusal is reported as ProviderRejection")
        else:
            text = generate_markdown(contract, names, py_accessor=args.py_accessor)
            prefixes = MARKDOWN_PROVENANCE
        if args.check:
            return _check(args.output, text, prefixes, args.strict_provenance, err)
        _write(args.output, text, out)
        return EXIT_OK
    except _Usage as exc:
        err.write(f"logos-bridge-codegen: error: {exc}\n")
        return EXIT_USAGE
    except CodegenError as exc:
        err.write(f"logos-bridge-codegen: {exc}\n")
        return exc.exit_code
    except (ContractRejected, InterfaceInvalid) as exc:
        err.write(f"logos-bridge-codegen: contract rejected: {exc}\n")
        return EXIT_REJECTED
    except ContractSourceError as exc:
        err.write(f"logos-bridge-codegen: source unavailable: {exc}\n")
        return exc.exit_code
    except (ConnectError, ConnectionClosed, ClientTimeout, RpcError) as exc:
        err.write(f"logos-bridge-codegen: source unavailable: {exc}\n")
        return EXIT_UNAVAILABLE
    except _WriteFailed as exc:
        err.write(f"logos-bridge-codegen: {exc}\n")
        return EXIT_WRITE
    except BridgeError as exc:
        err.write(f"logos-bridge-codegen: source unavailable: {exc}\n")
        return EXIT_UNAVAILABLE
    except Exception as exc:  # a bug: say so, with the type
        err.write(f"logos-bridge-codegen: internal error: {type(exc).__name__}: {exc}\n")
        return EXIT_INTERNAL


def main() -> None:
    """Console-script entry point."""
    sys.exit(run())
