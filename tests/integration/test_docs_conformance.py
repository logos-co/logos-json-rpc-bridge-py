"""The bridge's documents: the offline renderer's, valid against their meta-schemas, and true to traffic.

Every capture is a real frame or body from the bridge. Its sub-schema is found through the
documents' ``x-logos-*`` keys, and a copy mutated against its declared type must fail.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from harness import NOTES, PROVIDERS, NodeFactory, Stack
from live_util import KNOWN_BROKEN, NOT_FOUND_CLASSES, REJECTIONS, TABLES, frames_until, raw_ws

from logos_bridge.lidl import Interface, TypeRef
from logos_bridge.testing import docs
from logos_bridge.testing.live import LiveBridge, bridge_config, http_request, rpc_http
from logos_bridge.testing.conformance import ConformanceCase

pytestmark = pytest.mark.integration

KINDS = ("openrpc", "openapi", "asyncapi")
# Requests a provider tolerates although the contract refuses them (tests/unit/test_conformance.py STRICTER).
OFF_CONTRACT = {("ext-cases", "bstr/padded-base64"), ("ext-cases", "[bstr]/lenient-plain-string")}


def interface(module: str) -> Interface:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "ast" / f"{module}.json"
    return Interface.from_json(json.loads(path.read_bytes()))


@pytest.fixture(scope="module", autouse=True)
def jsonschema_installed(typed_views: object) -> None:
    if not docs.jsonschema_available():
        pytest.skip("the docs-test extra (jsonschema, referencing) is not installed")


@pytest.fixture(scope="module")
def docs_node(module_node_factory: NodeFactory) -> LiveBridge:
    # Typed modules only: offline, an untyped module's names are unknown.
    return module_node_factory(name="docs", config=bridge_config(PROVIDERS))


@dataclass
class Served:
    documents: dict[str, Any]
    raw: dict[str, bytes]


def fetch(node: LiveBridge) -> Served:
    discover = rpc_http(node.port, {"jsonrpc": "2.0", "id": 1, "method": "rpc.discover"})
    documents = {"openrpc": discover.json()["result"]}
    raw = {}
    for kind in ("openapi", "asyncapi"):
        answer = http_request(node.port, "GET", f"/{kind}.json")
        assert (answer.status, answer.headers.get("content-type")) == (200, "application/json"), kind
        documents[kind], raw[kind] = answer.json(), answer.body
    return Served(documents, raw)


def render(stack: Stack, node: LiveBridge, kind: str, version: str, directory: Path) -> bytes:
    if stack.docs_cli is None:
        pytest.skip("LOGOS_BRIDGE_DOCS_CLI is not set")
    config = directory / "bridge.json"
    config.write_text(json.dumps(node.config), encoding="utf-8")
    argv = [stack.docs_cli, "--config", str(config), "--format", kind, "--info-version", version]
    for module in PROVIDERS:
        argv += ["--lidl", f"{module}={stack.provider_file(module, '.lidl')}"]
    done = subprocess.run(argv, capture_output=True, check=False, timeout=60)
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout


@pytest.fixture(scope="module", params=["docs", "policy"])
def context(request: pytest.FixtureRequest, docs_node: LiveBridge, policy_node: LiveBridge) -> tuple[LiveBridge, Served]:
    node = docs_node if request.param == "docs" else policy_node
    return node, fetch(node)


def test_the_served_documents_are_the_offline_renderings(context: tuple[LiveBridge, Served], stack: Stack,
                                                           tmp_path: Path) -> None:
    node, served = context
    version = served.documents["openrpc"]["info"]["version"]
    assert version == node.info()["version"]
    for kind in KINDS:
        offline = render(stack, node, kind, version, tmp_path)
        assert json.loads(offline) == served.documents[kind], kind
        if kind in served.raw:
            assert offline == served.raw[kind], f"{kind}: same document, different bytes"


def test_the_documents_describe_exactly_the_exposure(context: tuple[LiveBridge, Served]) -> None:
    node, served = context
    views = node.views()
    exposures = {m: views[m]["exposure"] for m in PROVIDERS}
    assert docs.exposure_problems(served.documents["openrpc"], exposures) == []
    for kind in KINDS:
        listed = {entry["name"]: entry for entry in served.documents[kind]["x-logos-modules"]}
        for module in PROVIDERS:
            entry = listed[module]
            assert (entry["status"], entry["exposure"]) == ("ok", exposures[module]), (kind, module)
            assert entry["interface_sha256"] == views[module]["interface_sha256"]
            assert entry["contract_sha256"] == views[module]["contract_sha256"]


# -- meta-schemas -----------------------------------------------------------

MetaCheck = Callable[[Any], None]


def meta_checks(directory: Path) -> dict[str, list[MetaCheck]]:
    """The bridge's own checks (tests/docs_metaschema.py), with its vendored meta-schemas."""
    def load(name: str) -> Any:
        return json.loads((directory / name).read_bytes())

    orpc = load("openrpc/open-rpc-meta-schema-1.14.9.json")
    tools = load("openrpc/json-schema-tools-meta-schema-1.7.5.json")
    oas = {name: load(f"openapi/{name}.json") for name in (
        "schema-base-2022-10-07", "schema-2022-10-07", "schema-2024-11-14", "dialect-base", "meta-base")}
    oas_resources = {schema["$id"]: schema for schema in oas.values()}
    asyncapi = load("asyncapi/asyncapi-3.0.0.json")
    # OpenRPC refers to json-schema-tools without the trailing slash of its $id.
    orpc_resources = {tools["$id"]: tools, tools["$id"].rstrip("/"): tools}
    return {
        "openrpc": [lambda d: docs.validate_document(d, orpc, resources=orpc_resources, dialect=docs.DRAFT7)],
        "openapi": [
            lambda d: docs.validate_document(d, oas["schema-base-2022-10-07"], resources=oas_resources,
                                             dialect=docs.DRAFT2020),
            lambda d: docs.validate_document(d, oas["schema-2024-11-14"], resources=oas_resources,
                                             dialect=docs.DRAFT2020),
        ],
        "asyncapi": [lambda d: docs.validate_document(d, asyncapi, dialect=docs.DRAFT7)],
    }


