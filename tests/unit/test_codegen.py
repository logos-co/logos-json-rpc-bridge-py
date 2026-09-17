"""logos-bridge-codegen: naming, deterministic text, generated modules, and the CLI."""

from __future__ import annotations

import dataclasses
import inspect
import io
import json
import keyword
import os
import subprocess
import sys
import typing
from pathlib import Path
from typing import Any

import pytest
from codegen_util import fixture_client, generate, load_module
from fixture_util import (
    AST_DIR,
    CONTRACTS,
    EDGE_DIR,
    LIDL_DIR,
    ROOT,
    contract_assets,
    edge_doc,
    fake_lgx_cli,
    fake_lidl_cli,
    interface,
    lidl_text,
    real_lgx_cli,
    real_lidl_cli,
)

from logos_bridge import AsyncBridgeClient, BridgeClient, typed
from logos_bridge.codegen import CodegenError, Contract, Rename, generate_python, pascal, plan_names, run, snake
from logos_bridge.codegen.cli import build_parser
from logos_bridge.codegen.naming import CLIENT_MEMBERS, PY_KEYWORDS, escape
from logos_bridge.codegen.text import (
    docstring,
    markdown_text,
    md_code,
    py_literal,
    py_str,
    rest_text,
    split_code_blocks,
    summary_and_body,
    wrap_line,
)
from logos_bridge.errors import BindingIntegrityError, CodegenFormatError
from logos_bridge.lidl import IDENTITY_METHODS, Interface
from logos_bridge.testing import FakeBridge, ThreadedFakeBridge, async_test

# ---------------------------------------------------------------- naming


@pytest.mark.parametrize(("name", "expected"), [
    ("echoBlobMap", "echo_blob_map"), ("whoAmI", "who_am_i"), ("peerId", "peer_id"), ("uploadURL", "upload_u_r_l"),
    ("storage_module", "storage_module"), ("Foo", "foo"), ("_bytes", "_bytes"), ("__init__", "__init__"),
    ("x", "x"), ("A1b", "a1b"),
])
def test_snake_is_rustgens(name: str, expected: str) -> None:
    assert snake(name) == expected


@pytest.mark.parametrize(("name", "expected"), [
    ("storage_module", "StorageModule"), ("storageStart", "StorageStart"), ("test_fullapi_cpp", "TestFullapiCpp"),
    ("Blob", "Blob"), ("__x__", "X"), ("a__b", "AB"), ("_", ""),
])
def test_pascal_is_rustgens(name: str, expected: str) -> None:
    assert pascal(name) == expected


def test_the_keyword_list_is_frozen_and_complete() -> None:
    assert set(keyword.kwlist) <= PY_KEYWORDS  # a newer Python with a new keyword fails here
    assert escape("class") == "class_" and escape("events", frozenset({"events"})) == "events_"
    assert escape("__init__") == "lidl__init__" and escape("__private") == "lidl__private"
    assert escape("1x") == "lidl_1x" and escape("plain") == "plain"


def test_names_for_keywords_builtins_and_client_members() -> None:
    names = plan_names(Interface.from_json(edge_doc("names")).with_identity())
    assert (names.base, names.async_client, names.sync_client) == (
        "NamesModule", "AsyncNamesModuleClient", "NamesModuleClient")
    assert names.records == {"str": "Str"}
    assert names.fields["str"] == {"class": "class_", "from": "from_", "int": "int", "self": "self",
                                   "meta": "meta", "__init__": "lidl__init__", "_bytes": "_bytes", "list": "list"}
    assert names.methods == {"import": "import_", "events": "events_", "check_compat": "check_compat_",
                             "bridge": "bridge_", "print": "print", "None": "none", "async": "async_",
                             "name": "name", "version": "version", "lidl": "lidl"}
    assert names.params["import"] == ["lambda_", "timeout_", "self_"]
    assert names.params["print"] == ["type", "id", "str"]
    assert names.events == {"class": "class", "module": "module"}
    assert names.event_classes == {"class": "ClassEvent", "module": "ModuleEvent"}
    assert names.event_fields == {"class": {"meta": "meta_", "type": "type"}, "module": {"str": "str"}}
    assert names.subscriber("class") == "on_class" and names.decoder("module") == "decode_module"


CLIENT_CLASH = Interface.from_shape({
    "shape": 1, "module": "clash_module", "records": {}, "events": {},
    "methods": {name: {"params": [], "returns": "tstr"} for name in (
        "module", "bridge", "aio", "portal", "events", "check_compat", "_module", "_bridge", "_portal", "_aio")},
}).with_identity()


