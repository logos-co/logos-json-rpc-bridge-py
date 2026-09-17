"""The LIDL model: reading, exact writing, validation, identity, serializer, shape."""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

import pytest
from fixture_util import (
    AST_DIR,
    CONTRACTS,
    EDGE_CONTRACTS,
    EDGE_DIR,
    ast_bytes,
    ast_doc,
    edge_doc,
    interface,
    lidl_text,
)

from logos_bridge.digest import canonical_json, interface_sha256, shape_sha256
from logos_bridge.lidl import (
    IDENTITY_DESCRIPTIONS,
    UNMAPPABLE_CODES,
    IdentityError,
    Interface,
    InterfaceInvalid,
    LidlFormatError,
    Method,
    Slot,
    TypeRef,
    identity_method,
    json_type_name,
    parse_shape_type,
    shape_type,
)

TSTR = TypeRef.primitive("tstr")


# ------------------------------------------------------------------- reading


@pytest.mark.parametrize("name", CONTRACTS)
def test_fixture_asts_are_canonical_json(name: str) -> None:
    # interface_sha256 is the SHA-256 of the file without its newline.
    doc = ast_doc(name)
    assert canonical_json(doc) + b"\n" == ast_bytes(name)
    assert interface(name).interface_sha256() == interface_sha256(doc)


@pytest.mark.parametrize("name", CONTRACTS)
def test_to_json_reproduces_the_document(name: str) -> None:
    iface = interface(name)
    assert iface.to_json() == ast_doc(name)
    assert iface.has_identity and iface.with_identity() is iface
    assert iface.issues == ()


def test_the_fixture_contracts_read_as_expected() -> None:
    storage = interface("storage_module")
    assert (storage.name, storage.version, storage.category) == ("storage_module", "2.1.3", "protocol")
    assert len([m for m in storage.methods if not m.derived]) == 31
    assert storage.method_names[-3:] == ("name", "version", "lidl")
    assert len(storage.events) == 9
    import_files = storage.method("importFiles")
    assert import_files is not None and import_files.returns is None
    metrics = storage.method("collectMetrics")
    assert metrics is not None and metrics.returns is not None and metrics.returns.type.spell() == "{tstr: any}"
    mini = interface("mini_module")
    note = mini.record("Note")
    assert note is not None
    title, tag = note.field("title"), note.field("tag")
    assert title is not None and title.flag and title.optional and title.value_type == TSTR
    assert tag is not None and not tag.flag and tag.optional and tag.type.is_optional
    find = mini.method("find")
    assert find is not None and (find.min_args, find.max_args) == (1, 2)
    assert find.signature() == "find(id: tstr, prefix: ? tstr) -> result"
    assert mini.event("added") is not None and mini.event("nope") is None
    assert mini.record("Nope") is None and mini.method("nope") is None
    assert [p for p, _ in mini.slots()][:2] == ["/types/0/fields/0", "/types/0/fields/1"]


def test_the_derived_keys_are_what_the_reader_uses() -> None:
    doc = ast_doc("mini_module")
    field = doc["types"][0]["fields"][0]
    field["isOptional"] = True  # a hand-edited AST: the derived key wins, and validation says so
    iface = Interface.from_json(doc)
    record = iface.record("Note")
    assert record is not None and record.fields[0].optional
    assert [(i.code, i.pointer, i.origin) for i in iface.errors] == [
        ("derived_key_mismatch", "/types/0/fields/0", "python")]


def test_no_return_in_every_spelling() -> None:
    noreturn = Interface.from_json(edge_doc("noreturn"))
    notify, maybe, fold = noreturn.method("notify"), noreturn.method("maybe"), noreturn.method("fold")
    assert notify is not None and notify.returns is None
    assert "returnType" not in notify.to_json()
    assert maybe is not None and maybe.returns is not None and maybe.returns.optional
    assert fold is not None and (fold.min_args, fold.max_args) == (0, 2) and fold.signature() == \
        "fold(value: ? int, extra: ? bstr)"
    legacy = Interface.from_json(edge_doc("legacy_void"))
    reset = legacy.method("reset")
    assert reset is not None and reset.returns is None and reset.description == "Forget everything."
    ast = Interface.from_json(edge_doc("legacy_ast"))
    for name in ("primitiveVoid", "namedVoid", "nullReturn"):
        method = ast.method(name)
        assert method is not None and method.returns is None, name
    bare = ast.method("bare")
    assert bare is not None and bare.returns is not None and bare.returns.type.spell() == "[tstr]"
    assert bare.params[0].optional and bare.params[0].value_type == TypeRef.primitive("int")
    optional_void = ast.method("optionalVoid")
    assert optional_void is not None and optional_void.returns is not None
    assert [i.message for i in ast.errors] == ["Unknown type 'void'"]
    assert ast.to_json() == edge_doc("legacy_ast")


