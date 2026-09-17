from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

import pytest

from logos_bridge.digest import canonical_json, contract_sha256, interface_sha256, shape_sha256

VECTORS: list[tuple[Any, str]] = [
    ({"a": "\N{EN DASH}"}, '{"a":"\N{EN DASH}"}'),  # U+2013 stays raw (storage descriptions use it)
    ({"s": 'a\nb"c\\'}, '{"s":"a\\nb\\"c\\\\"}'),
    ({"s": "\x1f\x00\t"}, '{"s":"\\u001f\\u0000\\t"}'),  # lowercase hex escapes
    ({"s": "\U00002028\U00002029"}, '{"s":"\U00002028\U00002029"}'),  # raw, not escaped
    ({"s": "\x7f/"}, '{"s":"\x7f/"}'),  # DEL and / unescaped
    ({"b": {"d": 1, "c": 2}, "a": [3, {"f": 1, "e": 2}]}, '{"a":[3,{"e":2,"f":1}],"b":{"c":2,"d":1}}'),
    (
        {"u": 2**64 - 1, "i": -(2**63), "f": 1.5, "t": True, "n": None, "e": [], "o": {}},
        '{"e":[],"f":1.5,"i":-9223372036854775808,"n":null,"o":{},"t":true,"u":18446744073709551615}',
    ),
    ({"z": 1, "\N{LATIN SMALL LETTER E WITH ACUTE}": 2, "a": 3, "\U0001F600": 4, "\U0000fffd": 5, "Z": 6},
     '{"Z":6,"a":3,"z":1,"\N{LATIN SMALL LETTER E WITH ACUTE}":2,"\U0000fffd":5,"\U0001F600":4}'),
]


@pytest.mark.parametrize(("value", "text"), VECTORS)
def test_canonical_json_vectors(value: Any, text: str) -> None:
    assert canonical_json(value) == text.encode("utf-8")


@pytest.mark.parametrize(("value", "_text"), VECTORS)
def test_canonical_json_is_the_contract_expression(value: Any, _text: str) -> None:
    expected = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert canonical_json(value) == expected


def test_code_point_order_is_utf8_byte_order() -> None:
    # The bridge sorts keys as UTF-8 bytes (std::map<std::string>); Python sorts code points.
    keys = ["z", "\N{LATIN SMALL LETTER E WITH ACUTE}", "a", "\U0001F600", "\U0000fffd", "Z", "\N{EN DASH}", "\U00010000", "\U0000ffff"]
    assert sorted(keys) == sorted(keys, key=lambda k: k.encode("utf-8"))
    decoded = json.loads(canonical_json({k: 0 for k in keys}))
    assert list(decoded) == sorted(keys, key=lambda k: k.encode("utf-8"))


@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": float("inf")}])
def test_canonical_json_refuses_non_finite_numbers(value: Any) -> None:
    with pytest.raises(ValueError):
        canonical_json(value)


def test_canonical_json_refuses_lone_surrogates() -> None:
    with pytest.raises(UnicodeEncodeError):
        canonical_json({"x": "\ud800"})


def test_interface_sha256() -> None:
    assert interface_sha256({}) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    doc = {"name": "m", "methods": [{"name": "f", "description": "a \N{EN DASH} b"}]}
    assert interface_sha256(doc) == hashlib.sha256(canonical_json(doc)).hexdigest()
    reordered = {"methods": [{"description": "a \N{EN DASH} b", "name": "f"}], "name": "m"}
    assert interface_sha256(reordered) == interface_sha256(doc)
    assert interface_sha256({"name": "m "}) != interface_sha256({"name": "m"})


def test_contract_sha256_hashes_the_exact_bytes() -> None:
    assert contract_sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    text = "module m {\n  method f() -> tstr; // \N{EN DASH}\n}\n"
    assert contract_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert contract_sha256(text.encode("utf-8")) == contract_sha256(text)
    assert contract_sha256(text) != contract_sha256(text.rstrip("\n"))


# The bridge owns these vectors (tests/fixtures/SOURCES.md); both sides must agree byte for byte.
BRIDGE_VECTORS = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "interface-digest-vectors.json").read_bytes()
)


def test_the_bridge_vectors_file_is_complete() -> None:
    assert len(BRIDGE_VECTORS) == 13
    assert {v["name"] for v in BRIDGE_VECTORS} >= {"en_dash", "raw_u2028_u2029", "storage_module_identity_ast"}


@pytest.mark.parametrize("vector", BRIDGE_VECTORS, ids=[v["name"] for v in BRIDGE_VECTORS])
def test_canonical_json_matches_every_bridge_vector(vector: dict[str, Any]) -> None:
    assert canonical_json(vector["value"]) == vector["canonical"].encode("utf-8")
    assert interface_sha256(vector["value"]) == vector["sha256"]
    assert hashlib.sha256(vector["canonical"].encode("utf-8")).hexdigest() == vector["sha256"]


def test_the_storage_vector_is_the_storage_fixture() -> None:
    vector = next(v for v in BRIDGE_VECTORS if v["name"] == "storage_module_identity_ast")
    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ast" / "storage_module.json"
    assert fixture.read_bytes() == vector["canonical"].encode("utf-8") + b"\n"


def test_shape_sha256_accepts_every_form() -> None:
    from logos_bridge.lidl import Interface

    doc = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ast" / "mini_module.json")
                     .read_bytes())
    iface = Interface.from_json(doc)
    digest = shape_sha256(iface)
    assert digest == shape_sha256(doc) == shape_sha256(iface.shape())
    assert digest == hashlib.sha256(canonical_json(iface.shape())).hexdigest()
    assert digest != interface_sha256(doc)
