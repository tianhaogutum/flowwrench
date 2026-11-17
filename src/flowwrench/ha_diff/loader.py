"""Bounded JSON loading and lossless adapters for HA's existing trace formats."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonpointer import JsonPointer, JsonPointerException

MAX_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 20_000
MAX_DEPTH = 80
MAX_INTEGER_DIGITS = 1000
MAX_JSON_OBJECTS = 250_000
MAX_JSON_MEMBERS = 1_000_000
MAX_JSON_NODES = 2_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_SEGMENTS = 160
PATH_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


class TraceError(ValueError):
    """An input is not a supported, unambiguous HA trace."""


def pointer(parts: tuple | list) -> str:
    return str(JsonPointer.from_parts(parts))


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


@dataclass
class Event:
    path: str
    occurrence: int
    raw: dict[str, Any]
    parts: tuple
    sequence: int
    started: datetime | None


@dataclass
class TraceDocument:
    source: str
    document: dict[str, Any]
    payload: dict[str, Any]
    parts: tuple
    format: str
    events: dict[tuple[str, int], Event]
    warnings: list[str] = field(default_factory=list)
    chronological: bool = True
    content_sha256: str | None = None
    unfinished: bool = False

    def ref(
        self, parts: tuple | list, present: bool = True, *, include_value: bool = True
    ) -> dict[str, Any]:
        location = pointer(parts)
        if present:
            # Resolve from the untouched original, never from a normalized copy.
            reference = {
                "source": self.source,
                "pointer": location,
                "present": True,
            }
            if include_value:
                reference["value"] = JsonPointer(location).resolve(self.document)
            return reference
        return {"source": self.source, "pointer": None, "present": False}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TraceError("duplicate JSON object key; refusing an ambiguous trace")
        result[key] = value
    return result


def _bounded_objects():
    """Abort during stdlib decoding, before a tiny-object array grows unbounded."""
    objects = 0
    members = 0

    def decode(pairs):
        nonlocal objects, members
        objects += 1
        members += len(pairs)
        if objects > MAX_JSON_OBJECTS or members > MAX_JSON_MEMBERS:
            raise TraceError("JSON object/member safety budget exceeded")
        return _unique_object(pairs)

    return decode


def _invalid_constant(value: str) -> None:
    raise TraceError(f"non-finite JSON number {value} is not supported")


def _bounded_integer(value: str) -> int:
    if len(value.lstrip("-")) > MAX_INTEGER_DIGITS:
        raise TraceError(f"JSON integer exceeds {MAX_INTEGER_DIGITS} digits")
    return int(value)


def _validate_tree(document: Any) -> None:
    pending = [(document, 0)]
    visited = objects = members = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if visited + len(pending) > MAX_JSON_NODES:
            raise TraceError("JSON node safety budget exceeded")
        if depth > MAX_DEPTH:
            raise TraceError(f"JSON exceeds the maximum nesting depth ({MAX_DEPTH})")
        if isinstance(node, dict):
            objects += 1
            members += len(node)
            if objects > MAX_JSON_OBJECTS or members > MAX_JSON_MEMBERS:
                raise TraceError("JSON object/member safety budget exceeded")
            if visited + len(pending) + len(node) * 2 > MAX_JSON_NODES:
                raise TraceError("JSON node safety budget exceeded")
            if any(not isinstance(key, str) for key in node):
                raise TraceError("JSON object keys must be strings")
            pending.extend((value, depth + 1) for value in node.values())
            pending.extend((key, depth) for key in node)
        elif isinstance(node, list):
            if visited + len(pending) + len(node) > MAX_JSON_NODES:
                raise TraceError("JSON node safety budget exceeded")
            pending.extend((value, depth + 1) for value in node)
        elif isinstance(node, float) and not math.isfinite(node):
            raise TraceError("non-finite JSON number is not supported")
        elif isinstance(node, int) and abs(node) >= 10**MAX_INTEGER_DIGITS:
            raise TraceError(f"JSON integer exceeds {MAX_INTEGER_DIGITS} digits")
        elif isinstance(node, str):
            try:
                node.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise TraceError("JSON contains an unpaired Unicode surrogate") from exc
        elif node is not None and not isinstance(node, (str, bool, int, float)):
            raise TraceError("input contains a value that is not JSON")


def load_trace(path: str | Path, select: str = "") -> TraceDocument:
    """Read a regular JSON file, preserving its original envelope for references."""
    path = Path(path)
    try:
        # Reject named pipes before opening: an open() on one could block forever.
        if not stat.S_ISREG(path.stat().st_mode):
            raise TraceError(f"{path}: expected a regular JSON file")
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise TraceError(f"{path}: expected a regular JSON file")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise TraceError(f"{path}: input exceeds {MAX_BYTES // 1024 // 1024} MiB")
        document = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_bounded_objects(),
            parse_constant=_invalid_constant,
            parse_int=_bounded_integer,
        )
    except (OSError, UnicodeError) as exc:
        raise TraceError(f"{path}: cannot read UTF-8 JSON ({exc.__class__.__name__})") from exc
    except json.JSONDecodeError as exc:
        raise TraceError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    except RecursionError as exc:
        raise TraceError(f"{path}: JSON nesting is too deep") from exc
    trace = parse_trace(document, source=str(path), select=select)
    trace.content_sha256 = hashlib.sha256(raw).hexdigest()
    return trace


def parse_trace(document: Any, *, source: str = "<memory>", select: str = "") -> TraceDocument:
    """Adapt native downloads, raw trace/get, WS responses and saved-trace records."""
    if not isinstance(source, str):
        raise TraceError("source label must be UTF-8 text")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TraceError("source filename/label cannot be represented as UTF-8 text") from exc
    _validate_tree(document)
    if not isinstance(document, dict):
        raise TraceError(f"{source}: expected a JSON object containing one trace")
    try:
        selection = JsonPointer(select)
        payload = selection.resolve(document)
    except JsonPointerException as exc:
        raise TraceError(f"{source}: selection is not a resolvable JSON Pointer") from exc
    parts = tuple(selection.parts)
    format_name = "trace/get"
    if isinstance(payload, dict) and payload.get("type") == "result":
        if payload.get("success") is not True:
            raise TraceError(f"{source}: WebSocket response did not succeed")
        parts += ("result",)
        payload = payload.get("result")
        format_name = "websocket"
    elif isinstance(payload, dict) and "extended_dict" in payload:
        parts += ("extended_dict",)
        payload = payload["extended_dict"]
        format_name = "saved-record"
    elif (
        isinstance(payload, dict)
        and isinstance(payload.get("trace"), dict)
        and isinstance(payload["trace"].get("trace"), dict)
    ):
        parts += ("trace",)
        payload = payload["trace"]
        format_name = "download"
    if not isinstance(payload, dict):
        raise TraceError(f"{source}: selected value does not contain a trace object")
    if "data" in payload and payload.get("key") == "trace.saved_traces":
        raise TraceError(f"{source}: select one saved record with --left-pointer/--right-pointer")
    if payload.get("domain") not in ("automation", "script"):
        raise TraceError(f"{source}: trace domain must be automation or script")
    if not isinstance(payload.get("item_id"), str) or not payload["item_id"]:
        raise TraceError(f"{source}: trace item_id must be a non-empty string")
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise TraceError(f"{source}: trace run_id must be a non-empty string")
    if (
        "config" in payload
        and payload["config"] is not None
        and not isinstance(payload["config"], dict)
    ):
        raise TraceError(f"{source}: config must be an object or null")
    step_map = payload.get("trace")
    if not isinstance(step_map, dict):
        raise TraceError(f"{source}: expected the path-to-list trace map from a full trace export")
    warnings = []
    for key in ("state", "script_execution", "error"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise TraceError(f"{source}: {key} must be a string or null")
    if "not_triggered" in payload and type(payload["not_triggered"]) is not bool:
        raise TraceError(f"{source}: not_triggered must be a boolean")
    if "timestamp" in payload and not isinstance(payload["timestamp"], dict):
        raise TraceError(f"{source}: run timestamp must be an object")
    unknown_root = set(payload) - {
        "trace",
        "domain",
        "item_id",
        "run_id",
        "timestamp",
        "config",
        "blueprint_inputs",
        "context",
        "state",
        "script_execution",
        "error",
        "not_triggered",
        "last_step",
        "trigger",
    }
    if unknown_root:
        warnings.append(
            "Unknown run-level fields are outside the compared scope: "
            + ", ".join(sorted(unknown_root))
        )
    events: dict[tuple[str, int], Event] = {}
    unknown_keys: set[str] = set()
    ordering_bad = False
    possible_retention = []
    for path, entries in step_map.items():
        if len(path) > MAX_PATH_CHARS or path.count("/") >= MAX_PATH_SEGMENTS:
            raise TraceError(f"{source}: trace path safety budget exceeded")
        if not PATH_PATTERN.fullmatch(path):
            raise TraceError(f"{source}: invalid trace step path")
        if not isinstance(entries, list):
            raise TraceError(f"{source}: each trace path must contain a list of executions")
        if not entries:
            warnings.append(f"{path}: empty execution list; nothing was recorded for this path")
        if (
            len(entries) >= 20
            and "repeat" in path.split("/")
            and path.startswith(("action/", "sequence/"))
        ):
            possible_retention.append(path)
        previous = None
        for occurrence, raw in enumerate(entries):
            if len(events) >= MAX_EVENTS:
                raise TraceError(f"{source}: trace exceeds {MAX_EVENTS} recorded steps")
            if not isinstance(raw, dict) or raw.get("path") != path:
                raise TraceError(f"{source}: step path does not match its containing trace-map key")
            if "changed_variables" in raw and not isinstance(raw["changed_variables"], dict):
                raise TraceError(f"{source}: changed_variables must be an object")
            if "result" in raw and not isinstance(raw["result"], dict):
                raise TraceError(f"{source}: result must be an object")
            started = timestamp(raw.get("timestamp"))
            if started is None:
                warnings.append(f"{path} occurrence {occurrence}: missing/invalid zoned timestamp")
            if started is not None and previous is not None and started < previous:
                warnings.append(f"{path}: repeated execution timestamps are not monotonic")
                ordering_bad = True
            previous = started
            unknown_keys.update(
                set(raw)
                - {
                    "path",
                    "timestamp",
                    "result",
                    "changed_variables",
                    "error",
                    "template_errors",
                    "child_id",
                }
            )
            events[path, occurrence] = Event(
                path, occurrence, raw, parts + ("trace", path, occurrence), len(events), started
            )
    chronological = not ordering_bad and all(event.started is not None for event in events.values())
    if chronological:
        ordered = sorted(events.values(), key=lambda event: (event.started, event.sequence))
        for index, event in enumerate(ordered):
            event.sequence = index
    else:
        warnings.append("Chronology is uncertain; trace-map order is only a display fallback")
    if not events:
        warnings.append("No recorded steps; absence is not evidence that two runs behaved the same")
    if unknown_keys:
        warnings.append(
            "Unknown step fields are compared as raw evidence: " + ", ".join(sorted(unknown_keys))
        )
    if possible_retention:
        warnings.append(
            "Repeated-action history may be capped: HA Core 2026.11 retains at most 20 "
            "executions per action path and can discard older iterations. Compare only "
            "the evidence present in these files: " + ", ".join(possible_retention)
        )
    run_period = payload.get("timestamp", {})
    run_start = timestamp(run_period.get("start"))
    run_finish = timestamp(run_period.get("finish"))
    valid_period = run_start is not None and run_finish is not None and run_finish >= run_start
    unfinished = payload.get("state") not in ("stopped", "debugged") or not valid_period
    if not valid_period and payload.get("state") in ("stopped", "debugged"):
        warnings.append(
            "Run start/finish timestamps are missing, invalid or reversed; completion is unconfirmed"
        )
    if unfinished:
        warnings.append(
            "Run completion is unconfirmed; later steps and final outcomes may be missing"
        )
    return TraceDocument(
        source,
        document,
        payload,
        parts,
        format_name,
        events,
        warnings,
        chronological,
        unfinished=unfinished,
    )
