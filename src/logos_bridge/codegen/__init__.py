"""Code generation from LIDL contracts: typed Python clients and Markdown references.

The ``logos-bridge-codegen`` command (:func:`~logos_bridge.codegen.cli.main`) is the
usual entry point; :func:`generate_python` and :func:`generate_markdown` are the
library form.
"""

from __future__ import annotations

from .cli import build_parser, main, run
from .markdown import generate_markdown
from .naming import CodegenError, Names, Rename, pascal, plan_names, snake
from .python import Contract, generate_python, strip_provenance

__all__ = [
    "CodegenError",
    "Contract",
    "Names",
    "Rename",
    "build_parser",
    "generate_markdown",
    "generate_python",
    "main",
    "pascal",
    "plan_names",
    "run",
    "snake",
    "strip_provenance",
]
