"""Resolve a recorded path into raw HA configuration without normalizing its JSON.

Rules follow HA frontend data/trace.ts and data/automation.ts. JSON Pointer
encoding and resolution remain provided by python-json-pointer.
"""

from __future__ import annotations

from typing import Any

from .loader import TraceDocument

ROOT_KEYS = {
    "trigger": ("trigger", "triggers"),
    "condition": ("condition", "conditions"),
    "action": ("action", "actions"),
    "sequence": ("sequence",),
}


def _flat_triggers(value: Any, parts: tuple) -> list[tuple[Any, tuple]]:
    """Flatten nested trigger groups while retaining every original array index."""
    children = list(enumerate(value)) if isinstance(value, list) else [(None, value)]
    result = []
    for index, trigger in children:
        here = parts + (index,) if index is not None else parts
        if not isinstance(trigger, dict):
            continue
        if "triggers" in trigger:
            result.extend(_flat_triggers(trigger["triggers"], here + ("triggers",)))
        else:
            result.append((trigger, here))
    return result


def _index(token: str) -> int | None:
    if (
        not token.isascii()
        or not token.isdecimal()
        or len(token) > 9
        or (len(token) > 1 and token.startswith("0"))
    ):
        return None
    return int(token)


def configuration_for_path(
    document: TraceDocument, path: str, *, trigger_cache: dict | None = None
) -> dict:
    config = document.payload.get("config")
    if not isinstance(config, dict):
        return {
            "status": "unavailable",
            "reason": "No recorded configuration",
            "reference": None,
            "label": None,
            "label_reference": None,
            "unresolved_path": path,
        }
    tokens = path.split("/")
    if tokens == ["trigger"]:
        return {
            "status": "manual_trigger",
            "reason": "Manual invocation has no configured trigger",
            "reference": None,
            "label": None,
            "label_reference": None,
            "unresolved_path": "",
        }
    node = config
    parts = document.parts + ("config",)
    label = None
    label_ref = None

    def remember_label():
        nonlocal label, label_ref
        if isinstance(node, dict):
            for key in (
                "alias",
                "action",
                "service",
                "condition",
                "trigger",
                "platform",
                "event",
                "stop",
            ):
                if isinstance(node.get(key), str) and node[key]:
                    label = node[key]
                    label_ref = document.ref(parts + (key,))
                    break

    remember_label()
    consumed = 0
    reason = ""
    while consumed < len(tokens):
        token = tokens[consumed]
        if consumed == 0:
            keys = ROOT_KEYS.get(token, ())
            found = [key for key in keys if key in node]
            if not found:
                reason = "Trace root is absent from the recorded configuration"
                break
            key = found[0]
            node = node[key]
            parts += (key,)
            consumed += 1
            if token == "trigger" and consumed < len(tokens):
                ordinal = _index(tokens[consumed])
                if trigger_cache is not None and key in trigger_cache:
                    flat = trigger_cache[key]
                else:
                    flat = _flat_triggers(node, parts)
                    if trigger_cache is not None:
                        trigger_cache[key] = flat
                if ordinal is None or ordinal >= len(flat):
                    reason = "Trigger index is absent from the flattened recorded configuration"
                    break
                node, parts = flat[ordinal]
                consumed += 1
            remember_label()
            continue
        if token.isascii() and token.isdecimal():
            index = _index(token)
            if index is None:
                reason = "Trace index is not a supported canonical integer"
                break
            if isinstance(node, list) and index < len(node):
                node = node[index]
                parts += (index,)
            elif not isinstance(node, list) and index == 0:
                # HA traces a singleton action/condition as index 0 even when its
                # original YAML/JSON is not a list. There is no raw /0 segment.
                pass
            else:
                reason = "Execution index is absent from the recorded configuration"
                break
        elif token == "condition" and consumed > 0 and tokens[consumed - 1] == "if":
            # Core emits /if/condition/N; raw config keeps the list directly in if.
            pass
        elif isinstance(node, dict) and token in node:
            node = node[token]
            parts += (token,)
        elif token == "sequence" and (
            isinstance(node, list) or (isinstance(node, dict) and "sequence" not in node)
        ):
            # A sequence wrapper can be implicit around an original action list.
            pass
        else:
            reason = "No exact configuration field for the remaining trace path"
            break
        consumed += 1
        remember_label()
    status = "exact" if consumed == len(tokens) else "partial"
    return {
        "status": status,
        "reason": reason,
        "reference": document.ref(parts, include_value=False),
        "label": label,
        "label_reference": label_ref,
        "unresolved_path": "/".join(tokens[consumed:]),
    }
