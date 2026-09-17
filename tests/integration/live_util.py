"""The conformance tables as the live tests replay them, and small async helpers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from logos_bridge import SUBPROTOCOL
from logos_bridge.lidl import Interface
from logos_bridge.testing.conformance import ConformanceCase, ConformanceTable, materialize
from logos_bridge.typed import InterfacePlans

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: table -> (the provider it runs against, the table)
TABLES: dict[str, tuple[str, ConformanceTable]] = {
    "cases": ("test_fullapi_cpp", ConformanceTable.load(FIXTURES / "conformance" / "cases.json")),
    "ext-cases": ("test_fullapi_ext_cpp", ConformanceTable.load(FIXTURES / "conformance" / "ext-cases.json")),
}

#: Registered in the table's own known.json for this provider and the py consumer.
KNOWN_BROKEN: dict[tuple[str, str], str] = {
    ("cases", "adversarial/any/pending-call-canonical"):
        "known.json M4-residual: a canonical-shape sentinel still hijacks the call, which then hangs",
}

#: The generic client refuses these before sending (a padded pre-encoded tag).
GENERIC_REFUSED: dict[tuple[str, str], str] = {
    ("ext-cases", "bstr/padded-base64"): "characters outside the base64url alphabet",
}

#: Failure classes that never reach a provider through the bridge: both are -32601 there.
NOT_FOUND_CLASSES = ("MODULE_NOT_LOADED", "METHOD_NOT_FOUND")
REJECTIONS = ("dispatch_failed", "invalid_args")


def plans(table: str) -> InterfacePlans:
    """The typed plans of a table's contract, from the vendored AST."""
    provider = TABLES[table][0]
    return InterfacePlans(Interface.from_json(json.loads((FIXTURES / "ast" / f"{provider}.json").read_bytes())))


def expectation(table: str, case: ConformanceCase) -> Any:
    """The case's expectation for this table's provider (``pytest.skip`` if it has none)."""
    provider = TABLES[table][0]
    expected = case.expectations()
    if provider in expected:
        return expected[provider]
    if None in expected:
        return expected[None]
    pytest.skip(f"case {case.id} has no expectation for {provider}")


def expected_value(table: str, case: ConformanceCase) -> Any:
    """The expectation as the generic client returns it (tags decoded unless ``raw``)."""
    return materialize(expectation(table, case), raw=case.raw)


def method_cases(*, isolated: bool) -> Iterator[Any]:
    """Every case of both tables but the known-broken ones (they have tests of their own)."""
    for table, (_, conformance) in TABLES.items():
        for case in conformance.cases:
            if case.isolate == isolated and (table, case.id) not in KNOWN_BROKEN:
                yield pytest.param(table, case, id=f"{table}:{case.id}")


def event_tables() -> Iterator[Any]:
    for table, (provider, conformance) in TABLES.items():
        yield pytest.param(table, provider, conformance, id=table)


async def eventually(predicate: Callable[[], Any], timeout: float, what: str, interval: float = 0.1) -> Any:
    """Poll ``predicate`` (sync or async) until it returns something truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Any = None
    while True:
        try:
            last = predicate()
            if asyncio.iscoroutine(last):
                last = await last
        except Exception as exc:  # noqa: BLE001 - reported on timeout
            last = exc
        else:
            if last:
                return last
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what} (last: {last!r:.300})")
        await asyncio.sleep(interval)


async def blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking harness call (logoscore CLI, raw HTTP) off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def raw_ws(node: Any, **options: Any) -> Any:
    """A plain websockets client for ``node`` (``async with``), no logos_bridge in between."""
    options.setdefault("subprotocols", [SUBPROTOCOL])
    options.setdefault("compression", None)
    options.setdefault("proxy", None)
    options.setdefault("max_size", None)
    return connect(node.ws_url, **options)


async def frames_until(ws: Any, done: Callable[[list[Any]], bool], timeout: float) -> list[Any]:
    """Decoded frames from ``ws`` until ``done(frames)``; TimeoutError after ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    frames: list[Any] = []
    while not done(frames):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"no matching frame within {timeout:g}s: {frames!r:.300}")
        frames.append(json.loads(await asyncio.wait_for(ws.recv(), remaining)))
    return frames


async def frames_within(ws: Any, seconds: float) -> list[Any]:
    """Every decoded frame ``ws`` receives in the next ``seconds``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    frames: list[Any] = []
    while (remaining := deadline - loop.time()) > 0:
        try:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), remaining)))
        except asyncio.TimeoutError:
            break
    return frames
