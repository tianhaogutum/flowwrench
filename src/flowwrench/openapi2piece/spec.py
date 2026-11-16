"""Spec loading, local ``$ref`` resolution, identifiers and schema normalization (port of ``src/spec.js``)."""

from __future__ import annotations

import os
import re
import stat as _stat
import unicodedata
from collections.abc import Iterable
from typing import Any, NoReturn

from ._js import (
    UNDEFINED,
    UriError,
    coalesce,
    decode_uri_component,
    get,
    is_finite_number,
    is_integer_value,
    is_number,
    js_keys,
    js_sorted,
    js_string,
    js_stringify,
    js_truthy,
    js_typeof,
    same_value_zero_unique,
    utf8_length,
)
from ._yaml import YamlSyntaxError, YamlTagWarning, load_document

__all__ = [
    "SpecError",
    "MAX_BYTES",
    "fail",
    "identifier",
    "is_object",
    "load_spec",
    "normalize_schema",
    "parse_spec",
    "property_expression",
    "property_type",
    "require_object",
    "resolve_refs",
    "unique_identifier",
]

MAX_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024


class SpecError(Exception):
    """A user-facing specification error; its message is safe to print."""


def fail(message: str) -> NoReturn:
    raise SpecError(message)


def is_object(value: Any) -> bool:
    return isinstance(value, dict)


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{label} must be an object")
    return value


