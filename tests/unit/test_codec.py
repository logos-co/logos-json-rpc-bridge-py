from __future__ import annotations

import collections
import json
import types
from typing import Any

import pytest

from logos_bridge import codec
from logos_bridge.codec import (
    REJECTION_CODES,
    as_provider_rejection,
    decode_bytes_tag,
    decode_bytes_tags,
    dumps,
    encode_args,
    encode_bytes_tag,
    is_bytes_tag,
    is_logos_result,
    loads,
    rejection_from_result,
    unwrap_result,
)
from logos_bridge.errors import BytesDecodeError, ModuleResultError, ProviderRejection

# --------------------------------------------------------------------------- bytes

BYTES_VECTORS = [
    (b"", ""),
    # RFC 4648 section 10, unpadded
    (b"f", "Zg"),
    (b"fo", "Zm8"),
    (b"foo", "Zm9v"),
    (b"foob", "Zm9vYg"),
    (b"fooba", "Zm9vYmE"),
    (b"foobar", "Zm9vYmFy"),
    # the url alphabet
    (b"\xfb\xff", "-_8"),
    (b"\xfb\xef\xbe", "----"),
    (b"\xff\xff\xff", "____"),
    # logos-rust-sdk bytes.rs / logos-protocol test_codec.cpp parity vectors
    (b"x\x00y", "eAB5"),
    (bytes([0x00, 0x7F, 0x80, 0xFF]), "AH-A_w"),
]


@pytest.mark.parametrize(("raw", "text"), BYTES_VECTORS)
def test_bytes_tag_vectors(raw: bytes, text: str) -> None:
    assert encode_bytes_tag(raw) == {"_bytes": text}
    assert decode_bytes_tag({"_bytes": text}) == raw


def test_every_byte_value_round_trips() -> None:
    data = bytes(range(256)) * 3
    assert decode_bytes_tag(encode_bytes_tag(data)) == data


def test_bytes_like_inputs_are_encoded() -> None:
    assert encode_bytes_tag(bytearray(b"\xfb\xff")) == {"_bytes": "-_8"}
    assert encode_bytes_tag(memoryview(b"\xfb\xff")) == {"_bytes": "-_8"}


def test_trailing_padding_is_tolerated_like_the_checked_cpp_decoder() -> None:
    assert decode_bytes_tag({"_bytes": "AH-A_w=="}) == bytes([0x00, 0x7F, 0x80, 0xFF])


@pytest.mark.parametrize("bad", ["AH+A/w", "A", "AAAAA", "Zm 9v", "Zg=x", "\N{LATIN SMALL LETTER E WITH ACUTE}", "Zg\n"])
def test_strict_decode_refuses_invalid_base64url(bad: str) -> None:
    with pytest.raises(BytesDecodeError):
        decode_bytes_tag({"_bytes": bad})


@pytest.mark.parametrize("not_a_tag", [{"_bytes": 42}, {"_bytes": "AA", "x": 1}, "AA", ["AA"], None, {}])
def test_decode_bytes_tag_needs_the_exact_shape(not_a_tag: Any) -> None:
    assert not is_bytes_tag(not_a_tag)
    with pytest.raises(BytesDecodeError):
        decode_bytes_tag(not_a_tag)


def test_decode_bytes_tags_replaces_only_exact_tags_at_any_depth() -> None:
    value = {
        "blob": {"_bytes": "AQ"},
        "list": [{"_bytes": "-_8"}, [{"_bytes": ""}]],
        "data": {"_bytes": "AA", "x": 1},
        "number": {"_bytes": 5},
        "text": "AQ",
    }
    decoded = decode_bytes_tags(value)
    assert decoded == {
        "blob": b"\x01",
        "list": [b"\xfb\xff", [b""]],
        "data": {"_bytes": "AA", "x": 1},
        "number": {"_bytes": 5},
        "text": "AQ",
    }
    assert value["blob"] == {"_bytes": "AQ"}  # the input is not modified


def test_decode_bytes_tags_is_strict_at_depth() -> None:
    with pytest.raises(BytesDecodeError):
        decode_bytes_tags({"a": [{"_bytes": "AH+A"}]})


# ---------------------------------------------------------------------------- JSON


def test_dumps_is_compact_raw_utf8() -> None:
    assert dumps({"a": "\N{EN DASH}", "b": [1, 2.5, None, True]}) == '{"a":"\N{EN DASH}","b":[1,2.5,null,true]}'.encode()


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (2**63 - 1, "9223372036854775807"),
        (-(2**63), "-9223372036854775808"),
        (2**64 - 1, "18446744073709551615"),
        (0, "0"),
        (-1, "-1"),
    ],
)
def test_integer_extremes_are_exact_text(value: int, text: str) -> None:
    assert dumps([value]) == f"[{text}]".encode()
    assert loads(f"[{text}]") == [value]
    assert isinstance(loads(text), int)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_dumps_refuses_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        dumps({"x": [value]})


