"""Array and strict API-v2 ``{rev?, flows}`` document adaptation.

Port of ``lib/flow-document.js``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .flows import InputError, merge_flows, read_json_snapshot, validate_flow
from .jsjson import stringify

__all__ = ["ROLES", "adapt_flow_document", "merge_snapshots", "read_merge_snapshot", "serialize_merge_result"]

ROLES = ("base", "ours", "theirs")


def adapt_flow_document(value: Any, source: str = "flow") -> dict[str, Any]:
    """Only complete /flows documents are supported. Reject extra top-level fields
    rather than silently dropping credentials or single-flow export metadata."""
    if isinstance(value, list):
        return {"flow": validate_flow(value, source), "format": "array", "rev": {"present": False}}
    if not isinstance(value, dict) or "flows" not in value:
        raise InputError(f"{source}: expected a flat array or complete /flows object with flows")
    if any(key not in ("flows", "rev") for key in value):
        raise InputError(
            f"{source}: unsupported top-level fields; only flows and optional rev are accepted "
            "(no credentials or single-flow exports)"
        )
    present = "rev" in value
    if present and (not isinstance(value["rev"], str) or len(value["rev"]) == 0):
        raise InputError(f"{source}: rev must be a nonempty string when present")
    return {
        "flow": validate_flow(value["flows"], f"{source}.flows", 1),
        "format": "v2",
        "rev": {"present": True, "value": value["rev"]} if present else {"present": False},
    }


def read_merge_snapshot(file: str) -> dict[str, Any]:
    snapshot = read_json_snapshot(file)
    return {**adapt_flow_document(snapshot["value"], file), "hash": snapshot["hash"]}


def _describe(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    rev = snapshot.get("rev")
    return {
        "format": snapshot.get("format") if snapshot.get("format") is not None else "array",
        "rev": dict(rev) if rev is not None else {"present": False},
    }


def merge_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    prefer: str = "ours",
    config_refs: Sequence[str] = (),
    resolutions: Mapping[str, Any] | None = None,
    ignore_layout: bool = False,
) -> dict[str, Any]:
    """Revisions never enter node equality, conflict IDs, fallback or decisions.
    Older array-only library snapshots need no new metadata."""
    result = merge_flows(
        snapshots[0]["flow"],
        snapshots[1]["flow"],
        snapshots[2]["flow"],
        prefer=prefer,
        config_refs=config_refs,
        resolutions=resolutions,
        ignore_layout=ignore_layout,
    )
    ours = _describe(snapshots[1])
    if ours["format"] == "array":
        document: Any = result["flows"]
    else:
        document = {**({"rev": ours["rev"]["value"]} if ours["rev"].get("present") else {}), "flows": result["flows"]}
    return {
        **result,
        "document": document,
        "revisionProvenance": {
            **{role: _describe(snapshots[i]) for i, role in enumerate(ROLES)},
            "output": {**ours, "source": "ours"},
            "serverCurrentRevisionVerified": False,
        },
    }


def serialize_merge_result(result: Mapping[str, Any]) -> str:
    """merge_flows has already normalized the nodes. Do not normalize the wrapper
    or transform its opaque revision string in any way."""
    return f"{stringify(result['document'], 2)}\n"