def serve_clash(fake: FakeBridge | ThreadedFakeBridge) -> None:
    fake.module("clash_module", list(CLIENT_CLASH.method_names), interface=CLIENT_CLASH.to_json(), status="ok")
    for method in CLIENT_CLASH.method_names:
        fake.on_call("clash_module", method, f"called {method}")


def test_client_members_are_never_a_methods_name() -> None:
    assert not CLIENT_MEMBERS & set(IDENTITY_METHODS)
    names = plan_names(CLIENT_CLASH)
    assert names.methods == {**{m: f"{m}_" for m in CLIENT_CLASH.method_names if m not in IDENTITY_METHODS},
                             "name": "name", "version": "version", "lidl": "lidl"}


@async_test
async def test_a_generated_client_reaches_methods_named_like_its_members(tmp_path: Path) -> None:
    module = generate(tmp_path, CLIENT_CLASH)
    async with FakeBridge() as fake:
        serve_clash(fake)
        async with AsyncBridgeClient(fake.url) as bridge:
            client = module.AsyncClashModuleClient(bridge)
            assert client.module == "clash_module" and client.bridge is bridge
            for method, py in plan_names(CLIENT_CLASH).methods.items():
                assert await getattr(client, py)() == f"called {method}", method


def test_a_generated_blocking_client_reaches_methods_named_like_its_members(tmp_path: Path) -> None:
    module = generate(tmp_path, CLIENT_CLASH)
    with ThreadedFakeBridge() as fake:
        serve_clash(fake)
        with BridgeClient(fake.url) as bridge:
            client = module.ClashModuleClient(bridge)
            assert (client.module, client.bridge, client.portal) == ("clash_module", bridge, bridge.portal)
            assert isinstance(client.aio, module.AsyncClashModuleClient)
            for method, py in plan_names(CLIENT_CLASH).methods.items():
                assert getattr(client, py)() == f"called {method}", method


def test_collisions_are_errors_until_renamed() -> None:
    collisions = Interface.from_json(edge_doc("collisions")).with_identity()
    with pytest.raises(CodegenError) as excinfo:
        plan_names(collisions)
    assert excinfo.value.exit_code == 4
    assert str(excinfo.value) == (
        "Python names collide:\n"
        "  module-level names: record TickEvent and event tick's class both become 'TickEvent' "
        "(--rename event:tick=NAME)\n"
        "  client members: method fooBar and method foo_bar both become 'foo_bar' (--rename method:foo_bar=NAME)\n"
        "  client members: method onTick and event tick's subscriber both become 'on_tick' "
        "(--rename event:tick=NAME)"
    )
    renames = tuple(Rename.parse(t) for t in ("method:foo_bar=foo_bar_2", "event:tick=ticked",
                                              "eparam:tick.t=payload"))
    names = plan_names(collisions, renames=renames)
    assert names.methods["foo_bar"] == "foo_bar_2" and names.subscriber("tick") == "on_ticked"
    assert names.event_classes["tick"] == "TickedEvent" and names.event_fields["tick"] == {"t": "payload"}
    renamed = plan_names(collisions, renames=(Rename.parse("type:TickEvent=TickPayload"),
                                              Rename.parse("method:onTick=on_tick_now"),
                                              Rename.parse("method:foo_bar=clear_all")))
    assert renamed.records == {"TickEvent": "TickPayload"}


def test_scoped_collisions() -> None:
    doc = {"name": "m", "types": [{"name": "R", "fields": [
        {"name": n, "type": {"kind": "primitive", "name": "tstr", "elements": []}} for n in ("aB", "a_b")]}],
        "methods": [{"name": "f", "params": [
            {"name": n, "type": {"kind": "primitive", "name": "tstr", "elements": []}} for n in ("xY", "x_y")]}],
        "events": [{"name": "e", "params": [
            {"name": n, "type": {"kind": "primitive", "name": "tstr", "elements": []}} for n in ("pQ", "p_q")]},
            {"name": "m", "params": []}]}
    with pytest.raises(CodegenError) as excinfo:
        plan_names(Interface.from_json(doc))
    message = str(excinfo.value)
    assert "fields of record R: field aB and field a_b both become 'a_b'" in message
    assert "parameters of method f: parameter xY and parameter x_y both become 'x_y'" in message
    assert "parameters of event e: parameter pQ and parameter p_q both become 'p_q'" in message
    assert "the generated MEvent and event m's class both become 'MEvent'" in message
    assert plan_names(Interface.from_json({"name": "m", "events": [{"name": "m"}]}), class_name="Mod").event_classes