@pytest.mark.parametrize("value", ["\ud800", ["ok", "x\udfffy"], {"\ud83d": 1}])
def test_dumps_refuses_lone_surrogates(value: Any) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        dumps(value)


def test_a_surrogate_pair_written_as_one_code_point_is_fine() -> None:
    assert dumps("\U0001F600") == '"\U0001F600"'.encode()


@pytest.mark.parametrize("value", [{1: "a"}, {None: 1}, {True: 1}, {1.5: 1}, [{"a": {2: 3}}], ({(1, 2): 0},)])
def test_dumps_refuses_non_str_keys(value: Any) -> None:
    with pytest.raises(TypeError, match="keys must be str"):
        dumps(value)


@pytest.mark.parametrize("value", [{1, 2}, b"x", object()])
def test_dumps_refuses_unsupported_types(value: Any) -> None:
    with pytest.raises(TypeError):
        dumps({"v": value})


def test_dumps_detects_cycles() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        dumps(cycle)


def test_shared_subtrees_are_not_cycles() -> None:
    shared = {"k": 1}
    assert dumps([shared, shared]) == b'[{"k":1},{"k":1}]'


@pytest.mark.parametrize("text", ["NaN", "[Infinity]", '{"a": -Infinity}'])
def test_loads_refuses_non_standard_constants(text: str) -> None:
    with pytest.raises(ValueError):
        loads(text)


def test_loads_accepts_bytes() -> None:
    assert loads('{"a":"\N{EN DASH}"}'.encode()) == {"a": "\N{EN DASH}"}


# ---------------------------------------------------------------------- encode_args


def test_encode_args_converts_bytes_tuples_and_mappings() -> None:
    args = [b"\x01\x02", (1, 2), {"k": bytearray(b"\xff")}, collections.OrderedDict(z=1)]
    assert encode_args(args) == [{"_bytes": "AQI"}, [1, 2], {"k": {"_bytes": "_w"}}, {"z": 1}]
    assert encode_args([types.MappingProxyType({"a": memoryview(b"\x00")})]) == [{"a": {"_bytes": "AA"}}]


def test_encode_args_keeps_scalars() -> None:
    assert encode_args([True, 0, 2**64 - 1, -1.5, "s", None]) == [True, 0, 2**64 - 1, -1.5, "s", None]


def test_a_valid_pre_encoded_tag_passes() -> None:
    assert encode_args([{"_bytes": "AH-A_w"}, {"_bytes": ""}]) == [{"_bytes": "AH-A_w"}, {"_bytes": ""}]


@pytest.mark.parametrize("bad", ["AH+A_w", "AH/A_w", "AH-A_w==", "A", "AAAAA", " AA", "AA\n"])
def test_a_bad_pre_encoded_tag_is_refused(bad: str) -> None:
    with pytest.raises(BytesDecodeError):
        encode_args([{"_bytes": bad}])
    assert issubclass(BytesDecodeError, ValueError)


@pytest.mark.parametrize("value", [{"_bytes": "AA", "x": 1}, {"_bytes": 5}, {"_bytes": None}])
def test_the_bytes_key_is_reserved(value: Any) -> None:
    with pytest.raises(ValueError, match="reserved"):
        encode_args([{"outer": value}])


@pytest.mark.parametrize(("value", "error"), [({1: 2}, TypeError), (object(), TypeError), ({1}, TypeError),
                                               (float("nan"), ValueError), ([float("inf")], ValueError)])
