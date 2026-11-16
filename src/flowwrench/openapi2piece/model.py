"""Operation/security model construction with per-operation skip reasons (port of ``src/model.js``)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ._js import (
    UNDEFINED,
    UriError,
    coalesce,
    decode_uri_component,
    get,
    is_safe_integer,
    js_keys,
    js_sorted,
    js_string,
    js_stringify,
    js_truthy,
    object_entries,
    object_keys,
    optional_get,
    utf16_key,
    JS_WHITESPACE_CLASS,
)
from ._url import URLError, parse_url
from .spec import fail, is_object, normalize_schema, property_type, require_object, unique_identifier

__all__ = ["GenerationError", "build_model", "scheme_model", "server_url"]

GENERATOR = "openapi2piece"
GENERATOR_VERSION = "0.1.0"

_METHODS = frozenset(["get", "put", "post", "delete", "patch", "head", "options", "trace"])
_SUPPORTED_METHODS = frozenset(["get", "put", "post", "delete", "patch", "head"])
_BLOCKED_HEADERS = frozenset(
    ["authorization", "proxy-authorization", "host", "cookie", "connection", "content-length",
     "transfer-encoding", "content-type", "accept", "upgrade", "te", "trailer"]
)
_UNSAFE_API_KEY_HEADERS = frozenset(
    ["host", "cookie", "connection", "content-length", "transfer-encoding", "proxy-authorization",
     "content-type", "accept", "upgrade", "te", "trailer"]
)
_TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+")
_SCHEME_PREFIX_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
_SERVER_VARIABLE_RE = re.compile(r"\{([^}]+)\}")
_UNSAFE_RELATIVE_RE = re.compile("[?#\\\\" + JS_WHITESPACE_CLASS + "]")
_UNSAFE_PATH_RE = re.compile("[?#\\\\" + JS_WHITESPACE_CLASS + "]")
_DELIMITER_RE = re.compile(r"[?#\\]")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_JSON_MEDIA_RE = re.compile(r"application/(json|[a-z0-9.+-]+\+json)", re.IGNORECASE | re.ASCII)
_NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class GenerationError(Exception):
    """No operation was selected or supported; ``report`` lists every skip reason."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _ascii_lower(value: str) -> str:
    """Case folding of a JavaScript ``/i`` regex over ASCII-only alternatives."""
    return value.translate({c: c + 32 for c in range(0x41, 0x5B)})


def _error_message(error: BaseException) -> str:
    return str(error)


def server_url(servers: Any) -> str | None:
    if servers is UNDEFINED or (isinstance(servers, list) and not servers):
        return None
    if not isinstance(servers, list):
        fail("servers must be an array")
    server = require_object(servers[0], "server")
    url_template = get(server, "url")
    if not isinstance(url_template, str):
        fail("server.url must be a string")

    def substitute(match: re.Match[str]) -> str:
        value = optional_get(optional_get(get(server, "variables"), match.group(1)), "default")
        if not isinstance(value, str):
            fail("Server variable is missing a string default")
        return value

    url = _SERVER_VARIABLE_RE.sub(substitute, url_template)
    if re.search(r"[{}]", url):
        fail("Unresolved server variable")
    # A local spec has no trustworthy retrieval URL to resolve relative servers
    # against. Require the full absolute API base URL from the action user.
    if not _SCHEME_PREFIX_RE.match(url):
        try:
            decoded = decode_uri_component(url)
        except UriError:
            fail("Server URL has invalid URL encoding")
        if decoded.startswith("//") or _UNSAFE_RELATIVE_RE.search(decoded) or ".." in decoded.split("/"):
            fail("Server URL relative reference must be a safe path without authority, query, fragment or traversal")
        return None
    try:
        parsed = parse_url(url)
    except URLError:
        fail("Server URL must be absolute HTTP(S) or relative to the API host")
    if parsed.protocol not in ("http:", "https:") or parsed.username or parsed.password or parsed.search or parsed.hash:
        fail("Server URL must use HTTP(S), without credentials, query or fragment")
    href = parsed.href
    return href[:-1] if href.endswith("/") else href