def test_unknown_keys_are_kept_and_unknown_kinds_flagged() -> None:
    doc = edge_doc("forward")
    iface = Interface.from_json(doc)
    assert iface.to_json() == doc
    assert iface.extra == {"annotations": {"stability": "beta"}}
    assert iface.interface_sha256() == interface_sha256(doc)
    put = iface.method("put")
    assert put is not None and put.raw is not None and put.raw["deprecated"] is True
    scores = iface.method("scores")
    assert scores is not None and scores.params[0].type.kind == "set" and not scores.params[0].type.known
    item = iface.record("Item")
    assert item is not None and item.fields[1].type.elements[0].extra == {"annotations": ["trimmed"]}
    codes = [(i.code, i.pointer) for i in iface.errors]
    assert codes == [
        ("unknown_primitive", "/methods/0/params/1/type"),
        ("unknown_kind", "/methods/1/params/0/type"),
        ("map_key_not_string", "/methods/2/returnType/elements/0"),
        ("malformed_type", "/events/0/params/1/type"),
    ]
    assert {c for c, _ in codes} <= UNMAPPABLE_CODES
    assert iface.check(allow_unknown_types=True) is iface
    with pytest.raises(InterfaceInvalid, match="unknown type kind 'set'"):
        iface.check()
    # Built programmatically, the extras survive a rebuild too.
    rebuilt = dataclasses.replace(iface, raw=None)
    assert rebuilt.to_json()["annotations"] == {"stability": "beta"}


@pytest.mark.parametrize(
    ("mutate", "pointer", "needle"),
    [
        (lambda d: d.__setitem__("name", 5), "/name", "expected string, got number"),
        (lambda d: d.__setitem__("methods", {}), "/methods", "expected array, got object"),
        (lambda d: d["methods"].__setitem__(0, "put"), "/methods/0", "expected a method (an object), got string"),
        (lambda d: d["methods"][0]["params"][0].pop("type"), "/methods/0/params/0", 'missing "type"'),
        (lambda d: d["methods"][0]["params"][0]["type"].__setitem__("kind", None), "/methods/0/params/0/type/kind",
         "expected string, got null"),
        (lambda d: d["methods"][0].__setitem__("derived", "no"), "/methods/0/derived", "expected boolean"),
        (lambda d: d["methods"][0].__setitem__("returnIsOptional", 1), "/methods/0/returnIsOptional",
         "expected boolean, got number"),
        (lambda d: d["types"][0]["fields"][0].__setitem__("isOptional", "x"), "/types/0/fields/0/isOptional",
         "expected boolean"),
        (lambda d: d["depends"].append(3), "/depends/0", "expected string, got number"),
        (lambda d: d["events"][0]["params"][0]["type"]["elements"].append([]),
         "/events/0/params/0/type/elements/0", "expected a type (an object), got array"),
    ],
)
def test_format_errors_carry_a_pointer(mutate: Any, pointer: str, needle: str) -> None:
    doc = copy.deepcopy(ast_doc("mini_module"))
    mutate(doc)
    with pytest.raises(LidlFormatError) as excinfo:
        Interface.from_json(doc)
    assert excinfo.value.pointer == pointer
    assert needle in str(excinfo.value)


def test_loads_and_coerce() -> None:
    iface = Interface.loads(ast_bytes("mini_module"))
    assert Interface.coerce(iface) is iface
    assert Interface.coerce(ast_doc("mini_module")) == iface
    assert Interface.coerce(iface.shape()).shape() == iface.shape()
    with pytest.raises(LidlFormatError, match="not JSON"):
        Interface.loads("{")
    with pytest.raises(LidlFormatError, match=r"\(root\): expected a module"):
        Interface.loads("[]")
    assert json_type_name(object()) == "object" and json_type_name([]) == "array"


# --------------------------------------------------------------- validation


@pytest.mark.parametrize("name", EDGE_CONTRACTS)
def test_the_validator_mirrors_lidl_check(name: str) -> None:
    report = json.loads((EDGE_DIR / f"{name}.check.json").read_bytes())
    assert Interface.from_json(edge_doc(name)).lidl_report() == report