def test_rename_parsing() -> None:
    assert Rename.parse("field:Blob.id=ident") == Rename("field", "Blob.id", "ident")
    for bad in ("method:x", "nope:x=y", "method:=y", "field:x=y", "param:a.b.c=d", "method:x=class", "method:x=1y"):
        with pytest.raises(CodegenError) as excinfo:
            Rename.parse(bad)
        assert excinfo.value.exit_code == 2, bad
    with pytest.raises(CodegenError, match="names nothing in the contract: method:nope") as excinfo:
        plan_names(interface("mini_module"), renames=(Rename.parse("method:nope=x"),))
    assert excinfo.value.exit_code == 2
    with pytest.raises(CodegenError, match="pass --class-name"):
        plan_names(Interface.from_json({"name": "_"}))
    assert plan_names(interface("mini_module"), class_name="Notes", module_name="notes_v2").async_client == \
        "AsyncNotesClient"


# ------------------------------------------------------------------ text


def test_python_string_literals_are_deterministic() -> None:
    assert py_str('a"b\\c\n\t\r') == '"a\\"b\\\\c\\n\\t\\r"'
    assert py_str("\x00\x7f\x85–é😀") == '"\\x00\\x7f\\x85–é😀"'
    assert py_str(" ​﻿\ud800") == '"\\u2028\\u200b\\ufeff\\ud800"'
    for text in ("plain", "– dash", "tab\there", "\x1b[0m"):
        assert eval(py_str(text)) == text  # noqa: S307


def test_py_literal_wraps_only_what_does_not_fit() -> None:
    value = {"b": [1, None, True], "a": {"x": "y"}}
    assert py_literal(value) == '{"b": [1, None, True], "a": {"x": "y"}}'
    wrapped = py_literal(value, 0, 0, width=30)
    assert wrapped == '{\n    "b": [1, None, True],\n    "a": {"x": "y"},\n}'
    assert eval(wrapped) == value  # noqa: S307
    assert py_literal([], 0, 200, width=10) == "[]"
    with pytest.raises(TypeError):
        py_literal({"x": 1.5})


def test_doxygen_code_blocks() -> None:
    text = 'Intro:\n@code{.json}\n{"a": 1}\n@endcode\nAfter.\n@code\nraw\n@endcode\n@code{.json}\nunterminated'
    chunks = list(split_code_blocks(text))
    assert chunks == [("text", None, "Intro:"), ("code", "json", '{"a": 1}'), ("text", None, "After."),
                      ("code", None, "raw"), ("text", None, "@code{.json}\nunterminated")]
    assert rest_text(text) == ('Intro:\n\n::\n\n    {"a": 1}\n\nAfter.\n\n::\n\n    raw\n\n'
                               "@code{.json}\nunterminated")
    assert markdown_text(text) == ('Intro:\n\n```json\n{"a": 1}\n```\n\nAfter.\n\n```\nraw\n```\n\n'
                                   "@code{.json}\nunterminated")
    assert markdown_text("@code\na ``` b\n@endcode") == "~~~~\na ``` b\n~~~~"
    assert summary_and_body("  First line.\nSecond\n\nThird  ") == ("First line.", "Second\n\nThird")
    assert summary_and_body("") == ("", "")


def test_docstrings_escape_what_would_break_them() -> None:
    assert docstring("one line", 4) == '    """one line"""'
    assert docstring('ends with a quote"', 0) == '"""ends with a quote\\""""'
    assert docstring('has """ inside \\ and \t', 0) == '"""has \\"\\"\\" inside \\\\ and \\t"""'
    assert docstring("a\n\nb", 8) == '        """a\n\n        b\n        """'
    long = "word " * 30
    assert max(len(line) for line in docstring(long.strip(), 0, width=40).split("\n")) <= 43  # + the quotes
    assert wrap_line("    indented " * 20, 30) == ["    indented " * 20]
    assert md_code("a|b") == "`a\\|b`" and md_code("x`y") == "``x`y``" and md_code("`z") == "`` `z ``"


# ---------------------------------------------------------- generated code


