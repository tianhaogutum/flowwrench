"""JavaScript-compatible primitives that keep output identical to the Node tool.

The original implementation relies on V8 semantics in several observable places:

* ``JSON.parse``: numbers are IEEE doubles, ``NaN``/``Infinity`` literals are
  rejected, and integer-like object keys ("0", "17") enumerate first in
  ascending numeric order, before all other keys in insertion order.
* ``JSON.stringify``: shortest round-trip number formatting with JavaScript's
  exponent rules (``1.0`` -> ``1``), lone UTF-16 surrogates escaped, compact or
  2-space indentation, ``ensure_ascii``-free output.
* ``undefined`` and ``null`` are different values; JSON data never contains
  ``undefined`` but property reads of missing keys produce it.
* JavaScript truthiness (``[]`` and ``{}`` are truthy, ``0``/``""`` are not).
* Strings are sequences of UTF-16 code units for ``length`` and ``slice``.
* ``String.prototype.trim`` and the regular-expression ``\\s`` class use the
  ECMAScript whitespace set rather than Python's.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from json.decoder import scanstring
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_MAX_ARRAY_INDEX = 2**32 - 2


class _Undefined:
    """Singleton standing in for JavaScript ``undefined`` (a missing property)."""

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

    def __copy__(self) -> _Undefined:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Undefined:
        return self


UNDEFINED: Any = _Undefined()

# Properties every plain JavaScript object inherits from Object.prototype. A
# bracket lookup such as ``table[key]`` on an object literal finds these.
OBJECT_PROTOTYPE_KEYS = frozenset({
    "__proto__", "__defineGetter__", "__defineSetter__", "__lookupGetter__",
    "__lookupSetter__", "constructor", "hasOwnProperty", "isPrototypeOf",
    "propertyIsEnumerable", "toLocaleString", "toString", "valueOf",
})

# ECMAScript WhiteSpace + LineTerminator (used by trim() and the regexp \s class).
JS_WHITESPACE = (
    "\t\n\v\f\r          "
    "        　﻿"
)
_WS_CLASS_BODY = "".join(re.escape(char) for char in JS_WHITESPACE)
JS_S = f"[{_WS_CLASS_BODY}]"
JS_WS_CLASS_BODY = _WS_CLASS_BODY
_WS_RUN = re.compile(f"{JS_S}+")


# --------------------------------------------------------------------------- types


def is_number(value: Any) -> bool:
    """``typeof value === 'number'`` (Python ``bool`` is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """``typeof value === 'number' && Number.isFinite(value)``."""
    if not is_number(value):
        return False
    return isinstance(value, int) or math.isfinite(value)


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    if not is_number(value):
        return False
    return isinstance(value, int) or (math.isfinite(value) and value.is_integer())


def is_array(value: Any) -> bool:
    """``Array.isArray(value)``."""
    return isinstance(value, (list, tuple))


def is_object(value: Any) -> bool:
    """``v !== null && typeof v === 'object' && !Array.isArray(v)`` for mapping data."""
    return isinstance(value, dict)


def js_typeof(value: Any) -> str:
    """``typeof value``."""
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


def truthy(value: Any) -> bool:
    """JavaScript ``Boolean(value)``: every object and array is truthy."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if value is True:
        return True
    if is_number(value):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def nullish(value: Any) -> bool:
    """``value == null`` (``null`` or ``undefined``)."""
    return value is None or value is UNDEFINED


def coalesce(value: Any, default: Any) -> Any:
    """``value ?? default``."""
    return default if value is None or value is UNDEFINED else value


def prop(value: Any, key: str) -> Any:
    """``value?.[key]`` for JSON data: ``None`` when absent or not an object."""
    return value.get(key) if isinstance(value, dict) else None


def own(value: Any, key: str) -> Any:
    """``value[key]`` distinguishing a missing property (``UNDEFINED``) from ``null``."""
    return value.get(key, UNDEFINED) if isinstance(value, dict) else UNDEFINED


def strict_equal(a: Any, b: Any) -> bool:
    """``a === b`` for JSON scalars (``true !== 1`` unlike Python ``==``)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


def includes(options: tuple[Any, ...] | list[Any], value: Any) -> bool:
    """``options.includes(value)`` for scalar option lists."""
    return any(strict_equal(option, value) for option in options)


# --------------------------------------------------------------------------- strings


_ASTRAL = re.compile("[\U00010000-\U0010ffff]")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _code_units(text: str) -> list[int]:
    units: list[int] = []
    for char in text:
        point = ord(char)
        if point > 0xFFFF:
            point -= 0x10000
            units.append(0xD800 + (point >> 10))
            units.append(0xDC00 + (point & 0x3FF))
        else:
            units.append(point)
    return units


def _from_units(units: list[int]) -> str:
    chars: list[str] = []
    index, size = 0, len(units)
    while index < size:
        unit = units[index]
        if 0xD800 <= unit <= 0xDBFF and index + 1 < size and 0xDC00 <= units[index + 1] <= 0xDFFF:
            chars.append(chr(0x10000 + ((unit - 0xD800) << 10) + (units[index + 1] - 0xDC00)))
            index += 2
        else:
            chars.append(chr(unit))
            index += 1
    return "".join(chars)


def utf16_length(text: str) -> int:
    """JavaScript ``string.length`` (UTF-16 code units)."""
    return len(text) + len(_ASTRAL.findall(text))


def utf16_slice(text: str, start: int, end: int) -> str:
    """JavaScript ``string.slice(start, end)`` for non-negative indexes."""
    if not _ASTRAL.search(text) and not _SURROGATE.search(text):
        return text[start:end]
    return _from_units(_code_units(text)[start:end])


def from_code_units(text: str) -> str:
    """Join adjacent surrogate halves into code points; keep lone surrogates."""
    if not _SURROGATE.search(text):
        return text
    return _from_units(_code_units(text))


def well_formed(text: str) -> str:
    """Replace lone surrogates with U+FFFD, as Node does when encoding UTF-8."""
    if not _SURROGATE.search(text):
        return text
    return _SURROGATE.sub("�", from_code_units(text))


def utf8(text: str) -> bytes:
    """Node ``Buffer.from(text, 'utf8')``."""
    return well_formed(text).encode("utf-8")


def js_trim(text: str) -> str:
    """``String.prototype.trim``."""
    return text.strip(JS_WHITESPACE)


def split_whitespace(text: str) -> list[str]:
    """``text.split(/\\s+/)``."""
    return _WS_RUN.split(text)


def utf16_sort_key(text: str) -> bytes:
    """Default ``Array.prototype.sort`` order: UTF-16 code units."""
    return text.encode("utf-16-be", "surrogatepass")


# --------------------------------------------------------------------------- objects


def is_array_index(key: str) -> bool:
    """True for canonical decimal strings in 0..2**32-2 (V8 'integer index' keys)."""
    if not key or not ("0" <= key[0] <= "9"):
        return False
    if key == "0":
        return True
    if key[0] == "0" or not key.isascii() or not key.isdigit():
        return False
    return int(key) <= _MAX_ARRAY_INDEX


def _key_text(key: Any) -> str:
    return key if isinstance(key, str) else js_string(key)


def js_items(value: dict[Any, Any]) -> list[tuple[Any, Any]]:
    """``Object.entries`` order: integer-like keys ascending, then insertion order."""
    items = list(value.items())
    indexes = [item for item in items if is_array_index(_key_text(item[0]))]
    if not indexes:
        return items
    indexes.sort(key=lambda item: int(_key_text(item[0])))
    return indexes + [item for item in items if not is_array_index(_key_text(item[0]))]


def js_keys(value: dict[Any, Any]) -> list[Any]:
    """``Object.keys``."""
    return [key for key, _ in js_items(value)]


def js_entries(value: Any) -> list[tuple[str, Any]]:
    """``Object.entries(value)`` for any JSON value (strings yield code units)."""
    if isinstance(value, dict):
        return js_items(value)
    if is_array(value):
        return [(str(index), item) for index, item in enumerate(value)]
    if isinstance(value, str):
        return [(str(index), chr(unit)) for index, unit in enumerate(_code_units(value))]
    return []


def js_object(pairs: Any) -> dict[str, Any]:
    """Build a dict whose key order matches a JavaScript object's enumeration order."""
    result: dict[str, Any] = {}
    for key, value in pairs.items() if isinstance(pairs, dict) else pairs:
        result[key] = value
    if not any(is_array_index(key) for key in result):
        return result
    return dict(js_items(result))


# --------------------------------------------------------------------------- numbers


def number_to_string(value: float | int) -> str:
    """JavaScript ``String(number)`` (Number::toString)."""
    if isinstance(value, int) and abs(value) <= MAX_SAFE_INTEGER:
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
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        e = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e >= 0 else "e-") + str(abs(e))
    return sign + text