@pytest.fixture(scope="module")
def metaschemas(stack: Stack) -> dict[str, list[MetaCheck]]:
    if stack.metaschemas is None:
        pytest.skip("LOGOS_BRIDGE_METASCHEMAS is not set")
    return meta_checks(stack.metaschemas)


def test_the_documents_are_valid(context: tuple[LiveBridge, Served], metaschemas: dict[str, list[MetaCheck]]) -> None:
    _, served = context
    for kind in KINDS:
        for check in metaschemas[kind]:
            check(served.documents[kind])


def test_broken_documents_are_not(context: tuple[LiveBridge, Served], metaschemas: dict[str, list[MetaCheck]]) -> None:
    _, served = context
    for kind in KINDS:
        broken = copy.deepcopy(served.documents[kind])
        broken[kind] = "0.0.0"
        for check in metaschemas[kind]:
            with pytest.raises(docs.DocsValidationError):
                check(broken)


# -- captured traffic -------------------------------------------------------


@dataclass
class Capture:
    module: str
    member: str
    case: str
    request: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    rest: Any = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribe: tuple[dict[str, Any], dict[str, Any]] | None = None


def replayable(table: str, case: ConformanceCase) -> bool:
    # Isolated cases run on daemons of their own (test_live_calls); this is one shared node.
    return not case.isolate and case.expected_error not in NOT_FOUND_CLASSES and (table, case.id) not in KNOWN_BROKEN


