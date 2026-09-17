"""Generate a client module into a directory and import it."""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from fixture_util import interface

from logos_bridge.codegen import Contract, generate_python, plan_names
from logos_bridge.lidl import Interface

_counter = itertools.count()


def load_module(path: Path, name: str | None = None) -> ModuleType:
    """Import ``path`` under a fresh name (registered, as a normal import is)."""
    module_name = name or f"generated_{path.stem}_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


def generate(directory: Path, iface: Interface, *, contract_sha256: str | None = None, **options: Any) -> ModuleType:
    names = plan_names(iface, **{k: options.pop(k) for k in ("class_name", "module_name", "renames") if k in options})
    text = generate_python(Contract(iface, contract_sha256, f"test {iface.name}"), names, **options)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{iface.name}_client.py"
    path.write_text(text, encoding="utf-8")
    return load_module(path)


def fixture_client(directory: Path, name: str, **options: Any) -> ModuleType:
    from fixture_util import lidl_text

    from logos_bridge.digest import contract_sha256

    return generate(directory, interface(name), contract_sha256=contract_sha256(lidl_text(name)), **options)
