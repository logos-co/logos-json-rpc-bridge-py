"""Module calls through a real bridge: both conformance tables, refusals, untyped modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from harness import NodeFactory
from live_util import (
    GENERIC_REFUSED,
    KNOWN_BROKEN,
    NOT_FOUND_CLASSES,
    REJECTIONS,
    TABLES,
    expected_value,
    method_cases,
    plans,
)

from logos_bridge import (
    AsyncBridgeClient,
    BridgeHttpClient,
    BridgeWarning,
    BytesDecodeError,
    ClientRejection,
    MethodNotFound,
    ProviderRejection,
    UntypedModule,
)
from logos_bridge.codegen import run as codegen
from logos_bridge.compat import check_compat
from logos_bridge.lidl import Interface
from logos_bridge.testing import async_test
from logos_bridge.testing.conformance import ConformanceCase
from logos_bridge.testing.live import LiveBridge, bridge_config, http_request, rpc_http

pytestmark = pytest.mark.integration


def local_refusal(table: str, case: ConformanceCase, module: str) -> str:
    """How this package's codecs word the refusal a provider answers for ``case``."""
    with pytest.raises(ClientRejection) as excinfo:
        plans(table).method(case.method).encode_args(case.call_args(), module=module)
    assert excinfo.value.code == case.expected_error
    return excinfo.value.message


async def replay(node: LiveBridge, table: str, case: ConformanceCase) -> None:
    provider = TABLES[table][0]
    module = case.module or provider
    args = case.call_args()
    async with AsyncBridgeClient(node.ws_url) as bridge:
        refused = GENERIC_REFUSED.get((table, case.id))
        code = case.expected_error
        if refused is not None:
            with pytest.raises(BytesDecodeError, match=refused):
                await bridge.call(module, case.method, *args)
        elif code in NOT_FOUND_CLASSES:
            # Unexposed modules and unknown methods never reach a provider.
            with pytest.raises(MethodNotFound):
                await bridge.call(module, case.method, *args)
        elif code in REJECTIONS:
            with pytest.raises(ProviderRejection) as excinfo:
                await bridge.call(module, case.method, *args)
            rejection = excinfo.value
            assert (rejection.code, rejection.origin) == (code, provider)
            folded = await bridge.call(module, case.method, *args, detect_rejection=False, decode_bytes=False)
            assert folded == {"code": code, "message": rejection.message, "origin": provider}
            assert rejection.message == local_refusal(table, case, module)
        else:
            result = await bridge.call(module, case.method, *args, decode_bytes=not case.raw)
            assert result == expected_value(table, case)


@pytest.mark.parametrize(("table", "case"), list(method_cases(isolated=False)))
@async_test(timeout=60)
async def test_method_case(node: LiveBridge, table: str, case: ConformanceCase) -> None:
    await replay(node, table, case)


@pytest.fixture
def isolated_node(node_factory: NodeFactory, request: pytest.FixtureRequest) -> LiveBridge:
    # The tables' isolate rule: such a case gets a daemon of its own.
    return node_factory(name=f"isolated {request.node.callspec.id}")


@pytest.mark.parametrize(("table", "case"), list(method_cases(isolated=True)))
@async_test(timeout=60)
async def test_isolated_method_case(isolated_node: LiveBridge, table: str, case: ConformanceCase) -> None:
    await replay(isolated_node, table, case)


@pytest.fixture
def short_timeout_node(node_factory: NodeFactory) -> LiveBridge:
    config = bridge_config(["test_fullapi_cpp"], revalidate_ms=None, limits={"call_timeout_ms": 3000})
    return node_factory(name="short call timeout", providers=("test_fullapi_cpp",), config=config)


@async_test(timeout=90)
async def test_the_canonical_pending_call_sentinel_is_still_broken(short_timeout_node: LiveBridge) -> None:
    """known.json M4-residual, through the bridge: the map never comes back, the node survives."""
    table, case = "cases", TABLES["cases"][1].case("adversarial/any/pending-call-canonical")
    assert (table, case.id) in KNOWN_BROKEN
    async with AsyncBridgeClient(short_timeout_node.ws_url) as bridge:
        try:
            outcome: Any = await bridge.call("test_fullapi_cpp", case.method, *case.call_args(), timeout=30)
        except Exception as exc:  # noqa: BLE001 - any failure is the known one
            outcome = exc
        assert outcome != expected_value(table, case), (
            "M4-residual answers correctly through the bridge now: drop it from KNOWN_BROKEN")
        assert await bridge.call("test_fullapi_cpp", "echoInt", 7) == 7


