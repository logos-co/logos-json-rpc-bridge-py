"""logos_bridge.testing.docs: bridge documents and real traffic against them."""

from __future__ import annotations

import builtins
import copy
import json
import warnings
from typing import Any

import pytest
from fixture_util import FIXTURES, interface, lidl_text

from logos_bridge.digest import contract_sha256
from logos_bridge.testing import docs

DOC: dict[str, Any] = json.loads((FIXTURES / "docs" / "multi.openrpc.json").read_bytes())
OPENAPI: dict[str, Any] = json.loads((FIXTURES / "docs" / "multi.openapi.json").read_bytes())
ASYNCAPI: dict[str, Any] = json.loads((FIXTURES / "docs" / "multi.asyncapi.json").read_bytes())
NOTE = {"id": "n1", "body": {"_bytes": "aGk"}, "title": "t"}
REFUSED = {"error": {"code": -32601, "message": "method not found",
                     "data": {"logos_error_code": 1, "logos_error_name": "METHOD_NOT_FOUND"}}}
MINI_EXPOSURE = {"methods": ["put", "find", "clear", "name", "version", "lidl"], "events": ["added"]}
EXPOSURES = {
    "mini_module": MINI_EXPOSURE,
    "legacy_module": {"methods": ["ping", "add"], "events": ["ticked"]},
    "broken_module": {"methods": ["reset", "lidl"], "events": []},
    "late_module": {"methods": [], "events": []},
}


def needs_jsonschema() -> None:
    if not docs.jsonschema_available():
        pytest.skip("jsonschema is not installed (the docs-test extra)")


def test_the_bridge_and_this_package_agree_on_the_digests() -> None:
    mini = next(m for m in DOC["x-logos-modules"] if m["name"] == "mini_module")
    assert mini["interface_sha256"] == interface("mini_module").interface_sha256()
    assert mini["contract_sha256"] == contract_sha256(lidl_text("mini_module"))
    assert mini["exposure"] == MINI_EXPOSURE


def test_kinds_dialects_and_lookups() -> None:
    assert docs.document_kind(DOC) == "openrpc" and docs.dialect(DOC) == docs.DRAFT7
    assert docs.dialect({"openapi": "3.1.1"}) == docs.DRAFT2020
    assert docs.dialect({"openapi": "3.1.1", "jsonSchemaDialect": "urn:x"}) == "urn:x"
    assert docs.document_kind({"asyncapi": "3.0.0"}) == "asyncapi"
    with pytest.raises(ValueError):
        docs.document_kind({})
    index, method = docs.openrpc_method(DOC, "mini_module", "put")
    assert DOC["methods"][index] is method and method["x-logos-method"] == "put"
    with pytest.raises(KeyError):
        docs.openrpc_method(DOC, "mini_module", "nope")
    with pytest.raises(KeyError):
        docs.openrpc_event(DOC, "mini_module", "nope")
    assert ("mini_module", "added", "event") in docs.operations(DOC)
    assert ("legacy_module", "add", "method") in docs.operations(DOC)
    assert not any(module == "late_module" for module, _, _ in docs.operations(DOC))


def test_exposure_problems() -> None:
    assert docs.exposure_problems(DOC, EXPOSURES) == []
    narrower = dict(EXPOSURES, mini_module={"methods": ["find", "name", "version", "lidl"], "events": []})
    problems = docs.exposure_problems(DOC, narrower)
    assert "mini_module.put: an operation for a method that is not exposed" in problems
    assert "mini_module.added: an operation for a event that is not exposed" in problems
    assert any(p.startswith("x-logos-modules mini_module: exposure.methods is") for p in problems)
    missing = {k: v for k, v in EXPOSURES.items() if k != "legacy_module"}
    assert "legacy_module.ping: an operation for an unexpected module" in docs.exposure_problems(DOC, missing)


def test_captured_results_and_events_validate() -> None:
    needs_jsonschema()
    note = {"id": "n1", "body": {"_bytes": "aGk"}, "title": "t"}
    docs.validate_result(DOC, "mini_module", "put", note)
    docs.validate_result(DOC, "mini_module", "put", {"code": "dispatch_failed", "message": "m", "origin": "o"})
    docs.validate_result(DOC, "mini_module", "find", {"success": False, "value": None, "error": "no"})
    docs.validate_result(DOC, "mini_module", "clear", True)
    docs.validate_result(DOC, "legacy_module", "ping", {"anything": [1]})
    docs.validate_params(DOC, "mini_module", "find", ["x"])
    docs.validate_params(DOC, "mini_module", "find", ["x", None])
    docs.validate_event(DOC, "mini_module", "added", [note])
    docs.validate_event(DOC, "legacy_module", "ticked", [1, "two"])


