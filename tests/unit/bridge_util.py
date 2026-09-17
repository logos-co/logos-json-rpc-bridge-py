"""Helpers shared by the unit tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from logos_bridge import SUBPROTOCOL
from logos_bridge.testing import FakeBridge


async def eventually(predicate: Callable[[], bool], timeout: float = 5.0, what: str = "condition") -> None:
    """Poll ``predicate`` on the loop until it holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def raw_ws(fake: FakeBridge, **options: Any) -> ClientConnection:
    """A plain websockets client, for checking the fake without logos_bridge in the way."""
    options.setdefault("subprotocols", [SUBPROTOCOL])
    options.setdefault("compression", None)
    options.setdefault("proxy", None)
    return await connect(fake.url, **options)


async def exchange(ws: ClientConnection, request: Any) -> Any:
    """Send one frame (a JSON value or raw text) and decode the next answer."""
    await ws.send(request if isinstance(request, str) else json.dumps(request))
    return json.loads(await ws.recv())


def ping_request(req_id: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "rpc.ping"}


def standard_module(fake: FakeBridge) -> None:
    """A module ``m`` with a few scripted methods and two events."""
    fake.module("m", [("greet", ["who"]), ("echo", []), ("fire", ["value"])], ["tick", "tock"])
    fake.on_call("m", "greet", lambda ctx: f"hello, {ctx.params[0]}")
    fake.on_call("m", "echo", lambda ctx: ctx.params)

    def fire(ctx: Any) -> Any:
        ctx.emit("tick", ctx.params[0])
        return ctx.params[0]

    fake.on_call("m", "fire", fire)