@pytest.mark.parametrize("name", CONTRACTS)
def test_every_fixture_generates_an_importable_module(tmp_path: Path, name: str) -> None:
    module = fixture_client(tmp_path, name)
    iface = interface(name)
    assert module.MODULE_NAME == name
    assert module.INTERFACE_SHA256 == iface.interface_sha256()
    assert module.SHAPE_SHA256 == iface.shape_sha256()
    assert module.CONTRACT_SHA256 == typed_contract_sha256(name)
    assert Interface.from_shape(module.INTERFACE).shape() == iface.shape()
    assert list(module.EVENT_NAMES) == list(iface.event_names)
    assert set(module.RECORD_TYPES) == {r.name for r in iface.types}
    assert set(module.EVENT_TYPES) == set(iface.event_names)
    base = pascal(name)
    aio, sync = getattr(module, f"Async{base}Client"), getattr(module, f"{base}Client")
    for method in iface.methods:
        py = snake(method.name)
        assert inspect.iscoroutinefunction(getattr(aio, py)), method.name
        assert not inspect.iscoroutinefunction(getattr(sync, py))
        assert _parameters(getattr(aio, py)) == _parameters(getattr(sync, py))
    for member in ("events", "check_compat"):
        assert _parameters(getattr(aio, member)) == _parameters(getattr(sync, member))
    for event in iface.events:
        assert hasattr(aio, f"on_{snake(event.name)}") and hasattr(sync, f"decode_{snake(event.name)}")
    assert typing.get_type_hints(aio.lidl)["return"] is str


def typed_contract_sha256(name: str) -> str:
    from logos_bridge.digest import contract_sha256

    return contract_sha256(lidl_text(name))


def _parameters(fn: Any) -> list[tuple[str, Any, Any, Any]]:
    return [(p.name, p.kind, p.default, p.annotation) for p in inspect.signature(fn).parameters.values()]


def test_generation_is_deterministic_and_lf_only() -> None:
    iface = interface("storage_module")
    first = generate_python(Contract(iface, "a" * 64, "x"), plan_names(iface))
    second = generate_python(Contract(Interface.from_json(iface.to_json()), "a" * 64, "x"), plan_names(iface))
    assert first == second and "\r" not in first and first.endswith("\n") and not first.endswith("\n\n")
    assert "# CONTRACT_SHA256: " + "a" * 64 in first
    assert max(len(line) for line in first.splitlines()) <= 110
    assert "\\u2013" not in first and "–" in first  # the storage descriptions' en dashes stay readable


def test_the_header() -> None:
    iface = interface("mini_module")
    text = generate_python(Contract(iface, None, "ast somewhere", "lidl 9"), plan_names(iface), regen_hint="make it")
    lines = text.splitlines()
    assert lines[:9] == [
        "# GENERATED by logos-bridge-codegen. Do not edit: regenerate instead.",
        "# Contract: mini_module 0.1.0",
        f"# INTERFACE_SHA256: {iface.interface_sha256()}",
        "# CONTRACT_SHA256: unknown (the source had no contract text)",
        f"# SHAPE_SHA256: {iface.shape_sha256()}",
        f"# Codegen format: {typed.CODEGEN_FORMAT}",
        "# Regenerate: make it",
        "# Source: ast somewhere",
        "# Generator: logos-bridge 0.1.0; reader: lidl 9",
    ]
    assert "CONTRACT_SHA256: _typing.Final[str | None] = None" in text


