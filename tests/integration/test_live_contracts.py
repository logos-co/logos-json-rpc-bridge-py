"""Contract identities on a live bridge: digests, the reader, the build's artifacts, policy."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from harness import DENIED, PROVIDERS, Stack

from logos_bridge import AsyncBridgeClient, IncompatibleModule, MethodNotExposed, MethodNotFound
from logos_bridge.compat import CompatMembers, check_compat
from logos_bridge.digest import contract_sha256, interface_sha256
from logos_bridge.lidl import Interface
from logos_bridge.lidl_sources import LgxCli, LidlCli, load_from_bridge
from logos_bridge.testing import async_test
from logos_bridge.testing.live import LiveBridge

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tools(stack: Stack) -> tuple[LidlCli, LgxCli]:
    if not (stack.lidl_cli and stack.lgx_cli):
        pytest.skip("LOGOS_LIDL_CLI and LOGOS_LGX_CLI are needed (the nix checks set them)")
    return LidlCli(stack.lidl_cli), LgxCli(stack.lgx_cli)


@pytest.mark.parametrize("module", PROVIDERS)
def test_the_served_view(node: LiveBridge, typed_views: object, module: str) -> None:
    view = node.view(module)
    assert (view["interface_status"], view["source"], view["authoritative"], view["stale"]) == (
        "ok", "lidl", False, False)
    assert interface_sha256(view["interface"]) == view["interface_sha256"]
    assert view["cross_check"]["state"] == "consistent", view["cross_check"]
    assert "interface_error" not in view
    # rpc.list_modules carries the same view without the contract.
    listed = node.views()[module]
    assert "interface" not in listed
    assert {k: v for k, v in view.items() if k != "interface"} == listed


@pytest.mark.parametrize("module", PROVIDERS)
@async_test(timeout=60)
async def test_the_build_the_answer_and_the_package_are_one_contract(
        node: LiveBridge, typed_views: object, stack: Stack, module: str, tmp_path: Path) -> None:
    lidl, lgx = tools(stack)
    built = stack.provider_file(module, ".lidl")
    package = stack.provider_file(module, ".lgx")
    view = node.view(module)
    async with AsyncBridgeClient(node.ws_url) as bridge:
        answer = await bridge.call(module, "lidl")
    lgx.verify(package)
    asset = lgx.extract_assets(package, tmp_path / "extracted") / "lidl" / f"{module}.lidl"
    digests = {
        "sha256(lidl())": contract_sha256(answer),
        "contract_sha256": view["contract_sha256"],
        "sha256(#lidl)": sha256(built.read_bytes()),
        "sha256(LGX asset)": sha256(asset.read_bytes()),
    }
    assert len(set(digests.values())) == 1, digests
    # The build is the contract this repository vendors (and generated its clients from).
    assert answer == (FIXTURES / "lidl" / f"{module}.lidl").read_text(encoding="utf-8")
    # And the pinned reader turns the build output into exactly the served interface.
    assert lidl.json(built, identity=True) == view["interface"]


def test_the_bridge_reads_contracts_with_this_lidl(node: LiveBridge, stack: Stack) -> None:
    lidl, _ = tools(stack)
    version = lidl.version()
    assert node.info()["lidl_reader"] == version
    if stack.lidl_rev is not None:
        # A path: input has no revision: both then say "unknown", and the flake asserts at
        # evaluation that logos-lidl is the bridge's own input.
        assert version.endswith(f"({stack.lidl_rev})"), (version, stack.lidl_rev)


@pytest.mark.parametrize("module", PROVIDERS)
@async_test(timeout=60)
async def test_from_bridge_reads_the_served_contract(node: LiveBridge, typed_views: object, stack: Stack,
                                                     module: str) -> None:
    lidl, _ = tools(stack)
    source = await load_from_bridge(node.ws_url, module, lidl_cli=lidl)
    view = node.view(module)
    assert source.contract_sha256 == view["contract_sha256"]
    assert source.interface_sha256 == view["interface_sha256"] and not source.notes
    assert source.reader == lidl.version()


@pytest.mark.parametrize("module", PROVIDERS)
@async_test(timeout=60)
async def test_policy_restricts_calls_not_knowledge(node: LiveBridge, policy_node: LiveBridge,
                                                    typed_views: object, module: str) -> None:
    method, event = DENIED[module]
    open_view, denied_view = node.view(module), policy_node.view(module)
    for key in ("interface", "interface_sha256", "contract_sha256", "interface_status"):
        assert denied_view[key] == open_view[key], key
    exposure, narrowed = open_view["exposure"], denied_view["exposure"]
    assert set(exposure["methods"]) - set(narrowed["methods"]) == {method}
    assert set(exposure["events"]) - set(narrowed["events"]) == {event}
    assert set(narrowed["methods"]) >= {"name", "version", "lidl"}

    expected = Interface.from_json(open_view["interface"])
    async with AsyncBridgeClient(policy_node.ws_url) as bridge:
        report = await check_compat(bridge, module, expected)
        assert report.level == "exact"
        assert report.not_exposed == CompatMembers((method,), (event,))
        with pytest.raises(IncompatibleModule, match="not exposed by this bridge"):
            report.require(methods=[method])
        with pytest.raises(MethodNotFound) as refused:
            await bridge.call(module, method, None)
        assert refused.value.code == -32601
        proxy = await bridge.module(module, require_typed=True)
        with pytest.raises(MethodNotExposed):
            await proxy[method](None)
        # The built-ins stay callable, through the proxy's attributes too.
        text = (FIXTURES / "lidl" / f"{module}.lidl").read_text(encoding="utf-8")
        assert await bridge.call(module, "lidl") == text == await proxy.lidl()
        assert (await proxy.name(), await proxy.version()) == (module, "1.0.0")