@pytest.mark.parametrize(("check", "fragment"), [
    (lambda: docs.validate_result(DOC, "mini_module", "put", {"id": "n1", "body": {"_bytes": "a b"}}), "body"),
    (lambda: docs.validate_result(DOC, "mini_module", "put", {"id": 5, "body": {"_bytes": ""}}), "id"),
    (lambda: docs.validate_result(DOC, "mini_module", "clear", False), "(value)"),
    (lambda: docs.validate_event(DOC, "mini_module", "added", []), "(value)"),
    (lambda: docs.validate_event(DOC, "mini_module", "added", [{"id": "x"}]), "0"),
    (lambda: docs.validate_params(DOC, "mini_module", "find", ["x", 1]), "(value)"),
])
def test_mutated_captures_fail(check: Any, fragment: str) -> None:
    needs_jsonschema()
    with pytest.raises(docs.DocsValidationError) as excinfo:
        check()
    assert excinfo.value.errors and any(e.startswith(fragment) for e in excinfo.value.errors)


def test_arity_is_checked_before_schemas() -> None:
    with pytest.raises(docs.DocsValidationError, match="3 arguments for 2 parameters"):
        docs.validate_params(DOC, "mini_module", "find", ["x", None, 1])
    with pytest.raises(docs.DocsValidationError, match="0 arguments for 2 parameters"):
        docs.validate_params(DOC, "mini_module", "find", [])


def test_2020_12_documents_and_meta_schemas() -> None:
    needs_jsonschema()
    oas_dialect = {"openapi": "3.1.1", "jsonSchemaDialect": "https://spec.openapis.org/oas/3.1/dialect/base",
                   "components": {"schemas": {"Pair": {"type": "array", "prefixItems": [{"type": "string"}],
                                                       "items": False}}}}
    docs.validate_at(oas_dialect, "/components/schemas/Pair", ["a"])
    with pytest.raises(docs.DocsValidationError):
        docs.validate_at(oas_dialect, "/components/schemas/Pair", [1])
    openapi = {"openapi": "3.1.1", "components": {"schemas": {
        "Pair": {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}], "items": False}}}}
    docs.validate_at(openapi, "/components/schemas/Pair", ["a", 1])
    with pytest.raises(docs.DocsValidationError):
        docs.validate_at(openapi, "/components/schemas/Pair", ["a", 1, 2])
    meta = {"$schema": docs.DRAFT7, "type": "object", "required": ["openrpc"],
            "properties": {"openrpc": {"type": "string"}, "methods": {"$ref": "urn:test:methods"}}}
    methods = {"$schema": docs.DRAFT7, "type": "array"}
    docs.validate_document(DOC, meta, resources={"urn:test:methods": methods})
    with pytest.raises(docs.DocsValidationError, match="openrpc"):
        docs.validate_document({"methods": []}, meta, resources={"urn:test:methods": methods})


def test_members_are_found_through_their_x_logos_keys() -> None:
    renamed = copy.deepcopy(DOC)
    index, _ = docs.openrpc_method(DOC, "mini_module", "put")
    renamed["methods"][index]["name"] = "something else"
    assert docs.openrpc_method(renamed, "mini_module", "put")[0] == index
    path, verb, operation = docs.openapi_operation(OPENAPI, "mini_module", "find")
    assert (path, verb, operation["operationId"]) == ("/modules/mini_module/find", "post", "mini_module.find")
    for role in ("request", "result"):
        assert docs.asyncapi_message(ASYNCAPI, "mini_module", "find", role)[0] == f"mini_module.find.{role}"
    for role in ("subscribe", "subscribed", "event"):
        assert docs.asyncapi_message(ASYNCAPI, "mini_module", "added", role)[0] == f"mini_module.added.{role}"
    with pytest.raises(KeyError):
        docs.asyncapi_message(ASYNCAPI, "mini_module", "find", "event")
    with pytest.raises(KeyError):
        docs.openapi_operation(OPENAPI, "late_module", "anything")
    with pytest.raises(ValueError, match="role must be one of"):
        docs.asyncapi_message(ASYNCAPI, "mini_module", "find", "reply")