def test_edge_contracts_generate(tmp_path: Path) -> None:
    names = generate(tmp_path / "names", Interface.from_json(edge_doc("names")).with_identity())
    record = names.Str(class_="c", int=True, self="s", meta="m", lidl__init__="i", _bytes=b"", list=[])
    assert record.from_ is None and dataclasses.fields(record)[0].metadata == {"lidl": "class"}
    source = (tmp_path / "names" / "names_module_client.py").read_text()
    assert "from_: _builtins.int | None" in source and "list: _builtins.list[Str]" in source
    assert "async def import_(" in source and "lambda_: str, timeout_: int, self_: bool" in source
    assert typing.get_type_hints(names.Str)["list"] == list[names.Str]
    assert typing.get_type_hints(names.ClassEvent)["meta_"] is str
    assert inspect.iscoroutinefunction(names.AsyncNamesModuleClient.events_)
    assert inspect.iscoroutinefunction(names.AsyncNamesModuleClient.check_compat)
    assert set(names.EVENT_NAMES) == {"class", "module"}

    recursive = generate(tmp_path / "recursive", Interface.from_json(edge_doc("recursive")).with_identity())
    node = recursive.Node(value=1, children=[recursive.Node(value=2, children=[])])
    assert node.parent is None and typing.get_type_hints(recursive.Node)["parent"] == recursive.Node | None

    empty = generate(tmp_path / "empty", Interface.from_json(edge_doc("empty")).with_identity())
    assert empty.EVENT_NAMES == () and empty.EmptyModuleEvent is typing.NoReturn
    assert empty.RECORD_TYPES == {} and empty.EVENT_TYPES == {}

    noreturn = generate(tmp_path / "noreturn", Interface.from_json(edge_doc("noreturn")).with_identity())
    fold = inspect.signature(noreturn.AsyncNoreturnModuleClient.fold)
    assert [p.default for p in fold.parameters.values()][1:3] == [None, None]
    assert fold.return_annotation == "None"

    with pytest.raises(CodegenError, match="cannot map: <array>, <set\\(tstr\\)>, float32, \\{int: tstr\\}"):
        generate(tmp_path / "forward", Interface.from_json(edge_doc("forward")).with_identity())
    forward = generate(tmp_path / "forward", Interface.from_json(edge_doc("forward")).with_identity(),
                       allow_unknown_types=True)
    assert inspect.signature(forward.AsyncForwardModuleClient.put).parameters["when"].annotation == "_typing.Any"
    assert "# Typed as Any (unknown to this generator):" in (tmp_path / "forward" /
                                                               "forward_module_client.py").read_text()

    renamed = generate(tmp_path / "collisions", Interface.from_json(edge_doc("collisions")).with_identity(),
                       renames=(Rename.parse("method:foo_bar=foo_bar_2"), Rename.parse("event:tick=ticked")))
    assert hasattr(renamed.AsyncCollisionsModuleClient, "foo_bar_2") and renamed.TickedEvent


def test_an_edited_module_fails_at_import(tmp_path: Path) -> None:
    fixture_client(tmp_path, "mini_module")
    path = tmp_path / "mini_module_client.py"
    text = path.read_text()
    tampered = tmp_path / "tampered.py"
    tampered.write_text(text.replace('"returns": "Note"', '"returns": "tstr"', 1))
    with pytest.raises(BindingIntegrityError, match="the generated module was edited"):
        load_module(tampered)
    garbled = tmp_path / "garbled.py"
    garbled.write_text(text.replace('"shape": 1,', '"shape": 7,', 1))
    with pytest.raises(BindingIntegrityError, match="not a shape document"):
        load_module(garbled)
    future = tmp_path / "future.py"
    future.write_text(text.replace("_t.require_codegen_format(1)", "_t.require_codegen_format(99)"))
    with pytest.raises(CodegenFormatError):
        load_module(future)


def test_a_broken_generator_is_an_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from logos_bridge.codegen import python as generator

    monkeypatch.setattr(generator, "py_str", lambda text: '"unterminated')
    iface = interface("mini_module")
    with pytest.raises(CodegenError) as excinfo:
        generate_python(Contract(iface), plan_names(iface))
    assert excinfo.value.exit_code == 70 and "does not compile" in str(excinfo.value)


# ------------------------------------------------------------------- CLI


def cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(args), stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


@pytest.fixture
def lidl_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("LOGOS_LGX_CLI", raising=False)
    fake = fake_lidl_cli(tmp_path / "bin")
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(fake))
    return fake


def test_cli_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ast = str(AST_DIR / "mini_module.json")
    for args in ([], ["python"], ["python", "--ast", ast], ["python", "--ast", ast, "--lidl", "x", "-o", "-"],
                 ["frobnicate"], ["python", "--ast", ast, "-o", "-", "--bogus"]):
        assert cli(*args)[0] == 2, args
    assert cli("python", "--lgx", "x.lgx", "-o", "-")[2].endswith("--lgx and --from-bridge need --module\n")
    status, _, err = cli("python", "--ast", ast, "-o", "-", "--rename", "nope")
    assert status == 2 and "expected KIND:NAME=PYTHON_NAME" in err
    assert cli("python", "--ast", ast, "-o", "-", "--rename", "method:missing=x")[0] == 2
    assert cli("python", "--ast", ast, "-o", "-", "--check")[0] == 2
    assert cli("python", "--ast", ast, "-o", "-", "--class-name", "not valid")[0] == 2
    assert cli("--version") == (0, "", "")
    assert "logos-bridge-codegen 0.1.0" in capsys.readouterr().out
    assert "exit status: 0 ok, 1 drift" in build_parser().format_help()


