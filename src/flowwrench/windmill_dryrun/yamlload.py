"""Strict YAML 1.2 core-schema loading on top of PyYAML.

The Node.js tool parses with npm ``yaml`` 2.x ``parseDocument`` (YAML 1.2,
core schema, ``uniqueKeys: true``, ``maxAliasCount: 0``) and rejects every
error and warning. PyYAML implements YAML 1.1, so this module replaces its
resolver and constructor:

* implicit scalars follow the YAML 1.2 core schema only (``yes``/``on``/
  ``1_000``/``0b1``/``1:20``/timestamps stay strings; ``1e5`` is a number);
* only the ``null``/``bool``/``int``/``float``/``str``/``seq``/``map`` tags are
  accepted; any other explicit tag is rejected like an npm ``yaml`` warning;
* aliases, duplicate keys and multiple documents are rejected;
* mapping keys are converted to JavaScript property names.

PyYAML's YAML 1.1 scanner remains underneath: C1 control characters, some
plain-scalar/flow-context and tab edge cases and ``%YAML 1.1`` directives can be
treated differently from npm ``yaml``, and error messages use PyYAML positions.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from yaml.composer import Composer
from yaml.constructor import BaseConstructor
from yaml.error import Mark, MarkedYAMLError
from yaml.events import AliasEvent, StreamEndEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import BaseResolver
from yaml.scanner import Scanner

from .jsvalues import MAX_SAFE_INTEGER, combine_surrogates, is_number, number_to_string, quote

_TAG = "tag:yaml.org,2002:"
NULL_TAG = _TAG + "null"
BOOL_TAG = _TAG + "bool"
INT_TAG = _TAG + "int"
FLOAT_TAG = _TAG + "float"
STR_TAG = _TAG + "str"
SEQ_TAG = _TAG + "seq"
MAP_TAG = _TAG + "map"

# YAML 1.2.2 core schema (section 10.3.2), as implemented by npm yaml 2.x.
_NULL = re.compile(r"(?:~|null|Null|NULL|)\Z")
_BOOL = re.compile(r"(?:true|True|TRUE|false|False|FALSE)\Z")
_INT_DECIMAL = re.compile(r"[-+]?[0-9]+\Z")
_INT_OCTAL = re.compile(r"0o[0-7]+\Z")
_INT_HEX = re.compile(r"0x[0-9a-fA-F]+\Z")
_INT = re.compile(r"(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)\Z")
_FLOAT_SPECIAL = re.compile(r"(?:[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN)\Z")
_FLOAT_EXP = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+\Z")
_FLOAT_DOT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)\Z")
_FLOAT = re.compile(
    r"(?:[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN"
    r"|[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+"
    r"|[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*))\Z"
)


class YamlInputError(MarkedYAMLError):
    """A rejected YAML construct, reported with its position."""

    def __init__(self, problem: str, mark: Mark | None) -> None:
        super().__init__(problem=problem, problem_mark=mark)


def describe_error(error: MarkedYAMLError) -> str:
    """One-line ``problem at line L, column C`` text without source echo."""
    problem = error.problem or error.context or "invalid YAML"
    mark = error.problem_mark or error.context_mark
    if mark is None:
        return problem
    return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"


class _CoreResolver(BaseResolver):
    pass


_CoreResolver.add_implicit_resolver(NULL_TAG, _NULL, ["~", "n", "N", ""])
_CoreResolver.add_implicit_resolver(BOOL_TAG, _BOOL, list("tTfF"))
_CoreResolver.add_implicit_resolver(INT_TAG, _INT, list("-+0123456789"))
_CoreResolver.add_implicit_resolver(FLOAT_TAG, _FLOAT, list("-+0123456789."))


class _StrictComposer(Composer):
    def compose_node(self, parent: Node | None, index: Any) -> Node:
        if self.check_event(AliasEvent):
            # npm yaml with maxAliasCount: 0 rejects every alias.
            raise YamlInputError(
                "Excessive alias count indicates a resource exhaustion attack",
                self.peek_event().start_mark,
            )
        # Anchors without aliases are inert; YAML 1.2 allows redefining them.
        self.anchors = {}
        return super().compose_node(parent, index)

    def get_single_node(self) -> Node | None:
        self.get_event()  # STREAM-START
        document = None
        if not self.check_event(StreamEndEvent):
            document = self.compose_document()
        if not self.check_event(StreamEndEvent):
            raise YamlInputError("Source contains multiple documents", self.peek_event().start_mark)
        self.get_event()  # STREAM-END
        return document


def _unresolved(node: Node) -> YamlInputError:
    return YamlInputError(f"Unresolved tag: {node.tag}", node.start_mark)


def _scalar(node: Node) -> str:
    if not isinstance(node, ScalarNode):
        raise _unresolved(node)
    return node.value


def _safe_number(value: int) -> int | float:
    if abs(value) <= MAX_SAFE_INTEGER:
        return value
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


class _CoreConstructor(BaseConstructor):
    def construct_null(self, node: Node) -> None:
        if not _NULL.match(_scalar(node)):
            raise _unresolved(node)
        return None

    def construct_bool(self, node: Node) -> bool:
        value = _scalar(node)
        if not _BOOL.match(value):
            raise _unresolved(node)
        return value[0] in "tT"

    def construct_int(self, node: Node) -> int | float:
        value = _scalar(node)
        if _INT_OCTAL.match(value):
            return _safe_number(int(value[2:], 8))
        if _INT_HEX.match(value):
            return _safe_number(int(value[2:], 16))
        if not _INT_DECIMAL.match(value):
            raise _unresolved(node)
        # parseInt(): -0 keeps its sign; huge values become doubles.
        number = float(value)
        if number == 0:
            return -0.0 if value.startswith("-") else 0
        if abs(number) <= MAX_SAFE_INTEGER:
            return int(number)  # exact: never int(text), which caps at 4300 digits
        return number

    def construct_float(self, node: Node) -> float:
        value = _scalar(node)
        if _FLOAT_SPECIAL.match(value):
            if value.lower().endswith("nan"):
                return math.nan
            return -math.inf if value.startswith("-") else math.inf
        if not (_FLOAT_EXP.match(value) or _FLOAT_DOT.match(value)):
            raise _unresolved(node)
        return float(value)

    def construct_str(self, node: Node) -> str:
        return combine_surrogates(_scalar(node))

    def construct_seq(self, node: Node) -> list[Any]:
        if not isinstance(node, SequenceNode):
            raise _unresolved(node)
        return [self.construct_object(child, deep=True) for child in node.value]

    def construct_map(self, node: Node) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            raise _unresolved(node)
        mapping: dict[str, Any] = {}
        seen: set[tuple[str, Any]] = set()
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, (dict, list)):
                # npm yaml toJS() stringifies collection keys as flow YAML; collection
                # keys are never duplicates of each other (nodes compare by identity).
                mapping[_flow_yaml(key_node, self)] = self.construct_object(value_node, deep=True)
                continue
            identity = _key_identity(key)
            if identity is not None:
                if identity in seen:
                    raise YamlInputError("Map keys must be unique", key_node.start_mark)
                seen.add(identity)
            mapping[_property_name(key)] = self.construct_object(value_node, deep=True)
        return mapping

    def construct_unknown(self, node: Node) -> Any:
        raise _unresolved(node)


_CoreConstructor.add_constructor(NULL_TAG, _CoreConstructor.construct_null)
_CoreConstructor.add_constructor(BOOL_TAG, _CoreConstructor.construct_bool)
_CoreConstructor.add_constructor(INT_TAG, _CoreConstructor.construct_int)
_CoreConstructor.add_constructor(FLOAT_TAG, _CoreConstructor.construct_float)
_CoreConstructor.add_constructor(STR_TAG, _CoreConstructor.construct_str)
_CoreConstructor.add_constructor(SEQ_TAG, _CoreConstructor.construct_seq)
_CoreConstructor.add_constructor(MAP_TAG, _CoreConstructor.construct_map)
_CoreConstructor.add_constructor(None, _CoreConstructor.construct_unknown)


def _key_identity(key: Any) -> tuple[str, Any] | None:
    """Duplicate-key identity: npm yaml compares resolved scalars with ===."""
    if key is None:
        return ("null", None)
    if isinstance(key, bool):
        return ("bool", key)
    if is_number(key):
        if isinstance(key, float) and math.isnan(key):
            return None  # NaN !== NaN
        return ("number", key)
    return ("string", key)


def _property_name(key: Any) -> str:
    """JavaScript property key for a scalar YAML key (``String(key)``)."""
    if key is None:
        return ""
    if key is True:
        return "true"
    if key is False:
        return "false"
    if is_number(key):
        return number_to_string(key)
    return key


# A collection used as a mapping key becomes the property name npm yaml 2.x
# produces in toJS(): the key re-stringified as single-line flow YAML (default
# options: lineWidth 80, padding inside brackets). Comments, tags/anchors and
# long-string folding are not reproduced.
_LINE_WIDTH = 80
_PLAIN_UNSAFE = re.compile(
    r"^[\n\t ,\[\]{}#&*!|>'\"%@`]|^[?-]\Z|^[?-][ \t]|[\n:][ \t]|[ \t]\n|[\n\t ]#|[\n\t :]\Z"
)
_DOCUMENT_MARKER = re.compile(r"^(?:%|---|\.\.\.)", re.MULTILINE)
_JSON_CONTROL_ESCAPE = re.compile(r"(\\\\)|\\u00([0-9a-f]{2})")
_DOUBLE_ESCAPES = {"00": "\\0", "07": "\\a", "0b": "\\v", "1b": "\\e"}


def _double_quoted(value: str) -> str:
    """JSON.stringify(value), then YAML's short escapes for \\u00XX controls."""

    def escape(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1)
        return _DOUBLE_ESCAPES.get(match.group(2), f"\\x{match.group(2)}")

    return _JSON_CONTROL_ESCAPE.sub(escape, quote(value))


