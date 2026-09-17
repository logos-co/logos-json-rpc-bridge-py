"""This repository's integration stack: the test providers, the policy node, and the tools
the contract and document tests use. The daemon and bridge plumbing is
:mod:`logos_bridge.testing.live`.

The providers' modules come in through ``LOGOS_LIVE_MODULES_DIRS``, next to the bridge's
``LOGOS_BRIDGE_INSTALL_DIR``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from logos_bridge.testing.live import LiveBridge, LiveBridgeUnavailable, LiveStack

PROVIDERS = ("test_fullapi_cpp", "test_fullapi_ext_cpp")
# Built into logoscore and untyped there: it carries the untyped paths.
UNTYPED_CANDIDATE = "modules_state"
#: The policy node denies one method and one event of each provider.
DENIED = {"test_fullapi_cpp": ("echoInt", "intEvent"), "test_fullapi_ext_cpp": ("echoWrapper", "blobEvent")}
#: Lines for the run's summary (the conftest prints them under "integration stack").
NOTES: list[str] = []

REQUIRED_ENV = "LOGOS_BRIDGE_INTEGRATION"

#: What the conftest node fixtures return: ``factory(name=..., config=..., **LiveBridge options)``.
NodeFactory = Callable[..., LiveBridge]


def required_mode() -> bool:
    """The nix checks set LOGOS_BRIDGE_INTEGRATION=required: a missing piece fails, never skips."""
    return os.environ.get(REQUIRED_ENV) == "required"


def _path_or_none(value: str | None) -> Path | None:
    return Path(value) if value else None


@dataclass(frozen=True)
class Stack:
    """The live stack, plus what this suite checks the bridge against.

    ``LOGOS_BRIDGE_PROVIDERS_DIR`` holds each provider's ``<m>.lidl`` (its ``#lidl`` output)
    and ``<m>.lgx``; ``LOGOS_BRIDGE_DOCS_CLI``, ``LOGOS_BRIDGE_METASCHEMAS``,
    ``LOGOS_LIDL_CLI``, ``LOGOS_LGX_CLI`` and ``LOGOS_LIDL_EXPECTED_REV`` are optional.
    ``LOGOS_BRIDGE_FIXES=1`` says the bridge has the keep-alive fix.
    """

    live: LiveStack
    providers_dir: Path | None = None
    docs_cli: str | None = None
    metaschemas: Path | None = None
    lidl_cli: str | None = None
    lgx_cli: str | None = None
    lidl_rev: str | None = None
    fixes: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Stack:
        return cls(
            live=LiveStack.from_env(env),
            providers_dir=_path_or_none(env.get("LOGOS_BRIDGE_PROVIDERS_DIR")),
            docs_cli=env.get("LOGOS_BRIDGE_DOCS_CLI") or None,
            metaschemas=_path_or_none(env.get("LOGOS_BRIDGE_METASCHEMAS")),
            lidl_cli=env.get("LOGOS_LIDL_CLI") or None,
            lgx_cli=env.get("LOGOS_LGX_CLI") or None,
            lidl_rev=env.get("LOGOS_LIDL_EXPECTED_REV") or None,
            fixes=env.get("LOGOS_BRIDGE_FIXES") == "1",
        )

    def provider_file(self, module: str, suffix: str) -> Path:
        """``<providers_dir>/<module><suffix>``; raises LiveBridgeUnavailable when absent."""
        if self.providers_dir is None:
            raise LiveBridgeUnavailable("LOGOS_BRIDGE_PROVIDERS_DIR is not set")
        path = self.providers_dir / f"{module}{suffix}"
        if not path.exists():
            raise LiveBridgeUnavailable(f"{path} does not exist")
        return path
