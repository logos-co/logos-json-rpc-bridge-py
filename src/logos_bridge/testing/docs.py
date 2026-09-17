"""Checking a bridge's API documents, and real traffic against them.

The bridge describes itself in OpenRPC (``rpc.discover``), OpenAPI
(``GET /openapi.json``) and AsyncAPI (``GET /asyncapi.json``). These helpers find
the schema a document gives a member (through its ``x-logos-module`` and
``x-logos-method``/``x-logos-event`` keys), validate captured values, REST bodies and
WebSocket frames with it, validate whole documents against meta-schemas, and check
that no operation exists for a member the bridge does not expose.

Validation needs the ``docs-test`` extra (``jsonschema``). This module imports
without it; the validating functions then raise :class:`DocsValidationUnavailable`.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any, Final

DOC_URI: Final = "urn:logos-bridge:document"
DRAFT7: Final = "http://json-schema.org/draft-07/schema#"
DRAFT2020: Final = "https://json-schema.org/draft/2020-12/schema"


class DocsValidationUnavailable(ImportError):
    """``jsonschema`` is not installed (``pip install 'logos-bridge[docs-test]'``)."""


class DocsValidationError(AssertionError):
    """A value or a document does not match; ``errors`` lists every problem."""

    def __init__(self, what: str, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__(f"{what}:\n" + "\n".join(f"  {e}" for e in self.errors))


def jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
        import referencing  # noqa: F401
    except ImportError:
        return False
    return True


def document_kind(doc: Mapping[str, Any]) -> str:
    """``openrpc``, ``openapi`` or ``asyncapi``."""
    for kind in ("openrpc", "openapi", "asyncapi"):
        if kind in doc:
            return kind
    raise ValueError("not an OpenRPC, OpenAPI or AsyncAPI document")


def dialect(doc: Mapping[str, Any]) -> str:
    """The JSON Schema dialect a document's schemas use."""
    if document_kind(doc) == "openapi":
        declared = doc.get("jsonSchemaDialect")
        return declared if isinstance(declared, str) else DRAFT2020
    return DRAFT7


def _pointer(*steps: str | int) -> str:
    return "".join("/" + str(s).replace("~", "~0").replace("/", "~1") for s in steps)


def validate_at(doc: Mapping[str, Any], pointer: str, instance: Any, *, what: str | None = None) -> None:
    """Validate ``instance`` against the schema at ``pointer`` in ``doc`` (``$ref``s resolve in ``doc``)."""
    try:
        import jsonschema
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT7 as SPEC7
        from referencing.jsonschema import DRAFT202012 as SPEC2020
    except ImportError as exc:
        raise DocsValidationUnavailable(
            "document validation needs jsonschema: pip install 'logos-bridge[docs-test]'") from exc
    # OpenAPI 3.1 dialects (jsonSchemaDialect, e.g. oas/3.1/dialect/base) all build on 2020-12.
    modern = document_kind(doc) == "openapi"
    uri = DRAFT2020 if modern else DRAFT7
    spec = SPEC2020 if modern else SPEC7
    registry: Registry[Any] = Registry().with_resource(DOC_URI, Resource.from_contents(dict(doc),
                                                                                      default_specification=spec))
    schema = {"$schema": uri, "$ref": DOC_URI + "#" + urllib.parse.quote(pointer, safe="/~:@!$&'()*+,;=")}
    validator_class = jsonschema.Draft202012Validator if modern else jsonschema.Draft7Validator
    validator = validator_class(schema, registry=registry)
    problems = [_leaf(e) for e in validator.iter_errors(instance)]
    if problems:
        problems.sort(key=lambda e: ([str(p) for p in e.absolute_path], e.message))
        raise DocsValidationError(what or f"value at {pointer}", [
            f"{'/'.join(str(p) for p in e.absolute_path) or '(value)'}: {e.message}" for e in problems])


_SHALLOW: Final = frozenset({"additionalProperties", "required", "type"})


def _leaf(error: Any) -> Any:
    # anyOf/oneOf name only the value: descend to the deepest branch error, which names the field.
    while error.context:
        error = max(error.context, key=lambda e: (len(e.absolute_path), e.validator not in _SHALLOW))
    return error


