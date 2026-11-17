"""HA-specific alignment and interpretation, with DeepDiff doing the nested diff."""

from __future__ import annotations

from typing import Any

from deepdiff import DeepDiff
from jsonpointer import JsonPointer, JsonPointerException

from .alignment import align_events, event_for_row
from .configuration import configuration_for_path
from .loader import Event, TraceDocument, TraceError, pointer

SCHEMA = "ha-trace-diff/v1"
DEFAULT_CONTEXTS = (
    ("changed_variables", "context"),
    ("changed_variables", "this", "context"),
    ("changed_variables", "trigger", "event", "context"),
    ("changed_variables", "trigger", "from_state", "context"),
    ("changed_variables", "trigger", "to_state", "context"),
    ("changed_variables", "wait", "trigger", "event", "context"),
    ("changed_variables", "wait", "trigger", "from_state", "context"),
    ("changed_variables", "wait", "trigger", "to_state", "context"),
    ("result", "wait", "trigger", "event", "context"),
    ("result", "wait", "trigger", "from_state", "context"),
    ("result", "wait", "trigger", "to_state", "context"),
)
MAX_FIELD_DIFFS = 10_000
MAX_TOTAL_CHANGES = 5_000


def _category(parts: list) -> str:
    if not parts:
        return "execution"
    if parts[0] == "changed_variables":
        return "trigger" if len(parts) > 1 and parts[1] == "trigger" else "variable"
    if parts[0] in ("error", "template_errors"):
        return "error"
    if parts[:2] == ["result", "result"]:
        return "condition"
    if parts[:2] == ["result", "choice"]:
        return "branch"
    return "result"


def _exclusions(exact: bool, ignored_fields: tuple[str, ...]) -> list[tuple]:
    paths: list[tuple] = [("path",), ("timestamp",)]
    if not exact:
        paths.extend(
            context + (key,) for context in DEFAULT_CONTEXTS for key in ("id", "parent_id")
        )
        paths.append(("child_id", "run_id"))
    for value in ignored_fields:
        try:
            parts = tuple(JsonPointer(value).parts)
        except JsonPointerException as exc:
            raise TraceError(f"invalid --ignore-field JSON Pointer: {value}") from exc
        if not parts:
            raise TraceError("--ignore-field cannot ignore an entire step; specify a field")
        paths.append(parts)
    return paths


_OMIT = object()
_END = object()


def _project(value: dict, exclusions: list[tuple]) -> tuple[dict, dict[tuple, tuple], set[tuple]]:
    """Remove exact policy fields before DeepDiff, retaining array-index provenance.

    DeepDiff's exclude_paths cannot suppress an ignored descendant when its entire
    parent is newly added. Projecting also handles numeric JSON Pointer segments
    without confusing an object key "0" with an array index 0.
    """
    trie: dict = {}
    for parts in exclusions:
        branch = trie
        for part in parts:
            branch = branch.setdefault(str(part), {})
        branch[_END] = True
    remapped: dict[tuple, tuple] = {}
    filtered_only: set[tuple] = set()

    def walk(node, branch, normalized_parts, raw_parts):
        if _END in branch:
            return _OMIT, True
        if not branch:
            return node, False
        if isinstance(node, dict):
            result = {}
            only_ignored = bool(node)
            for key, child in node.items():
                projected, ignored = walk(
                    child, branch.get(key, {}), normalized_parts + (key,), raw_parts + (key,)
                )
                only_ignored &= ignored
                if projected is not _OMIT:
                    result[key] = projected
            if only_ignored:
                filtered_only.add(normalized_parts)
            return result, only_ignored
        if isinstance(node, list):
            result = []
            only_ignored = bool(node)
            for index, child in enumerate(node):
                normalized = normalized_parts + (len(result),)
                original = raw_parts + (index,)
                projected, ignored = walk(child, branch.get(str(index), {}), normalized, original)
                only_ignored &= ignored
                if projected is not _OMIT:
                    remapped[normalized] = original
                    result.append(projected)
            if only_ignored:
                filtered_only.add(normalized_parts)
            return result, only_ignored
        return node, False

    projected, _ = walk(value, trie, (), ())
    return ({} if projected is _OMIT else projected), remapped, filtered_only


