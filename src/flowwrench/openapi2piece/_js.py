"""JavaScript value semantics needed for byte-identical parity with the Node.js tool.

The original generator relies on a handful of ECMAScript behaviours that differ
from Python defaults: ``JSON.stringify`` formatting (number rendering, string
escapes, integer-like key order), UTF-16 code-unit string sorting, ``??``/``||``
semantics, ``undefined`` versus ``null`` and ``decodeURIComponent``.  Every
helper here reproduces exactly the subset the generator uses.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "UNDEFINED",
    "JsTypeError",
    "UriError",
    "coalesce",
    "decode_uri_component",
    "get",
    "is_finite_number",
    "is_integer_value",
    "is_nullish",
    "is_number",
    "is_safe_integer",
    "js_keys",
    "js_sorted",
    "js_string",
    "js_stringify",
    "js_trim",
    "js_truthy",
    "js_typeof",
    "number_to_string",
    "object_entries",
    "object_keys",
    "optional_get",
    "same_value_zero_unique",
    "to_utf8",
    "utf16_key",
    "utf8_length",
    "JS_WHITESPACE_CLASS",
]

MAX_SAFE_INTEGER = 2**53 - 1


class _Undefined:
    """Singleton standing in for JavaScript ``undefined`` (distinct from ``None``/``null``)."""

    _instance: _Undefined | None = None
    __slots__ = ()

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Undefined:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Undefined:
        return self

    def __reduce__(self) -> str:
        return "UNDEFINED"


UNDEFINED: Any = _Undefined()


class JsTypeError(TypeError):
    """Mirror of a V8 ``TypeError`` raised by property access on null/undefined."""


class UriError(ValueError):
    """Mirror of ``URIError: URI malformed``."""


# ECMAScript WhiteSpace + LineTerminator: the set matched by ``\s`` and removed by ``trim``.
_JS_WHITESPACE = "\t\n\x0b\x0c\r \xa0                　﻿"
JS_WHITESPACE_CLASS = "\t\n\x0b\x0c\r \xa0  -     　﻿"


def is_nullish(value: Any) -> bool:
    return value is None or value is UNDEFINED


def coalesce(*values: Any) -> Any:
    """``a ?? b ?? c``: the first value that is neither null nor undefined, else the last one."""
    for value in values[:-1]:
        if not is_nullish(value):
            return value
    return values[-1]


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """``typeof v === 'number' && Number.isFinite(v)``."""
    if not is_number(value):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value)


def is_integer_value(value: Any) -> bool:
    """``Number.isInteger``."""
    if not is_number(value):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value) and value.is_integer()


def is_safe_integer(value: Any) -> bool:
    """``Number.isSafeInteger``."""
    return is_integer_value(value) and abs(value) <= MAX_SAFE_INTEGER


def js_typeof(value: Any) -> str:
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if callable(value):
        return "function"
    return "object"


def js_truthy(value: Any) -> bool:
    if value is UNDEFINED or value is None or value is False:
        return False
    if value is True:
        return True
    if is_number(value):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def number_to_string(value: int | float) -> str:
    """ECMAScript ``Number::toString`` (radix 10), also used by ``JSON.stringify`` for finite numbers."""
    if isinstance(value, int):
        if -MAX_SAFE_INTEGER - 1 <= value <= MAX_SAFE_INTEGER + 1:
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
    text = repr(abs(value))  # Shortest round-trip digits, like V8.
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    point = len(int_part) + exponent
    stripped = all_digits.lstrip("0")
    point -= len(all_digits) - len(stripped)
    digits = stripped.rstrip("0")
    k = len(digits)
    n = point
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits + "e" + exp if k == 1 else digits[0] + "." + digits[1:] + "e" + exp
    return sign + body


def js_string(value: Any) -> str:
    """``String(value)`` for JSON-like values."""
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
        return ",".join("" if is_nullish(item) else js_string(item) for item in value)
    if isinstance(value, Mapping):
        return "[object Object]"
    return str(value)


_INDEX_RE = re.compile(r"0|[1-9][0-9]*")


def _is_array_index(key: Any) -> bool:
    return isinstance(key, str) and _INDEX_RE.fullmatch(key) is not None and int(key) < 4294967295


def js_keys(obj: Mapping[Any, Any]) -> list[Any]:
    """Own property key order of an ordinary JS object: array indexes ascending, then insertion order."""
    keys = list(obj)
    indexes = [k for k in keys if _is_array_index(k)]
    if not indexes:
        return keys
    indexes.sort(key=int)
    return indexes + [k for k in keys if not _is_array_index(k)]


def object_keys(value: Any) -> list[str]:
    """``Object.keys`` for any JSON-like value."""
    if is_nullish(value):
        raise JsTypeError("Cannot convert undefined or null to object")
    if isinstance(value, Mapping):
        return [k if isinstance(k, str) else js_string(k) for k in js_keys(value)]
    if isinstance(value, (list, tuple)):
        return [str(i) for i in range(len(value))]
    if isinstance(value, str):
        return [str(i) for i in range(len(value.encode("utf-16-le", "surrogatepass")) // 2)]
    return []


def object_entries(value: Any) -> list[tuple[str, Any]]:
    """``Object.entries`` for any JSON-like value."""
    if isinstance(value, Mapping):
        return [(k if isinstance(k, str) else js_string(k), value[k]) for k in js_keys(value)]
    if isinstance(value, (list, tuple)):
        return [(str(i), item) for i, item in enumerate(value)]
    if isinstance(value, str):
        return [(str(i), ch) for i, ch in enumerate(value)]
    if is_nullish(value):
        raise JsTypeError("Cannot convert undefined or null to object")
    return []


def get(obj: Any, key: str) -> Any:
    """``obj[key]`` on JSON-like values; ``UNDEFINED`` for missing properties."""
    if obj is None or obj is UNDEFINED:
        kind = "null" if obj is None else "undefined"
        raise JsTypeError(f"Cannot read properties of {kind} (reading '{key}')")
    if isinstance(obj, Mapping):
        return obj.get(key, UNDEFINED)
    if isinstance(obj, (list, tuple, str)):
        if key == "length":
            return len(obj)
        if _is_array_index(key) and int(key) < len(obj):
            return obj[int(key)]
    return UNDEFINED


def optional_get(obj: Any, key: str) -> Any:
    """``obj?.[key]``."""
    if is_nullish(obj):
        return UNDEFINED
    return get(obj, key)


def utf16_key(value: str) -> bytes:
    """Sort key reproducing JavaScript's UTF-16 code-unit string comparison."""
    return value.encode("utf-16-be", "surrogatepass")