def validate_document(doc: Mapping[str, Any], metaschema: Mapping[str, Any], *,
                      resources: Mapping[str, Mapping[str, Any]] | None = None,
                      dialect: str | None = None) -> None:
    """Validate a whole document against its (vendored) meta-schema.

    ``resources`` maps URIs to the schemas it refers to. ``dialect`` (:data:`DRAFT7` or
    :data:`DRAFT2020`) applies to schemas whose ``$schema`` is not a JSON Schema dialect,
    as OpenRPC's meta-schema, which names its own.
    """
    try:
        import jsonschema
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT7 as SPEC7
        from referencing.jsonschema import DRAFT202012 as SPEC2020
    except ImportError as exc:
        raise DocsValidationUnavailable("document validation needs jsonschema") from exc
    spec = None if dialect is None else (SPEC2020 if dialect == DRAFT2020 else SPEC7)
    registry: Registry[Any] = Registry()
    known = dict(resources or {})
    if isinstance(metaschema.get("$id"), str):
        known.setdefault(metaschema["$id"], metaschema)  # so its embedded $ids resolve too
    for uri, contents in known.items():
        resource = (Resource.from_contents(dict(contents)) if spec is None
                    else Resource.from_contents(dict(contents), default_specification=spec))
        registry = registry.with_resource(uri, resource)
    registry = registry.crawl()
    if dialect is None:
        validator_class = jsonschema.validators.validator_for(metaschema)
    else:
        fallback = jsonschema.Draft202012Validator if dialect == DRAFT2020 else jsonschema.Draft7Validator
        validator_class = jsonschema.validators.validator_for(metaschema, default=fallback)
    validator = validator_class(dict(metaschema), registry=registry)
    problems = sorted(validator.iter_errors(dict(doc)), key=lambda e: (list(e.absolute_path), e.message))
    if problems:
        raise DocsValidationError("document", [
            f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in problems])


def _targets(obj: Any, module: str, member: str, key: str) -> bool:
    return bool(isinstance(obj, Mapping) and obj.get("x-logos-module") == module and obj.get(key) == member)


def openrpc_method(doc: Mapping[str, Any], module: str, method: str) -> tuple[int, Mapping[str, Any]]:
    """The index and object of the OpenRPC method that targets ``module.method``."""
    for index, entry in enumerate(doc.get("methods", [])):
        if _targets(entry, module, method, "x-logos-method"):
            return index, entry
    raise KeyError(f"{module}.{method} is not an operation of this document")


def validate_result(doc: Mapping[str, Any], module: str, method: str, result: Any) -> None:
    """A captured ``rpc.call`` result against its OpenRPC result schema."""
    index, _ = openrpc_method(doc, module, method)
    validate_at(doc, _pointer("methods", index, "result", "schema"), result, what=f"result of {module}.{method}")


def validate_params(doc: Mapping[str, Any], module: str, method: str, params: list[Any]) -> None:
    """Positional arguments against the OpenRPC parameter schemas (arity included)."""
    index, entry = openrpc_method(doc, module, method)
    declared = entry.get("params", [])
    required = [p for p in declared if p.get("required")]
    if not len(required) <= len(params) <= len(declared):
        raise DocsValidationError(f"arguments of {module}.{method}",
                                  [f"{len(params)} arguments for {len(declared)} parameters"])
    for position, value in enumerate(params):
        validate_at(doc, _pointer("methods", index, "params", position, "schema"), value,
                    what=f"argument {declared[position].get('name')} of {module}.{method}")


def openrpc_event(doc: Mapping[str, Any], module: str, event: str) -> tuple[int, Mapping[str, Any]]:
    for index, entry in enumerate(doc.get("x-logos-events", [])):
        if entry.get("module") == module and entry.get("event") == event:
            return index, entry
    raise KeyError(f"{module}.{event} is not an event of this document")


def validate_event(doc: Mapping[str, Any], module: str, event: str, data: list[Any]) -> None:
    """A captured ``rpc.event``'s ``data`` against its ``x-logos-events`` schema."""
    index, _ = openrpc_event(doc, module, event)
    validate_at(doc, _pointer("x-logos-events", index, "dataSchema"), data, what=f"event {module}.{event}")


def openapi_operation(doc: Mapping[str, Any], module: str, method: str) -> tuple[str, str, Mapping[str, Any]]:
    """``(path, verb, operation)`` of the OpenAPI operation that targets ``module.method``."""
    for path, item in doc.get("paths", {}).items():
        for verb, operation in item.items():
            if _targets(operation, module, method, "x-logos-method"):
                return path, verb, operation
    raise KeyError(f"{module}.{method} is not an operation of this document")