def test_cli_python_from_an_ast(tmp_path: Path) -> None:
    out = tmp_path / "pkg" / "mini.py"
    status, stdout, err = cli("python", "--ast", str(AST_DIR / "mini_module.json"), "-o", str(out),
                              "--regen-hint", "nix run .#regen-goldens")
    assert (status, stdout, err) == (0, "", "")
    data = out.read_bytes()
    assert b"\r\n" not in data and b"# Regenerate: nix run .#regen-goldens" in data
    module = load_module(out)
    assert module.CONTRACT_SHA256 is None
    status, stdout, _ = cli("python", "--ast", str(AST_DIR / "mini_module.json"), "-o", "-",
                            "--regen-hint", "nix run .#regen-goldens")
    assert status == 0 and stdout.encode() == data
    assert cli("python", "--ast", str(AST_DIR / "mini_module.json"), "--module", "other", "-o", "-")[0] == 4


def test_cli_check(tmp_path: Path) -> None:
    ast = str(AST_DIR / "mini_module.json")
    out = tmp_path / "mini.py"
    assert cli("python", "--ast", ast, "-o", str(out))[0] == 0
    assert cli("python", "--ast", ast, "-o", str(out), "--check") == (0, "", "")
    moved = tmp_path / "moved.json"
    moved.write_bytes(Path(ast).read_bytes())
    assert cli("python", "--ast", str(moved), "-o", str(out), "--check")[0] == 0  # only provenance differs
    status, _, err = cli("python", "--ast", str(moved), "-o", str(out), "--check", "--strict-provenance")
    assert status == 1 and "-# Source: ast " in err and "+# Source: ast " in err
    status, _, err = cli("python", "--ast", ast, "-o", str(out), "--check", "--regen-hint", "other")
    assert status == 1 and "is out of date" in err and "+# Regenerate: other" in err
    assert cli("python", "--ast", ast, "-o", str(tmp_path / "missing.py"), "--check")[0] == 1
    assert cli("python", "--ast", ast, "-o", str(tmp_path), "--check")[0] == 1  # a directory
    md = tmp_path / "mini.md"
    assert cli("markdown", "--ast", ast, "-o", str(md))[0] == 0
    assert cli("markdown", "--ast", ast, "-o", str(md), "--check")[0] == 0
    assert cli("markdown", "--ast", str(moved), "-o", str(md), "--check")[0] == 0
    assert cli("markdown", "--ast", str(moved), "-o", str(md), "--check", "--strict-provenance")[0] == 1
    assert cli("markdown", "--ast", ast, "-o", str(md), "--check", "--py-accessor", "x")[0] == 1


def test_cli_sources_and_rejections(tmp_path: Path, lidl_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status, out, err = cli("python", "--lidl", str(LIDL_DIR / "storage_module.lidl"), "-o", "-")
    assert status == 0 and "class AsyncStorageModuleClient:" in out
    assert "note: collectMetrics returns {tstr: any}" in err
    assert f"CONTRACT_SHA256: _typing.Final[str | None] = (\n    \"{typed_contract_sha256('storage_module')}\"" in out
    assert "# Generator: logos-bridge 0.1.0; reader: lidl 0.0.0 (fake)" in out
    status, _, err = cli("python", "--lidl", str(EDGE_DIR / "legacy_void.lidl"), "-o", "-")
    assert status == 0 and "not in canonical form" in err
    assert cli("python", "--lidl", str(tmp_path / "missing.lidl"), "-o", "-")[0] == 3
    broken = tmp_path / "broken.lidl"
    broken.write_text("parse error")
    status, _, err = cli("python", "--lidl", str(broken), "-o", "-")
    assert status == 4 and "broken.lidl:1:1: Expected 'module'" in err
    status, _, err = cli("python", "--lidl", str(EDGE_DIR / "identity_lidl.lidl"), "-o", "-")
    assert status == 4 and "generator-owned built-in" in err
    status, _, err = cli("python", "--lidl", str(EDGE_DIR / "invalid.lidl"), "-o", "-")
    assert status == 4 and "Duplicate method definition 'f'" in err
    status, _, err = cli("python", "--lidl", str(EDGE_DIR / "collisions.lidl"), "-o", "-")
    assert status == 4 and "--rename method:foo_bar=NAME" in err
    assert cli("python", "--lidl", str(EDGE_DIR / "collisions.lidl"), "-o", "-", "--rename",
               "method:foo_bar=foo_bar_2", "--rename", "event:tick=ticked")[0] == 0
    status, _, err = cli("python", "--ast", str(EDGE_DIR / "forward.json"), "-o", "-")
    assert status == 4 and "unknown type kind 'set'" in err
    assert cli("python", "--ast", str(EDGE_DIR / "forward.json"), "-o", "-", "--allow-unknown-types")[0] == 0
    status, out, _ = cli("python", "--lidl", str(EDGE_DIR / "noreturn.lidl"), "--no-identity", "-o", "-")
    assert status == 0 and "async def lidl(" not in out
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(tmp_path / "no-such-lidl"))
    status, _, err = cli("python", "--lidl", str(LIDL_DIR / "mini_module.lidl"), "-o", "-")
    assert status == 3 and "given by LOGOS_LIDL_CLI" in err
    status, _, err = cli("python", "--lidl", str(LIDL_DIR / "mini_module.lidl"), "-o", "-",
                         "--lidl-cli", str(lidl_env))
    assert status == 0


