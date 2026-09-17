from __future__ import annotations

import dataclasses

import pytest

from logos_bridge.models import CrossCheck, CrossCheckFinding, Event, Exposure, Health, InvalidParamsDetail, ModuleInfo

LEGACY_VIEW = {
    "module": "storage_module",
    "resolved": True,
    "events_declared": False,
    "methods": ["init", "start", 7],
    "events": [],
    "source": "getPluginInterface",
    "authoritative": False,
}


def test_module_info_from_a_legacy_view() -> None:
    info = ModuleInfo.from_json(LEGACY_VIEW)
    assert info.module == "storage_module"
    assert info.resolved and not info.events_declared and not info.authoritative
    assert info.methods == ("init", "start")  # non-strings are dropped from the tuple
    assert info.events == ()
    assert info.source == "getPluginInterface"
    assert not info.typed
    assert info.interface_status is None and info.interface is None and info.exposure is None
    assert info.raw is LEGACY_VIEW and info.extra == {}


def test_module_info_from_a_typed_view_keeps_unknown_keys() -> None:
    view = {
        **LEGACY_VIEW,
        "source": "lidl",
        "interface_status": "ok",
        "interface": {"name": "storage_module", "methods": []},
        "exposure": {"methods": ["init", "lidl"], "events": ["storage.done"], "note": 1},
        "interface_sha256": "ab" * 32,
        "contract_sha256": "cd" * 32,
        "cross_check": {"state": "warnings", "findings": [
            {"severity": "warning", "code": "non_canonical", "detail": "not canonical"},
            {"severity": "info", "code": "runtime_version_unavailable", "member": "version", "detail": "x"},
            "junk",
        ]},
        "stale": False,
        "lidl_reader": "0.3.0",
    }
    info = ModuleInfo.from_json(view)
    assert info.typed
    assert info.interface == {"name": "storage_module", "methods": []}
    assert info.exposure == Exposure(("init", "lidl"), ("storage.done",))
    assert info.exposure is not None and info.exposure.raw["note"] == 1
    assert (info.interface_sha256, info.contract_sha256) == ("ab" * 32, "cd" * 32)
    assert info.cross_check == CrossCheck("warnings", (
        CrossCheckFinding("warning", "non_canonical", "not canonical"),
        CrossCheckFinding("info", "runtime_version_unavailable", "x", "version"),
    ))
    assert info.cross_check is not None and info.cross_check.warnings == info.cross_check.findings[:1]
    assert info.cross_check.errors == ()
    assert info.stale is False and not info.pending
    assert info.callable_methods == ("init", "lidl") and info.subscribable_events == ("storage.done",)
    assert info.extra == {"lidl_reader": "0.3.0"}


def test_module_info_for_invalid_and_pending_modules() -> None:
    invalid = ModuleInfo.from_json({**LEGACY_VIEW, "interface_status": "invalid",
                                    "interface_error": "the contract does not match the running module",
                                    "exposure": {"methods": ["init"], "events": []},
                                    "cross_check": {"state": "inconsistent", "findings": [
                                        {"severity": "error", "code": "declared_method_missing",
                                         "member": "stop", "detail": "declared, but not in the live interface"}]},
                                    "interface": "not an object", "stale": True})
    assert not invalid.typed and invalid.stale is True
    assert invalid.interface_error == "the contract does not match the running module"
    assert invalid.interface is None
    assert invalid.cross_check is not None and invalid.cross_check.errors[0].member == "stop"
    odd = ModuleInfo.from_json({"module": "m", "interface_error": {"reason": "x"}, "stale": "yes"})
    assert odd.interface_error == "{'reason': 'x'}" and odd.stale is None
    pending = ModuleInfo.from_json({"module": "m", "interface_status": "pending", "cross_check": None,
                                    "interface_sha256": None, "contract_sha256": None})
    assert pending.interface_status == "pending" and pending.pending
    assert not pending.resolved and pending.methods == () and pending.cross_check is None
    assert pending.callable_methods == ()


@pytest.mark.parametrize("value", [None, [], "m", {"resolved": True}, {"module": 5}])
def test_module_info_needs_an_object_with_a_module(value: object) -> None:
    with pytest.raises(TypeError):
        ModuleInfo.from_json(value)


def test_health() -> None:
    health = Health.from_json({"status": "ok", "uptime_seconds": 12, "protocol": "json-rpc-2.0"})
    assert health.ok and not health.draining
    assert (health.uptime_seconds, health.protocol) == (12, "json-rpc-2.0")
    draining = Health.from_json({"status": "draining", "uptime_seconds": True})
    assert draining.draining and not draining.ok and draining.uptime_seconds is None
    assert Health.from_json({}).status == "unknown"


def test_invalid_params_detail() -> None:
    detail = InvalidParamsDetail.from_json({"reason": "schema-mismatch", "path": "who"})
    assert detail is not None and (detail.reason, detail.path) == ("schema-mismatch", "who")
    assert InvalidParamsDetail.from_json({"path": "x"}) is None
    assert InvalidParamsDetail.from_json(None) is None


def test_event_keeps_data_verbatim_and_decodes_on_request() -> None:
    event = Event("s1", "m", "e", [{"_bytes": "-_8"}, {"_bytes": "AA", "k": 1}], 2, 1750000000123)
    assert event.data[0] == {"_bytes": "-_8"}
    assert event.decoded_data() == [b"\xfb\xff", {"_bytes": "AA", "k": 1}]


def test_models_are_frozen() -> None:
    info = ModuleInfo.from_json(LEGACY_VIEW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.module = "x"  # type: ignore[misc]
    event = Event("s1", "m", "e", [], 1, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.data = [1]  # type: ignore[misc]
    assert ModuleInfo.from_json(LEGACY_VIEW) == info  # raw is not part of equality