def js_sorted(values: Iterable[str]) -> list[str]:
    """``[...values].sort()`` for strings."""
    return sorted(values, key=utf16_key)


def js_trim(value: str) -> str:
    return value.strip(_JS_WHITESPACE)


def utf8_length(value: str) -> int:
    """``Buffer.byteLength(value)``; lone surrogates count as the 3-byte replacement character."""
    return len(value.encode("utf-8", "surrogatepass"))


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def to_utf8(value: str) -> bytes:
    """Encode like Node's UTF-8 writer, replacing lone surrogates with U+FFFD."""
    return _LONE_SURROGATE_RE.sub("�", value).encode("utf-8")


def _svz_key(value: Any) -> tuple[Any, ...]:
    if value is UNDEFINED:
        return ("undefined",)
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if is_number(value):
        if isinstance(value, float) and math.isnan(value):
            return ("nan",)
        return ("number", float(value) if abs(value) <= MAX_SAFE_INTEGER else value)
    if isinstance(value, str):
        return ("string", value)
    return ("object", id(value))


def same_value_zero_unique(values: Iterable[Any]) -> int:
    """``new Set(values).size``."""
    return len({_svz_key(v) for v in values})


_PERCENT_RUN_RE = re.compile(r"(?:%[0-9A-Fa-f]{2})+")


def decode_uri_component(value: str) -> str:
    """``decodeURIComponent``; raises :class:`UriError` for malformed input."""
    parts: list[str] = []
    position = 0
    for match in _PERCENT_RUN_RE.finditer(value):
        chunk = value[position : match.start()]
        if "%" in chunk:
            raise UriError("URI malformed")
        parts.append(chunk)
        run = match.group()
        data = bytes(int(run[i + 1 : i + 3], 16) for i in range(0, len(run), 3))
        try:
            parts.append(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise UriError("URI malformed") from None
        position = match.end()
    rest = value[position:]
    if "%" in rest:
        raise UriError("URI malformed")
    parts.append(rest)
    return "".join(parts)


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_QUOTE_RE = re.compile('["\\\\\x00-\x1f\ud800-\udfff]')


def _escape(match: re.Match[str]) -> str:
    char = match.group()
    return _ESCAPES.get(char) or "\\u%04x" % ord(char)


def quote(value: str) -> str:
    """``JSON.stringify(string)`` (well-formed: lone surrogates become ``\\udxxx``)."""
    return '"' + _QUOTE_RE.sub(_escape, value) + '"'


def _skip(value: Any) -> bool:
    return value is UNDEFINED or callable(value)


def _serialize(value: Any, indent: str | None, current: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return quote(value)
    if is_number(value):
        if isinstance(value, float) and not math.isfinite(value):
            return "null"
        text = number_to_string(value)
        return "null" if text in ("Infinity", "-Infinity", "NaN") else text
    inner = current + indent if indent is not None else current
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = ["null" if _skip(item) else _serialize(item, indent, inner) for item in value]
        if indent is None:
            return "[" + ",".join(items) + "]"
        return "[\n" + inner + (",\n" + inner).join(items) + "\n" + current + "]"
    if isinstance(value, Mapping):
        separator = ":" if indent is None else ": "
        entries = []
        for key in js_keys(value):
            item = value[key]
            if _skip(item):
                continue
            name = key if isinstance(key, str) else js_string(key)
            entries.append(quote(name) + separator + _serialize(item, indent, inner))
        if not entries:
            return "{}"
        if indent is None:
            return "{" + ",".join(entries) + "}"
        return "{\n" + inner + (",\n" + inner).join(entries) + "\n" + current + "}"
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def js_stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value)`` or ``JSON.stringify(value, null, indent)`` for JSON-like values."""
    return _serialize(value, " " * indent if indent else None, "")