def test_validation_findings_have_codes_and_pointers() -> None:
    invalid = Interface.from_json(edge_doc("invalid"))
    assert [(i.severity, i.code, i.pointer) for i in invalid.issues if i.origin == "lidl"] == [
        ("error", "type_shadows_builtin", "/types/0/name"),
        ("error", "optional_map_key", "/types/0/fields/0/type/elements/0/elements/0"),
        ("warning", "optional_twice", "/types/1/fields/0"),
        ("warning", "optional_any", "/types/1/fields/1/type"),
        ("error", "duplicate_type", "/types/2/name"),
        ("error", "unknown_type", "/types/2/fields/0/type"),
        ("warning", "redundant_optional", "/methods/0/returnType/elements/0/elements/0"),
        ("error", "unknown_type", "/methods/0/params/0/type"),
        ("warning", "redundant_optional", "/methods/0/params/1/type/elements/0"),
        ("warning", "optional_any", "/methods/0/params/1/type/elements/0/elements/0"),
        ("error", "duplicate_param", "/methods/0/params/1/name"),
        ("error", "duplicate_method", "/methods/1/name"),
        ("warning", "optional_any", "/methods/2/returnType/elements/1/elements/0"),
        ("error", "unknown_type", "/events/0/params/0/type/elements/0"),
        ("error", "duplicate_event", "/events/1/name"),
    ]
    python = [(i.code, i.pointer) for i in invalid.issues if i.origin == "python"]
    assert python == [
        ("map_key_not_string", "/types/0/fields/0/type/elements/0/elements/0"),
        ("map_key_not_string", "/methods/2/params/0/type/elements/0"),
    ]
    assert str(invalid.errors[0]) == "/types/0/name: error: Type 'tstr' shadows a builtin type"
    with pytest.raises(InterfaceInvalid) as excinfo:
        invalid.check()
    assert "contract invalid_module is invalid" in str(excinfo.value) and excinfo.value.issues == invalid.issues


def test_python_specific_checks() -> None:
    recursive = Interface.from_json(edge_doc("recursive"))
    assert [(i.severity, i.code, i.pointer) for i in recursive.issues] == [
        ("warning", "infinite_record", "/types/1")]
    assert recursive.errors == ()
    doc = ast_doc("mini_module")
    doc["name"] = "bad-name"
    doc["methods"][0]["params"][0]["name"] = "a b"
    doc["events"][0]["params"].append(copy.deepcopy(doc["events"][0]["params"][0]))
    doc["types"][0]["fields"][0]["type"] = {"elements": [], "kind": "array", "name": ""}
    doc["types"][0]["fields"][0]["valueType"] = doc["types"][0]["fields"][0]["type"]
    issues = [(i.code, i.pointer) for i in Interface.from_json(doc).errors]
    assert issues == [
        ("invalid_identifier", "/name"),
        ("malformed_type", "/types/0/fields/0/type"),
        ("invalid_identifier", "/methods/0/params/0/name"),
        ("duplicate_event_param", "/events/0/params/1/name"),
    ]
    empty = Interface.from_json({"name": ""})
    assert [i.message for i in empty.errors] == ["Module name is empty"]


# ------------------------------------------------------------------ identity


@pytest.mark.parametrize("name", EDGE_CONTRACTS)
def test_the_identity_mirror_matches_the_cli(name: str) -> None:
    plain = Interface.from_json(edge_doc(name))
    err = EDGE_DIR / f"{name}.identity.err"
    if err.exists():
        with pytest.raises(IdentityError) as excinfo:
            plain.with_identity()
        assert f"{name}.lidl: {excinfo.value}\n" == err.read_text(encoding="utf-8")
        return
    expected = (EDGE_DIR / f"{name}.identity.json").read_bytes()
    injected = plain.with_identity()
    assert canonical_json(injected.to_json()) + b"\n" == expected
    assert injected.with_identity() is injected  # idempotent


def test_identity_details() -> None:
    authored = Interface.from_json(edge_doc("identity_authored")).with_identity()
    assert authored.method_names == ("version", "name", "ping", "lidl")
    version = authored.method("version")
    assert version is not None and not version.derived and version.description.startswith("Authored")
    lidl = authored.method("lidl")
    assert lidl is not None and lidl.derived and lidl.is_identity
    assert lidl.description == IDENTITY_DESCRIPTIONS["lidl"]
    assert identity_method("name").to_json() == {
        "derived": True, "description": "The module's name, as declared in its metadata.",
        "jsonReturn": False, "name": "name", "params": [], "resultReturn": False,
        "returnIsOptional": False, "returnType": {"elements": [], "kind": "primitive", "name": "tstr"},
        "returnValueType": {"elements": [], "kind": "primitive", "name": "tstr"},
    }
    with pytest.raises(ValueError):
        identity_method("whoAmI")
    plain = Interface.from_json(edge_doc("noreturn"))
    assert not plain.has_identity and plain.with_identity().has_identity