async def capture_all(node: LiveBridge) -> list[Capture]:
    captures: list[Capture] = []
    async with raw_ws(node) as ws:
        ids = iter(range(1, 1_000_000))
        for table, (provider, conformance) in TABLES.items():
            for case in conformance.cases:
                if not replayable(table, case):
                    continue
                request = {"jsonrpc": "2.0", "id": next(ids), "method": f"{provider}.{case.method}",
                           "params": case.wire_args()}
                await ws.send(json.dumps(request))
                frames = await frames_until(ws, lambda got, rid=request["id"]: any(f.get("id") == rid for f in got), 30)
                captures.append(Capture(provider, case.method, f"{table}:{case.id}", request, frames[-1]))
            names = sorted({e.event for e in conformance.events})
            subscribed = {}
            for name in names:
                subscribe = {"jsonrpc": "2.0", "id": next(ids), "method": "rpc.subscribe",
                             "params": {"subscription": f"docs-{name}", "module": provider, "event": name}}
                await ws.send(json.dumps(subscribe))
                ack = (await frames_until(ws, lambda got, rid=subscribe["id"]: any(f.get("id") == rid for f in got),
                                          30))[-1]
                subscribed[name] = (subscribe, ack)
            for event in conformance.events:
                fire = {"jsonrpc": "2.0", "id": next(ids), "method": f"{provider}.{event.fire}",
                        "params": event.values}
                await ws.send(json.dumps(fire))
                frames = await frames_until(ws, lambda got, rid=fire["id"]: any(f.get("id") == rid for f in got)
                                            and any(f.get("method") == "rpc.event" for f in got), 30)
                capture = Capture(provider, event.event, f"{table}:{event.id}", subscribe=subscribed[event.event])
                capture.events = [f for f in frames if f.get("method") == "rpc.event"]
                captures.append(capture)
    for capture in captures:
        if capture.request is not None:
            answer = http_request(node.port, "POST", f"/modules/{capture.module}/{capture.member}",
                                  json.dumps(capture.request["params"]).encode(),
                                  headers={"Content-Type": "application/json"})
            assert answer.status == 200, capture.case
            capture.rest = answer.json()
    return captures


@pytest.fixture(scope="module")
def captures(docs_node: LiveBridge) -> list[Capture]:
    return asyncio.run(capture_all(docs_node))


@pytest.fixture(scope="module")
def served(docs_node: LiveBridge) -> dict[str, Any]:
    return fetch(docs_node).documents


WRONG: dict[str, Any] = {"tstr": 7, "int": "7", "uint": -1, "float64": "7.5", "bool": "true",
                         "bstr": {"_bytes": "not base64url"}, "result": {"success": "yes"}}


def mutate(iface: Interface, type_: TypeRef, value: Any) -> Any:
    """A copy of ``value`` its declared type refuses, or ``None`` when that type accepts anything."""
    t = type_.value_type
    if t.kind == "primitive":
        return WRONG.get(t.name)
    if t.kind == "array" and isinstance(value, list):
        for index, item in enumerate(value):
            changed = mutate(iface, t.elements[0], item)
            if changed is not None:
                return [*value[:index], changed, *value[index + 1:]]
        return {"not": "an array"}
    if t.kind == "map" and isinstance(value, dict):
        for key, item in value.items():
            changed = mutate(iface, t.elements[1], item)
            if changed is not None:
                return {**value, key: changed}
        return ["not", "an", "object"]
    if t.kind == "named" and isinstance(value, dict):
        record = iface.record(t.name)
        assert record is not None
        required = [f.name for f in record.fields if not f.optional and f.name in value]
        return {k: v for k, v in value.items() if k != required[0]} if required else "not a record"
    return None


def calls(captures: list[Capture]) -> Iterator[Capture]:
    return (c for c in captures if c.request is not None)


