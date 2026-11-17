"""Evidence-only execution alignment; no inferred scopes or engine replay."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .loader import Event, TraceDocument


@dataclass
class AlignedEvents:
    id: str
    path: str
    occurrence: int | None
    left: Event | None
    right: Event | None
    strategy: str
    confidence: str
    repeat_index: int | None = None


def _repeat_indices(events: list[Event]) -> dict[int, Event] | None:
    indices = {}
    previous = 0
    for event in events:
        repeat = event.raw.get("changed_variables", {}).get("repeat")
        index = repeat.get("index") if isinstance(repeat, dict) else None
        # bool is an int subclass but is never a legitimate HA loop index.
        if type(index) is not int or index <= previous or index in indices:
            return None
        indices[index] = event
        previous = index
    return indices


def align_events(
    left: TraceDocument, right: TraceDocument, *, align_repeats: bool = True
) -> tuple[list[AlignedEvents], list[str]]:
    """Use a repeat index only when directly recorded and unique on BOTH sides.

    No index is carried from a parent or a previous step. Duplicate indices,
    as in nested loops, cannot identify an outer iteration and fall back to
    explicitly uncertain occurrence ordinals.
    """
    before: dict[str, list[Event]] = defaultdict(list)
    after: dict[str, list[Event]] = defaultdict(list)
    for event in left.events.values():
        before[event.path].append(event)
    for event in right.events.values():
        after[event.path].append(event)
    paths = list(dict.fromkeys([*before, *after]))
    rows = []
    uncertain = []
    for path in paths:
        a, b = before.get(path, []), after.get(path, [])
        repeat_depth = path.split("/").count("repeat")
        is_repeat_path = repeat_depth > 0
        # An inner index alone cannot establish which OUTER iteration it belongs to,
        # even when the observed inner indices happen to be unique in this file.
        can_anchor = repeat_depth == 1 and align_repeats
        a_indices = _repeat_indices(a) if can_anchor else None
        b_indices = _repeat_indices(b) if can_anchor else None
        if a_indices is not None and b_indices is not None:
            for index in dict.fromkeys([*a_indices, *b_indices]):
                rows.append(
                    AlignedEvents(
                        f"{path}@repeat={index}",
                        path,
                        None,
                        a_indices.get(index),
                        b_indices.get(index),
                        "recorded_repeat_index",
                        "recorded",
                        index,
                    )
                )
            continue
        ambiguous = is_repeat_path or max(len(a), len(b)) > 1
        if ambiguous:
            uncertain.append(path)
        for occurrence in range(max(len(a), len(b))):
            rows.append(
                AlignedEvents(
                    f"{path}#{occurrence}",
                    path,
                    occurrence,
                    a[occurrence] if occurrence < len(a) else None,
                    b[occurrence] if occurrence < len(b) else None,
                    "occurrence",
                    "ordinal_only" if ambiguous else "recorded",
                )
            )
    return rows, uncertain


def event_for_row(row: dict, document: TraceDocument, side: str) -> Event | None:
    occurrence = row[f"{side}_occurrence"]
    if occurrence is None:
        return None
    return document.events[row["path"], occurrence]
