"""Python-only checks of the JavaScript-compatibility layer (no Node counterpart)."""

from __future__ import annotations

from typing import Any

import pytest

from flowwrench.n8n2ap.jscompat import (
    JSONSyntaxError,
    js_round,
    js_string,
    js_trim,
    number_to_string,
    parse_json,
    split_whitespace,
    stringify,
    truthy,
    utf16_length,
    utf16_slice,
)


@pytest.mark.parametrize(("value", "expected"), [
    (1.0, "1"), (5000 / 1000, "5"), (1250 / 1000, "1.25"), (-0.0, "0"), (100.0, "100"), (66.67, "66.67"),
    (0.1 + 0.2, "0.30000000000000004"), (1e21, "1e+21"), (1e-7, "1e-7"), (123456789012345680000.0, "123456789012345680000"),
    (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"), (2**53 + 2, "9007199254740994"),
])
def test_number_to_string_matches_javascript(value: float, expected: str) -> None:
    assert number_to_string(value) == expected


def test_stringify_matches_json_stringify_layout() -> None:
    value = {"b": [], "a": {}, "2": 1.0, "10": [None, True], "1": "é \ud800"}
    assert stringify(value) == '{"1":"é \\ud800","2":1,"10":[null,true],"b":[],"a":{}}'
    assert stringify({"a": [1, {"b": 2}]}, 2) == '{\n  "a": [\n    1,\n    {\n      "b": 2\n    }\n  ]\n}'


def test_parse_json_number_and_key_order_semantics() -> None:
    parsed = parse_json('{"z": 1, "1": 2, "0": 3, "big": 12345678901234567890, "f": 1.0}')
    assert list(parsed) == ["0", "1", "z", "big", "f"]
    assert parsed["big"] == 12345678901234567890.0 and isinstance(parsed["big"], float)
    with pytest.raises(JSONSyntaxError):
        parse_json("NaN")
    deep = "[" * 5000 + "]" * 5000
    value: Any = parse_json(deep)
    for _ in range(4999):
        value = value[0]
    assert value == []


def test_strings_use_utf16_code_units_and_ecmascript_whitespace() -> None:
    assert utf16_length("a😀") == 3
    assert utf16_slice("a😀b", 0, 2) == "a\ud83d"
    assert js_trim(" ﻿ x 　") == "x"
    assert split_whitespace("a b  c") == ["a", "b", "c"]


def test_truthiness_string_conversion_and_rounding() -> None:
    assert truthy([]) and truthy({}) and not truthy(0) and not truthy("") and not truthy(None)
    assert js_string([1, None, "x"]) == "1,,x"
    assert js_string({"a": 1}) == "[object Object]"
    assert js_round(2.5) == 3 and js_round(-2.5) == -2 and js_round(0.49999999999999994) == 0