def scheme_model(name: str, value: Any, used: set[str]) -> dict[str, Any]:
    require_object(value, "securityScheme")
    scheme_id = unique_identifier(f"auth_{name}", used)
    scheme: dict[str, Any] = {"id": scheme_id, "name": name}
    kind = get(value, "type")
    if kind == "apiKey":
        location = get(value, "in")
        if not (isinstance(location, str) and location in ("header", "query")):
            fail("Only header/query apiKey security schemes are supported")
        key = get(value, "name")
        if not isinstance(key, str) or not key:
            fail("apiKey requires a name")
        if location == "header" and (not _TOKEN_RE.fullmatch(key) or _ascii_lower(key) in _UNSAFE_API_KEY_HEADERS):
            fail("Unsafe apiKey header name")
        return {**scheme, "kind": "apiKey", "in": location, "key": key}
    http_scheme = get(value, "scheme")
    if kind == "http" and isinstance(http_scheme, str) and http_scheme.lower() in ("basic", "bearer"):
        http_kind = http_scheme.lower()
        if http_kind == "basic":
            return {
                **scheme,
                "kind": http_kind,
                "usernameField": unique_identifier(f"{scheme_id}_username", used),
                "passwordField": unique_identifier(f"{scheme_id}_password", used),
            }
        return {**scheme, "kind": http_kind}
    if kind == "oauth2":
        flow = optional_get(get(value, "flows"), "authorizationCode")
        if not js_truthy(flow):
            fail("Only OAuth2 authorizationCode flow is supported")
        for key in ("authorizationUrl", "tokenUrl"):
            try:
                parsed = parse_url(js_string(get(flow, key)))
            except URLError:
                fail(f"OAuth2 {key} must be an absolute URL")
            if parsed.protocol != "https:" or parsed.username or parsed.password or parsed.hash or parsed.search:
                fail("OAuth2 URLs require HTTPS without credentials, query or fragment")
        scopes = coalesce(get(flow, "scopes"), {})
        require_object(scopes, "OAuth2 scopes")
        return {
            **scheme,
            "kind": "oauth2",
            "authUrl": get(flow, "authorizationUrl"),
            "tokenUrl": get(flow, "tokenUrl"),
            "scopes": js_sorted(object_keys(scopes)),
        }
    fail(f"Unsupported security scheme type {js_string(kind)}")


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _operation_model(
    doc: dict[str, Any],
    path: str,
    item: dict[str, Any],
    method: str,
    op: dict[str, Any],
    schemes: dict[str, dict[str, Any]],
    scheme_errors: dict[str, str],
) -> dict[str, Any]:
    upper = method.upper()
    if method not in _SUPPORTED_METHODS:
        fail(f"HTTP {upper} is unsupported by the target HttpMethod enum")
    if not path.startswith("/") or path.startswith("//") or _UNSAFE_PATH_RE.search(path):
        fail("Invalid or unsafe operation path")
    try:
        decoded = decode_uri_component(path)
    except UriError:
        fail("Invalid URL encoding in operation path")
    if any(p in (".", "..") for p in decoded.split("/")) or _DELIMITER_RE.search(decoded):
        fail("Unsafe path traversal or URL delimiter")
    require_object(op, "operation")
    responses = require_object(get(op, "responses"), "operation.responses")
    if not responses:
        fail("operation.responses must not be empty")
    callbacks = get(op, "callbacks")
    if js_truthy(callbacks) and object_keys(callbacks):
        fail("Callbacks are unsupported")
    security = coalesce(get(op, "security"), get(doc, "security"), [])
    if not isinstance(security, list):
        fail("security must be an array")
    security_choice = len(security) > 1
    if security_choice and len(security) > 16:
        fail("Alternative security supports at most 16 requirements")
    scopes_by_name: dict[str, list[str]] = {}
    requirement_keys: set[str] = set()
    groups: list[dict[str, Any]] = []
    for requirement in security:
        entries = object_entries(require_object(requirement, "security requirement"))
        names = js_sorted(name for name, _ in entries)
        value = js_stringify(names)
        if value in requirement_keys:
            fail("Alternative security requirements must use distinct scheme sets (OAuth scope variants are unsupported)")
        requirement_keys.add(value)
        groups.append({"value": value, "names": names})
        auth_slots: set[str] = set()
        for name, scopes in entries:
            if name in scheme_errors:
                fail(scheme_errors[name])
            if name not in schemes:
                fail("Undefined security scheme")
            if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
                fail("Security scopes must be a string array")
            scheme = schemes[name]
            if scheme["kind"] == "oauth2" and any(s not in scheme["scopes"] for s in scopes):
                fail("OAuth2 requirement references an undefined scope")
            if scheme["kind"] != "oauth2" and scopes:
                fail("Only OAuth2 schemes may declare scopes")
            scopes_by_name[name] = js_sorted(_dedupe([*scopes_by_name.get(name, []), *scopes]))
            if scheme["kind"] == "apiKey":
                slot_key = scheme["key"].lower() if scheme["in"] == "header" else scheme["key"]
                slot = f"{scheme['in']}:{slot_key}"
            else:
                slot = "header:authorization"
            if slot in auth_slots:
                fail("Security schemes write to the same credential location")
            auth_slots.add(slot)
    requirements = js_sorted(scopes_by_name)
    # Preserve the single-scheme choice contract; compound and anonymous choices
    # use canonical JSON arrays so scheme names cannot alias a requirement.
    security_requirements = (
        sorted(groups, key=lambda g: utf16_key(g["value"]))
        if security_choice and any(len(g["names"]) != 1 for g in groups)
        else None
    )
    params: dict[str, dict[str, Any]] = {}
    for parameters in (coalesce(get(item, "parameters"), []), coalesce(get(op, "parameters"), [])):
        if not isinstance(parameters, list):
            fail("parameters must be an array")
        local: set[str] = set()
        for p in parameters:
            require_object(p, "parameter")
            p_name, p_in = get(p, "name"), get(p, "in")
            if not isinstance(p_name, str) or not p_name or not (
                isinstance(p_in, str) and p_in in ("path", "query", "header", "cookie")
            ):
                fail("Invalid parameter name/location")
            key = f"{p_in}:{p_name.lower() if p_in == 'header' else p_name}"
            if key in local:
                fail("Duplicate parameter in the same parameters list")
            local.add(key)
            params[key] = p
    props: list[dict[str, Any]] = []
    prop_ids = {"base_url", "body", "auth_scheme", "auth_requirement"}
    for _, p in sorted(params.items(), key=lambda kv: utf16_key(kv[0])):
        p_name, p_in = p["name"], p["in"]
        if p_in == "cookie":
            fail("Cookie parameters are unsupported")
        required = get(p, "required")
        if required is not UNDEFINED and not isinstance(required, bool):
            fail("parameter.required must be a boolean")
        description = get(p, "description")
        if description is not UNDEFINED and not isinstance(description, str):
            fail("parameter.description must be a string")
        if p_in == "header" and (_ascii_lower(p_name) in _BLOCKED_HEADERS or not _TOKEN_RE.fullmatch(p_name)):
            fail("Reserved or unsafe header parameter")
        if p_in == "path" and required is not True:
            fail("Path parameters must be required")
        if js_truthy(get(p, "content")):
            fail("Parameter content serialization is unsupported")
        if js_truthy(get(p, "allowReserved")) or js_truthy(get(p, "allowEmptyValue")):
            fail("allowReserved/allowEmptyValue parameters are unsupported")
        schema = normalize_schema(get(p, "schema"), f"parameter {p_name}")
        if js_truthy(schema.get("nullable")) or js_truthy(schema.get("oneOf")) or schema["type"] in ("array", "object", "null"):
            fail("Only non-null scalar path/query/header parameters are supported")
        expected_style = "form" if p_in == "query" else "simple"
        style = get(p, "style")
        if style is not UNDEFINED and not (isinstance(style, str) and style == expected_style):
            fail("Unsupported parameter serialization style")
        explode = get(p, "explode")
        if explode is not UNDEFINED and not isinstance(explode, bool):
            fail("explode must be a boolean")
        prop_id = unique_identifier(f"{p_in}_{p_name}", prop_ids)
        props.append(
            {
                "id": prop_id,
                "in": p_in,
                "name": p_name,
                "required": required is True,
                "schema": schema,
                "description": coalesce(description, ""),
            }
        )
    placeholders = _PLACEHOLDER_RE.findall(path)
    if re.search(r"[{}]", _PLACEHOLDER_RE.sub("", path)):
        fail("Invalid path template")
    path_names = [p["name"] for p in props if p["in"] == "path"]
    if any(name not in path_names for name in placeholders) or any(name not in placeholders for name in path_names):
        fail("Path template and path parameters do not match")
    body: dict[str, Any] | None = None
    request_body = get(op, "requestBody")
    if js_truthy(request_body):
        if method in ("get", "head"):
            fail("GET/HEAD request bodies are unsupported")
        rb = require_object(request_body, "requestBody")
        rb_required = get(rb, "required")
        if rb_required is not UNDEFINED and not isinstance(rb_required, bool):
            fail("requestBody.required must be a boolean")
        rb_description = get(rb, "description")
        if rb_description is not UNDEFINED and not isinstance(rb_description, str):
            fail("requestBody.description must be a string")
        content = require_object(get(rb, "content"), "requestBody.content")
        types = js_sorted(js_keys(content))
        # Explicitly skip multi-content bodies instead of quietly choosing an encoding.
        if len(types) != 1 or not _JSON_MEDIA_RE.fullmatch(types[0]):
            fail("Only a single JSON request body media type is supported (no multipart/form data)")
        openapi = get(doc, "openapi")
        body = {
            "id": "body",
            "name": "JSON body",
            "in": "body",
            "required": rb_required is True,
            "schema": normalize_schema(
                get(content[types[0]], "schema"),
                "request body",
                0,
                project_request=isinstance(openapi, str) and openapi.startswith("3.0."),
            ),
            "description": coalesce(rb_description, ""),
            "mediaType": types[0],
        }
        property_type(body["schema"])  # Reject unrepresentable alternatives in the per-operation report.
        props.append(body)
    # Protect security fields from accidental replacement by ordinary parameters.
    for name in requirements:
        s = schemes[name]
        if s["kind"] == "apiKey" and any(
            p["in"] == s["in"] and (p["name"].lower() == s["key"].lower() if p["in"] == "header" else p["name"] == s["key"])
            for p in props
        ):
            fail("Security scheme conflicts with an ordinary parameter")
    targets = [
        {**schemes[n], "scopes": js_sorted(scopes_by_name[n])} if schemes[n]["kind"] == "oauth2" else schemes[n]
        for n in requirements
    ]
    action: dict[str, Any] = {
        "method": upper,
        "path": path,
        "operationId": coalesce(get(op, "operationId"), None),
        "displayName": _first_truthy(get(op, "summary"), get(op, "operationId"), f"{upper} {path}"),
        "description": coalesce(get(op, "description"), ""),
        "tags": coalesce(get(op, "tags"), []),
        "props": props,
        "body": body,
        "security": targets,
    }
    if security_choice:
        action["securityChoice"] = True
    if security_requirements:
        action["securityRequirements"] = security_requirements
    action["baseUrl"] = server_url(coalesce(get(op, "servers"), get(item, "servers"), get(doc, "servers")))
    return action