def _follow(doc: Mapping[str, Any], pointer: str) -> str:
    """``pointer``, or where the ``$ref`` found there leads (OpenAPI shares bodies and responses)."""
    node: Any = doc
    for token in pointer.split("/")[1:]:
        node = node[token.replace("~1", "/").replace("~0", "~")]
    ref = node.get("$ref") if isinstance(node, Mapping) else None
    if isinstance(ref, str) and ref.startswith("#/"):
        return _follow(doc, urllib.parse.unquote(ref[1:]))
    return pointer


def _rest_schema(doc: Mapping[str, Any], module: str, method: str, *where: str) -> str:
    path, verb, _ = openapi_operation(doc, module, method)
    return _follow(doc, _pointer("paths", path, verb, *where)) + _pointer("content", "application/json", "schema")


def validate_rest_request(doc: Mapping[str, Any], module: str, method: str, body: Any) -> None:
    """A REST request body (``POST /modules/<m>/<method>``) against its OpenAPI schema."""
    validate_at(doc, _rest_schema(doc, module, method, "requestBody"), body,
                what=f"REST request to {module}.{method}")


def validate_rest_response(doc: Mapping[str, Any], module: str, method: str, body: Any) -> None:
    """A REST answer (HTTP 200) against its OpenAPI schema."""
    validate_at(doc, _rest_schema(doc, module, method, "responses", "200"), body,
                what=f"REST answer of {module}.{method}")


#: The frames an AsyncAPI message can describe, by the suffix of its name.
FRAME_ROLES: Final = ("request", "result", "subscribe", "subscribed", "event")


def asyncapi_message(doc: Mapping[str, Any], module: str, member: str, role: str) -> tuple[str, Mapping[str, Any]]:
    """The component message that describes ``role`` frames of ``module.member``."""
    if role not in FRAME_ROLES:
        raise ValueError(f"role must be one of {', '.join(FRAME_ROLES)}")
    key = "x-logos-method" if role in ("request", "result") else "x-logos-event"
    for name, message in doc.get("components", {}).get("messages", {}).items():
        if _targets(message, module, member, key) and name.endswith(f".{role}"):
            return name, message
    raise KeyError(f"no {role} message for {module}.{member} in this document")


def validate_frame(doc: Mapping[str, Any], module: str, member: str, role: str, frame: Any) -> None:
    """A whole WebSocket frame (the decoded JSON-RPC object) against its AsyncAPI message payload."""
    name, _ = asyncapi_message(doc, module, member, role)
    validate_at(doc, _pointer("components", "messages", name, "payload"), frame,
                what=f"{role} frame of {module}.{member}")


def operations(doc: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    """``(module, member, kind)`` for every module operation an OpenRPC document lists."""
    found: set[tuple[str, str, str]] = set()
    for entry in doc.get("methods", []):
        module, method = entry.get("x-logos-module"), entry.get("x-logos-method")
        if isinstance(module, str) and isinstance(method, str):
            found.add((module, method, "method"))
    for entry in doc.get("x-logos-events", []):
        module, event = entry.get("module"), entry.get("event")
        if isinstance(module, str) and isinstance(event, str):
            found.add((module, event, "event"))
    return found


def exposure_problems(doc: Mapping[str, Any], exposures: Mapping[str, Mapping[str, Iterable[str]]]) -> list[str]:
    """Operations for members outside ``exposures`` (``{module: {"methods", "events"}}``),
    and ``x-logos-modules`` entries that disagree with them."""
    problems = []
    for module, member, kind in sorted(operations(doc)):
        exposure = exposures.get(module)
        if exposure is None:
            problems.append(f"{module}.{member}: an operation for an unexpected module")
        elif member not in set(exposure.get(f"{kind}s", ())):
            problems.append(f"{module}.{member}: an operation for a {kind} that is not exposed")
    for entry in doc.get("x-logos-modules", []):
        name = entry.get("name")
        expected = exposures.get(name) if isinstance(name, str) else None
        served = entry.get("exposure")
        if expected is not None and isinstance(served, Mapping):
            for kind in ("methods", "events"):
                if list(served.get(kind, [])) != list(expected.get(kind, [])):
                    problems.append(f"x-logos-modules {name}: exposure.{kind} is {served.get(kind)}, "
                                    f"expected {list(expected.get(kind, []))}")
    return problems
