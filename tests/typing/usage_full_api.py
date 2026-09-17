"""Static checks of the fullapi client (events union, results, no-return); mypy only."""

from __future__ import annotations

from typing import Any

from test_fullapi_cpp_client import (
    AsyncTestFullapiCppClient,
    TestFullapiCppClient,
    TestFullapiCppEvent,
    TripleEventEvent,
)
from typing_extensions import assert_type

from logos_bridge.typed import LogosResult


async def usage(c: AsyncTestFullapiCppClient, s: TestFullapiCppClient) -> None:
    result = await c.make_result(True)
    assert_type(result, LogosResult[Any])
    assert_type(result.success, bool)
    assert_type(await c.echo_any({"k": [1]}), Any)
    assert_type(await c.echo_triple(1, "s", b"b"), str)
    assert_type(await c.echo_uint(2**64 - 1), int)
    assert_type(s.echo_double_list([1.0, 2]), list[float])
    assert_type(s.echo_map({"k": None}), dict[str, Any])
    async with c.events("stringEvent", "tripleEvent") as sub:
        async for ev in sub:
            assert_type(ev, TestFullapiCppEvent)
            if isinstance(ev, TripleEventEvent):
                assert_type(ev.b, bytes)

    _ = await c.do_void()  # type: ignore[func-returns-value]
    await c.do_void(1)  # type: ignore[misc]
    await c.echo_int("1")  # type: ignore[arg-type]
    c.events("stringEvent", "nope")  # type: ignore[arg-type]
