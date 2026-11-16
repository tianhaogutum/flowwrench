"""JavaScript-compatible JSON and value semantics.

The Node.js tool relies on ``JSON.parse``/``JSON.stringify`` and on several
JavaScript value rules (truthiness, UTF-16 string ordering, integer-like
property ordering, number formatting).  This module reproduces exactly the
subset the tool needs so that outputs, conflict IDs and hashes match the
original implementation byte for byte.

Value model after :func:`parse`:

* JSON objects -> ``dict`` (insertion order = source order), arrays -> ``list``.
* Numbers are JavaScript doubles.  Integral values with magnitude below 2**53
  are represented as ``int`` (``1.0``, ``1e2`` and ``-0`` all become ``int``);
  everything else is a ``float`` (possibly ``inf`` on overflow, which callers
  reject as non-finite, as the Node tool does).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

__all__ = [
    "JSONParseError",
    "is_js_integer",
    "is_number",
    "js_keys",
    "js_number",
    "js_to_string",
    "js_trim",
    "js_truthy",
    "parse",
    "sorted_js",
    "strict_equal",
    "stringify",
    "to_utf8",
    "utf16_key",
]

MAX_SAFE = 2**53
_ARRAY_INDEX_LIMIT = 2**32 - 1
_INDEX_RE = re.compile(r"0|[1-9][0-9]*")
_ESCAPE_RE = re.compile('[\x00-\x1f"\\\\\ud800-\udfff]')
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SHORT_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}
# ECMAScript WhiteSpace + LineTerminator code points (String.prototype.trim).
_JS_WHITESPACE = (
    "\t\n\v\f\r          "
    "        　﻿"
)


class JSONParseError(ValueError):
    """Raised for input that ``JSON.parse`` would reject."""


# --------------------------------------------------------------------------
# Parsing


def _parse_int(text: str) -> int | float:
    value = float(text)  # float() has no digit limit; huge values become inf
    if abs(value) < MAX_SAFE:
        return int(text)
    return value


def _parse_float(text: str) -> int | float:
    value = float(text)
    if math.isfinite(value) and value.is_integer() and abs(value) < MAX_SAFE:
        return int(value)
    return value


def _parse_constant(name: str) -> Any:
    raise JSONParseError(f"invalid JSON literal {name}")


def parse(text: str) -> Any:
    """Parse *text* like ``JSON.parse`` (NaN/Infinity literals rejected)."""
    return json.loads(
        text,
        parse_int=_parse_int,
        parse_float=_parse_float,
        parse_constant=_parse_constant,
    )


# --------------------------------------------------------------------------
# Value helpers


def is_number(value: Any) -> bool:
    """``typeof value === 'number'``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_js_integer(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness for JSON values (``[]`` and ``{}`` are truthy)."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if is_number(value):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def utf16_key(text: str) -> bytes:
    """Sort key reproducing JavaScript's UTF-16 code-unit string comparison."""
    return text.encode("utf-16-be", "surrogatepass")


def sorted_js(strings: Any) -> list[str]:
    """``[...strings].sort()`` with JavaScript string ordering."""
    return sorted(strings, key=utf16_key)


def _is_array_index(key: str) -> bool:
    return _INDEX_RE.fullmatch(key) is not None and int(key) < _ARRAY_INDEX_LIMIT


def js_keys(obj: dict[str, Any]) -> list[str]:
    """``Object.keys`` order: array-index keys ascending, then insertion order."""
    indices: list[str] | None = None
    others: list[str] = []
    for key in obj:
        if key and "0" <= key[0] <= "9" and _is_array_index(key):
            if indices is None:
                indices = []
            indices.append(key)
        else:
            others.append(key)
    if indices is None:
        return others
    indices.sort(key=int)
    return indices + others


def strict_equal(a: Any, b: Any) -> bool:
    """``util.isDeepStrictEqual`` restricted to JSON values."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    return a is b


def js_trim(text: str) -> str:
    """``String.prototype.trim``."""
    return text.strip(_JS_WHITESPACE)


def to_utf8(text: str) -> bytes:
    """Encode like Node.js does for strings: lone surrogates become U+FFFD."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return _SURROGATE_RE.sub("�", text).encode("utf-8")


# --------------------------------------------------------------------------
# Number formatting (Number.prototype.toString, radix 10)


def js_number(value: int | float) -> str:
    """Format a finite number exactly as JavaScript's ``String(number)``."""
    if isinstance(value, int) and not isinstance(value, bool):
        if -MAX_SAFE <= value <= MAX_SAFE:
            return str(value)
        try:
            value = float(value)
        except OverflowError:
            return "-Infinity" if value < 0 else "Infinity"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))  # shortest round-trip digits, like ECMAScript
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + exponent
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
        body = digits + exp if k == 1 else f"{digits[0]}.{digits[1:]}{exp}"
    return sign + body


def js_to_string(value: Any) -> str:
    """``String(value)`` / template-literal conversion for JSON values."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_number(value):
        return js_number(value)
    if isinstance(value, list):
        return ",".join("" if item is None else js_to_string(item) for item in value)
    return "[object Object]"


# --------------------------------------------------------------------------
# Serialization (JSON.stringify)


def _escape(match: re.Match[str]) -> str:
    char = match.group(0)
    short = _SHORT_ESCAPES.get(char)
    if short is not None:
        return short
    return f"\\u{ord(char):04x}"


def quote(text: str) -> str:
    """``JSON.stringify(string)`` (well-formed: lone surrogates escaped)."""
    return '"' + _ESCAPE_RE.sub(_escape, text) + '"'


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)`` for JSON-compatible values."""
    gap = " " * indent if indent else ""
    parts: list[str] = []
    _encode(value, gap, "", parts)
    return "".join(parts)


def _encode(value: Any, gap: str, current: str, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(quote(value))
    elif is_number(value):
        if isinstance(value, float) and not math.isfinite(value):
            out.append("null")
        else:
            out.append(js_number(value))
    elif isinstance(value, list):
        if not value:
            out.append("[]")
            return
        inner = current + gap
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            if gap:
                out.append("\n" + inner)
            _encode(item, gap, inner, out)
        if gap:
            out.append("\n" + current)
        out.append("]")
    elif isinstance(value, dict):
        keys = js_keys(value)
        if not keys:
            out.append("{}")
            return
        inner = current + gap
        out.append("{")
        for i, key in enumerate(keys):
            if i:
                out.append(",")
            if gap:
                out.append("\n" + inner)
            out.append(quote(key))
            out.append(": " if gap else ":")
            _encode(value[key], gap, inner, out)
        if gap:
            out.append("\n" + current)
        out.append("}")
    else:  # pragma: no cover - programming error
        raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")
