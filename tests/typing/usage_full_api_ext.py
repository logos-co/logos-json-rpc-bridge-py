"""Static checks of a generated client: mypy --strict reads this file, pytest never collects it.

The ``type: ignore`` lines are negative assertions: under --strict, an ignore that
suppresses nothing is itself an error (warn_unused_ignores).
"""

from __future__ import annotations

from test_fullapi_ext_cpp_client import (
    EVENT_NAMES,
    AsyncTestFullapiExtCppClient,
    Blob,
    BlobEventEvent,
    Opt,
    TestFullapiExtCppClient,
    TestFullapiExtCppEventName,
    Wrapper,
)
from typing_extensions import assert_type

from logos_bridge import CompatReport, EventDecodeError
from logos_bridge.models import Event


async def usage(c: AsyncTestFullapiExtCppClient, s: TestFullapiExtCppClient, b: Blob, raw: Event) -> None:
    assert_type(await c.echo_blob(b), Blob)
    assert_type(await c.echo_optional(None), str | None)
    assert_type(await c.echo_optional(), str | None)
    assert_type(await c.echo_blob_map({"k": b}), dict[str, Blob])
    assert_type(await c.echo_blob_list((b, b)), list[Blob])
    assert_type(await c.echo_wrapper(Wrapper(inner=b, tags=["t"], blobs=[b])), Wrapper)
    assert_type(await c.echo_nested_ints([[1], (2, 3)]), list[list[int]])
    assert_type(await c.echo_opt(Opt(required="r")), Opt)
    assert_type(await c.echo_map_of_bytes_lists({"k": [b"x"]}, timeout=1.0), dict[str, list[bytes]])
    assert_type(await c.who_am_i(), str)
    assert_type(await c.name(), str)
    assert_type(await c.lidl(), str)
    assert_type(await c.check_compat(), CompatReport)
    assert_type(c.decode_blob_event(raw), BlobEventEvent)
    assert_type(s.echo_bytes_list([b"a"]), list[bytes])
    assert_type(s.echo_int_map({"a": 1}, timeout=1.0), dict[str, int])
    assert_type(s.check_compat(allow_untyped=True), CompatReport)
    assert_type(s.aio, AsyncTestFullapiExtCppClient)
    assert_type(b.payload, bytes)
    assert_type(Opt(required="r").count, int | None)
    assert_type(EVENT_NAMES, tuple[TestFullapiExtCppEventName, ...])

    async with c.on_blob_event() as sub:
        async for ev in sub:
            assert_type(ev, BlobEventEvent)
            assert_type(ev.v, Blob)
            assert_type(ev.meta, Event)
        async for item in sub.results():
            assert_type(item, BlobEventEvent | EventDecodeError)
    async with c.events("blobEvent") as stream:
        assert_type(await stream.get(timeout=1.0), BlobEventEvent)
    with s.on_blob_event(max_pending=10) as blocking:
        for event in blocking:
            assert_type(event, BlobEventEvent)

    await c.echo_blob("nope")  # type: ignore[arg-type]
    await c.echo_blob(b, 5.0)  # type: ignore[misc]
    await c.echo_bytes_map({"k": "not bytes"})  # type: ignore[dict-item]
    c.events("nope")  # type: ignore[arg-type]
    s.echo_bytes_list(["a"])  # type: ignore[list-item]
    Blob(id="x", n=1)  # type: ignore[call-arg]
    b.id = "y"  # type: ignore[misc]