def _quoted(value: str, style: str | None, implicit_key: bool) -> str:
    if style == "'" or (style != '"' and "'" not in value and '"' in value):
        if not (implicit_key and "\n" in value) and not re.search(r"[ \t]\n|\n[ \t]", value):
            return "'" + value.replace("'", "''") + "'"
    return _double_quoted(value)


def _flow_string(value: str, style: str | None, implicit_key: bool) -> str:
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]", value):
        return _double_quoted(value)
    if style in ("'", '"'):
        return _quoted(value, style, implicit_key)
    if style in ("|", ">"):
        return _quoted(value, None, implicit_key)
    if (
        not value
        or "\n" in value
        or re.search(r"[\[\]{},]", value)
        or _PLAIN_UNSAFE.search(value)
        or _DOCUMENT_MARKER.search(value)
        or any(p.match(value) for p in (_NULL, _BOOL, _INT, _FLOAT))
    ):
        return _quoted(value, None, implicit_key)
    return value


def _exponential(value: float) -> str:
    """``Number.prototype.toExponential()`` with the shortest round-trip digits."""
    sign, digits, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    text = "".join(map(str, digits))
    power = int(exponent) + len(text) - 1
    mantissa = text[0] + ("." + text[1:] if len(text) > 1 else "")
    return f"{'-' if value < 0 else ''}{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _flow_number(node: ScalarNode, value: int | float) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return ".nan" if math.isnan(value) else ("-.inf" if value < 0 else ".inf")
    text = node.value
    if _INT_HEX.match(text) or _INT_OCTAL.match(text):
        return ("0x%x" if text[1] == "x" else "0o%o") % int(value)
    if _FLOAT_EXP.match(text):
        return _exponential(float(value))
    if isinstance(value, float) and value == 0 and math.copysign(1, value) < 0:
        return "-0"
    result = number_to_string(value)
    dot = text.find(".")
    if _FLOAT_DOT.match(text) and text.endswith("0") and result[:1].isdigit():
        if "." not in result:
            result += "."
        result += "0" * (len(text) - dot - 1 - (len(result) - result.index(".") - 1))
    return result