# ---------------------------------------------------------------- serializer


@pytest.mark.parametrize("name", CONTRACTS)
def test_the_serializer_mirror_reproduces_canonical_contracts(name: str) -> None:
    assert interface(name).to_lidl() == lidl_text(name)


@pytest.mark.parametrize("name", EDGE_CONTRACTS)
def test_the_serializer_mirror_matches_lidl_fmt(name: str) -> None:
    fmt = EDGE_DIR / f"{name}.fmt.lidl"
    source = fmt if fmt.exists() else EDGE_DIR / f"{name}.lidl"
    assert Interface.from_json(edge_doc(name)).to_lidl() == source.read_text(encoding="utf-8")


def test_the_serializer_refuses_unwritable_types() -> None:
    with pytest.raises(ValueError, match="not a well-formed LIDL type"):
        Interface.from_json(edge_doc("forward")).to_lidl()


def test_the_serializer_escapes_descriptions_only() -> None:
    method = Method("m", description='a "q"\\\n\tb', derived=False)
    iface = Interface("x", version='1"0', description="line\nnext", methods=(method,))
    assert iface.to_lidl() == (
        'module x {\n  version "1"0"\n  description "line\\nnext"\n  depends []\n\n'
        '  method m() description "a \\"q\\"\\\\\\n\\tb"\n}\n'
    )


# --------------------------------------------------------------------- types


def test_type_refs() -> None:
    nested = TypeRef.optional(TypeRef.optional(TypeRef.array(TypeRef.map(TSTR, TypeRef.named("Blob")))))
    assert nested.spell() == "? ? [{tstr: Blob}]"
    assert str(nested.value_type) == "[{tstr: Blob}]"
    assert nested.well_formed and nested.known
    assert [n.kind for n in nested.walk()] == ["optional", "optional", "array", "map", "primitive", "named"]
    assert TypeRef.from_json(nested.to_json()) == nested
    assert not TypeRef.map(TypeRef.primitive("int"), TSTR).known
    assert TypeRef.map(TypeRef.primitive("int"), TSTR).well_formed
    assert not TypeRef.primitive("float32").known and TypeRef.primitive("float32").well_formed
    odd = TypeRef("set", "", (TSTR,))
    assert odd.spell() == "<set(tstr)>" and not odd.well_formed
    assert TypeRef("array").spell() == "<array>"
    assert Slot.make("x", TSTR, flag=True).spell() == "? x: tstr"
    assert Slot.make("x", TypeRef.optional(TSTR)).optional


@pytest.mark.parametrize(
    ("type_", "text"),
    [
        (TSTR, "tstr"),
        (TypeRef.optional(TSTR), "?tstr"),
        (TypeRef.optional(TypeRef.optional(TSTR)), "?tstr"),
        (TypeRef.optional(TypeRef.primitive("any")), "any"),
        (TypeRef.array(TypeRef.optional(TypeRef.named("Blob"))), "[?Blob]"),
        (TypeRef.map(TSTR, TypeRef.array(TypeRef.primitive("bstr"))), "{tstr:[bstr]}"),
        (TypeRef.map(TypeRef.primitive("int"), TSTR), "{int:tstr}"),
    ],
)
def test_shape_types_round_trip(type_: TypeRef, text: str) -> None:
    assert shape_type(type_) == text
    assert shape_type(parse_shape_type(text)) == text


def test_uncompactable_shape_types_use_the_ast_form() -> None:
    for odd in (TypeRef("set", "", (TSTR,)), TypeRef.primitive("float32"), TypeRef.named("tstr"),
                TypeRef.array(TypeRef.named("bad name"))):
        spelled = shape_type(odd)
        assert isinstance(spelled, dict)
        assert shape_type(parse_shape_type(spelled)) == spelled
    for bad in ("", "[tstr", "{tstr tstr}", "tstr]", "?", "1x", 5):
        with pytest.raises(LidlFormatError):
            parse_shape_type(bad, "/x")


# --------------------------------------------------------------------- shape


def test_shape_drops_spelling_and_metadata() -> None:
    mini = interface("mini_module")
    shape = mini.shape()
    assert shape["records"]["Note"] == {"id": "tstr", "body": "bstr", "title": "?tstr", "tag": "?tstr"}
    assert shape["methods"]["find"] == {"params": [["id", "tstr"], ["prefix", "?tstr"]], "returns": "result"}
    assert shape["methods"]["clear"] == {"params": [], "returns": None}
    assert shape["events"] == {"added": [["note", "Note"]]}
    doc = ast_doc("mini_module")
    doc["version"] = "9.9.9"
    doc["description"] = "changed"
    doc["methods"][0]["description"] = "changed"
    doc["methods"].reverse()
    field = doc["types"][0]["fields"][2]  # `? title: tstr` respelled `title: ?tstr`
    field["optional"] = False
    field["type"] = {"elements": [field["type"]], "kind": "optional", "name": ""}
    respelled = Interface.from_json(doc)
    assert respelled.interface_sha256() != mini.interface_sha256()
    assert respelled.shape_sha256() == mini.shape_sha256() == shape_sha256(shape) == shape_sha256(ast_doc("mini_module"))