def test_encode_args_refuses_what_json_cannot_carry(value: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        encode_args([value])


# ------------------------------------------------------------ provider rejections
# Mirrors logos-rust-sdk src/args.rs (as_dispatch_rejection and its tests), case by case.


def dispatch_failed(origin: str, message: str) -> dict[str, str]:
    return {"code": "dispatch_failed", "message": message, "origin": origin}


def invalid_args(origin: str, expected: int, got: int) -> dict[str, str]:
    return {"code": "invalid_args", "message": f"expected {expected} arguments, got {got}", "origin": origin}


def test_the_closed_set_matches_rust() -> None:
    assert REJECTION_CODES == ("dispatch_failed", "invalid_args", "unknown_method")


def test_a_rejection_object_is_recognised_and_yields_its_message() -> None:
    value = dispatch_failed("my_module", "expected integer at arg0, got string")
    assert as_provider_rejection(value) == "expected integer at arg0, got string"


@pytest.mark.parametrize("value", [0, "", [], None, False])
def test_a_plain_value_is_not_a_rejection(value: Any) -> None:
    assert as_provider_rejection(value) is None


@pytest.mark.parametrize("code", REJECTION_CODES)
def test_every_rejection_code_is_detected(code: str) -> None:
    assert as_provider_rejection({"code": code, "message": "m", "origin": "o"}) == "m"


def test_a_real_invalid_args_object_is_a_rejection() -> None:
    assert as_provider_rejection(invalid_args("my_module", 4, 2)) == "expected 4 arguments, got 2"


def test_a_user_map_never_false_matches() -> None:
    # Right code, wrong arity: 2 keys and 4 keys.
    assert as_provider_rejection({"code": "dispatch_failed", "message": "m"}) is None
    assert as_provider_rejection({"code": "dispatch_failed", "message": "m", "origin": "o", "extra": 1}) is None
    # Right arity and keys, code outside the closed set.
    for code in ["", "ok", "not_found", "DISPATCH_FAILED", "dispatch_failed ", "invalid_argument",
                 "unknown_methods", "user_error"]:
        assert as_provider_rejection({"code": code, "message": "m", "origin": "o"}) is None, code
    # Right shape and a good code, but a non-string value in each slot.
    for code in REJECTION_CODES:
        assert as_provider_rejection({"code": code, "message": 7, "origin": "o"}) is None, code
        assert as_provider_rejection({"code": code, "message": "m", "origin": None}) is None, code
        assert as_provider_rejection({"code": 1, "message": "m", "origin": "o"}) is None, code


def test_python_only_shapes_stay_data() -> None:
    assert as_provider_rejection({"code": True, "message": "m", "origin": "o"}) is None
    assert as_provider_rejection({"code": "dispatch_failed", "message": "m", "other": "o"}) is None
    assert as_provider_rejection(types.MappingProxyType(dispatch_failed("o", "m"))) is None
    assert as_provider_rejection([["code", "dispatch_failed"]]) is None


def test_rejection_from_result_builds_the_exception() -> None:
    rejection = rejection_from_result(dispatch_failed("m", "bad"), module="m", method="f")
    assert isinstance(rejection, ProviderRejection)
    assert (rejection.code, rejection.message, rejection.origin) == ("dispatch_failed", "bad", "m")
    assert (rejection.module, rejection.method) == ("m", "f")
    assert "m.f" in str(rejection)
    assert rejection_from_result({"code": "mine", "message": "x", "origin": "y"}) is None


# --------------------------------------------------------------------- LogosResult


@pytest.mark.parametrize(
    "value",
    [
        {"success": True, "value": 1, "error": None},
        {"success": False, "value": None, "error": "boom"},
        {"success": True, "value": {"_bytes": "AA"}, "error": None},
    ],
)
def test_is_logos_result_accepts_the_canonical_shape(value: Any) -> None:
    assert is_logos_result(value)


@pytest.mark.parametrize(
    "value",
    [
        {"success": True, "value": 1},
        {"success": True, "value": 1, "error": None, "extra": 0},
        {"success": "true", "value": 1, "error": None},
        {"success": 1, "value": 1, "error": None},
        {"success": False, "value": None, "error": 5},
        [True, 1, None],
        None,
    ],
)
def test_is_logos_result_refuses_other_shapes(value: Any) -> None:
    assert not is_logos_result(value)


def test_unwrap_result_returns_the_value() -> None:
    assert unwrap_result({"success": True, "value": [1, 2], "error": None}) == [1, 2]


def test_unwrap_result_raises_on_failure() -> None:
    result = {"success": False, "value": None, "error": "the widget was not frobnicated"}
    with pytest.raises(ModuleResultError, match="frobnicated") as excinfo:
        unwrap_result(result)
    assert excinfo.value.error == "the widget was not frobnicated"
    assert excinfo.value.result is result


def test_unwrap_result_with_a_null_error() -> None:
    with pytest.raises(ModuleResultError, match="without an error message") as excinfo:
        unwrap_result({"success": False, "value": None, "error": None})
    assert excinfo.value.error is None


@pytest.mark.parametrize("value", [True, {"value": 1}, "ok", None])
def test_unwrap_result_refuses_non_results(value: Any) -> None:
    with pytest.raises(TypeError):
        unwrap_result(value)


def test_module_exports() -> None:
    assert codec.BYTES_KEY == "_bytes"
    assert json.loads(dumps(encode_args([b"\x00"]))) == [{"_bytes": "AA"}]
