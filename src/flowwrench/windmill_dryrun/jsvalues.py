"""JavaScript value semantics needed for byte-compatible reports.

The Node.js tool hashes and prints ``JSON.stringify`` output, compares values
with ``util.isDeepStrictEqual`` and iterates objects in JavaScript property
order. This module reproduces exactly those behaviours for JSON-shaped Python
values (``dict``/``list``/``str``/``int``/``float``/``bool``/``None``).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any, Final

MAX_SAFE_INTEGER: Final = 2**53 - 1
_MAX_ARRAY_INDEX: Final = 2**32 - 1


class _Undefined:
    """Sentinel for JavaScript ``undefined`` (an absent value)."""

    __slots__ = ()
    _instance: _Undefined | None = None

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED: Final[Any] = _Undefined()

# JavaScript String.prototype.trim() whitespace (WhiteSpace + LineTerminator).
_JS_WHITESPACE: Final = (
    "\t\n\v\f\r          "
    "        　﻿"
)
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
_INDEX_KEY: Final = re.compile(r"(?:0|[1-9][0-9]*)\Z")


# --------------------------------------------------------------------------
# Type predicates (typeof / Number.isInteger / Number.isSafeInteger)


def is_number(value: Any) -> bool:
    """``typeof value === 'number'``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """``Number.isFinite(value)``."""
    if not is_number(value):
        return False
    return isinstance(value, int) or math.isfinite(value)


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    if not is_finite_number(value):
        return False
    return isinstance(value, int) or float(value).is_integer()


def is_safe_integer(value: Any) -> bool:
    """``Number.isSafeInteger(value)``."""
    return is_integer(value) and abs(value) <= MAX_SAFE_INTEGER


def strict_equals(left: Any, right: Any) -> bool:
    """``left === right`` for JSON primitives (objects compare by identity)."""
    if is_number(left) and is_number(right):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is right


def js_add(left: int | float, right: int | float) -> int | float:
    """``left + right`` with IEEE-754 double rounding beyond 2**53."""
    total = left + right
    if isinstance(total, int) and abs(total) > MAX_SAFE_INTEGER:
        return float(left) + float(right)
    return total


def to_number(text: str) -> int | float:
    """``Number(text)`` for a string of ASCII digits."""
    if len(text) <= 15:
        return int(text)
    value = float(text)
    # A safe integer is exact as a double; int(text) could hit the 4300-digit limit (leading zeros).
    return int(value) if abs(value) <= MAX_SAFE_INTEGER else value


# --------------------------------------------------------------------------
# Strings


def js_length(text: str) -> int:
    """``text.length``: the number of UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def js_trim(text: str) -> str:
    """``text.trim()``."""
    return text.strip(_JS_WHITESPACE)


def combine_surrogates(text: str) -> str:
    """Join UTF-16 surrogate pairs into code points, keeping lone surrogates."""
    if not _SURROGATE.search(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def replace_lone_surrogates(text: str) -> str:
    """Node writes a lone surrogate as U+FFFD when encoding UTF-8."""
    return _SURROGATE.sub("�", text)


def number_to_string(value: int | float) -> str:
    """``Number.prototype.toString()`` (ECMA-262 Number::toString, radix 10)."""
    if isinstance(value, int):
        if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            return str(value)
        try:
            value = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() yields the shortest round-tripping digits, like JavaScript.
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    significant = digits.lstrip("0")
    n = len(whole) + (int(exponent) if exponent else 0) - (len(digits) - len(significant))
    s = significant.rstrip("0")
    k = len(s)
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        e = n - 1
        body = (s if k == 1 else s[0] + "." + s[1:]) + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def js_string(value: Any) -> str:
    """``String(value)`` for JSON-shaped values (used in diagnostics)."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if is_number(value):
        return number_to_string(value)
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None or item is UNDEFINED else js_string(item) for item in value)
    return "[object Object]"


# --------------------------------------------------------------------------
# Property order


def _is_index_key(key: str) -> bool:
    return len(key) <= 10 and bool(_INDEX_KEY.match(key)) and int(key) < _MAX_ARRAY_INDEX