@pytest.mark.parametrize("name", [*CONTRACTS, "recursive", "names", "noreturn", "empty"])
def test_shapes_round_trip(name: str) -> None:
    source = interface(name) if name in CONTRACTS else Interface.from_json(edge_doc(name)).with_identity()
    rebuilt = Interface.from_shape(source.shape())
    assert rebuilt.shape() == source.shape()
    assert rebuilt.structural() == source.structural()
    assert rebuilt.has_identity
    assert all(m.derived for m in rebuilt.methods if m.name in ("name", "version", "lidl"))
    assert rebuilt.with_identity() is rebuilt


def test_shape_documents_are_checked() -> None:
    with pytest.raises(LidlFormatError, match="not a shape document"):
        Interface.from_shape({"shape": 2})
    base = interface("mini_module").shape()
    for key, value, needle in (
        ("module", 3, "expected string"),
        ("records", [], "expected object"),
        ("methods", {"m": []}, "expected object"),
        ("methods", {"m": {"params": {}}}, "expected array"),
        ("methods", {"m": {"params": [["x"]]}}, "expected \\[name, type\\]"),
        ("events", {"e": [["x", "[tstr"]]}, "expected"),
        ("records", {"R": []}, "expected object"),
    ):
        broken = {**base, key: value}
        with pytest.raises(LidlFormatError, match=needle):
            Interface.from_shape(broken)


def test_structural_forms_ignore_names_that_do_not_reach_the_wire() -> None:
    ext = interface("test_fullapi_ext_cpp")
    doc = ast_doc("test_fullapi_ext_cpp")
    text = json.dumps(doc).replace('"Blob"', '"Chunk"').replace('"test_fullapi_ext_cpp"', '"other_provider"')
    renamed = Interface.from_json(json.loads(text))
    renamed_doc = renamed.to_json()
    renamed_doc["methods"][1]["params"][0]["name"] = "value"
    renamed = Interface.from_json(renamed_doc)
    assert renamed.shape() != ext.shape()
    assert renamed.structural() == ext.structural()
    structural = ext.structural()
    assert "module" not in structural and set(structural["records"]) == {"R0", "R1", "R2"}
    assert structural["methods"]["echoWrapper"] == {"params": ["R2"], "returns": "R2"}
    unused = copy.deepcopy(doc)
    unused["types"].append({"fields": [], "name": "Unused"})
    assert Interface.from_json(unused).structural() == structural


def test_member_signatures() -> None:
    ext = interface("test_fullapi_ext_cpp")
    wrapper = ext.member_signature("method", "echoWrapper")
    assert wrapper is not None
    assert wrapper["signature"] == {"params": [["v", "Wrapper"]], "returns": "Wrapper"}
    assert set(wrapper["records"]) == {"Wrapper", "Blob"}
    structural = ext.member_signature("method", "echoWrapper", structural=True)
    assert structural is not None and structural["signature"] == {"params": ["R0"], "returns": "R0"}
    assert set(structural["records"]) == {"R0", "R1"}
    event = ext.member_signature("event", "blobEvent")
    assert event == {"signature": {"params": [["v", "Blob"]]}, "records": {
        "Blob": {"id": "tstr", "n": "uint", "payload": "bstr"}}}
    assert ext.member_signature("method", "nope") is None
    recursive = Interface.from_json(edge_doc("recursive"))
    grow = recursive.member_signature("method", "grow", structural=True)
    assert grow is not None and grow["signature"] == {"params": ["R0"], "returns": "{tstr:[R0]}"}
    assert grow["records"] == {"R0": {"leaf": "?int", "pair": "?R1"}, "R1": {"left": "R0", "right": "?R0"}}


def test_models_are_frozen_and_hashable() -> None:
    mini = interface("mini_module")
    with pytest.raises(dataclasses.FrozenInstanceError):
        mini.name = "x"  # type: ignore[misc]
    assert hash(mini) == hash(Interface.from_json(ast_doc("mini_module")))
    assert len({mini, Interface.from_json(ast_doc("mini_module"))}) == 1
    assert AST_DIR.is_dir()
