"""Compatibility levels between an expected contract and a bridge's view."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from fixture_util import ast_doc, interface

from logos_bridge import AsyncBridgeClient, DiscoveryPending, ModuleInfo
from logos_bridge.compat import LEVELS, CompatMembers, CompatReport, check_compat, compare
from logos_bridge.errors import IncompatibleModule, UntypedModule
from logos_bridge.lidl import Interface
from logos_bridge.testing import FakeBridge, async_test

EXT = interface("test_fullapi_ext_cpp")


def served(doc: dict[str, Any], *, digest: str | None = None, **view: Any) -> ModuleInfo:
    iface = Interface.from_json(doc)
    return ModuleInfo.from_json({
        "module": doc["name"], "resolved": True, "events_declared": True,
        "methods": list(iface.method_names), "events": list(iface.event_names),
        "source": "lidl", "authoritative": False, "interface_status": "ok", "stale": False,
        "interface": doc, "interface_sha256": digest or iface.interface_sha256(),
        "contract_sha256": "c" * 64, "cross_check": {"state": "consistent", "findings": []},
        "exposure": {"methods": list(iface.method_names), "events": list(iface.event_names)},
        **view,
    })


def untyped(status: str | None, methods: list[str], events: list[str], **view: Any) -> ModuleInfo:
    body: dict[str, Any] = {"module": "test_fullapi_ext_cpp", "resolved": True, "events_declared": True,
                            "methods": methods, "events": events, "source": "getPluginInterface",
                            "authoritative": False, **view}
    if status is not None:
        body.update(interface_status=status, stale=False, interface_sha256=None, contract_sha256=None,
                    cross_check=None, exposure={"methods": methods, "events": events})
    return ModuleInfo.from_json(body)


def test_exact() -> None:
    report = compare(EXT, served(ast_doc("test_fullapi_ext_cpp")))
    assert report.level == "exact" and report.compatible and report.typed
    assert report.expected_interface_sha256 == report.served_interface_sha256 == EXT.interface_sha256()
    assert report.served_shape_sha256 == report.expected_shape_sha256 == EXT.shape_sha256()
    assert report.served_contract_sha256 == "c" * 64 and report.status == "ok"
    assert not (report.missing or report.mismatched or report.extra or report.not_exposed)
    assert report.served == EXT and report.info is not None and report.notes == ()
    assert report.raise_if_incompatible() is report
    assert report.describe() == "test_fullapi_ext_cpp: exact (status ok)"
    assert LEVELS == ("exact", "shape", "structural", "names")


def test_shape_tolerates_spelling_description_metadata_and_new_members() -> None:
    doc = ast_doc("test_fullapi_ext_cpp")
    doc["version"] = "1.1.0"
    doc["description"] = "reworded"
    doc["methods"][0]["description"] = "reworded"
    field = doc["types"][2]["fields"][1]  # `maybe: ? tstr` respelled `? maybe: tstr`
    field["optional"] = True
    field["type"] = field["valueType"]
    added = copy.deepcopy(doc["methods"][0])
    added["name"] = "brandNew"
    doc["methods"].insert(0, added)
    doc["events"].append({"description": "", "name": "newEvent", "params": []})
    report = compare(EXT, served(doc))
    assert report.level == "shape"
    assert report.extra == CompatMembers(("brandNew",), ("newEvent",))
    assert not report.missing and not report.mismatched


def test_structural_ignores_module_parameter_and_record_names() -> None:
    text = json.dumps(ast_doc("test_fullapi_ext_cpp"))
    text = text.replace('"Blob"', '"Chunk"').replace('"test_fullapi_ext_cpp"', '"test_fullapi_ext_rust"')
    doc = json.loads(text)
    doc["methods"][1]["params"][0]["name"] = "value"
    report = compare(EXT, served(doc))
    assert report.level == "structural" and report.module == "test_fullapi_ext_rust"
    renamed_param = ast_doc("test_fullapi_ext_cpp")
    renamed_param["methods"][1]["params"][0]["name"] = "value"
    assert compare(EXT, served(renamed_param)).level == "structural"


def test_incompatible_contracts() -> None:
    doc = ast_doc("test_fullapi_ext_cpp")
    doc["methods"][2]["params"][0]["type"] = {"elements": [], "kind": "primitive", "name": "tstr"}
    doc["methods"][2]["params"][0]["valueType"] = doc["methods"][2]["params"][0]["type"]
    doc["methods"] = [m for m in doc["methods"] if m["name"] != "echoOpt"]
    doc["types"][0]["fields"][1]["type"]["name"] = "int"  # Blob.n: uint -> int
    doc["types"][0]["fields"][1]["valueType"]["name"] = "int"
    report = compare(EXT, served(doc))
    assert report.level is None and not report.compatible and not report.typed
    assert report.missing == CompatMembers(("echoOpt",), ())
    assert "echoWrapper" in report.mismatched.methods and "echoBlob" in report.mismatched.methods
    assert report.mismatched.events == ("blobEvent",)
    assert "whoAmI" not in report.mismatched.methods
    with pytest.raises(IncompatibleModule) as excinfo:
        report.raise_if_incompatible()
    assert excinfo.value.report is report and not isinstance(excinfo.value, UntypedModule)
    message = str(excinfo.value)
    assert message.startswith("test_fullapi_ext_cpp serves a contract this client was not generated for")
    assert "missing: methods echoOpt" in message and "regenerate the client" in message
    assert report.require(methods=["whoAmI"]) is report
    with pytest.raises(IncompatibleModule, match="method echoOpt is not served; method echoWrapper has a "
                                                 "different signature; event blobEvent has a different"):
        report.require(methods=["echoOpt", "echoWrapper"], events=["blobEvent"])


def test_not_exposed_and_require() -> None:
    doc = ast_doc("test_fullapi_ext_cpp")
    iface = Interface.from_json(doc)
    methods = [m for m in iface.method_names if m != "echoWrapper"]
    report = compare(EXT, served(doc, exposure={"methods": methods, "events": []}))
    assert report.level == "exact"
    assert report.not_exposed == CompatMembers(("echoWrapper",), ("blobEvent",))
    assert "not exposed: methods echoWrapper; events blobEvent" in report.describe()
    report.require(methods=["echoBlob", "lidl"])
    with pytest.raises(IncompatibleModule, match="method echoWrapper is not exposed by this bridge"):
        report.require(methods=["echoWrapper"])
    with pytest.raises(IncompatibleModule, match="event blobEvent is not exposed"):
        report.require(events=["blobEvent"])


def test_a_digest_mismatch_is_a_note_not_a_failure() -> None:
    report = compare(EXT, served(ast_doc("test_fullapi_ext_cpp"), digest="0" * 64))
    assert report.level == "shape"  # the served digest is what the bridge says
    assert report.notes and "hashes to" in report.notes[0]


def test_a_shape_derived_expectation_with_its_recorded_digest() -> None:
    expected = Interface.from_shape(EXT.shape())
    info = served(ast_doc("test_fullapi_ext_cpp"))
    assert compare(expected, info).level == "shape"
    assert compare(expected, info, expected_interface_sha256=EXT.interface_sha256()).level == "exact"


@pytest.mark.parametrize("status", ["untyped", "invalid", None])
def test_names_level(status: str | None) -> None:
    methods = ["whoAmI", "echoBlob", "lidl"]
    info = untyped(status, methods, ["blobEvent"],
                   **({"interface_error": "lidl() did not parse: 1:1"} if status == "invalid" else {}))
    report = compare(EXT, info, allow_untyped=True)
    assert report.level == "names" and report.compatible and not report.typed
    assert "echoWrapper" in report.missing.methods and "whoAmI" not in report.missing.methods
    assert report.missing.events == ()
    refused = compare(EXT, info)
    assert refused.level is None
    with pytest.raises(UntypedModule) as excinfo:
        refused.raise_if_incompatible()
    text = str(excinfo.value)
    assert f"(status: {status or 'no lidl discovery'}" in text and "allow_untyped=True" in text
    if status == "invalid":
        assert "lidl() did not parse: 1:1" in text and "interface_error" in refused.describe()
    report.require(methods=["whoAmI"])
    with pytest.raises(IncompatibleModule, match="method echoWrapper is not served"):
        report.require(methods=["echoWrapper"])


def test_names_level_with_unknown_names() -> None:
    unresolved = ModuleInfo.from_json({"module": "m", "resolved": False, "interface_status": "untyped",
                                       "exposure": {"methods": [], "events": []}})
    report = compare(EXT, unresolved, allow_untyped=True)
    assert report.level == "names" and not report.missing
    untagged = untyped("untyped", ["whoAmI"], [], events_declared=False)
    assert compare(EXT, untagged, allow_untyped=True).missing.events == ()
    exposure_only = untyped("untyped", ["whoAmI"], ["blobEvent"])
    hidden = ModuleInfo.from_json({**exposure_only.raw, "exposure": {"methods": [], "events": []}})
    assert compare(EXT, hidden, allow_untyped=True).not_exposed == CompatMembers(("whoAmI",), ("blobEvent",))


def test_pending_is_never_compatible() -> None:
    info = ModuleInfo.from_json({"module": "m", "interface_status": "pending"})
    report = compare(EXT, info, allow_untyped=True)
    assert report.level is None and report.notes == ("discovery is still pending",)
    with pytest.raises(UntypedModule, match="status: pending"):
        report.raise_if_incompatible()


def test_report_members() -> None:
    assert not CompatMembers() and CompatMembers(("a",))
    assert CompatMembers(("a", "b"), ("e",)).describe() == "methods a, b; events e"
    report = CompatReport("m", "ok", None, "x", "y")
    assert report.describe() == "m: incompatible (status ok)"


@async_test
async def test_check_compat_waits_out_pending() -> None:
    doc = ast_doc("test_fullapi_ext_cpp")
    async with FakeBridge() as fake:
        fake.module("test_fullapi_ext_cpp", ["whoAmI"], status="pending")
        async with AsyncBridgeClient(fake.url) as client:

            async def resolve() -> None:
                await fake.wait_for_request("rpc.schema", count=2)
                fake.module("test_fullapi_ext_cpp", list(EXT.method_names), list(EXT.event_names),
                            interface=doc, status="ok")

            task = asyncio.ensure_future(resolve())
            report = await check_compat(client, "test_fullapi_ext_cpp", EXT, discovery_wait=5)
            await task
            assert report.level == "exact"
            fake.module("late", ["x"], status="pending")
            with pytest.raises(DiscoveryPending):
                await check_compat(client, "late", EXT, discovery_wait=0.1)
            fake.module("test_fullapi_ext_cpp", ["whoAmI"], status="untyped")
            with pytest.raises(UntypedModule):
                await check_compat(client, "test_fullapi_ext_cpp", EXT)
            names = await check_compat(client, "test_fullapi_ext_cpp", EXT, allow_untyped=True)
            assert names.level == "names" and "echoBlob" in names.missing.methods
            mini = ast_doc("mini_module")
            fake.module("test_fullapi_ext_cpp", ["put"], interface=mini, status="ok")
            with pytest.raises(IncompatibleModule, match="serves a contract this client was not generated for"):
                await check_compat(client, "test_fullapi_ext_cpp", EXT)