def test_rest_bodies_and_frames_validate() -> None:
    needs_jsonschema()
    docs.validate_rest_request(OPENAPI, "mini_module", "find", {"id": "x"})
    docs.validate_rest_request(OPENAPI, "mini_module", "clear", {})  # a shared NoParams body ($ref)
    docs.validate_rest_request(OPENAPI, "legacy_module", "ping", {"any": "thing"})
    docs.validate_rest_request(OPENAPI, "mini_module", "find", ["x", None])
    docs.validate_rest_response(OPENAPI, "mini_module", "find", {"result": {"success": True, "value": 1, "error": None}})
    docs.validate_rest_response(OPENAPI, "mini_module", "put", {"result": NOTE})
    docs.validate_rest_response(OPENAPI, "mini_module", "put", REFUSED)
    call = {"jsonrpc": "2.0", "id": 1, "method": "mini_module.put", "params": [NOTE]}
    docs.validate_frame(ASYNCAPI, "mini_module", "put", "request", call)
    docs.validate_frame(ASYNCAPI, "mini_module", "put", "result", {"jsonrpc": "2.0", "id": 1, "result": NOTE})
    target = {"subscription": "s1", "module": "mini_module", "event": "added"}
    docs.validate_frame(ASYNCAPI, "mini_module", "added", "subscribe",
                        {"jsonrpc": "2.0", "id": 2, "method": "rpc.subscribe", "params": target})
    docs.validate_frame(ASYNCAPI, "mini_module", "added", "subscribed", {
        "jsonrpc": "2.0", "id": 2, "result": {**target, "operation": "subscribe", "state": "registered"}})
    docs.validate_frame(ASYNCAPI, "mini_module", "added", "event", {
        "jsonrpc": "2.0", "method": "rpc.event", "params": {**target, "data": [NOTE], "generation": 1, "ts": 5}})


EVENT = {"jsonrpc": "2.0", "method": "rpc.event",
         "params": {"subscription": "s1", "module": "mini_module", "event": "added", "data": [NOTE],
                    "generation": 1, "ts": 5}}


@pytest.mark.parametrize(("check", "fragment"), [
    (lambda: docs.validate_rest_request(OPENAPI, "mini_module", "find", {"id": "x", "extra": 1}), "(value)"),
    (lambda: docs.validate_rest_request(OPENAPI, "mini_module", "find", ["x", None, 1]), "(value)"),
    (lambda: docs.validate_rest_request(OPENAPI, "mini_module", "clear", ["x"]), "(value)"),
    (lambda: docs.validate_rest_response(OPENAPI, "mini_module", "put", {"result": {"id": "n1"}}), "result"),
    (lambda: docs.validate_rest_response(OPENAPI, "mini_module", "put", {"error": {"code": "x"}}), "error/code"),
    (lambda: docs.validate_frame(ASYNCAPI, "mini_module", "put", "request",
                                 {"jsonrpc": "2.0", "id": 1, "method": "mini_module.find", "params": [NOTE]}),
     "method"),
    (lambda: docs.validate_frame(ASYNCAPI, "mini_module", "added", "event",
                                 {**EVENT, "params": {**EVENT["params"], "data": []}}), "params/data"),
    (lambda: docs.validate_frame(ASYNCAPI, "mini_module", "added", "event",
                                 {**EVENT, "params": {**EVENT["params"], "ts": -1}}), "params/ts"),
])
def test_mutated_bodies_and_frames_fail(check: Any, fragment: str) -> None:
    needs_jsonschema()
    with pytest.raises(docs.DocsValidationError) as excinfo:
        check()
    assert any(e.startswith(fragment) for e in excinfo.value.errors), excinfo.value.errors


def test_a_meta_schema_that_names_its_own_dialect() -> None:
    needs_jsonschema()
    # OpenRPC's meta-schema declares "$schema": "https://meta.json-schema.tools/": read it as draft-07.
    own = "urn:test:own-meta"
    meta = {"$schema": own, "type": "object", "properties": {"methods": {"$ref": "urn:test:methods"}}}
    methods = {"$schema": own, "type": "array", "items": [{"type": "object"}], "additionalItems": False}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        docs.validate_document({"methods": [{}]}, meta, resources={"urn:test:methods": methods},
                               dialect=docs.DRAFT7)
        with pytest.raises(docs.DocsValidationError, match="methods"):
            docs.validate_document({"methods": [{}, 5]}, meta, resources={"urn:test:methods": methods},
                                   dialect=docs.DRAFT7)


def test_the_module_imports_without_jsonschema(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in ("jsonschema", "referencing"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert not docs.jsonschema_available()
    with pytest.raises(docs.DocsValidationUnavailable, match="docs-test"):
        docs.validate_result(DOC, "mini_module", "clear", True)
    with pytest.raises(docs.DocsValidationUnavailable):
        docs.validate_document(DOC, {})
    assert docs.exposure_problems(DOC, EXPOSURES) == []  # the non-validating helpers still work
