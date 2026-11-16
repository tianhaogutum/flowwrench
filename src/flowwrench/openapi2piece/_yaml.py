"""YAML/JSON loading that mirrors ``parseDocument(text, {uniqueKeys, strict}).toJS({maxAliasCount: 50})``.

The Node tool uses the ``yaml`` npm package (v2), which defaults to YAML 1.2 and
its *core* schema.  PyYAML implements YAML 1.1 resolution (``yes``/``on`` are
booleans, ``0755`` is octal, timestamps, ``<<`` merge keys), so this module uses
PyYAML only as a tokenizer/parser: it consumes PyYAML *events* and performs its
own composition and YAML 1.2 core-schema scalar resolution:

* ``null``: ``~``, ``null``, ``Null``, ``NULL`` and the empty plain scalar;
* ``bool``: ``true``/``True``/``TRUE``/``false``/``False``/``FALSE`` only;
* ``int``: ``[-+]?[0-9]+`` (decimal, so ``0755`` is 755), ``0o17``, ``0x1F``;
* ``float``: decimal/exponent forms, ``.inf``, ``-.inf``, ``.nan``;
* everything else (including ``yes``, ``on``, ``2001-12-14``, ``1_000``) is a string.

Only plain scalars are resolved; quoted and block scalars are always strings.
Duplicate keys are syntax errors, merge keys are ordinary keys, tags outside the
core schema raise :class:`YamlTagWarning`, and alias expansion is bounded with
the same ``count * aliasCount > 50`` rule as ``yaml``.  Integers beyond 2**53
become floats, like JavaScript numbers.

Documents that are valid JSON are decoded with :mod:`json` first (with
duplicate-key detection): PyYAML rejects some valid JSON, such as tab
indentation, that ``yaml`` accepts.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.scanner import Scanner
from yaml.tokens import DirectiveToken

from ._js import MAX_SAFE_INTEGER, number_to_string

__all__ = ["YamlSyntaxError", "YamlTagWarning", "YamlAliasError", "load_document", "MAX_ALIAS_COUNT"]

MAX_ALIAS_COUNT = 50
_CORE = "tag:yaml.org,2002:"


class YamlSyntaxError(Exception):
    """Equivalent of a non-empty ``doc.errors``."""


class YamlTagWarning(Exception):
    """Equivalent of a non-empty ``doc.warnings`` (unresolved tags, unsupported directives)."""


class YamlAliasError(Exception):
    """Equivalent of an exception thrown by ``doc.toJS`` (excessive aliases, unrepresentable keys)."""


class _DuplicateKey(Exception):
    pass


class _NotJson(ValueError):
    pass


# --- scalar resolution (yaml v2 core schema) ---------------------------------------------

_NULL_RE = re.compile(r"(?:~|[Nn]ull|NULL)?")
_BOOL_RE = re.compile(r"[Tt]rue|TRUE|[Ff]alse|FALSE")
_INT_OCT_RE = re.compile(r"0o[0-7]+")
_INT_RE = re.compile(r"[-+]?[0-9]+")
_INT_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT_NAN_RE = re.compile(r"[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN")
_FLOAT_EXP_RE = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+")
_FLOAT_RE = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)")


def _js_number(value: int) -> int | float:
    """A JavaScript number for an integer: exact up to 2**53, otherwise the nearest double."""
    if -MAX_SAFE_INTEGER - 1 <= value <= MAX_SAFE_INTEGER + 1:
        return value
    try:
        return float(value)
    except OverflowError:
        return float("inf") if value > 0 else float("-inf")


def _resolve_null(text: str) -> tuple[bool, Any]:
    return (True, None) if _NULL_RE.fullmatch(text) else (False, None)


def _resolve_bool(text: str) -> tuple[bool, Any]:
    if _BOOL_RE.fullmatch(text):
        return True, text[0] in "tT"
    return False, None


def _resolve_int(text: str) -> tuple[bool, Any]:
    if _INT_OCT_RE.fullmatch(text):
        return True, _js_number(int(text[2:], 8))
    if _INT_RE.fullmatch(text):
        return True, _js_number(int(text, 10))
    if _INT_HEX_RE.fullmatch(text):
        return True, _js_number(int(text[2:], 16))
    return False, None


def _resolve_float(text: str) -> tuple[bool, Any]:
    if _FLOAT_NAN_RE.fullmatch(text):
        if text[-3:].lower() == "nan":
            return True, float("nan")
        return True, float("-inf") if text[0] == "-" else float("inf")
    if _FLOAT_EXP_RE.fullmatch(text) or _FLOAT_RE.fullmatch(text):
        return True, float(text)
    return False, None


def _resolve_plain(text: str) -> Any:
    for resolver in (_resolve_null, _resolve_bool, _resolve_int, _resolve_float):
        matched, value = resolver(text)
        if matched:
            return value
    return text


_TAGGED_RESOLVERS = {
    _CORE + "null": _resolve_null,
    _CORE + "bool": _resolve_bool,
    _CORE + "int": _resolve_int,
    _CORE + "float": _resolve_float,
}


# --- event source -------------------------------------------------------------------------


class _EventSource(Reader, Scanner, Parser):
    """PyYAML's reader/scanner/parser only: no composer, resolver or constructor."""

    def __init__(self, text: str) -> None:
        Reader.__init__(self, text)
        Scanner.__init__(self)
        Parser.__init__(self)
        self.unsupported_directive = False

    def process_directives(self) -> Any:
        seen: list[DirectiveToken] = []
        original = self.get_token

        def recording(*args: Any, **kwargs: Any) -> Any:
            token = original(*args, **kwargs)
            if isinstance(token, DirectiveToken):
                seen.append(token)
            return token

        self.get_token = recording  # type: ignore[method-assign]
        try:
            result = super().process_directives()
        finally:
            del self.get_token
        for token in seen:
            if token.name == "YAML":
                # yaml v2 switches to its YAML 1.1 schema for "%YAML 1.1"; that
                # schema is not reproduced here, so it is rejected like an
                # unsupported version.
                if tuple(token.value) != (1, 2):
                    self.unsupported_directive = True
            elif token.name != "TAG":
                self.unsupported_directive = True
        return result


