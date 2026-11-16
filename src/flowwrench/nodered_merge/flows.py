"""Node-RED flow validation, normalization, semantic diff and three-way merge.

Port of ``lib/flows.js``.  Never executes function code or loads Node-RED
node packages.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .jsjson import (
    is_js_integer,
    js_keys,
    js_to_string,
    js_truthy,
    parse,
    sorted_js,
    stringify,
    utf16_key,
)

__all__ = [
    "ABSENT",
    "LIMITS",
    "InputError",
    "check_integrity",
    "diff_flows",
    "merge_flows",
    "normalize",
    "parse_json_snapshot",
    "read_flow",
    "read_flow_snapshot",
    "read_json_snapshot",
    "serialize",
    "summarize_merge",
    "validate_config_refs",
    "validate_flow",
    "validate_resolution_decision",
]

Node = dict[str, Any]
Flow = list[Node]


class _Absent:
    """Marker for a missing property/node (distinct from JSON ``null``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"

    def __deepcopy__(self, memo: dict[int, Any]) -> _Absent:
        return self


ABSENT: Final = _Absent()


@dataclass(frozen=True)
class Limits:
    bytes: int = 32 * 1024 * 1024
    nodes: int = 100000
    depth: int = 64


LIMITS: Final = Limits()
LAYOUT: Final = frozenset({"x", "y", "w", "h"})
BUILTIN_CONFIG_REFS: Final = ("mqtt in.broker", "mqtt out.broker")
RESERVED: Final = frozenset({"id", "type", "z", "name", "label", "info", "func"})
STRUCTURAL: Final = ("wires", "in", "out", "status")
NON_RUNTIME_TYPES: Final = ("tab", "subflow", "group")
_CONFIG_REF_FORBIDDEN = re.compile("[\r\n\0]")


class InputError(Exception):
    """Invalid input, unsupported format or rejected operation (exit code 2)."""

    @property
    def message(self) -> str:
        return str(self)


def _own(value: Mapping[str, Any], key: str) -> Any:
    return value[key] if key in value else ABSENT


def _union(*arrays: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for array in arrays:
        for item in array:
            seen[item] = None
    return sorted_js(seen)


def _pointer(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _entries(value: dict[str, Any] | list[Any]) -> Iterable[tuple[str, Any]]:
    """``Object.entries`` for objects and arrays."""
    if isinstance(value, list):
        return ((str(i), item) for i, item in enumerate(value))
    return ((key, value[key]) for key in js_keys(value))


def _lookup(nodes: Mapping[str, Node], key: Any) -> Node | None:
    """``Map.get`` keyed by node ID: non-string keys never match."""
    return nodes.get(key) if isinstance(key, str) else None


def _check_depth(value: Any, depth: int = 0) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise InputError("non-finite JSON numbers are unsupported")
    if depth > LIMITS.depth:
        raise InputError(f"JSON exceeds maximum depth {LIMITS.depth}")
    if isinstance(value, dict):
        for key in js_keys(value):
            _check_depth(value[key], depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_depth(child, depth + 1)


def _valid_subflow_port(port: Any, key: str) -> bool:
    if not isinstance(port, dict) or not isinstance(port.get("wires"), list):
        return False
    for wire in port["wires"]:
        if not isinstance(wire, dict) or not _nonempty_str(wire.get("id")):
            return False
        if "port" in wire and (not is_js_integer(wire["port"]) or wire["port"] < 0):
            return False
        if key != "in" and not is_js_integer(wire.get("port")):
            return False
    return True


def validate_flow(flow: Any, source: str = "flow", initial_depth: int = 0) -> Flow:
    if not isinstance(flow, list):
        raise InputError(f"{source}: expected a flat array of nodes (not API v2, YAML, or credentials)")
    if len(flow) > LIMITS.nodes:
        raise InputError(f"{source}: too many nodes (maximum {LIMITS.nodes})")
    _check_depth(flow, initial_depth)
    ids: set[str] = set()
    for index, node in enumerate(flow):
        if not isinstance(node, dict) or not _nonempty_str(node.get("id")) or not _nonempty_str(node.get("type")):
            raise InputError(f"{source}: node {index} requires nonempty string id and type")
        node_id = node["id"]
        if node_id in ids:
            raise InputError(f"{source}: duplicate node id {stringify(node_id)}")
        ids.add(node_id)
        if "z" in node and not isinstance(node["z"], str):
            raise InputError(f"{source}: {node_id}.z must be a string")
        if "wires" in node:
            wires = node["wires"]
            if not isinstance(wires, list) or any(
                not isinstance(port, list) or any(not _nonempty_str(target) for target in port) for port in wires
            ):
                raise InputError(f"{source}: {node_id}.wires must be an array of arrays of nonempty node IDs")
        if node["type"] == "subflow":
            if "nodes" in node or "configs" in node:
                raise InputError(
                    f"{source}: nested subflow nodes/configs are unsupported; use a flat flows.json export"
                )
            for key in ("in", "out", "status"):
                if key not in node:
                    continue
                ports = [node[key]] if key == "status" else node[key]
                if not isinstance(ports, list) or any(not _valid_subflow_port(port, key) for port in ports):
                    raise InputError(f"{source}: {node_id}.{key} has invalid subflow ports")
    return flow


def _error_code(error: BaseException) -> str:
    number = getattr(error, "errno", None)
    if isinstance(number, int) and number in errno.errorcode:
        return errno.errorcode[number]
    return "invalid JSON"


def _decode_utf8(data: bytes) -> str:
    # TextDecoder('utf-8', { fatal: true }) strips one leading BOM.
    text = data.decode("utf-8")
    return text[1:] if text.startswith("﻿") else text


def parse_json_snapshot(data: bytes, source: str, max_bytes: int = LIMITS.bytes) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise InputError(f"{source}: exceeds {max_bytes} byte limit")
    try:
        text = _decode_utf8(data)
    except UnicodeDecodeError:
        # Node's TextDecoder error code; the message never quotes private input.
        raise InputError(f"{source}: cannot read or parse JSON (ERR_ENCODING_INVALID_ENCODED_DATA)") from None
    try:
        value = parse(text)
    except (ValueError, RecursionError):
        # JSON parse messages may quote private input; report only its source.
        raise InputError(f"{source}: cannot read or parse JSON (invalid JSON)") from None
    return {"value": value, "hash": hashlib.sha256(data).hexdigest()}


def read_json_snapshot(file: str, max_bytes: int = LIMITS.bytes) -> dict[str, Any]:
    try:
        info = os.stat(file)
    except (OSError, ValueError) as error:
        raise InputError(f"{file}: cannot read or parse JSON ({_error_code(error)})") from None
    if not stat.S_ISREG(info.st_mode):
        raise InputError(f"{file}: expected a regular file")
    if info.st_size > max_bytes:
        raise InputError(f"{file}: exceeds {max_bytes} byte limit")
    try:
        data = Path(file).read_bytes()
    except OSError as error:
        # JSON parse messages may quote private input; report only the filename.
        raise InputError(f"{file}: cannot read or parse JSON ({_error_code(error)})") from None
    return parse_json_snapshot(data, file, max_bytes)


def read_flow_snapshot(file: str) -> dict[str, Any]:
    snapshot = read_json_snapshot(file)
    return {"flow": validate_flow(snapshot["value"], file), "hash": snapshot["hash"]}


def read_flow(file: str) -> Flow:
    return read_flow_snapshot(file)["flow"]


def canonical(value: Any) -> Any:
    """Deep copy with object keys sorted (JavaScript string order)."""
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: canonical(value[key]) for key in sorted(value, key=utf16_key)}


def _equal(a: Any, b: Any) -> bool:
    if a is ABSENT or b is ABSENT:
        return a is b
    return stringify(canonical(a)) == stringify(canonical(b))


def normalize(flow: Any) -> Flow:
    validate_flow(flow)
    result: Flow = []
    for node in flow:
        item = canonical(node)
        if "wires" in item:
            item["wires"] = [_union(port) for port in item["wires"]]
        result.append(item)
    result.sort(key=lambda node: utf16_key(node["id"]))
    return result


def serialize(flow: Any) -> str:
    return f"{stringify(normalize(flow), 2)}\n"


def _indexed(flow: Flow) -> dict[str, Node]:
    return {node["id"]: node for node in flow}


def _label(node: Node | None) -> str:
    if node is None:
        return "(missing)"
    for key in ("name", "label"):
        value = node.get(key)
        if js_truthy(value):
            return f"{js_to_string(value)} [{node['id']}]"
    return f"{js_to_string(node.get('type'))} [{node['id']}]"


def _display(value: Any) -> dict[str, Any]:
    return {"present": False} if value is ABSENT else {"present": True, "value": value}


def _config_node(node: Node) -> bool:
    return node.get("type") not in NON_RUNTIME_TYPES and not any(key in node for key in ("x", "y", "wires"))


def _changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if _equal(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict[str, Any]] = []
        for key in _union(before.keys(), after.keys()):
            result.extend(_changes(_own(before, key), _own(after, key), f"{path}/{_pointer(key)}"))
        return result
    return [{"property": path, "before": _display(before), "after": _display(after), "layout": path[1:] in LAYOUT}]


@dataclass
class _ReferenceContext:
    configs: dict[str, Node] = field(default_factory=dict)
    rules: dict[str, list[str]] = field(default_factory=dict)


def _split_ref(ref: str) -> tuple[str, str]:
    split = ref.rfind(".")
    return ref[:split], ref[split + 1 :]


def _reference_context(versions: Sequence[Flow], config_refs: Sequence[str]) -> _ReferenceContext:
    validate_config_refs(config_refs)
    context = _ReferenceContext()
    for flow in versions:
        for node in flow:
            if _config_node(node) and node["id"] not in context.configs:
                context.configs[node["id"]] = node
    for ref in _union(BUILTIN_CONFIG_REFS, config_refs):
        type_, key = _split_ref(ref)
        keys = context.rules.setdefault(type_, [])
        if key not in keys:
            keys.append(key)
    return context


def _references(node: Node | None, context: _ReferenceContext, ignore_layout: bool) -> dict[str, str]:
    refs: dict[str, str] = {}
    if node is None:
        return refs

    def scan(value: Any, path: str) -> None:
        if isinstance(value, str) and value in context.configs:
            refs[path] = value
        elif isinstance(value, (dict, list)):
            for key, child in _entries(value):
                scan(child, f"{path}/{_pointer(key)}")

    for key in js_keys(node):
        if key not in RESERVED and key not in STRUCTURAL and not (ignore_layout and key in LAYOUT):
            scan(node[key], f"/{_pointer(key)}")
    for key in context.rules.get(node["type"], ()):
        if key not in node or node[key] == "" or (ignore_layout and key in LAYOUT):
            continue
        raw = node[key]
        values = raw if isinstance(raw, list) else [raw]
        for i, value in enumerate(values):
            if not isinstance(value, str):
                raise InputError(f"{node['type']}.{key}: expected a config ID string or array of strings")
            refs[f"/{_pointer(key)}{f'/{i}' if isinstance(raw, list) else ''}"] = value
    return refs


def _wire_endpoint_validity(node: Node, target_id: str, nodes: Mapping[str, Node]) -> dict[str, Any]:
    # Ordinary wires compare flows only when both z strings are nonempty.
    # Keep reason priority and acceptance identical for annotations and pruning.
    target = nodes.get(target_id)
    reason: str | None = None
    if target is None:
        reason = "missing-target"
    elif target["type"] in NON_RUNTIME_TYPES or _config_node(target):
        reason = "non-runtime-target"
    elif js_truthy(node.get("z")) and js_truthy(target.get("z")) and node["z"] != target["z"]:
        reason = "wrong-flow"
    return {"valid": reason is None, "reason": reason}


def _subflow_endpoint_validity(node: Node, kind: str, wire: dict[str, Any], nodes: Mapping[str, Node]) -> dict[str, Any]:
    # Called only after validate_flow: ports are nonnegative integers (optional on in).
    # Keep summary annotations and merge pruning on the same local integrity rule.
    reason: str | None = None
    target = nodes.get(wire["id"])
    if wire["id"] == node["id"]:
        if kind == "in":
            reason = "self-input-reference"
        elif wire["port"] >= len(node.get("in") or []):
            reason = "input-port-out-of-range"
    elif target is None:
        reason = "missing-target"
    elif target.get("z") != node["id"]:
        reason = "wrong-flow"
    elif target["type"] in NON_RUNTIME_TYPES or _config_node(target):
        reason = "non-runtime-target"
    elif kind != "in":
        outputs = target.get("outputs")
        count = outputs if is_js_integer(outputs) else (len(target["wires"]) if "wires" in target else None)
        # Unknown counts are accepted, just as before; no runtime schema is loaded.
        if count is not None and wire["port"] >= count:
            reason = "output-port-out-of-range"
    return {"valid": reason is None, "reason": reason}


def _wire_changes(before: Node | None, after: Node | None, a: Mapping[str, Node], b: Mapping[str, Node]) -> list[dict[str, Any]]:
    old_ports = (before.get("wires") if before is not None else None) or []
    new_ports = (after.get("wires") if after is not None else None) or []
    result: list[dict[str, Any]] = []
    for port in range(max(len(old_ports), len(new_ports))):
        old_targets = list(dict.fromkeys(old_ports[port] if port < len(old_ports) else []))
        new_targets = list(dict.fromkeys(new_ports[port] if port < len(new_ports) else []))
        old_set, new_set = set(old_targets), set(new_targets)
        removed = [
            {"id": target, "name": _label(a.get(target)), **_wire_endpoint_validity(before, target, a)}  # type: ignore[arg-type]
            for target in old_targets
            if target not in new_set
        ]
        added = [
            {"id": target, "name": _label(b.get(target)), **_wire_endpoint_validity(after, target, b)}  # type: ignore[arg-type]
            for target in new_targets
            if target not in old_set
        ]
        if removed or added:
            result.append({"port": port, "removed": removed, "added": added})
    return result


def _subflow_ports(node: Node | None, kind: str) -> list[Any]:
    if node is None or node.get("type") != "subflow":
        return []
    if kind == "status":
        return [node["status"]] if js_truthy(node.get("status")) else []
    return node.get(kind) or []


def _edges(port: Any) -> dict[str, dict[str, Any]]:
    wires = (port.get("wires") if isinstance(port, dict) else None) or []
    return {stringify([wire["id"], wire.get("port")]): wire for wire in wires}


def _subflow_wire_changes(before: Node | None, after: Node | None, a: Mapping[str, Node], b: Mapping[str, Node]) -> list[dict[str, Any]]:
    # These arrays remain ordered/atomic properties. Only the edge annotation
    # treats duplicate (id, port) pairs as one connection within a fixed port.
    result: list[dict[str, Any]] = []
    for kind in ("in", "out", "status"):
        old_ports, new_ports = _subflow_ports(before, kind), _subflow_ports(after, kind)
        for port in range(max(len(old_ports), len(new_ports))):
            old_edges = _edges(old_ports[port] if port < len(old_ports) else None)
            new_edges = _edges(new_ports[port] if port < len(new_ports) else None)

            def describe(source: dict[str, Any], other: dict[str, Any], node: Node | None, nodes: Mapping[str, Node]) -> list[dict[str, Any]]:
                wires = [wire for key, wire in source.items() if key not in other]
                wires.sort(key=lambda w: (utf16_key(w["id"]), -1 if w.get("port") is None else w["port"]))
                # A nonempty edge map implies *node* is the subflow definition it came from.
                return [
                    {
                        "id": wire["id"],
                        "name": _label(nodes[wire["id"]]) if wire["id"] in nodes else f"(missing) [{wire['id']}]",
                        "port": wire.get("port"),
                        "passThrough": kind != "in" and wire["id"] == node["id"],  # type: ignore[index]
                        **_subflow_endpoint_validity(node, kind, wire, nodes),  # type: ignore[arg-type]
                    }
                    for wire in wires
                ]

            removed = describe(old_edges, new_edges, before, a)
            added = describe(new_edges, old_edges, after, b)
            if removed or added:
                result.append({
                    "kind": kind,
                    "property": "/status/wires" if kind == "status" else f"/{kind}/{port}/wires",
                    "port": port,
                    "removed": removed,
                    "added": added,
                })
    return result


def _diff_normalized(before: Flow, after: Flow, ignore_layout: bool, context: _ReferenceContext) -> dict[str, list[dict[str, Any]]]:
    a, b = _indexed(before), _indexed(after)
    result: dict[str, list[dict[str, Any]]] = {"added": [], "removed": [], "changed": [], "layoutOnly": []}

    def describe(node_id: str, old_refs: dict[str, str], new_refs: dict[str, str]) -> dict[str, Any]:
        old_node, new_node = a.get(node_id), b.get(node_id)
        node = new_node if new_node is not None else old_node
        assert node is not None
        entry: dict[str, Any] = {"id": node_id, "type": node["type"], "name": _label(node)}
        wires = _wire_changes(old_node, new_node, a, b)
        if wires:
            entry["wireChanges"] = wires
        subflow_wires = _subflow_wire_changes(old_node, new_node, a, b)
        if subflow_wires:
            entry["subflowWireChanges"] = subflow_wires

        def describe_ref(prop: str, ref_id: str, nodes: Mapping[str, Node]) -> dict[str, Any]:
            target = nodes.get(ref_id)
            known = target if target is not None else context.configs.get(ref_id)
            reason = "missing-target" if target is None else (None if _config_node(target) else "non-config-target")
            return {
                "property": prop,
                "id": ref_id,
                "name": _label(known) if known is not None else f"(missing) [{ref_id}]",
                "valid": reason is None,
                "reason": reason,
            }

        removed = [describe_ref(path, ref, a) for path, ref in old_refs.items() if new_refs.get(path) != ref]
        added = [describe_ref(path, ref, b) for path, ref in new_refs.items() if old_refs.get(path) != ref]
        if removed or added:
            entry["configReferences"] = {"removed": removed, "added": added}
        return entry

    for node_id in _union(a.keys(), b.keys()):
        # Validate declared references even on unchanged nodes, as merge integrity does.
        old_refs = _references(a.get(node_id), context, ignore_layout)
        new_refs = _references(b.get(node_id), context, ignore_layout)
        if node_id not in a:
            result["added"].append(describe(node_id, old_refs, new_refs))
            continue
        if node_id not in b:
            result["removed"].append(describe(node_id, old_refs, new_refs))
            continue
        properties = _changes(a[node_id], b[node_id])
        if not properties:
            continue
        only_layout = all(change["layout"] for change in properties)
        if ignore_layout:
            properties = [change for change in properties if not change["layout"]]
        if not properties:
            continue
        entry = {**describe(node_id, old_refs, new_refs), "properties": properties}
        for change in properties:
            if change["property"] == "/wires":
                change["ports"] = entry.get("wireChanges", [])
        result["layoutOnly" if only_layout else "changed"].append(entry)
    return result


def diff_flows(before: Any, after: Any, *, ignore_layout: bool = False, config_refs: Sequence[str] = ()) -> dict[str, list[dict[str, Any]]]:
    versions = [normalize(before), normalize(after)]
    return _diff_normalized(versions[0], versions[1], bool(ignore_layout), _reference_context(versions, config_refs))


def _merge_summary(versions: Sequence[Flow], merged: Flow, *, ignore_layout: bool = False, config_refs: Sequence[str] = ()) -> dict[str, Any]:
    context = _reference_context([*versions, merged], config_refs)
    base, ours, theirs = versions
    ignore = bool(ignore_layout)
    return {
        "ignoreLayout": ignore,
        "ours": _diff_normalized(base, ours, ignore, context),
        "theirs": _diff_normalized(base, theirs, ignore, context),
        "merged": _diff_normalized(base, merged, ignore, context),
    }


def summarize_merge(base: Any, ours: Any, theirs: Any, merged: Any, *, ignore_layout: bool = False, config_refs: Sequence[str] = ()) -> dict[str, Any]:
    """Compare each branch and the actual merged graph to base; does not merge or repair."""
    b, o, t, m = (normalize(flow) for flow in (base, ours, theirs, merged))
    return _merge_summary([b, o, t], m, ignore_layout=ignore_layout, config_refs=config_refs)


def _wire_merge(base: list[list[str]], ours: list[list[str]], theirs: list[list[str]]) -> list[list[str]]:
    result: list[list[str]] = []
    for port in range(len(ours)):
        b = set(base[port]) if port < len(base) else set()
        o, t = set(ours[port]), set(theirs[port])
        result.append([target for target in _union(o, t) if ((target in o and target in t) if target in b else (target in o or target in t))])
    return result


def merge_flows(
    base_flow: Any,
    our_flow: Any,
    their_flow: Any,
    *,
    prefer: str = "ours",
    config_refs: Sequence[str] = (),
    resolutions: Mapping[str, Any] | None = None,
    ignore_layout: bool = False,
) -> dict[str, Any]:
    """Never executes function code or loads Node-RED node packages."""
    if not isinstance(prefer, str) or prefer not in ("ours", "theirs"):
        raise InputError("prefer must be ours or theirs")
    validate_config_refs(config_refs)
    versions = [normalize(flow) for flow in (base_flow, our_flow, their_flow)]
    base, ours, theirs = (_indexed(flow) for flow in versions)
    conflicts: list[dict[str, Any]] = []

    def conflict(kind: str, node_id: str, prop: str, b: Any, o: Any, t: Any) -> Any:
        entry: dict[str, Any] = {
            "kind": kind,
            "nodeId": node_id,
            "property": prop,
            "base": _display(b),
            "ours": _display(o),
            "theirs": _display(t),
        }
        entry["id"] = "sha256:" + hashlib.sha256(stringify(canonical(entry)).encode("utf-8")).hexdigest()
        selected: str = prefer
        value = o if prefer == "ours" else t
        if resolutions is not None:
            decision = resolutions.get(entry["id"])
            validate_resolution_decision(decision, entry["id"])
            selected = decision["choice"]
            if selected == "ours":
                value = o
            elif selected == "theirs":
                value = t
            else:
                value = decision["value"] if decision["present"] else ABSENT
            if prop == "" and value is not ABSENT:
                validate_flow([value], "custom node resolution")
                if value["id"] != node_id:
                    raise InputError(f"resolution {entry['id']}: replacement node must keep its ID")
            entry["resolved"] = True
        conflicts.append({**entry, "selected": selected})
        return value

    def merge_value(b: Any, o: Any, t: Any, node_id: str, path: str) -> Any:
        if _equal(o, t):
            return o
        if _equal(b, o):
            return t
        if _equal(b, t):
            return o
        if path == "/wires" and all(isinstance(v, list) for v in (b, o, t)):
            if len(o) != len(t):
                return conflict("wire-port-count", node_id, path, b, o, t)
            return _wire_merge(b, o, t)
        if all(isinstance(v, dict) for v in (b, o, t)):
            value: dict[str, Any] = {}
            for key in _union(b.keys(), o.keys(), t.keys()):
                child = merge_value(_own(b, key), _own(o, key), _own(t, key), node_id, f"{path}/{_pointer(key)}")
                if child is not ABSENT:
                    value[key] = child
            return value
        if path == "":
            kind = "add/add" if b is ABSENT else "modify/delete"
        else:
            kind = "property"
        return conflict(kind, node_id, path, b, o, t)

    merged: Flow = []
    for node_id in _union(base.keys(), ours.keys(), theirs.keys()):
        node = merge_value(base.get(node_id, ABSENT), ours.get(node_id, ABSENT), theirs.get(node_id, ABSENT), node_id, "")
        if node is not ABSENT:
            merged.append(copy.deepcopy(node))
    # Property merges may combine individually valid fields into an invalid schema.
    # Preserve a complete side and record the schema conflict rather than emitting bad flows.
    i = 0
    while i < len(merged):
        try:
            validate_flow([merged[i]])
        except InputError:
            node_id = merged[i].get("id")
            selected = conflict(
                "schema", node_id, "", base.get(node_id, ABSENT), ours.get(node_id, ABSENT), theirs.get(node_id, ABSENT)
            )
            if selected is ABSENT:
                del merged[i]
                continue
            merged[i] = copy.deepcopy(selected)
        i += 1
    integrity = check_integrity(merged, versions, config_refs)
    flows = normalize(merged)
    return {
        "flows": flows,
        "conflicts": conflicts,
        "integrity": integrity,
        "clean": all(c.get("resolved") for c in conflicts) and not integrity,
        "summary": _merge_summary(versions, flows, ignore_layout=ignore_layout, config_refs=config_refs),
    }


def validate_resolution_decision(decision: Any, conflict_id: Any) -> None:
    if not isinstance(decision, dict):
        raise InputError(f"unresolved conflict {js_to_string(conflict_id)}: an explicit decision is required")
    keys = sorted_js(decision)
    choice = decision.get("choice")
    if isinstance(choice, str) and choice in ("ours", "theirs") and keys == ["choice"]:
        return
    present = decision.get("present")
    if choice == "custom" and isinstance(present, bool):
        expected = ["choice", "present", "value"] if present else ["choice", "present"]
        if keys == expected:
            if present:
                _check_depth(decision["value"])
            return
    raise InputError(
        f"resolution {js_to_string(conflict_id)}: expected ours/theirs, or custom with explicit presence and value"
    )


def validate_config_refs(refs: Iterable[str]) -> None:
    for ref in refs:
        split = ref.rfind(".")
        if split < 1 or split == len(ref) - 1 or _CONFIG_REF_FORBIDDEN.search(ref):
            raise InputError('config reference must be TYPE.PROPERTY, e.g. "mqtt in.broker"')


def check_integrity(flow: Any, history: Sequence[Flow] | None = None, config_refs: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Check (and prune invalid wires of) *flow* in place; returns integrity issues."""
    if history is None:
        history = [flow]
    validate_flow(flow)
    validate_config_refs(config_refs)
    nodes = _indexed(flow)
    issues: list[dict[str, Any]] = []
    issue_keys: set[str] = set()
    configs = {node["id"] for version in history for node in version if _config_node(node)}

    def report(kind: str, node_id: str, prop: str, target: Any, action: str = "review", reason: str | None = None) -> None:
        key = stringify([kind, node_id, prop, target, action])
        if key in issue_keys:
            return
        issue_keys.add(key)
        issue: dict[str, Any] = {"kind": kind, "nodeId": node_id, "property": prop, "target": target, "action": action}
        # Actual graph metadata only; keep the existing five-field issue identity.
        present = _lookup(nodes, target) is not None
        if kind == "missing-config":
            issue["reason"] = "non-config-target" if present else "missing-target"
        if kind == "missing-tab":
            issue["reason"] = "non-flow-target" if present else "missing-target"
        if kind == "missing-group":
            issue["reason"] = "non-group-target" if present else "missing-target"
        if kind == "missing-subflow":
            issue["reason"] = "non-subflow-target" if present else "missing-target"
        if kind in ("dangling-wire", "dangling-subflow-wire"):
            issue["reason"] = reason
        issues.append(issue)

    rules = [_split_ref(ref) + (ref,) for ref in _union(BUILTIN_CONFIG_REFS, config_refs)]
    for node in flow:
        node_id = node["id"]
        z = node.get("z")
        if js_truthy(z) and (z not in nodes or nodes[z]["type"] not in ("tab", "subflow")):
            report("missing-tab", node_id, "/z", z)
        if node["type"].startswith("subflow:"):
            definition = node["type"][8:]
            if (nodes.get(definition) or {}).get("type") != "subflow":
                report("missing-subflow", node_id, "/type", definition)
        group = node.get("g")
        if js_truthy(group) and (_lookup(nodes, group) or {}).get("type") != "group":
            report("missing-group", node_id, "/g", group)
        outputs = node.get("outputs")
        if "wires" in node and is_js_integer(outputs) and outputs >= 0 and len(node["wires"]) > outputs:
            report("output-port-count", node_id, "/wires", outputs)
        if "wires" in node:
            pruned_ports: list[list[str]] = []
            for index, port in enumerate(node["wires"]):
                kept: list[str] = []
                for target in port:
                    validity = _wire_endpoint_validity(node, target, nodes)
                    if validity["valid"]:
                        kept.append(target)
                    else:
                        report("dangling-wire", node_id, f"/wires/{index}", target, "pruned", validity["reason"])
                pruned_ports.append(kept)
            node["wires"] = pruned_ports
        if node["type"] == "subflow":
            for key in ("in", "out", "status"):
                if key == "status":
                    ports = [node["status"]] if js_truthy(node.get("status")) else []
                else:
                    ports = node.get(key) or []
                for i, port in enumerate(ports):
                    kept_wires: list[dict[str, Any]] = []
                    for wire in port["wires"]:
                        validity = _subflow_endpoint_validity(node, key, wire, nodes)
                        if validity["valid"]:
                            kept_wires.append(wire)
                        else:
                            report("dangling-subflow-wire", node_id, f"/{key}/{i}/wires", wire["id"], "pruned", validity["reason"])
                    port["wires"] = kept_wires

        def scan(value: Any, path: str) -> None:
            if isinstance(value, str) and value in configs and (value not in nodes or not _config_node(nodes[value])):
                report("missing-config", node_id, path, value)
            elif isinstance(value, (dict, list)):
                for key, child in _entries(value):
                    scan(child, f"{path}/{_pointer(key)}")

        for key in js_keys(node):
            if key not in RESERVED and key not in STRUCTURAL:
                scan(node[key], f"/{_pointer(key)}")
        for type_, key, ref in rules:
            if node["type"] != type_ or key not in node or node[key] == "":
                continue
            raw = node[key]
            values = raw if isinstance(raw, list) else [raw]
            for i, value in enumerate(values):
                if not isinstance(value, str):
                    raise InputError(f"{ref}: expected a config ID string or array of strings")
                if value not in nodes or not _config_node(nodes[value]):
                    report("missing-config", node_id, f"/{_pointer(key)}{f'/{i}' if isinstance(raw, list) else ''}", value)
    return issues