def _first_truthy(*values: Any) -> Any:
    """``a || b || c``."""
    for value in values[:-1]:
        if js_truthy(value):
            return value
    return values[-1]


def _invalid_metadata(op: Any) -> bool:
    if not is_object(op):
        return True
    if any(get(op, k) is not UNDEFINED and not isinstance(op[k], str) for k in ("operationId", "summary", "description")):
        return True
    tags = get(op, "tags")
    return tags is not UNDEFINED and (not isinstance(tags, list) or any(not isinstance(t, str) for t in tags))


def build_model(
    doc: dict[str, Any],
    *,
    name: Any,
    include_tags: Sequence[str] | None = None,
    include_ops: Sequence[str] | None = None,
    max_actions: int | None = None,
) -> dict[str, Any]:
    """Build the generation model; unsupported operations are skipped with reasons."""
    include_tags = [] if include_tags is None else include_tags
    include_ops = [] if include_ops is None else include_ops
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or len(name) > 64:
        fail("--name must be a lowercase slug (1–64 characters, starting with a letter)")
    if (
        not isinstance(include_tags, (list, tuple))
        or not isinstance(include_ops, (list, tuple))
        or any(not isinstance(x, str) or not x for x in [*include_tags, *include_ops])
    ):
        fail("Filters must contain nonempty strings")
    if max_actions is not None and (not is_safe_integer(max_actions) or max_actions < 1):
        fail("--max-actions must be a positive integer")
    schemes: dict[str, dict[str, Any]] = {}
    scheme_errors: dict[str, str] = {}
    ids: set[str] = set()
    security_schemes = coalesce(optional_get(get(doc, "components"), "securitySchemes"), {})
    for key, value in sorted(object_entries(security_schemes), key=lambda kv: utf16_key(kv[0])):
        try:
            schemes[key] = scheme_model(key, value, ids)
        except Exception as error:  # noqa: BLE001 - JS catches every error here.
            scheme_errors[key] = _error_message(error)
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    used: set[str] = set()
    paths = get(doc, "paths")
    for path in js_sorted(object_keys(paths)):
        if path.startswith("x-"):
            continue
        item = require_object(get(paths, path), "Path Item")
        for method in js_sorted(m for m in js_keys(item) if m in _METHODS):
            op = item[method]
            entry = {"method": method.upper(), "path": path, "operationId": coalesce(optional_get(op, "operationId"), None)}
            if _invalid_metadata(op):
                skipped.append({**entry, "reason": "Invalid operation metadata (expected strings and a string tags array)"})
                continue
            tags = get(op, "tags")
            if include_tags and not any(isinstance(tags, list) and t in tags for t in include_tags):
                skipped.append({**entry, "reason": "Excluded by --include-tags"})
                continue
            operation_id = get(op, "operationId")
            if include_ops and operation_id not in include_ops and f"{method.upper()} {path}" not in include_ops:
                skipped.append({**entry, "reason": "Excluded by --include-ops"})
                continue
            if js_truthy(max_actions) and len(actions) >= max_actions:  # type: ignore[operator]
                skipped.append({**entry, "reason": "Excluded by --max-actions"})
                continue
            try:
                action = _operation_model(doc, path, item, method, op, schemes, scheme_errors)
                action["id"] = unique_identifier(operation_id if js_truthy(operation_id) else f"{method}_{path}", used)
                if js_truthy(operation_id) and action["id"] != js_string(operation_id).lower():
                    warnings.append({**entry, "message": f"operationId sanitized or renamed to {action['id']}"})
                actions.append(action)
            except Exception as error:  # noqa: BLE001 - JS reports every error as a skip reason.
                skipped.append({**entry, "reason": _error_message(error)})
    if not actions:
        raise GenerationError(
            "No operations selected or supported; inspect the attached report / CLI error details",
            {"generated": [], "skipped": skipped, "warnings": warnings},
        )
    used_schemes: dict[str, dict[str, Any]] = {}
    for action in actions:
        for s in action["security"]:
            existing = used_schemes.get(s["name"])
            used_schemes[s["name"]] = (
                {**s, "scopes": js_sorted(_dedupe([*existing["scopes"], *s["scopes"]]))}
                if existing and s["kind"] == "oauth2"
                else s
            )
    auth_schemes = sorted(used_schemes.values(), key=lambda s: utf16_key(s["id"]))
    auth_kind = "none" if not auth_schemes else "custom" if len(auth_schemes) > 1 else auth_schemes[0]["kind"]
    if auth_kind == "custom":
        warnings.append(
            {
                "message": "CustomAuth uses visible ShortText connection fields: framework 0.32.0 metadata rejects "
                "SecretText children. Use a private connection editor or generate one security scheme at a time."
            }
        )
    if auth_kind == "custom" and any(s["kind"] == "oauth2" for s in auth_schemes):
        warnings.append(
            {"message": "Mixed OAuth2 security uses manually supplied access tokens in CustomAuth; automatic refresh is unavailable"}
        )
    return {
        "name": name,
        "title": get(get(doc, "info"), "title"),
        "actions": actions,
        "auth": {"kind": auth_kind, "schemes": auth_schemes},
        "report": {
            "generator": GENERATOR,
            "version": GENERATOR_VERSION,
            "name": name,
            "openapi": get(doc, "openapi"),
            "generated": [
                {
                    "method": a["method"],
                    "path": a["path"],
                    "operationId": a["operationId"],
                    "name": a["id"],
                    "file": f"src/lib/actions/{a['id']}.ts",
                }
                for a in actions
            ],
            "skipped": skipped,
            "warnings": warnings,
        },
    }