def js_round(value: float) -> float | int:
    """``Math.round``: halves round towards positive infinity."""
    if not math.isfinite(value):
        return value
    floor = math.floor(value)
    return floor + 1 if value - floor >= 0.5 else floor


def js_string(value: Any) -> str:
    """``String(value)`` for JSON data."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_number(value):
        return number_to_string(value)
    if isinstance(value, str):
        return value
    if is_array(value):
        return ",".join("" if nullish(item) else js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


# --------------------------------------------------------------------------- JSON.stringify


_ESCAPE = re.compile('[\\x00-\\x1f"\\\\\ud800-\udfff]')
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(match: re.Match[str]) -> str:
    char = match.group()
    return _SHORT_ESCAPES.get(char) or f"\\u{ord(char):04x}"


def quote(text: str) -> str:
    """``JSON.stringify(string)`` including well-formed lone-surrogate escapes."""
    return '"' + _ESCAPE.sub(_escape, from_code_units(text)) + '"'


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value)`` or ``JSON.stringify(value, null, indent)``."""
    out: list[str] = []
    with recursion_headroom():
        _emit(value, " " * indent if indent else "", "", out)
    return "".join(out)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return quote(value)
    if is_number(value):
        return number_to_string(value) if is_finite_number(value) else "null"
    raise TypeError(f"cannot serialize {type(value).__name__} as JSON")


