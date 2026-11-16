"""Editable decision manifests: generation and verified resolution.

Port of ``lib/resolution.js``.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .flow_document import ROLES, merge_snapshots
from .flows import LIMITS, InputError, read_json_snapshot, validate_resolution_decision
from .jsjson import is_number, strict_equal

__all__ = ["MANIFEST_KIND", "create_manifest", "resolve_manifest"]

MANIFEST_KIND = "nrflow-merge-resolution"
SYNTHETIC_BASE_HASH = hashlib.sha256(b"[]\n").hexdigest()
_NON_HEX = re.compile("[^a-f0-9]")
_MISSING = object()


def _exact_keys(value: Any, keys: Sequence[str]) -> bool:
    return isinstance(value, dict) and set(value) == set(keys) and len(value) == len(keys)


def _validate_git_index(value: Any, hashes: Mapping[str, str]) -> None:
    """Validate only the shape/local consistency of this editable capture claim.
    Never consult Git, resolve the recorded paths, or authenticate its blob IDs."""

    def fail() -> None:
        raise InputError("invalid resolution manifest gitIndex provenance")

    stages_value = value.get("stages") if isinstance(value, dict) else None
    synthetic = isinstance(stages_value, dict) and "base" in stages_value and stages_value["base"] is None
    if not _exact_keys(value, ("repo", "path", "stages", "syntheticBase") if synthetic else ("repo", "path", "stages")):
        fail()
    repo, path, stages = value["repo"], value["path"], value["stages"]
    if not isinstance(repo, str) or not posixpath.isabs(repo) or "\0" in repo:
        fail()
    if not isinstance(path, str) or "\0" in path or any(part in ("", ".", "..") for part in path.split("/")):
        fail()
    if not _exact_keys(stages, ROLES):
        fail()
    blob_length: int | None = None
    for i, role in enumerate(ROLES):
        stage = stages[role]
        if synthetic and role == "base":
            continue
        if (
            not _exact_keys(stage, ("stage", "mode", "blob"))
            or not (is_number(stage["stage"]) and stage["stage"] == i + 1)
            or not (isinstance(stage["mode"], str) and stage["mode"] in ("100644", "100755"))
            or not isinstance(stage["blob"], str)
            or len(stage["blob"]) not in (40, 64)
            or _NON_HEX.search(stage["blob"])
            or (blob_length is not None and len(stage["blob"]) != blob_length)
        ):
            fail()
        blob_length = len(stage["blob"])
    if synthetic and (
        not strict_equal(value.get("syntheticBase", _MISSING), {"reason": "missing-stage-1", "value": []})
        or hashes["base"] != SYNTHETIC_BASE_HASH
    ):
        fail()


def create_manifest(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    prefer: str = "ours",
    config_refs: Sequence[str] = (),
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if result is None:
        result = merge_snapshots(snapshots, prefer=prefer, config_refs=config_refs)
    return {
        "version": 1,
        "kind": MANIFEST_KIND,
        "inputHashes": {role: snapshots[i]["hash"] for i, role in enumerate(ROLES)},
        "options": {"prefer": prefer, "configRefs": list(config_refs)},
        "conflicts": [
            {**{key: value for key, value in conflict.items() if key not in ("selected", "resolved")}, "decision": None}
            for conflict in result["conflicts"]
        ],
    }


def resolve_manifest(snapshots: Sequence[Mapping[str, Any]], file: str, *, ignore_layout: bool = False) -> dict[str, Any]:
    # Three input versions plus report metadata. Never read unbounded manifests.
    manifest = read_json_snapshot(file, LIMITS.bytes * 4)["value"]
    options = manifest.get("options") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or not (is_number(manifest.get("version")) and manifest["version"] == 1)
        or manifest.get("kind") != MANIFEST_KIND
        or not isinstance(options, dict)
        or not (isinstance(options.get("prefer"), str) and options["prefer"] in ("ours", "theirs"))
        or not isinstance(options.get("configRefs"), list)
        or any(not isinstance(ref, str) for ref in options["configRefs"])
        or not isinstance(manifest.get("conflicts"), list)
    ):
        raise InputError("invalid resolution manifest format/version")
    hashes = {role: snapshots[i]["hash"] for i, role in enumerate(ROLES)}
    if not strict_equal(manifest.get("inputHashes", _MISSING), hashes):
        raise InputError("stale resolution manifest: input hashes do not match")
    has_git_index = "gitIndex" in manifest
    if has_git_index:
        _validate_git_index(manifest["gitIndex"], hashes)
    prefer, config_refs = options["prefer"], options["configRefs"]
    expected = create_manifest(snapshots, prefer=prefer, config_refs=config_refs)
    conflicts = {
        conflict["id"]: {key: value for key, value in conflict.items() if key != "decision"}
        for conflict in expected["conflicts"]
    }
    resolutions: dict[str, Any] = {}
    for entry in manifest["conflicts"]:
        if not isinstance(entry, dict):
            raise InputError("invalid resolution manifest conflict")
        decision = entry.get("decision")
        conflict = {key: value for key, value in entry.items() if key != "decision"}
        conflict_id = conflict.get("id")
        known = conflicts.get(conflict_id) if isinstance(conflict_id, str) else None
        if (isinstance(conflict_id, str) and conflict_id in resolutions) or known is None or not strict_equal(known, conflict):
            raise InputError("resolution manifest has an unknown, duplicate or changed conflict")
        validate_resolution_decision(decision, conflict_id)
        resolutions[conflict_id] = decision
    if len(resolutions) != len(conflicts):
        raise InputError("resolution manifest has unresolved or missing conflicts")
    result = merge_snapshots(
        snapshots, prefer=prefer, config_refs=config_refs, resolutions=resolutions, ignore_layout=ignore_layout
    )
    if result["integrity"]:
        raise InputError(f"resolution has {len(result['integrity'])} remaining integrity issue(s); no files written")
    if has_git_index:
        result["gitIndex"] = manifest["gitIndex"]
    return result
