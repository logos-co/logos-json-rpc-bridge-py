from __future__ import annotations

import asyncio
import importlib
import importlib.resources
import pathlib
import re

import pytest

import logos_bridge
import logos_bridge.testing
import logos_bridge.typed
from logos_bridge.testing import async_test, free_port

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_matches_pyproject() -> None:
    if not PYPROJECT.is_file():
        pytest.skip("pyproject.toml is not next to the tests")
    match = re.search(r'^version = "([^"]+)"$', PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    assert logos_bridge.__version__ == match.group(1)


def test_public_constants() -> None:
    assert logos_bridge.DEFAULT_URL == "ws://127.0.0.1:8645/ws"
    assert logos_bridge.DEFAULT_HTTP_URL == "http://127.0.0.1:8645"
    assert logos_bridge.SUBPROTOCOL == "jsonrpc-bridge.v1"


@pytest.mark.parametrize("module", [logos_bridge, logos_bridge.testing, logos_bridge.typed])
def test_every_export_resolves(module: object) -> None:
    names = getattr(module, "__all__")
    assert len(names) == len(set(names))
    for name in names:
        assert getattr(module, name) is not None, name


def test_submodules_import() -> None:
    for name in ("client", "codec", "digest", "errors", "http", "models", "portal", "subscription", "sync",
                 "_protocol", "_transport", "lidl", "lidl_sources", "typed", "testing.conformance", "compat", "dynamic",
                 "codegen", "codegen.cli", "codegen.naming", "codegen.python", "codegen.markdown", "codegen.text",
                 "testing.docs"):
        importlib.import_module(f"logos_bridge.{name}")


def test_the_typing_marker_ships() -> None:
    assert importlib.resources.files("logos_bridge").joinpath("py.typed").is_file()


def test_free_port() -> None:
    port = free_port()
    assert 0 < port < 65536


@async_test
async def test_async_test_passes_fixtures(tmp_path: pathlib.Path) -> None:
    await asyncio.sleep(0)
    assert tmp_path.is_dir()


@async_test(timeout=5)
async def test_async_test_accepts_a_timeout() -> None:
    await asyncio.sleep(0)


def test_async_test_reports_an_overrun() -> None:
    @async_test(timeout=0.05)
    async def slow() -> None:
        await asyncio.sleep(10)

    with pytest.raises(AssertionError, match="did not finish within 0.05s"):
        slow()


def test_async_test_reports_leftover_tasks() -> None:
    @async_test
    async def leaky() -> None:
        asyncio.get_running_loop().create_task(asyncio.sleep(10))

    with pytest.raises(AssertionError, match="left tasks running"):
        leaky()


def test_async_test_reports_unretrieved_task_errors() -> None:
    @async_test
    async def sloppy() -> None:
        async def fail() -> None:
            raise RuntimeError("nobody looked")

        task = asyncio.get_running_loop().create_task(fail())
        await asyncio.sleep(0.01)
        del task

    with pytest.raises(AssertionError, match="unhandled errors.*nobody looked"):
        sloppy()


def test_async_test_keeps_the_test_failure() -> None:
    @async_test
    async def failing() -> None:
        raise KeyError("original")

    with pytest.raises(KeyError, match="original"):
        failing()


def test_async_test_refuses_plain_functions() -> None:
    with pytest.raises(TypeError):
        async_test(lambda: None)  # type: ignore[arg-type]