def test_captured_calls_match_their_schemas(captures: list[Capture], served: dict[str, Any]) -> None:
    rpc, api, aapi = served["openrpc"], served["openapi"], served["asyncapi"]
    checked = 0
    for capture in calls(captures):
        module, method = capture.module, capture.member
        assert capture.result is not None and "result" in capture.result, capture.case
        result = capture.result["result"]
        table, case_id = capture.case.split(":", 1)
        case = TABLES[table][1].case(case_id)
        docs.validate_result(rpc, module, method, result)
        docs.validate_frame(aapi, module, method, "result", capture.result)
        docs.validate_rest_response(api, module, method, capture.rest)
        assert capture.rest == {"result": result}, capture.case
        if case.expected_error in REJECTIONS or (table, case.id) in OFF_CONTRACT:
            # The provider refused (or tolerated) what the contract refuses: so do the documents.
            if (table, case.id) not in OFF_CONTRACT:
                assert isinstance(result, dict) and set(result) == {"code", "message", "origin"}, capture.case
            with pytest.raises(docs.DocsValidationError):
                docs.validate_frame(aapi, module, method, "request", capture.request)
            with pytest.raises(docs.DocsValidationError):
                docs.validate_rest_request(api, module, method, capture.request["params"])
        else:
            docs.validate_params(rpc, module, method, capture.request["params"])
            docs.validate_frame(aapi, module, method, "request", capture.request)
            docs.validate_rest_request(api, module, method, capture.request["params"])
        checked += 1
    assert checked >= 100, checked
    NOTES.append(f"docs: {checked} captured calls validated against all three documents")


def test_mutated_call_captures_fail(captures: list[Capture], served: dict[str, Any]) -> None:
    rpc, api, aapi = served["openrpc"], served["openapi"], served["asyncapi"]
    mutated = 0
    for capture in calls(captures):
        assert capture.result is not None
        result = capture.result["result"]
        if isinstance(result, dict) and set(result) == {"code", "message", "origin"}:
            continue
        iface = interface(capture.module)
        method = iface.method(capture.member)
        assert method is not None
        wrong = False if method.returns is None else mutate(iface, method.returns.type, result)
        if wrong is None:
            continue  # `any`: every value is valid
        with pytest.raises(docs.DocsValidationError):
            docs.validate_result(rpc, capture.module, capture.member, wrong)
        with pytest.raises(docs.DocsValidationError):
            docs.validate_frame(aapi, capture.module, capture.member, "result", {**capture.result, "result": wrong})
        with pytest.raises(docs.DocsValidationError):
            docs.validate_rest_response(api, capture.module, capture.member, {"result": wrong})
        mutated += 1
    assert mutated >= 60, mutated
    NOTES.append(f"docs: {mutated} mutated call results refused by all three documents")


def test_captured_events_match_their_schemas(captures: list[Capture], served: dict[str, Any]) -> None:
    rpc, aapi = served["openrpc"], served["asyncapi"]
    checked = 0
    for capture in captures:
        if capture.subscribe is None:
            continue
        subscribe, ack = capture.subscribe
        docs.validate_frame(aapi, capture.module, capture.member, "subscribe", subscribe)
        docs.validate_frame(aapi, capture.module, capture.member, "subscribed", ack)
        assert len(capture.events) == 1, capture.case
        frame = capture.events[0]
        data = frame["params"]["data"]
        docs.validate_event(rpc, capture.module, capture.member, data)
        docs.validate_frame(aapi, capture.module, capture.member, "event", frame)

        iface = interface(capture.module)
        declared = iface.event(capture.member)
        assert declared is not None
        mutations = [data[:-1]]  # one value short
        for index, param in enumerate(declared.params):
            changed = mutate(iface, param.type, data[index])
            if changed is not None:
                mutations.append([*data[:index], changed, *data[index + 1:]])
        for wrong in mutations:
            with pytest.raises(docs.DocsValidationError):
                docs.validate_event(rpc, capture.module, capture.member, wrong)
            with pytest.raises(docs.DocsValidationError):
                docs.validate_frame(aapi, capture.module, capture.member, "event",
                                    {**frame, "params": {**frame["params"], "data": wrong}})
        checked += 1
    assert checked == sum(len(table.events) for _, table in TABLES.values())
    NOTES.append(f"docs: {checked} captured events validated, and their mutations refused")
