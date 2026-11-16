"""Explicit credential -> placeholder bindings (port of ``lib/connections.js``)."""

from __future__ import annotations

import re
from typing import Any

from .graph import node_kind
from .input import InputError, validate_json_data
from .jscompat import UNDEFINED, coalesce, is_array, is_object, js_items, js_trim, utf16_length

# Placeholders are local CODE outputs, not account connections or secret values.
HTTP_AUTH: dict[str, dict[str, Any]] = {
    "httpBasicAuth": {"authType": "BASIC", "fields": ("username", "password")},
    "httpBearerAuth": {"authType": "BEARER_TOKEN", "fields": ("token",)},
}
_PLACEHOLDER = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Marker for "no connection map option given" (JavaScript ``undefined``); ``None`` is a JSON null map.
UNSET: Any = UNDEFINED


def _text(value: Any) -> bool:
    return isinstance(value, str) and len(js_trim(value)) > 0 and utf16_length(value) <= 200


def _keys(value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> bool:
    return (is_object(value) and all(k in value for k in required)
            and all(k in required or k in optional for k in value))


def _source_key(source: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (source.get("node"), source.get("type"), source.get("name"))


def connection_bindings(workflow: dict[str, Any], mapping: Any = UNSET) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if mapping is UNSET:
        return result
    validate_json_data(mapping)
    bindings = mapping.get("bindings") if is_object(mapping) else None
    if (not _keys(mapping, ("formatVersion", "bindings")) or mapping["formatVersion"] != 1
            or isinstance(mapping["formatVersion"], bool) or not is_array(bindings) or len(bindings) > 500):
        raise InputError("Invalid connection map: expected formatVersion 1 and up to 500 bindings")
    # Source IDs are comparison-only: never attach them to returned bindings.
    expected: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    credential_ids: dict[tuple[str, str], str] = {}
    for n in workflow["nodes"]:
        credentials = coalesce(n.get("credentials"), {})
        for cred_type, ref in js_items(credentials):
            if (not _keys(ref, ("name",), ("id",)) or not _text(ref["name"])
                    or ("id" in ref and not _text(ref["id"]))):
                raise InputError(
                    "Connection map requires credential references with name and optional id only; never credential values"
                )
            params = n["parameters"]
            if (node_kind(n) != "httpRequest" or params.get("authentication") != "genericCredentialType"
                    or params.get("genericAuthType") != cred_type or cred_type not in HTTP_AUTH
                    or len(credentials) != 1):
                raise InputError("Connection map supports only one HTTP Basic or Bearer generic credential per HTTP Request node")
            source = {"node": n["name"], "type": cred_type, "name": ref["name"]}
            credential_key = (cred_type, ref["name"])
            if "id" in ref:
                if credential_key in credential_ids and credential_ids[credential_key] != ref["id"]:
                    raise InputError(
                        "Conflicting source credential IDs for the same type/name; disambiguate source references before mapping"
                    )
                credential_ids[credential_key] = ref["id"]
            expected[_source_key(source)] = {"source": source, "id": ref.get("id")}
    # Auth enabled without an exported credential reference must never become NONE.
    for n in workflow["nodes"]:
        if (node_kind(n) == "httpRequest" and coalesce(n["parameters"].get("authentication"), "none") != "none"
                and not coalesce(n.get("credentials"), {})):
            raise InputError("Missing source credential reference for connection binding")
    aliases: dict[str, dict[str, Any]] = {}
    sources: set[tuple[Any, Any, Any]] = set()
    for binding in bindings:
        if (not _keys(binding, ("source", "placeholder")) or not _keys(binding["source"], ("node", "type", "name"))
                or not all(_text(v) for v in binding["source"].values())
                or not isinstance(binding["placeholder"], str) or not _PLACEHOLDER.fullmatch(binding["placeholder"])):
            raise InputError(
                "Invalid connection binding: exact source node/type/name and a simple placeholder alias are required; no credential values"
            )
        key = _source_key(binding["source"])
        if key in sources:
            raise InputError("Ambiguous connection map: duplicate source")
        if key not in expected:
            raise InputError("Connection binding does not match an exported source node/type/name")
        identity = expected[key]
        source, source_id = identity["source"], identity["id"]
        auth = HTTP_AUTH[source["type"]]
        placeholder: str = binding["placeholder"]
        previous = aliases.get(placeholder)
        if previous is not None and (source_id is None or previous["id"] != source_id
                                     or previous["source"]["type"] != source["type"]
                                     or previous["source"]["name"] != source["name"]):
            raise InputError(
                "Ambiguous connection map: shared placeholder requires matching credential type/name and a nonempty source ID on every reference"
            )
        result[source["node"]] = {"source": source, "placeholder": placeholder, "step": f"connection_{placeholder}",
                                  "fields": list(auth["fields"]), "status": "unresolved"}
        sources.add(key)
        aliases[placeholder] = identity
    if len(sources) != len(expected):
        raise InputError("Missing connection binding for an exported credential reference")
    return result


def http_auth_input(binding: dict[str, Any]) -> dict[str, Any]:
    return {"authType": HTTP_AUTH[binding["source"]["type"]]["authType"],
            "authFields": {field: f"{{{{{binding['step']}.output.{field}}}}}" for field in binding["fields"]}}