def _original_path(parts: list, remapped: dict[tuple, tuple]) -> tuple:
    parts = tuple(parts)
    for length in range(len(parts), 0, -1):
        if parts[:length] in remapped:
            return remapped[parts[:length]] + parts[length:]
    return parts


def _field_changes(
    left: TraceDocument,
    right: TraceDocument,
    before: dict,
    after: dict,
    left_parts: tuple,
    right_parts: tuple,
    exclude: list[tuple] | None = None,
    limit: int = MAX_TOTAL_CHANGES,
) -> tuple[list[dict], bool]:
    before, left_remap, left_filtered = _project(before, exclude or [])
    after, right_remap, right_filtered = _project(after, exclude or [])
    diff = DeepDiff(
        before,
        after,
        view="tree",
        verbose_level=2,
        ignore_private_variables=False,
        threshold_to_diff_deeper=0,
        zip_ordered_iterables=True,
        max_diffs=MAX_FIELD_DIFFS,
    )
    changes = []
    for kind, nodes in diff.items():
        for node in nodes:
            a_parts = node.path(output_format="list")
            b_parts = node.path(output_format="list", use_t2=True)
            left_present = kind not in ("dictionary_item_added", "iterable_item_added")
            right_present = kind not in ("dictionary_item_removed", "iterable_item_removed")
            if (not left_present and tuple(b_parts) in right_filtered) or (
                not right_present and tuple(a_parts) in left_filtered
            ):
                # Ignore a newly present/absent container ONLY when all of its
                # recorded content was filtered. Keep its empty shell otherwise,
                # so a later added user_id points to that field, not to a falsely
                # missing context container.
                continue
            a_original = _original_path(a_parts, left_remap)
            b_original = _original_path(b_parts, right_remap)
            relative = b_original if right_present else a_original
            left_ref = left.ref(left_parts + a_original, left_present)
            right_ref = right.ref(right_parts + b_original, right_present)
            for ref, compared in ((left_ref, node.t1), (right_ref, node.t2)):
                if ref["present"] and ref["value"] != compared:
                    ref["compared_value"] = compared
            changes.append(
                {
                    "kind": kind,
                    "category": _category(list(relative)),
                    "field": pointer(relative),
                    "left": left_ref,
                    "right": right_ref,
                }
            )
    changes.sort(key=lambda change: (change["field"], change["kind"]))
    truncated = bool(diff.get_stats().get("MAX DIFF LIMIT REACHED")) or len(changes) > limit
    return changes[:limit], truncated


def _event_ref(
    document: TraceDocument, event: Event | None, *, include_value: bool = False
) -> dict:
    return (
        document.ref(event.parts, include_value=include_value) if event else document.ref((), False)
    )


def _first_candidates(rows: list[dict], left: TraceDocument, right: TraceDocument) -> list[str]:
    """Do not invent a total order when both runs disagree about which step was first."""
    changed = [row for row in rows if row["changes"]]
    if not changed or not left.chronological or not right.chronological:
        return []
    candidates = []
    for side, document in (("left", left), ("right", right)):
        reached = [row for row in changed if row[side]["present"]]
        if not reached:
            continue
        first = min(
            reached,
            key=lambda row: event_for_row(row, document, side).sequence,
        )
        first_time = event_for_row(first, document, side).started
        # Equal timestamps do not establish a first step (common with parallel actions).
        for row in reached:
            if (
                event_for_row(row, document, side).started == first_time
                and row["id"] not in candidates
            ):
                candidates.append(row["id"])
    return candidates