def test_cli_write_and_internal_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ast = str(AST_DIR / "mini_module.json")
    blocker = tmp_path / "file"
    blocker.write_text("")
    status, _, err = cli("python", "--ast", ast, "-o", str(blocker / "sub" / "out.py"))
    assert status == 5 and "cannot write" in err
    status, _, err = cli("python", "--ast", ast, "-o", str(tmp_path))
    assert status == 5
    from logos_bridge.codegen import cli as cli_module

    def explode(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "generate_python", explode)
    status, _, err = cli("python", "--ast", ast, "-o", "-")
    assert status == 70 and "internal error: RuntimeError: boom" in err


def test_cli_digest(lidl_env: Path) -> None:
    ast = str(AST_DIR / "mini_module.json")
    iface = interface("mini_module")
    assert cli("digest", "--ast", ast) == (0, iface.interface_sha256() + "\n", "")
    assert cli("digest", "--ast", ast, "--shape") == (0, iface.shape_sha256() + "\n", "")
    status, _, err = cli("digest", "--ast", ast, "--contract")
    assert status == 3 and "no contract text" in err
    assert cli("digest", "--lidl", str(LIDL_DIR / "mini_module.lidl"), "--contract") == (
        0, typed_contract_sha256("mini_module") + "\n", "")
    assert cli("digest", "--ast", ast, "--shape", "--contract")[0] == 2


def test_cli_markdown(tmp_path: Path) -> None:
    status, out, _ = cli("markdown", "--ast", str(AST_DIR / "storage_module.json"), "-o", "-",
                         "--py-accessor", "client.raw")
    assert status == 0
    assert "| [`uploadUrl`](#method-uploadUrl) | `await client.raw.upload_url(file_path, chunk_size)` |" in out
    assert "```json\n{\n\"log-level\": \"info\"," in out
    assert "A legitimate value shaped like a provider refusal" in out
    assert "`async with client.raw.on_storage_start() as events:` yields `StorageStartEvent`" in out
    status, out, _ = cli("markdown", "--ast", str(AST_DIR / "test_fullapi_ext_cpp.json"), "-o", "-")
    assert "| `maybe` | `maybe: ? tstr` | `maybe: str \\| None` | yes |" in out
    assert "| `v` | `{tstr: Blob}` | `v: Mapping[str, Blob]` |" in out


@pytest.mark.parametrize("mode", ["assets-only", "no-assets-only"])
def test_cli_lgx_with_dependency_contracts(tmp_path: Path, lidl_env: Path, monkeypatch: pytest.MonkeyPatch,
                                          mode: str) -> None:
    assets = contract_assets(tmp_path / "assets", "mini_module", "storage_module")
    lgx = fake_lgx_cli(tmp_path / "lgxbin", mode, assets)
    package = tmp_path / "pkg.lgx"
    package.write_bytes(b"opened only by lgx")
    for name in ("mini_module", "storage_module"):
        status, out, err = cli("python", "--lgx", str(package), "--module", name, "-o", "-", "--lgx-cli", str(lgx))
        assert status == 0, err
        assert f"MODULE_NAME: _typing.Final = \"{name}\"" in out
        assert f"# Source: lgx {package} (module {name}, package fake_pkg 1.0.0)" in out
    status, _, err = cli("python", "--lgx", str(package), "--module", "test_fullapi_cpp", "-o", "-",
                         "--lgx-cli", str(lgx))
    assert status == 3 and "it carries: mini_module, storage_module" in err