def load_spec(file: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and parse a local spec file (``loadSpec``)."""
    info = os.stat(file)
    if not _stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        fail("Spec must be a regular file of at most 8 MiB")
    with open(file, "rb") as handle:
        data = handle.read()
    # Buffer#toString('utf8') semantics: invalid sequences become U+FFFD, a BOM is kept.
    text = data.decode("utf-8", errors="replace")
    if utf8_length(text) > MAX_BYTES:
        fail("Spec exceeds 8 MiB")
    return parse_spec(text)


_OPENAPI_RE = re.compile(r"3\.(0|1)\.[0-9]+")


def parse_spec(text: str) -> dict[str, Any]:
    """Parse YAML/JSON text, validate the OpenAPI envelope and resolve local references."""
    try:
        doc = load_document(text)
    except YamlSyntaxError:
        raise SpecError("Invalid YAML/JSON syntax") from None
    except YamlTagWarning:
        raise SpecError("Unsupported YAML tags or directives") from None
    except RecursionError:
        # Node parses arbitrarily deep documents; resolveRefs then rejects them.
        raise SpecError("Spec exceeds reference/node/depth limit") from None
    except Exception:
        raise SpecError("Invalid YAML/JSON document or excessive YAML aliases") from None
    require_object(doc, "Spec")
    openapi = get(doc, "openapi")
    if not isinstance(openapi, str) or not _OPENAPI_RE.fullmatch(openapi):
        fail("Only OpenAPI 3.0.x and 3.1.x are supported (not Swagger 2.0)")
    info = require_object(get(doc, "info"), "info")
    if not isinstance(get(info, "title"), str) or not isinstance(get(info, "version"), str):
        fail("info.title and info.version must be strings")
    require_object(get(doc, "paths"), "paths")
    return resolve_refs(doc)


_INDEX_RE = re.compile(r"0|[1-9][0-9]*")
_BAD_POINTER_ESCAPE_RE = re.compile(r"~(?![01])")


def _has_own(target: Any, key: str) -> bool:
    if isinstance(target, dict):
        return key in target
    if isinstance(target, list):
        return key == "length" or (_INDEX_RE.fullmatch(key) is not None and int(key) < len(target))
    return False


def _own(target: Any, key: str) -> Any:
    if isinstance(target, dict):
        return target[key]
    return len(target) if key == "length" else target[int(key)]


def resolve_refs(root: Any) -> Any:
    """Resolve the whole document, including unselected operations. Never loads external resources."""
    visits = 0
    expanded_bytes = 0
    active: set[int] = set()

    def walk(value: Any, depth: int = 0) -> Any:
        nonlocal visits, expanded_bytes
        visits += 1
        if visits > 100000 or depth > 80:
            fail("Spec exceeds reference/node/depth limit")
        if isinstance(value, str):
            expanded_bytes += utf8_length(value)
        if expanded_bytes > MAX_EXPANDED_BYTES:
            fail("Expanded spec exceeds 16 MiB")
        if not isinstance(value, (dict, list)):
            return value
        if id(value) in active:
            fail("Circular $ref or YAML alias is unsupported")
        active.add(id(value))
        result: Any
        if isinstance(value, dict) and "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                fail("Only local JSON Pointer $ref values (#/...) are supported")
            target = root
            try:
                pointer = decode_uri_component(ref[2:])
            except UriError:
                fail("Invalid URI encoding in $ref")
            for part in pointer.split("/"):
                if _BAD_POINTER_ESCAPE_RE.search(part):
                    fail("Invalid JSON Pointer escape in $ref")
                key = part.replace("~1", "/").replace("~0", "~")
                if not _has_own(target, key):
                    fail("Unresolved local $ref")
                target = _own(target, key)
            if not isinstance(target, dict):
                fail("$ref target must be an object")
            siblings = [k for k in js_keys(value) if k != "$ref"]
            if any(k not in ("description", "summary") for k in siblings):
                fail("Structural $ref siblings are unsupported")
            result = dict(walk(target, depth + 1))
            for k in siblings:
                result[k] = walk(value[k], depth + 1)
        elif isinstance(value, list):
            result = []
            for index, item in enumerate(value):
                expanded_bytes += len(str(index))
                result.append(walk(item, depth + 1))
        else:
            result = {}
            for key in js_keys(value):
                expanded_bytes += utf8_length(key)
                result[key] = walk(value[key], depth + 1)
        active.discard(id(value))
        return result

    return walk(root)


_COMBINING_RE = re.compile("[̀-ͯ]")
_NON_IDENTIFIER_RE = re.compile(r"[^a-z0-9]+")
_EDGE_UNDERSCORES_RE = re.compile(r"^_+|_+$")


def identifier(value: Any, fallback: str = "action") -> str:
    """Stable ASCII identifier: NFKD, strip combining marks, lowercase, ``[^a-z0-9]+`` → ``_``."""
    text = unicodedata.normalize("NFKD", js_string(value))
    text = _COMBINING_RE.sub("", text).lower()
    ident = _EDGE_UNDERSCORES_RE.sub("", _NON_IDENTIFIER_RE.sub("_", text))[:80]
    if not ident:
        ident = fallback
    if not ("a" <= ident[0] <= "z"):
        ident = f"op_{ident}"
    return ident


def unique_identifier(value: Any, used: set[str]) -> str:
    base = identifier(value)
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


_SUPPORTED = frozenset(
    [
        "type", "nullable", "title", "description", "format", "default", "example", "examples", "deprecated",
        "oneOf", "enum", "properties", "required", "additionalProperties", "items", "minimum", "maximum",
        "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
        "readOnly", "writeOnly", "xml", "externalDocs", "$schema",
    ]
)
_ANNOTATIONS = frozenset(
    ["oneOf", "title", "description", "format", "default", "example", "examples",
     "deprecated", "readOnly", "writeOnly", "xml", "externalDocs", "$schema"]
)
_SCALAR_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")
_BOUND_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems")
_COUNT_KEYS = frozenset(["minLength", "maxLength", "minItems", "maxItems"])
_ONE_OF_SIBLING_MESSAGE = (
    "supported wrappers are type: object with properties, required and additionalProperties, "
    "type: string (optionally nullable) with enum, minLength and maxLength, "
    "type: number/integer (optionally nullable) with enum, minimum, maximum, exclusiveMinimum and exclusiveMaximum, "
    "type: boolean (optionally nullable) with enum, "
    "or type: array (optionally nullable) with items, minItems, maxItems and uniqueItems"
)


def _includes(values: Any, item: str) -> bool:
    """``Array.prototype.includes`` for a string needle."""
    return any(isinstance(v, str) and v == item for v in values)


def _type_pair(type_value: Any, name: str) -> bool:
    return isinstance(type_value, list) and len(type_value) == 2 and _includes(type_value, name) and _includes(type_value, "null")


def normalize_schema(
    input: Any,
    label: str = "schema",
    depth: int = 0,
    *,
    project_request: bool = False,
    property: bool = False,
) -> dict[str, Any]:
    """A documented subset, with unsupported validation keywords rejected rather than ignored."""
    require_object(input, label)
    if depth > 30:
        fail(f"{label}: schema nesting limit exceeded")
    keys = js_keys(input)
    for k in keys:
        if k not in _SUPPORTED and not k.startswith("x-"):
            fail(f"{label}: unsupported schema keyword {k}")
    for k in ("nullable", "readOnly", "writeOnly", "deprecated"):
        if k in input and not isinstance(input[k], bool):
            fail(f"{label}: {k} must be a boolean")
    schema_format = input.get("format")
    if isinstance(schema_format, str) and schema_format in ("binary", "byte"):
        fail(f"{label}: binary/byte schemas are unsupported")
    if input.get("readOnly") is True and input.get("writeOnly") is True:
        fail(f"{label}: readOnly and writeOnly cannot both be true")
    if input.get("readOnly") is True and (not project_request or not property or "oneOf" in input):
        fail(f"{label}: readOnly request schemas are unsupported outside OpenAPI 3.0 object properties without composition")
    if "oneOf" in input:
        return _normalize_one_of(input, keys, label, depth)

    type_value = get(input, "type")
    nullable = input.get("nullable") is True
    if isinstance(type_value, list):
        nullable = _includes(type_value, "null")
        rest = [t for t in type_value if not (isinstance(t, str) and t == "null")]
        if len(rest) != 1 or same_value_zero_unique(type_value) != len(type_value):
            fail(f"{label}: only a single type plus null is supported")
        type_value = rest[0]
    if type_value is None or type_value is UNDEFINED:
        type_value = "object" if js_truthy(get(input, "properties")) or "additionalProperties" in input else UNDEFINED
    if not (isinstance(type_value, str) and type_value in _SCALAR_TYPES):
        fail(f"{label}: explicit supported schema type required")
    if "uniqueItems" in input and type_value != "array":
        fail(f"{label}: uniqueItems requires type: array")
    if type_value == "null":
        nullable = True
    result: dict[str, Any] = {"type": type_value, "nullable": nullable}
    for key in _BOUND_KEYS:
        if key in input:
            v = input[key]
            if key in ("exclusiveMinimum", "exclusiveMaximum") and isinstance(v, bool):
                if v:
                    result[key] = get(input, "minimum" if key == "exclusiveMinimum" else "maximum")
                if v and not is_number(result[key]):
                    fail(f"{label}: exclusive bound requires minimum/maximum")
            else:
                if not is_finite_number(v):
                    fail(f"{label}: invalid {key}")
                if key in _COUNT_KEYS and (not is_integer_value(v) or v < 0):
                    fail(f"{label}: invalid {key}")
                result[key] = v
    if "enum" in input:
        enum = input["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or any(v is not None and js_typeof(v) not in ("string", "number", "boolean") for v in enum)
        ):
            fail(f"{label}: only nonempty scalar enums are supported")
        if any(is_number(v) and not is_finite_number(v) for v in enum):
            fail(f"{label}: enum numbers must be finite")
        result["enum"] = enum

        def mismatch(v: Any) -> bool:
            if v is None:
                return not nullable
            if type_value == "integer":
                return not is_integer_value(v)
            return js_typeof(v) != type_value

        if any(mismatch(v) for v in enum):
            fail(f"{label}: enum values must match the schema type/nullability")
    if type_value == "object":
        props = coalesce(get(input, "properties"), {})
        require_object(props, f"{label}.properties")
        required = coalesce(get(input, "required"), [])
        if (
            not isinstance(required, list)
            or any(not isinstance(k, str) for k in required)
            or same_value_zero_unique(required) != len(required)
        ):
            fail(f"{label}: invalid required list")
        properties: dict[str, Any] = {}
        result["properties"] = properties
        read_only: list[str] = []
        for k in js_sorted(js_keys(props)):
            # Validate even excluded schemas: projection must not hide unsupported
            # keywords or invalid annotations. Never mutate the shared source schema.
            child = normalize_schema(props[k], f"{label}.{k}", depth + 1, project_request=project_request, property=True)
            if project_request and props[k].get("readOnly") is True:
                if k not in read_only:
                    read_only.append(k)
            else:
                properties[k] = child
        if read_only:
            result["readOnlyProperties"] = list(read_only)
        result["required"] = [k for k in required if k not in read_only]
        additional = get(input, "additionalProperties")
        if isinstance(additional, dict):
            result["additionalProperties"] = normalize_schema(
                additional, f"{label} additionalProperties", depth + 1, project_request=project_request
            )
        else:
            result["additionalProperties"] = coalesce(additional, True)
        if not isinstance(result["additionalProperties"], (bool, dict)):
            fail(f"{label}: invalid additionalProperties")
    if type_value == "array":
        if "uniqueItems" in input:
            if not isinstance(input["uniqueItems"], bool):
                fail(f"{label}: uniqueItems must be a boolean")
            result["uniqueItems"] = input["uniqueItems"]
        result["items"] = normalize_schema(get(input, "items"), f"{label}.items", depth + 1, project_request=project_request)
    return result


def _normalize_one_of(input: dict[str, Any], keys: list[Any], label: str, depth: int) -> dict[str, Any]:
    one_of = input["oneOf"]
    if not isinstance(one_of, list) or not one_of:
        fail(f"{label}: oneOf must be a nonempty array")
    if len(one_of) > 16:
        fail(f"{label}: oneOf supports at most 16 alternatives")
    siblings = [k for k in keys if k not in _ANNOTATIONS and not k.startswith("x-")]
    type_value = get(input, "type")
    boolean_wrapper = type_value == "boolean" or _type_pair(type_value, "boolean")
    string_wrapper = type_value == "string" or _type_pair(type_value, "string")
    numeric_wrapper = (isinstance(type_value, str) and type_value in ("number", "integer")) or (
        isinstance(type_value, list)
        and len(type_value) == 2
        and _includes(type_value, "null")
        and (_includes(type_value, "number") or _includes(type_value, "integer"))
    )
    array_wrapper = type_value == "array" or _type_pair(type_value, "array")
    object_wrapper = type_value == "object" or _type_pair(type_value, "object")
    if numeric_wrapper:
        allowed: Iterable[str] = ("type", "nullable", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
    elif string_wrapper:
        allowed = ("type", "nullable", "enum", "minLength", "maxLength")
    elif boolean_wrapper:
        allowed = ("type", "nullable", "enum")
    elif array_wrapper:
        allowed = ("type", "nullable", "items", "minItems", "maxItems", "uniqueItems")
    elif object_wrapper:
        allowed = ("type", "nullable", "properties", "required", "additionalProperties")
    else:
        allowed = ("type", "properties", "required", "additionalProperties")
    for k in siblings:
        if k not in allowed:
            fail(f"{label}: oneOf validation sibling {k} is unsupported; {_ONE_OF_SIBLING_MESSAGE}")
    constraints: dict[str, Any] | None = None
    if siblings:
        if not (boolean_wrapper or string_wrapper or numeric_wrapper or array_wrapper or object_wrapper):
            fail(
                f"{label}: oneOf validation siblings require explicit type: object (optionally nullable), "
                "string (optionally nullable), number/integer (optionally nullable), array (optionally nullable), "
                "or boolean (optionally nullable)"
            )
        wrapper = {k: v for k, v in input.items() if k != "oneOf"}
        # Keep the intersection separate: merging wrapper constraints into branches
        # would change their properties/items/bounds and exactly-one matches.
        constraints = normalize_schema(wrapper, f"{label} oneOf wrapper", depth + 1)
    branches = [normalize_schema(s, f"{label}.oneOf[{i}]", depth + 1) for i, s in enumerate(one_of)]
    result: dict[str, Any] = {"type": "oneOf", "nullable": False, "oneOf": branches}
    if constraints is not None:
        result["constraints"] = constraints
    return result


_METHODS = {"string": "ShortText", "number": "Number", "boolean": "Checkbox", "object": "Object", "array": "Array"}


def property_expression(schema: dict[str, Any], *, display_name: str, required: bool, description: str | None = None) -> str:
    """Source for the Activepieces ``Property.*`` control that holds one input."""
    options: dict[str, Any] = {"displayName": display_name, "required": required}
    if js_truthy(description):
        options["description"] = description
    kind = property_type(schema)
    method = _METHODS.get(kind)
    # Required by the real framework metadata schema even for unstructured arrays.
    if method == "Array":
        options["properties"] = {}
    enum = schema.get("enum")
    if js_truthy(enum) and schema.get("type") != "object" and schema.get("type") != "array":
        method = "StaticDropdown"
        options["options"] = {"options": [{"label": js_string(value), "value": value} for value in enum]}
    return f"Property.{method}({js_stringify(options)})"


def property_type(schema: dict[str, Any]) -> str:
    """Nested JSON fields need no independent UI control; the whole input must fit one real property."""
    types: list[str] = []

    def visit(s: dict[str, Any]) -> None:
        if js_truthy(s.get("oneOf")):
            for branch in s["oneOf"]:
                visit(branch)
        elif s.get("type") != "null":
            kind = "number" if s.get("type") == "integer" else s.get("type")
            if kind not in types:
                types.append(kind)

    visit(schema)
    if not types:
        fail("null-only input cannot be represented by a framework property")
    if len(types) > 1:
        fail(f"oneOf alternatives require incompatible framework properties: {', '.join(js_sorted(types))}")
    return types[0]