def compare_traces(
    left: TraceDocument,
    right: TraceDocument,
    *,
    exact: bool = False,
    ignored_fields: tuple[str, ...] = (),
    align_repeats: bool = True,
) -> dict[str, Any]:
    """Align identical paths using recorded repeat indices or occurrence ordinals.

    Changed-variable maps are recorded deltas. They are never merged into an
    invented global state: nested/parallel scopes and deletions aren't encoded.
    """
    identity = (left.payload["domain"], left.payload["item_id"])
    if identity != (right.payload["domain"], right.payload["item_id"]):
        raise TraceError("inputs belong to different automations/scripts (domain/item_id differ)")
    exclusions = _exclusions(exact, ignored_fields)
    warnings = [f"Left: {message}" for message in left.warnings]
    warnings.extend(f"Right: {message}" for message in right.warnings)
    if left.payload["run_id"] == right.payload["run_id"]:
        warnings.append("Both inputs have the same run_id; this may compare copies of one run")
    config_before = {
        key: left.payload[key] for key in ("config", "blueprint_inputs") if key in left.payload
    }
    config_after = {
        key: right.payload[key] for key in ("config", "blueprint_inputs") if key in right.payload
    }
    config_changes, truncated = _field_changes(
        left,
        right,
        config_before,
        config_after,
        left.parts,
        right.parts,
        limit=MAX_TOTAL_CHANGES,
    )
    for change in config_changes:
        change["category"] = "configuration"
    if config_changes:
        warnings.append(
            "Configuration differs: identical step paths may describe different actions"
        )
    if left.payload.get("config") is None or right.payload.get("config") is None:
        warnings.append(
            "Configuration is unavailable on at least one side; path identity is unverified"
        )
    metadata = (
        "state",
        "script_execution",
        "error",
        "not_triggered",
        "last_step",
        "trigger",
        "context",
    )
    run_exclusions = [] if exact else [("context", "id"), ("context", "parent_id")]
    run_changes, run_truncated = _field_changes(
        left,
        right,
        {key: left.payload[key] for key in metadata if key in left.payload},
        {key: right.payload[key] for key in metadata if key in right.payload},
        left.parts,
        right.parts,
        exclude=run_exclusions,
        limit=max(0, MAX_TOTAL_CHANGES - len(config_changes)),
    )
    truncated |= run_truncated
    for change in run_changes:
        change["category"] = "run"
    alignments, uncertain_paths = align_events(left, right, align_repeats=align_repeats)
    rows = []
    remaining = max(0, MAX_TOTAL_CHANGES - len(config_changes) - len(run_changes))
    unexamined = 0
    configuration_cache = {"left": {}, "right": {}}
    trigger_cache = {"left": {}, "right": {}}
    for alignment in alignments:
        before, after = alignment.left, alignment.right
        path, occurrence = alignment.path, alignment.occurrence
        compared = remaining > 0
        event_truncated = False
        if not compared:
            changes = []
            unexamined += 1
        elif before is None or after is None:
            changes = [
                {
                    "kind": "step_not_recorded_left"
                    if before is None
                    else "step_not_recorded_right",
                    "category": "execution",
                    "field": "",
                    "left": _event_ref(left, before, include_value=True),
                    "right": _event_ref(right, after, include_value=True),
                }
            ]
        else:
            changes, event_truncated = _field_changes(
                left,
                right,
                before.raw,
                after.raw,
                before.parts,
                after.parts,
                exclusions,
                limit=remaining,
            )
            truncated |= event_truncated
        remaining -= len(changes)
        rows.append(
            {
                "id": alignment.id,
                "path": path,
                "occurrence": occurrence,
                "left_occurrence": before.occurrence if before else None,
                "right_occurrence": after.occurrence if after else None,
                "alignment": {
                    "strategy": alignment.strategy,
                    "confidence": alignment.confidence,
                    "repeat_index": alignment.repeat_index,
                },
                "left": _event_ref(left, before),
                "right": _event_ref(right, after),
                "changes": changes,
                "comparison_complete": compared and not event_truncated,
                "status": (
                    "different"
                    if changes
                    else "same_recorded_fields"
                    if compared and not event_truncated
                    else "not_fully_compared"
                    if compared
                    else "not_compared_limit"
                ),
            }
        )
        if changes:
            configs = {}
            for side, document in (("left", left), ("right", right)):
                if path not in configuration_cache[side]:
                    configuration_cache[side][path] = configuration_for_path(
                        document, path, trigger_cache=trigger_cache[side]
                    )
                configs[side] = configuration_cache[side][path]
            rows[-1]["configuration"] = configs
    rows.sort(
        key=lambda row: (
            min(
                event_for_row(row, left, "left").sequence
                if row["left"]["present"]
                else MAX_FIELD_DIFFS * 10,
                event_for_row(row, right, "right").sequence
                if row["right"]["present"]
                else MAX_FIELD_DIFFS * 10,
            ),
            row["path"],
            row["id"],
        )
    )
    if uncertain_paths:
        warnings.append(
            "These paths use uncertain occurrence ordinals: unique recorded repeat.index "
            "anchors are unavailable or disabled. Skipped/nested iterations may shift pairings: "
            + ", ".join(sorted(uncertain_paths))
        )
    truncated |= bool(unexamined)
    first_candidates = [] if truncated else _first_candidates(rows, left, right)
    control_rows = [
        row
        for row in rows
        if any(
            change["category"] in ("condition", "branch", "error", "execution")
            for change in row["changes"]
        )
    ]
    control_candidates = [] if truncated else _first_candidates(control_rows, left, right)
    changed_rows = sum(bool(row["changes"]) for row in rows)
    partial = truncated or not left.events or not right.events
    partial |= left.unfinished or right.unfinished
    rows_by_id = {row["id"]: row for row in rows}

    def unique_candidate(candidates):
        if (
            not partial
            and len(candidates) == 1
            and rows_by_id[candidates[0]]["alignment"]["confidence"] == "recorded"
        ):
            return candidates[0]
        return None

    first_divergence = unique_candidate(first_candidates)
    first_control = unique_candidate(control_candidates)
    if changed_rows and first_divergence is None:
        warnings.append(
            "A unique first differing step cannot be established from the recorded timelines, "
            "alignment confidence and input coverage"
        )
    if truncated:
        warnings.append(
            "A field-diff safety limit was reached; comparison is incomplete. "
            f"At most {MAX_TOTAL_CHANGES} changes are retained; {unexamined} step rows were not compared."
        )
    has_changes = bool(changed_rows or config_changes or run_changes)
    return {
        "schema": SCHEMA,
        "status": "different"
        if has_changes
        else "inconclusive"
        if partial
        else "no_recorded_difference",
        "coverage": "partial" if partial else "recorded",
        "identity": {"domain": identity[0], "item_id": identity[1]},
        "inputs": {
            side: {
                "source": doc.source,
                "format": doc.format,
                "run_id": doc.payload["run_id"],
                "payload_pointer": pointer(doc.parts),
                "recorded_steps": len(doc.events),
                "sha256": doc.content_sha256,
            }
            for side, doc in (("left", left), ("right", right))
        },
        "policy": {
            "alignment": (
                "exact path; unique directly recorded repeat.index when available, otherwise "
                "zero-based occurrence ordinal"
            ),
            "use_recorded_repeat_indices": align_repeats,
            "profile": "exact" if exact else "semantic",
            "ignored_step_fields": [pointer(parts) for parts in exclusions],
            "chronology": "recorded step start timestamps; no causal ordering across runs",
            "variable_scope": "recorded changed_variables only; no engine/state reconstruction",
            "maximum_retained_changes": MAX_TOTAL_CHANGES,
            "compared_run_fields": list(metadata),
            "ignored_run_fields": [pointer(parts) for parts in run_exclusions],
        },
        "summary": {
            "aligned_rows": len(rows),
            "different_rows": changed_rows,
            "field_changes": sum(len(row["changes"]) for row in rows),
            "configuration_changes": len(config_changes),
            "run_changes": len(run_changes),
            "uncompared_rows": unexamined,
        },
        "first_divergence": first_divergence,
        "first_divergence_candidates": first_candidates,
        "first_control_divergence": first_control,
        "first_control_divergence_candidates": control_candidates,
        "configuration_changes": config_changes,
        "run_changes": run_changes,
        "steps": rows,
        "warnings": list(dict.fromkeys(warnings)),
        "interpretation": (
            "Differences are recorded evidence, not proof of a root cause. A missing step is not "
            "recorded on that side; this alone does not prove why it was skipped."
        ),
    }