def test_cli_lgx_error_paths(tmp_path: Path, lidl_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "pkg.lgx"
    package.write_bytes(b"x")
    assets = contract_assets(tmp_path / "assets", "mini_module")
    args = ("python", "--lgx", str(package), "--module", "mini_module", "-o", "-")
    status, _, err = cli(*args)
    assert status == 3 and "the lgx CLI is not installed" in err
    status, _, err = cli(*args, "--lgx-cli", str(tmp_path / "nope"))
    assert status == 3 and "given by argument" in err
    failing = fake_lgx_cli(tmp_path / "failing", "verify-fails", assets)
    status, _, err = cli(*args, "--lgx-cli", str(failing))
    assert status == 4 and "failed lgx verify" in err
    empty = fake_lgx_cli(tmp_path / "empty", "no-contracts", assets)
    status, _, err = cli(*args, "--lgx-cli", str(empty))
    assert status == 3 and "no contract for 'mini_module'" in err and "it carries: none" in err
    assert cli("python", "--lgx", str(tmp_path / "missing.lgx"), "--module", "m", "-o", "-",
               "--lgx-cli", str(empty))[0] == 3


def test_cli_lgx_with_the_real_clis(tmp_path: Path) -> None:
    lgx, lidl = real_lgx_cli(), real_lidl_cli()
    from test_lidl_sources import build_real_package

    package = build_real_package(tmp_path, lgx)
    out = tmp_path / "storage.py"
    status, _, err = cli("python", "--lgx", str(package), "--module", "storage_module", "-o", str(out),
                         "--lgx-cli", lgx, "--lidl-cli", lidl)
    assert status == 0, err
    module = load_module(out)
    assert module.CONTRACT_SHA256 == typed_contract_sha256("storage_module")
    assert module.INTERFACE_SHA256 == interface("storage_module").interface_sha256()


def test_cli_from_bridge(tmp_path: Path, lidl_env: Path) -> None:
    from logos_bridge.digest import contract_sha256

    with ThreadedFakeBridge() as fake:
        fake.module("legacy", ["ping"], status="untyped")
        doc = json.loads((AST_DIR / "mini_module.json").read_bytes())
        module = fake.module("mini_module", ["put"], interface=doc, status="ok")
        module.contract_sha256 = contract_sha256(lidl_text("mini_module"))
        fake.on_call("mini_module", "lidl", lidl_text("mini_module"))
        status, _, err = cli("python", "--from-bridge", fake.url, "--module", "legacy", "-o", "-")
        assert status == 3 and "legacy does not expose LIDL (status: untyped)" in err
        status, out, err = cli("python", "--from-bridge", fake.url, "--module", "mini_module", "-o", "-")
        assert status == 0, err
        assert f"# Source: bridge {fake.url} (module mini_module)" in out
        assert f"CONTRACT_SHA256: _typing.Final[str | None] = (\n    \"{contract_sha256(lidl_text('mini_module'))}\"" \
            in out
        status, _, err = cli("python", "--from-bridge", fake.url, "--module", "nope", "-o", "-")
        assert status == 3 and "method not found" in err
        fake.module("late", ["x"], status="pending")
        status, _, err = cli("python", "--from-bridge", fake.url, "--module", "late", "-o", "-",
                             "--discovery-wait", "0.1")
        assert status == 3 and "interface_status: pending" in err
        status, _, err = cli("python", "--from-bridge", fake.url, "--module", "mini_module", "-o", "-",
                             "--host-header", "localhost")
        assert status == 0, err
    status, _, err = cli("python", "--from-bridge", "ws://127.0.0.1:9/ws", "--module", "m", "-o", "-")
    assert status == 3 and "cannot connect" in err
    assert cli("python", "--from-bridge", "http://x", "--module", "m", "-o", "-")[0] == 2


def test_the_console_script_runs_as_a_module(tmp_path: Path) -> None:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.run([sys.executable, "-m", "logos_bridge.codegen", "digest", "--ast",
                           str(AST_DIR / "mini_module.json")], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout == interface("mini_module").interface_sha256() + "\n"
    proc = subprocess.run([sys.executable, "-m", "logos_bridge.codegen", "python"], capture_output=True,
                          text=True, env=env, check=False)
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_the_entry_point_is_declared() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'logos-bridge-codegen = "logos_bridge.codegen.cli:main"' in pyproject