def js_keys(mapping: dict[str, Any]) -> list[str]:
    """``Object.keys(mapping)``: array-index keys ascending, then insertion order."""
    keys = list(mapping)
    indices = [key for key in keys if _is_index_key(key)]
    if not indices:
        return keys
    indices.sort(key=int)
    return indices + [key for key in keys if not _is_index_key(key)]


def js_items(mapping: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """``Object.entries(mapping)``."""
    for key in js_keys(mapping):
        yield key, mapping[key]


# --------------------------------------------------------------------------
# JSON.stringify / JSON.parse


def quote(text: str) -> str:
    """``JSON.stringify(text)`` for a string (well-formed JSON.stringify)."""
    encoded = json.dumps(text, ensure_ascii=False)
    if _SURROGATE.search(encoded):
        encoded = _SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", encoded)
    return encoded


def _json_number(value: int | float) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return "null"
    return number_to_string(value)


def _emit(value: Any, out: list[str], indent: int | None, level: int) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, (int, float)):
        out.append(_json_number(value))
    elif isinstance(value, dict):
        keys = [key for key in js_keys(value) if value[key] is not UNDEFINED]
        if not keys:
            out.append("{}")
            return
        out.append("{")
        inner = None if indent is None else "\n" + " " * (indent * (level + 1))
        for position, key in enumerate(keys):
            if position:
                out.append(",")
            if inner is None:
                out.append(quote(key) + ":")
            else:
                out.append(inner + quote(key) + ": ")
            _emit(value[key], out, indent, level + 1)
        if indent is not None:
            out.append("\n" + " " * (indent * level))
        out.append("}")
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append("[]")
            return
        out.append("[")
        inner = None if indent is None else "\n" + " " * (indent * (level + 1))
        for position, item in enumerate(value):
            if position:
                out.append(",")
            if inner is not None:
                out.append(inner)
            if item is UNDEFINED:
                out.append("null")
            else:
                _emit(item, out, indent, level + 1)
        if indent is not None:
            out.append("\n" + " " * (indent * level))
        out.append("]")
    else:
        raise TypeError(f"cannot serialize {type(value).__name__} as JSON")


def stringify(value: Any, indent: int | None = None) -> str | None:
    """``JSON.stringify(value, null, indent)``; ``None`` for ``undefined``."""
    if value is UNDEFINED:
        return None
    out: list[str] = []
    _emit(value, out, indent, 0)
    return "".join(out)


def _parse_int(text: str) -> int | float:
    if text == "-0":
        return -0.0
    digits = text.lstrip("-")
    if len(digits) <= 15:
        return int(text)
    value = float(text)
    return int(text) if abs(value) <= MAX_SAFE_INTEGER else value


def _reject_constant(name: str) -> Any:
    raise ValueError(f'"{name}" is not valid JSON')


def parse_json(text: str, object_pairs_hook: Any = None) -> Any:
    """``JSON.parse(text)``: doubles for unsafe integers, -0, no NaN/Infinity."""
    return json.loads(
        text,
        parse_int=_parse_int,
        parse_constant=_reject_constant,
        object_pairs_hook=object_pairs_hook,
    )


# --------------------------------------------------------------------------
# util.isDeepStrictEqual


def _negative_zero(value: int | float) -> bool:
    return isinstance(value, float) and value == 0 and math.copysign(1.0, value) < 0


def deep_equal(left: Any, right: Any) -> bool:
    """``util.isDeepStrictEqual`` for JSON-shaped values."""
    if left is UNDEFINED or right is UNDEFINED:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if is_number(left) and is_number(right):
        if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
            return True
        return left == right and _negative_zero(left) == _negative_zero(right)
    if left is None or right is None:
        return left is right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(deep_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        # Own keys count even when their value is undefined (strict mode).
        if len(left) != len(right) or set(left) != set(right):
            return False
        return all(deep_equal(left[key], right[key]) for key in left)
    return False


def clone(value: Any) -> Any:
    """``structuredClone`` for JSON-shaped values."""
    if isinstance(value, dict):
        return {key: clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clone(item) for item in value]
    return value