def _flow_collection(items: list[str], start: str, end: str, indent: str) -> str:
    if not items:
        return start + end
    if any("\n" in item for item in items) or sum(len(item) + 2 for item in items) + 2 > _LINE_WIDTH:
        lines = "".join(f"\n  {indent}{item}" for item in items)
        return f"{start}{lines}\n{indent}{end}"
    return f"{start} {' '.join(items)} {end}"


def _flow_yaml(node: Node, loader: Any, indent: str = "", implicit_key: bool = False) -> str:
    if isinstance(node, SequenceNode):
        inner = indent + "    "
        items = [_flow_yaml(child, loader, inner) for child in node.value]
        return _flow_collection([*(item + "," for item in items[:-1]), *items[-1:]], "[", "]", indent)
    if isinstance(node, MappingNode):
        inner = indent + "  "
        pairs = []
        for key_node, value_node in node.value:
            explicit = isinstance(key_node, (SequenceNode, MappingNode)) or getattr(key_node, "style", None) in ("|", ">")
            key = _flow_yaml(key_node, loader, inner + "  ", implicit_key=not explicit)
            key = f"? {key}\n{inner}:" if explicit else f"{key}:"
            value = _flow_yaml(value_node, loader, inner + "  ")
            if value and "\n" in value and isinstance(value_node, (SequenceNode, MappingNode)):
                pairs.append(f"{key}\n{inner}  {value}")
            else:
                pairs.append(f"{key} {value}" if value else key)
        return _flow_collection([*(pair + "," for pair in pairs[:-1]), *pairs[-1:]], "{", "}", indent)
    assert isinstance(node, ScalarNode)
    value = loader.construct_object(node, deep=True)
    if value is None or isinstance(value, bool):
        return node.value
    if is_number(value):
        return _flow_number(node, value)
    return _flow_string(value, node.style, implicit_key)


class _Loader(Reader, Scanner, Parser, _StrictComposer, _CoreConstructor, _CoreResolver):
    def __init__(self, stream: str) -> None:
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        _StrictComposer.__init__(self)
        _CoreConstructor.__init__(self)
        _CoreResolver.__init__(self)


def load_yaml(text: str) -> Any:
    """Parse one YAML 1.2 core-schema document; raise on any rejected construct.

    Raises ``yaml.YAMLError`` subclasses; use :func:`describe_error` for text.
    """
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
        return None if node is None else loader.construct_document(node)
    finally:
        loader.dispose()
