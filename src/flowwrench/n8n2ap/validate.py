"""Structural validation of the generated template (port of ``lib/validate.js``)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

from .input import ConversionError
from .jscompat import (
    is_array,
    is_finite_number,
    is_object,
    js_items,
    js_string,
    js_typeof,
    recursion_headroom,
    strict_equal,
)

SCHEMA_DIRECTORY = "schema"
SCHEMA_FILE = "activepieces-template.schema.json"


def load_template_schema() -> dict[str, Any]:
    """The checked-in JSON Schema (package data, read via :mod:`importlib.resources`)."""
    resource = resources.files("flowwrench.n8n2ap").joinpath(SCHEMA_DIRECTORY).joinpath(SCHEMA_FILE)
    text = resource.read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


template_schema: dict[str, Any] = load_template_schema()

_AUTH_REFERENCE = re.compile(r"\{\{(connection_[a-z][a-z0-9_]{0,63})\.output\.([a-z]+)\}\}")
_AUTH_FIELDS: dict[str, tuple[str, ...]] = {"NONE": (), "BASIC": ("username", "password"), "BEARER_TOKEN": ("token",)}


@lru_cache(maxsize=None)
def _pattern(source: str) -> re.Pattern[str]:
    # ECMAScript ``$`` (no m flag) matches only at the very end; Python's also
    # matches before a final newline, so translate a trailing unescaped ``$``.
    if source.endswith("$") and not source.endswith("\\$"):
        source = source[:-1] + r"\Z"
    return re.compile(source)


def _schema_type(value: Any) -> str:
    if value is None:
        return "null"
    if is_array(value):
        return "array"
    return js_typeof(value)


def _check(value: Any, schema: dict[str, Any], path: str) -> str | None:
    # Small, non-executing validator for exactly the JSON Schema keywords in our
    # checked-in schema. Plain loops keep deep step chains off the C stack.
    if "$ref" in schema:
        return _check(value, template_schema["$defs"][schema["$ref"].split("/")[-1]], path)
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            if _check(value, variant, path) is None:
                return None
        return f"{path}: no matching schema variant"
    if "const" in schema and not strict_equal(value, schema["const"]):
        return f"{path}: wrong constant"
    if "enum" in schema:
        matched = False
        for option in schema["enum"]:
            if strict_equal(value, option):
                matched = True
                break
        if not matched:
            return f"{path}: unexpected enum"
    expected = schema.get("type")
    if expected:
        actual = _schema_type(value)
        if actual != expected or (actual == "number" and not is_finite_number(value)):
            return f"{path}: expected {expected}"
    if schema.get("pattern") and not _pattern(schema["pattern"]).search(js_string(value)):
        return f"{path}: invalid string pattern"
    if expected == "object":
        for key in schema.get("required", ()):
            if key not in value:
                return f"{path}: missing {key}"
        properties = schema.get("properties") or {}
        for key, child in js_items(value):
            child_schema = properties.get(key)
            if child_schema is None and schema.get("additionalProperties") is False:
                return f"{path}: unexpected property"
            if child_schema is not None:
                error = _check(child, child_schema, f"{path}.{key}")
                if error:
                    return error
    if expected == "array":
        if ("minItems" in schema and len(value) < schema["minItems"]) or (
                "maxItems" in schema and len(value) > schema["maxItems"]):
            return f"{path}: invalid array length"
        for index, item in enumerate(value):
            error = _check(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    return None


def validate_template(template: dict[str, Any]) -> dict[str, Any]:
    with recursion_headroom():
        error = _check(template, template_schema, "template")
        if error:
            raise ConversionError(f"Activepieces output validation failed: {error}")
        names: set[Any] = set()

        def walk(step: Any, available: frozenset[str] = frozenset()) -> None:
            if not step:
                return
            if step["name"] in names:
                raise ConversionError("Duplicate Activepieces step name")
            names.add(step["name"])
            if step["type"] == "ROUTER" and len(step["children"]) != len(step["settings"]["branches"]):
                raise ConversionError("Router child/branch mismatch")
            settings = step["settings"]
            if (step["type"] == "PIECE" and settings.get("pieceName") == "@activepieces/piece-http"
                    and settings.get("actionName") == "send_request"):
                _check_http_auth(settings["input"], available)
            next_available = available | {step["name"]}
            walk(step.get("nextAction"), next_available)
            for child in step.get("children") or ():
                walk(child, next_available)

        walk(template["flows"][0]["trigger"])
    return template


def _check_http_auth(input_: dict[str, Any], available: frozenset[str]) -> None:
    auth_type = input_.get("authType")
    auth_fields = input_.get("authFields")
    required = _AUTH_FIELDS.get(auth_type) if isinstance(auth_type, str) else None
    if (required is None or not is_object(auth_fields) or len(auth_fields) != len(required)
            or not all(field in auth_fields for field in required)):
        raise ConversionError("Activepieces HTTP auth validation failed: unsupported auth type or field shape")
    bindings: set[str] = set()
    for field in required:
        reference = auth_fields[field]
        match = _AUTH_REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if not match or match.group(2) != field or match.group(1) not in available:
            raise ConversionError(
                "Activepieces HTTP auth validation failed: expected an earlier connection placeholder output"
            )
        bindings.add(match.group(1))
    if len(bindings) > 1:
        raise ConversionError("Activepieces HTTP auth validation failed: ambiguous placeholder fields")