def _emit(value: Any, step: str, current: str, out: list[str]) -> None:
    # Plain loops (no generators inside str.join) keep deep nesting off the C stack.
    if isinstance(value, dict):
        items = [(key, item) for key, item in js_items(value) if item is not UNDEFINED]
        if not items:
            out.append("{}")
            return
        inner = current + step
        out.append("{")
        first = True
        for key, item in items:
            if not first:
                out.append(",")
            first = False
            if step:
                out.append("\n")
                out.append(inner)
            out.append(quote(_key_text(key)))
            out.append(": " if step else ":")
            _emit(item, step, inner, out)
        if step:
            out.append("\n")
            out.append(current)
        out.append("}")
        return
    if is_array(value):
        if not value:
            out.append("[]")
            return
        inner = current + step
        out.append("[")
        first = True
        for item in value:
            if not first:
                out.append(",")
            first = False
            if step:
                out.append("\n")
                out.append(inner)
            if item is UNDEFINED:
                out.append("null")
            else:
                _emit(item, step, inner, out)
        if step:
            out.append("\n")
            out.append(current)
        out.append("]")
        return
    out.append(_scalar(value))


# --------------------------------------------------------------------------- JSON.parse


class JSONSyntaxError(ValueError):
    """Raised when text is not accepted by ``JSON.parse``."""


def _reject_constant(name: str) -> Any:
    raise JSONSyntaxError(f"invalid literal {name}")


def _parse_int(text: str) -> int | float:
    # Every JSON number is a double. Short integers stay exact Python ints;
    # anything beyond 2**53 becomes the correctly rounded double (or inf).
    digits = len(text) - (1 if text.startswith("-") else 0)
    if digits <= 16:
        value = int(text)
        if abs(value) <= MAX_SAFE_INTEGER:
            return value
    return float(text)


