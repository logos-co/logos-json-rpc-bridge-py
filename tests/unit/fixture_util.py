"""Fixture paths, the real CLIs (or a skip), and fake CLIs for error paths."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from logos_bridge.lidl import Interface

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
LIDL_DIR = FIXTURES / "lidl"
AST_DIR = FIXTURES / "ast"
EDGE_DIR = FIXTURES / "ast-edge"
CONTRACTS = ("test_fullapi_cpp", "test_fullapi_ext_cpp", "storage_module", "mini_module")
EDGE_CONTRACTS = tuple(sorted(p.stem for p in EDGE_DIR.glob("*.lidl") if not p.name.endswith(".fmt.lidl")))


def ast_bytes(name: str) -> bytes:
    return (AST_DIR / f"{name}.json").read_bytes()


def ast_doc(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(ast_bytes(name))
    return doc


def interface(name: str) -> Interface:
    return Interface.from_json(ast_doc(name))


def lidl_text(name: str) -> str:
    return (LIDL_DIR / f"{name}.lidl").read_text(encoding="utf-8")


def edge_doc(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((EDGE_DIR / f"{name}.json").read_bytes())
    return doc


def real_cli(env: str) -> str:
    """The CLI named by ``env`` (set by the nix checks), or skip."""
    value = os.environ.get(env)
    if not value:
        pytest.skip(f"{env} is not set (the nix checks provide it)")
    return value


def real_lidl_cli() -> str:
    return real_cli("LOGOS_LIDL_CLI")


def real_lgx_cli() -> str:
    return real_cli("LOGOS_LGX_CLI")


def write_script(directory: Path, name: str, python_body: str) -> Path:
    """An executable ``name`` that runs ``python_body`` with this interpreter.

    The wrapper is ``/bin/sh`` (present in the nix sandbox, unlike ``/usr/bin/env``).
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"{name}.py"
    script.write_text(textwrap.dedent(python_body), encoding="utf-8")
    wrapper = directory / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


FAKE_LIDL = """
import json, pathlib, sys
AST = pathlib.Path({ast!r})
EDGE = pathlib.Path({edge!r})
args = sys.argv[1:]
if args == ["--version"]:
    print("lidl 0.0.0 (fake)")
    sys.exit(0)
command, rest = args[0], [a for a in args[1:] if a != "--"]
identity = "--identity" in rest
path = pathlib.Path([a for a in rest if not a.startswith("--")][-1])
if not path.exists():
    print(f"lidl: cannot read '{{path}}': No such file or directory", file=sys.stderr)
    sys.exit(2)
text = path.read_text()
if "parse error" in text:
    print(f"{{path}}:1:1: Expected 'module'", file=sys.stderr)
    sys.exit(3)
stem = path.stem
if command == "fmt":
    fmt = EDGE / f"{{stem}}.fmt.lidl"
    sys.stdout.write(fmt.read_text() if fmt.exists() else text)
    sys.exit(0)
if identity and (EDGE / f"{{stem}}.identity.err").exists():
    sys.stderr.write((EDGE / f"{{stem}}.identity.err").read_text())
    sys.exit(4)
candidates = [AST / f"{{stem}}.json"] if identity else []
candidates += [EDGE / f"{{stem}}.identity.json"] if identity else [EDGE / f"{{stem}}.json"]
for candidate in candidates:
    if candidate.exists():
        sys.stdout.write(candidate.read_text())
        sys.exit(0)
print(f"fake lidl: no AST for {{stem}}", file=sys.stderr)
sys.exit(70)
"""


def fake_lidl_cli(directory: Path) -> Path:
    """A ``lidl`` that answers from the vendored ASTs (by file stem)."""
    return write_script(directory, "lidl", FAKE_LIDL.format(ast=str(AST_DIR), edge=str(EDGE_DIR)))


FAKE_LGX = """
import json, pathlib, shutil, sys
MODE = {mode!r}
ASSETS = pathlib.Path({assets!r})
args = sys.argv[1:]
log = pathlib.Path(__file__).with_suffix(".log")
with log.open("a") as fh:
    fh.write(" ".join(args) + "\\n")
command = args[0]
if command == "extract" and "--help" in args:
    print("lgx extract <pkg.lgx> [--variant <v>" + (" | --assets-only" if MODE != "no-assets-only" else "") + "]")
    sys.exit(0)
if command == "verify":
    if MODE == "verify-fails":
        print("Error: Package validation failed:\\n  - Failed to decompress: Not valid gzip data", file=sys.stderr)
        sys.exit(1)
    print("Package structure is valid")
    sys.exit(0)
if command == "manifest":
    print(json.dumps({{"name": "fake_pkg", "version": "1.0.0", "main": {{"linux-amd64": "a.so", "darwin-arm64": "a.dylib"}}}}))
    sys.exit(0)
if command == "extract":
    out = pathlib.Path(args[args.index("--output") + 1])
    if "--assets-only" in args:
        target = out / "assets"
    else:
        target = out / args[args.index("--variant") + 1] / "assets"
    target.mkdir(parents=True, exist_ok=True)
    if MODE != "no-contracts":
        shutil.copytree(ASSETS, target, dirs_exist_ok=True)
    sys.exit(0)
sys.exit(1)
"""


def fake_lgx_cli(directory: Path, mode: str, assets: Path) -> Path:
    """An ``lgx`` whose ``extract`` copies ``assets`` (``mode`` scripts failures)."""
    return write_script(directory, "lgx", FAKE_LGX.format(mode=mode, assets=str(assets)))


def contract_assets(directory: Path, *names: str) -> Path:
    """An ``assets/`` tree with ``lidl/<name>.lidl`` for each fixture contract."""
    lidl = directory / "lidl"
    lidl.mkdir(parents=True, exist_ok=True)
    for name in names:
        (lidl / f"{name}.lidl").write_bytes((LIDL_DIR / f"{name}.lidl").read_bytes())
    return directory
