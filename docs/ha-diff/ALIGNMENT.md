# What can safely be aligned

Home Assistant records a map from a step path to an array of executions. Array
positions are evidence locations, not a reliable identifier for a loop iteration.

For an ordinary path, this adapter pairs zero-based occurrence ordinals. For a
path containing exactly one `repeat` segment, it instead uses directly recorded
`changed_variables.repeat.index` when **every recorded execution on both sides**
has a positive integer index, those indices are unique, and their order increases.
An absent path on one side is allowed. It never carries an index forward from a
different step or a parent scope.

For example, `[1, 3]` versus `[1, 2, 3]` produces one missing-iteration row for 2.
Iteration 3 links to raw array index 1 on the left and index 2 on the right. These
are iteration-number anchors, not proof that two foreach items have the same
identity. Changes to the recorded `repeat.item` remain visible.

Nested repeats are always ordinal-only: a unique inner index alone does not
identify its outer iteration. Missing, duplicate or non-increasing indices also
fall back to explicitly uncertain ordinals. `--ordinal-repeats` requests that
fallback deliberately. A candidate first difference with uncertain alignment
cannot become a claimed unique first divergence.

The output includes the strategy, confidence, repeat index and both original
occurrence ordinals for every aligned row. IDs are `path#ordinal` for ordinal
rows and `path@repeat=N` for anchored rows.

## Recorded chronology

Each run is ordered independently by timezone-aware step start timestamps.
Earliest differing rows must agree between the runs to establish a unique first
candidate. Tied times, missing/non-monotonic timestamps, partial runs and safety
limits keep uncertainty explicit. A parent step's result can be computed after
its start timestamp; the report does not claim the time that a condition was
actually evaluated or a causal order across parallel branches.

Core 2026.11.1's `ACTION_TRACE_NODE_MAX_LEN = 20` caps repeated action-node
history; `trace_append_element` discards the oldest entry at that bound. The
adapter warns on potentially capped repeat paths. “First” always means the
earliest difference in the **recorded evidence**, not the first difference in an
unobserved complete execution.

The index format and retention rule were checked in the actual
[script source](https://github.com/home-assistant/core/blob/2026.11.1/homeassistant/helpers/script.py)
and [trace source](https://github.com/home-assistant/core/blob/2026.11.1/homeassistant/helpers/trace.py).
No Home Assistant runtime or execution simulation is part of this adapter.

