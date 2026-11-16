"""Input bounds, workflow shape validation and secret heuristics (port of ``lib/input.js``)."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from .jscompat import (
    JS_S,
    JS_WS_CLASS_BODY,
    JSONSyntaxError,
    is_array,
    is_finite_number,
    is_number,
    is_object,
    js_items,
    js_trim,
    js_typeof,
    parse_json,
    utf16_length,
)


class Limits(NamedTuple):
    bytes: int
    nodes: int
    depth: int


LIMITS = Limits(bytes=5 * 1024 * 1024, nodes=500, depth=40)

_UNSAFE_KEYS = ("__proto__", "constructor", "prototype")


class InputError(Exception):
    """The workflow, connection map or options are not acceptable input."""


class ConversionError(Exception):
    """A generic converter failure (JavaScript ``Error``, not ``InputError``)."""


def validate_json_data(data: Any) -> None:
    count = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal count
        if depth > LIMITS.depth:
            raise InputError("JSON nesting exceeds 40 levels")
        if is_number(value) and not is_finite_number(value):
            raise InputError("Non-finite JSON number")
        if value is not None and js_typeof(value) not in ("object", "string", "boolean", "number"):
            raise InputError("Expected JSON data only")
        count += 1
        if count > 200000:
            raise InputError("JSON has too many values")
        if isinstance(value, dict):
            for key, child in js_items(value):
                if key in _UNSAFE_KEYS:
                    raise InputError("Unsafe object key in input")
                walk(child, depth + 1)
        elif is_array(value):
            for child in value:
                walk(child, depth + 1)

    walk(data, 0)


def validate_input(workflow: Any) -> Any:
    validate_json_data(workflow)
    nodes = workflow.get("nodes") if is_object(workflow) else None
    if (not is_object(workflow) or not is_array(nodes) or not is_object(workflow.get("connections"))
            or not nodes or len(nodes) > LIMITS.nodes):
        raise InputError(
            "Expected one n8n workflow object with 1–500 nodes and a connections object (no export arrays)"
        )
    names: set[str] = set()
    ids: set[str] = set()
    for n in nodes:
        if (not is_object(n) or not isinstance(n.get("name"), str) or not js_trim(n["name"])
                or utf16_length(n["name"]) > 200 or not isinstance(n.get("type"), str)
                or not is_object(n.get("parameters"))):
            raise InputError("Each node needs a name, type and parameters object")
        if n["name"] in names:
            raise InputError("Duplicate node name")
        if "id" in n and (not isinstance(n["id"], str) or n["id"] in ids):
            raise InputError("Invalid or duplicate node id")
        if "typeVersion" in n and not is_finite_number(n["typeVersion"]):
            raise InputError("Invalid node typeVersion")
        if "credentials" in n and not is_object(n["credentials"]):
            raise InputError("Invalid credentials object")
        names.add(n["name"])
        if "id" in n:
            ids.add(n["id"])
    return workflow


# Deliberately conservative heuristic. No detected secret value enters a report.
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|authorization|cookie|api[-_ ]?key|private[-_ ]?key|credential)",
    re.IGNORECASE | re.ASCII,
)
_TOKEN_VALUE = re.compile(
    r"\b(?:Bearer|Basic)" + JS_S + r"+[A-Za-z0-9+/_.=-]{6,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{12,})",
    re.ASCII,
)
_URL_SECRET = re.compile(
    r"https?://[^" + JS_WS_CLASS_BODY + r"/]*@|[?&](?:[^=&]*(?:token|secret|key|password)[^=&]*)=",
    re.IGNORECASE | re.ASCII,
)
_JSON_LIKE = re.compile(JS_S + r"*[\[{]")
_SAFE_SEGMENT = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,40}")


def sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key) is not None


def find_secrets(value: Any) -> list[str]:
    paths: list[str] = []

    def walk(v: Any, path: str, depth: int = 0) -> None:
        if depth > LIMITS.depth:
            paths.append("parameters.<encoded-depth-limit>")
            return
        if isinstance(v, str):
            if _TOKEN_VALUE.search(v):
                paths.append(path)
            if _URL_SECRET.search(v):
                paths.append(path)
            if _JSON_LIKE.match(v):
                try:
                    decoded = parse_json(v)
                except JSONSyntaxError:
                    pass
                else:
                    try:
                        validate_json_data(decoded)
                    except InputError:
                        paths.append(path + ".<uninspectable-json>")
                    else:
                        walk(decoded, path + ".json", depth + 1)
        if is_array(v):
            for index, item in enumerate(v):
                walk(item, f"{path}[{index}]", depth + 1)
        elif is_object(v):
            pair_name = v.get("name")
            if pair_name is None:
                pair_name = v.get("key")
            if (isinstance(pair_name, str) and sensitive_key(pair_name) and "value" in v
                    and not (isinstance(v["value"], str) and v["value"] == "")):
                paths.append(path + ".value")
            for key, child in js_items(v):
                if sensitive_key(key) and not (isinstance(child, str) and child == "") and child is not None \
                        and child is not False:
                    paths.append(path + ".<sensitive-field>")
                else:
                    segment = key if _SAFE_SEGMENT.fullmatch(key) else "<field>"
                    walk(child, path + "." + segment, depth + 1)

    walk(value, "parameters")
    return list(dict.fromkeys(paths))