# --- composition --------------------------------------------------------------------------


class _Node:
    __slots__ = ("anchor",)

    def __init__(self, anchor: str | None) -> None:
        self.anchor = anchor


class _Scalar(_Node):
    __slots__ = ("value",)

    def __init__(self, anchor: str | None, value: Any) -> None:
        super().__init__(anchor)
        self.value = value


class _Seq(_Node):
    __slots__ = ("items",)

    def __init__(self, anchor: str | None) -> None:
        super().__init__(anchor)
        self.items: list[_Node] = []


_NO_KEY: Any = object()


class _Map(_Node):
    __slots__ = ("pairs", "pending", "seen")

    def __init__(self, anchor: str | None) -> None:
        super().__init__(anchor)
        self.pairs: list[tuple[_Node, _Node]] = []
        self.pending: Any = _NO_KEY
        self.seen: set[tuple[Any, ...]] = set()


class _Alias(_Node):
    __slots__ = ("source",)

    def __init__(self, source: _Node) -> None:
        super().__init__(None)
        self.source = source


def _key_identity(value: Any) -> tuple[Any, ...] | None:
    """``a.value === b.value`` for scalar keys (NaN never equals itself)."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, float) and value != value:
        return None
    return ("number", value)


def _scalar_value(event: ScalarEvent) -> tuple[Any, bool]:
    """Resolve a scalar; the flag reports an unresolved tag (a yaml warning)."""
    tag, text = event.tag, event.value
    if tag is None:
        return (_resolve_plain(text) if event.style is None else text), False
    if tag in ("!", _CORE + "str"):
        return text, False
    resolver = _TAGGED_RESOLVERS.get(tag)
    if resolver is not None:
        matched, value = resolver(text)
        if matched:
            return value, False
    return text, True


def _compose(text: str) -> tuple[_Node | None, bool]:
    try:
        # The reader rejects non-printable characters while constructing.
        source = _EventSource(text)
    except yaml.YAMLError:
        raise YamlSyntaxError("Invalid YAML") from None
    try:
        events = []
        while source.check_event():
            events.append(source.get_event())
    except yaml.YAMLError:
        raise YamlSyntaxError("Invalid YAML") from None
    finally:
        source.dispose()

    warned = source.unsupported_directive
    anchors: dict[str, _Node] = {}
    stack: list[_Node] = []
    root: _Node | None = None
    documents = 0

    def attach(node: _Node) -> None:
        nonlocal root
        if not stack:
            root = node
            return
        parent = stack[-1]
        if isinstance(parent, _Seq):
            parent.items.append(node)
            return
        assert isinstance(parent, _Map)
        if parent.pending is _NO_KEY:
            parent.pending = node
            return
        key = parent.pending
        parent.pending = _NO_KEY
        if isinstance(key, _Scalar):
            identity = _key_identity(key.value)
            if identity is not None:
                if identity in parent.seen:
                    raise YamlSyntaxError("Map keys must be unique")
                parent.seen.add(identity)
        parent.pairs.append((key, node))

    for event in events:
        if isinstance(event, DocumentStartEvent):
            documents += 1
            if documents > 1:
                raise YamlSyntaxError("Source contains multiple documents")
            continue
        node: _Node
        if isinstance(event, AliasEvent):
            target = anchors.get(event.anchor)
            if target is None:
                raise YamlSyntaxError("Aliased anchor not found")
            node = _Alias(target)
        elif isinstance(event, ScalarEvent):
            value, unresolved = _scalar_value(event)
            warned = warned or unresolved
            node = _Scalar(event.anchor, value)
            if event.anchor is not None:
                anchors[event.anchor] = node
        elif isinstance(event, (SequenceStartEvent, MappingStartEvent)):
            is_map = isinstance(event, MappingStartEvent)
            if event.tag not in (None, "!", _CORE + ("map" if is_map else "seq")):
                warned = True
            node = _Map(event.anchor) if is_map else _Seq(event.anchor)
            if event.anchor is not None:
                anchors[event.anchor] = node
            attach(node)
            stack.append(node)
            continue
        elif isinstance(event, (SequenceEndEvent, MappingEndEvent)):
            stack.pop()
            continue
        else:  # Stream start/end, document end.
            continue
        attach(node)
    return root, warned


# --- conversion to JS-like values ---------------------------------------------------------


class _AnchorData:
    __slots__ = ("count", "alias_count", "res")

    def __init__(self) -> None:
        self.count = 1
        self.alias_count = 0
        self.res: Any = _NO_KEY


class _Converter:
    def __init__(self) -> None:
        self.anchors: dict[int, _AnchorData] = {}

    def convert(self, node: _Node) -> Any:
        if isinstance(node, _Alias):
            return self._alias(node)
        data: _AnchorData | None = None
        if node.anchor is not None:
            data = _AnchorData()
            self.anchors[id(node)] = data
        if isinstance(node, _Scalar):
            if data is not None:
                data.res = node.value
            return node.value
        if isinstance(node, _Seq):
            items: list[Any] = []
            if data is not None:
                data.res = items
            for item in node.items:
                items.append(self.convert(item))
            return items
        assert isinstance(node, _Map)
        mapping: dict[str, Any] = {}
        if data is not None:
            data.res = mapping
        for key_node, value_node in node.pairs:
            key = self.convert(key_node)
            mapping[self._stringify_key(key)] = self.convert(value_node)
        return mapping

    @staticmethod
    def _stringify_key(key: Any) -> str:
        if key is None:
            return ""
        if isinstance(key, bool):
            return "true" if key else "false"
        if isinstance(key, str):
            return key
        if isinstance(key, (int, float)):
            return number_to_string(key)
        # yaml stringifies collection keys as YAML text; not reproduced.
        raise YamlAliasError("Collection keys are unsupported")

    def _alias(self, node: _Alias) -> Any:
        source = node.source
        data = self.anchors.get(id(source))
        if data is None:
            self.convert(source)
            data = self.anchors.get(id(source))
        if data is None or data.res is _NO_KEY:
            raise YamlAliasError("Alias anchor was not resolved")
        data.count += 1
        if data.alias_count == 0:
            data.alias_count = self._alias_count(source)
        if data.count * data.alias_count > MAX_ALIAS_COUNT:
            raise YamlAliasError("Excessive alias count indicates a resource exhaustion attack")
        return data.res

    def _alias_count(self, node: _Node) -> int:
        if isinstance(node, _Alias):
            data = self.anchors.get(id(node.source))
            return data.count * data.alias_count if data is not None else 0
        if isinstance(node, _Seq):
            return max((self._alias_count(item) for item in node.items), default=0)
        if isinstance(node, _Map):
            return max((max(self._alias_count(k), self._alias_count(v)) for k, v in node.pairs), default=0)
        return 1


# --- JSON fast path -----------------------------------------------------------------------


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise _NotJson(name)


def _json_int(text: str) -> int | float:
    return _js_number(int(text))


def _load_json(text: str) -> tuple[bool, Any]:
    candidate = text[1:] if text.startswith("﻿") else text
    try:
        value = json.loads(
            candidate,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
            parse_int=_json_int,
        )
    except _DuplicateKey:
        raise YamlSyntaxError("Map keys must be unique") from None
    except ValueError:
        return False, None
    return True, value


def load_document(text: str) -> Any:
    """Parse a YAML 1.2 or JSON document into dict/list/str/int/float/bool/None values.

    Raises :class:`YamlSyntaxError`, :class:`YamlTagWarning`, :class:`YamlAliasError`
    or ``RecursionError`` for pathologically deep documents.
    """
    is_json, value = _load_json(text)
    if is_json:
        return value
    root, warned = _compose(text)
    if warned:
        raise YamlTagWarning("Unsupported YAML tags or directives")
    if root is None:
        return None
    return _Converter().convert(root)
