"""Translate the safe n8n expression subset to Activepieces references (port of ``lib/expressions.js``)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any, TypedDict

from .jscompat import UNDEFINED, is_array, js_items, js_string, js_trim, truthy

_PATH = r"(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
_JSON_REF = re.compile(rf"\$json({_PATH})")
_NAMED_REF = re.compile(rf"""\$\((['"])([^'"\\]+)\1\)\.item\.json({_PATH})""")
_OLD_REF = re.compile(rf"""\$node\[(['"])([^'"\\]+)\1\]\.json({_PATH})""")
_TEMPLATE = re.compile(r"\{\{([\s\S]*?)\}\}")
_WEBHOOK_PAYLOAD = re.compile(r"\.(body|headers)(?:\.|\Z)")
_FORBIDDEN = frozenset({"__proto__", "prototype", "constructor"})


class Translation(TypedDict):
    value: Any
    issues: list[str]


def translate_expression(
    value: Any,
    *,
    current: Mapping[str, Any] | None = None,
    sources: Mapping[str, Mapping[str, Any] | None] | None = None,
    allowed: Collection[str] | None = None,
) -> Translation:
    """Return ``{"value": ..., "issues": [...]}``; a rejected value becomes ``None``."""
    sources = {} if sources is None else sources
    allowed = set(sources.keys()) if allowed is None else allowed
    issues: list[str] = []

    def problem(message: str) -> None:
        issues.append(message)
        return None

    def reference(code: str) -> str | None:
        source: Mapping[str, Any] | None = current
        match = _JSON_REF.fullmatch(code)
        if match:
            path = match.group(1)
        else:
            match = _NAMED_REF.fullmatch(code) or _OLD_REF.fullmatch(code)
            if not match:
                return problem("Expression is outside the simple property-reference subset")
            if match.group(2) not in allowed:
                return problem("Expression references a node not guaranteed to have run")
            source = sources.get(match.group(2))
            path = match.group(3)
        if source is None:  # JavaScript ``!source``: every source record is an object
            return problem("Expression has no unambiguous input data source")
        if any(part in _FORBIDDEN for part in path.split(".")):
            return problem("Unsafe expression property path")
        if truthy(source.get("webhook")):
            if path == ".query" or path.startswith(".query."):
                if not truthy(source.get("canonical")):
                    path = ".queryParams" + path[6:]
            elif path and not _WEBHOOK_PAYLOAD.match(path):
                return problem("Webhook metadata property has no supported equivalent")
        return "{{" + js_string(source.get("ref", UNDEFINED)) + path + "}}"

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            if not v.startswith("="):
                if "{{" in v or "}}" in v:
                    return problem("Literal template delimiters could be interpreted by Activepieces")
                return v
            s = v[1:]
            if "{{" not in s:
                return problem("Expression must use ={{ … }} delimiters")
            end = 0
            result = ""
            for m in _TEMPLATE.finditer(s):
                literal = s[end:m.start()]
                if "{{" in literal or "}}" in literal:
                    return problem("Malformed expression delimiters")
                ref = reference(js_trim(m.group(1)))
                if ref is None:
                    return None
                result += literal + ref
                end = m.end()
            tail = s[end:]
            if not end or "{{" in tail or "}}" in tail:
                return problem("Malformed expression delimiters")
            return result + tail
        if is_array(v):
            return [walk(item) for item in v]
        if isinstance(v, dict):
            translated: dict[Any, Any] = {}
            for key, item in js_items(v):
                if "{{" in key or key.startswith("="):
                    problem("Dynamic object keys are unsupported")
                if key in _FORBIDDEN:
                    problem("Unsafe object key in expression value")
                translated[key] = walk(item)
            return translated
        return v

    return {"value": walk(value), "issues": issues}