def _parse_number(text: str) -> int | float:
    if "." in text or "e" in text or "E" in text:
        return float(text)
    return _parse_int(text)


_DECODER = json.JSONDecoder(object_pairs_hook=js_object, parse_int=_parse_int, parse_constant=_reject_constant)


def parse_json(text: str) -> Any:
    """``JSON.parse(text)`` with JavaScript number and key-order semantics."""
    try:
        return _DECODER.decode(text)
    except RecursionError:
        # CPython's decoder recurses; V8's does not. Deep input stays parseable.
        return _IterativeParser(text).parse()
    except ValueError as error:
        raise JSONSyntaxError(str(error)) from None


_JSON_WS = re.compile(r"[ \t\n\r]*")
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_LITERALS = (("true", True), ("false", False), ("null", None))


class _IterativeParser:
    """Stack-based JSON parser used only for nesting deeper than CPython allows."""

    def __init__(self, text: str) -> None:
        self.text = text

    def _ws(self, pos: int) -> int:
        match = _JSON_WS.match(self.text, pos)
        assert match is not None
        return match.end()

    def _expect(self, pos: int, char: str) -> int:
        if self.text[pos : pos + 1] != char:
            raise JSONSyntaxError(f"expected {char!r} at {pos}")
        return pos + 1

    def _key(self, pos: int) -> tuple[str, int]:
        pos = self._expect(pos, '"')
        try:
            key, pos = scanstring(self.text, pos, True)
        except ValueError as error:
            raise JSONSyntaxError(str(error)) from None
        pos = self._expect(self._ws(pos), ":")
        return key, self._ws(pos)

    def parse(self) -> Any:
        text = self.text
        stack: list[list[Any]] = []
        pos = self._ws(0)
        while True:
            char = text[pos : pos + 1]
            value: Any
            if char == "{":
                pos = self._ws(pos + 1)
                if text[pos : pos + 1] == "}":
                    value = {}
                    pos += 1
                else:
                    key, pos = self._key(pos)
                    stack.append([[], key])
                    continue
            elif char == "[":
                pos = self._ws(pos + 1)
                if text[pos : pos + 1] == "]":
                    value = []
                    pos += 1
                else:
                    stack.append([[]])
                    continue
            elif char == '"':
                try:
                    value, pos = scanstring(text, pos + 1, True)
                except ValueError as error:
                    raise JSONSyntaxError(str(error)) from None
            else:
                match = _JSON_NUMBER.match(text, pos)
                if match:
                    value, pos = _parse_number(match.group()), match.end()
                else:
                    for literal, literal_value in _LITERALS:
                        if text.startswith(literal, pos):
                            value, pos = literal_value, pos + len(literal)
                            break
                    else:
                        raise JSONSyntaxError(f"unexpected value at {pos}")
            while True:
                pos = self._ws(pos)
                if not stack:
                    if pos != len(text):
                        raise JSONSyntaxError(f"extra data at {pos}")
                    return value
                top = stack[-1]
                separator = text[pos : pos + 1]
                if len(top) == 1:
                    top[0].append(value)
                    if separator == ",":
                        pos = self._ws(pos + 1)
                        break
                    pos = self._expect(pos, "]")
                    value = top[0]
                else:
                    top[0].append((top[1], value))
                    if separator == ",":
                        top[1], pos = self._key(self._ws(pos + 1))
                        break
                    pos = self._expect(pos, "}")
                    value = js_object(top[0])
                stack.pop()


# --------------------------------------------------------------------------- recursion


@contextmanager
def recursion_headroom(limit: int = 20000) -> Iterator[None]:
    """V8's stack comfortably holds the 500-node nesting the converter allows.

    The converter, validator and serializer recurse once (or a few times) per
    emitted step, which can exceed CPython's default limit of 1000 frames.
    """
    previous = sys.getrecursionlimit()
    if previous < limit:
        sys.setrecursionlimit(limit)
    try:
        yield
    finally:
        if previous < limit:
            sys.setrecursionlimit(previous)
