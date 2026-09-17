"""Test helpers that need no pytest plugin."""

from __future__ import annotations

import asyncio
import functools
import gc
import inspect
import socket
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, overload

P = ParamSpec("P")
R = TypeVar("R")


def free_port(host: str = "127.0.0.1") -> int:
    """A TCP port that was free a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port: int = sock.getsockname()[1]
        return port


async def _run_test(
    fn: Callable[..., Awaitable[Any]], args: tuple[Any, ...], kwargs: dict[str, Any], timeout: float
) -> None:
    loop = asyncio.get_running_loop()
    problems: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: problems.append(context))

    async def body() -> Any:
        return await fn(*args, **kwargs)

    test: asyncio.Task[Any] = loop.create_task(body(), name=f"test {getattr(fn, '__qualname__', fn)}")
    done, _ = await asyncio.wait({test}, timeout=timeout)
    if not done:
        test.cancel()
        done, _ = await asyncio.wait({test}, timeout=5.0)
        tail = "" if done else ", and it ignored cancellation"
        raise AssertionError(f"test did not finish within {timeout:g}s{tail}")
    test.result()  # re-raises the test's own failure

    current = asyncio.current_task()
    for _ in range(20):
        await asyncio.sleep(0)
    leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if leftovers:
        await asyncio.wait(leftovers, timeout=1.0)
        leftovers = [t for t in leftovers if not t.done()]
    if leftovers:
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)
        raise AssertionError("test left tasks running: " + ", ".join(repr(t) for t in leftovers))
    gc.collect()
    await asyncio.sleep(0)
    if problems:
        described = "; ".join(str(p.get("message")) + (f" ({p['exception']!r})" if "exception" in p else "")
                              for p in problems)
        raise AssertionError(f"the event loop reported unhandled errors: {described}")


def _decorate(fn: Callable[P, Awaitable[Any]], timeout: float) -> Callable[P, None]:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError("async_test decorates 'async def' functions")

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(_run_test(fn, args, kwargs, timeout))

    return wrapper


@overload
def async_test(timeout: Callable[P, Awaitable[Any]]) -> Callable[P, None]: ...


@overload
def async_test(timeout: float = 30.0) -> Callable[[Callable[P, Awaitable[Any]]], Callable[P, None]]: ...


def async_test(timeout: Any = 30.0) -> Any:
    """Run an ``async def`` test in a fresh loop via ``asyncio.run``, with a hard timeout.

    ``@async_test`` or ``@async_test(timeout=5)``. The test fails if it overruns, if it
    leaves tasks running, or if the loop reported unhandled errors (for example
    "Task exception was never retrieved"). No pytest plugin is involved.
    """
    if callable(timeout):
        return _decorate(timeout, 30.0)
    limit = float(timeout)

    def decorator(fn: Callable[P, Awaitable[Any]]) -> Callable[P, None]:
        return _decorate(fn, limit)

    return decorator