@async_test(timeout=60)
async def test_denied_unexposed_and_unknown_answer_identically(policy_node: LiveBridge) -> None:
    port = policy_node.port
    requests = {
        "denied, by alias": {"method": "test_fullapi_cpp.echoInt", "params": [1]},
        "denied, by rpc.call": {"method": "rpc.call",
                                "params": {"module": "test_fullapi_cpp", "method": "echoInt", "params": [1]}},
        "loaded but not exposed": {"method": "modules_state.name"},
        "unknown module": {"method": "ghost_module.echoInt", "params": [1]},
        "unknown method": {"method": "test_fullapi_cpp.noSuchMethod"},
    }
    bodies = {}
    for label, request in requests.items():
        answer = rpc_http(port, {"jsonrpc": "2.0", "id": 4, **request})
        assert answer.status == 200, label
        bodies[label] = answer.body
    assert len(set(bodies.values())) == 1, bodies
    assert json.loads(bodies["unknown module"])["error"]["code"] == -32601
    # The views of an unexposed module and an unknown one are the same 404.
    views = [http_request(port, "GET", f"/modules/{m}") for m in ("modules_state", "ghost_module")]
    assert [v.status for v in views] == [404, 404] and views[0].body == views[1].body
    async with AsyncBridgeClient(policy_node.ws_url) as bridge:
        for module, method in (("test_fullapi_cpp", "echoInt"), ("modules_state", "name"), ("ghost_module", "x")):
            with pytest.raises(MethodNotFound) as excinfo:
                await bridge.call(module, method, 1)
            assert excinfo.value.logos_error_name == "METHOD_NOT_FOUND"
        with pytest.raises(MethodNotFound):
            await bridge.schema("modules_state")


def test_the_http_client(node: LiveBridge) -> None:
    http = BridgeHttpClient(node.http_url)
    health = http.healthz()
    assert health.ok and health.protocol == "json-rpc-2.0"
    assert http.ping() >= 0
    views = {info.module: info for info in http.list_modules()}
    assert set(views) == set(node.exposed)
    assert http.schema("test_fullapi_cpp").module == "test_fullapi_cpp"
    with pytest.raises(MethodNotFound):
        http.schema("ghost_module")
    assert http.call("test_fullapi_cpp", "echoBytes", b"\x00\xff") == b"\x00\xff"
    assert http.call("test_fullapi_cpp", "makeResult", False) == {
        "success": False, "value": None, "error": "deliberate error for testing"}
    with pytest.raises(ProviderRejection) as excinfo:
        http.call("test_fullapi_cpp", "echoInt", 1, 2)
    assert excinfo.value.code == "invalid_args"
    # The REST projection answers the same call as {"result": ...}.
    rest = http_request(node.port, "POST", "/modules/test_fullapi_cpp/echoInt", b"[5]",
                        headers={"Content-Type": "application/json"})
    assert (rest.status, rest.json()) == (200, {"result": 5})


@async_test(timeout=60)
async def test_an_untyped_module(node: LiveBridge, untyped_module: str) -> None:
    async with AsyncBridgeClient(node.ws_url) as bridge:
        info = await bridge.schema(untyped_module)
        assert (info.interface_status, info.source, info.authoritative) == ("untyped", "getPluginInterface", False)
        assert info.interface is None and info.interface_sha256 is None and info.cross_check is None
        assert info.exposure is not None and set(info.exposure.methods) == set(info.methods)
        # No lidl() in its live report, so there is none to call.
        assert "lidl" not in info.methods
        with pytest.raises(MethodNotFound):
            await bridge.call(untyped_module, "lidl")
        assert await bridge.call(untyped_module, "name") == untyped_module
        with pytest.warns(BridgeWarning, match="untyped"):
            proxy = await bridge.module(untyped_module)
        assert (proxy.is_typed, proxy.interface_status) == (False, "untyped")
        assert await proxy.name() == untyped_module
        with pytest.raises(UntypedModule):
            await bridge.module(untyped_module, require_typed=True)
        expected = Interface.from_shape({"shape": 1, "module": untyped_module, "records": {}, "events": {},
                                         "methods": {"name": {"params": [], "returns": "tstr"}}})
        report = await check_compat(bridge, untyped_module, expected, allow_untyped=True)
        assert (report.level, report.status, report.missing.methods) == ("names", "untyped", ())
        with pytest.raises(UntypedModule, match="status: untyped"):
            await check_compat(bridge, untyped_module, expected)


def test_codegen_refuses_an_untyped_module(node: LiveBridge, untyped_module: str, tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "client.py"
    status = codegen(["python", "--from-bridge", node.ws_url, "--module", untyped_module, "-o", str(out)])
    assert status == 3 and not out.exists()
    assert "does not expose LIDL (status: untyped)" in capsys.readouterr().err
