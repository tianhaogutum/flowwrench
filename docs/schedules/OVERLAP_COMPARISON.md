# flowwrench schedules: duration-aware comparison contract

Part of [`flowwrench schedules`](../schedules.md); `compare`, `overlap` and `suggest`
below are `flowwrench schedules` subcommands.

`compare` optionally accepts **both** `--before-durations JSON` and
`--after-durations JSON`. Each is a v1 duration sidecar as documented for
`overlap`, validated against only that side's inventory IDs. Either side may be
source exports or a v1 snapshot. Snapshots never contain durations.

Each sidecar selector may appear only once, including `--option=FILE` syntax
and mixed equals/separate forms. Repeating the same file, a different file, or
a missing/empty first path is a usage error (stderr, exit 2, empty stdout)
before any export, snapshot or sidecar is read. The same rule applies to
standalone `overlap --durations` and `suggest --durations`. Selecting one
sidecar per comparison side remains valid, including the same file on both sides.

Without these options the existing start-count report is unchanged. With them,
the existing minute-start peaks, deltas, events, hazards and metadata remain,
and an `overlap` object is added with `before` and `after` full overlap reports,
`complete` (both overlap sides complete), and `peak_delta` (after minus before
only when complete, otherwise JSON null). Each overlap side retains `known_peak`,
nullable `peak`, `known_peak_intervals`, carry-in/start counts, unknown IDs,
coverage and schedules with durations. No delta of lower bounds is presented as
a concurrency change. Top-level `complete` additionally requires overlap
completeness. Existing `before.complete` and `after.complete` still describe
schedule coverage; duration coverage lives in `overlap.before/after.complete`.

Concrete example: compare `tests/schedules/examples/overlap/jobs.crontab` with itself for
2026-11-01 / one UTC day, using `durations.json` before and `short.json` after.
Starts stay unchanged (2), minute-start peaks remain 1 -> 1 with delta 0, but
running peaks are 2 -> 1 with overlap delta -1. Before peak interval is
[00:05:00Z, 00:10:00Z); after is [00:00:00Z, 00:10:00Z).
Changing only the after side to `partial.json` gives known peaks 2 -> 1,
after peak null and overlap delta null. Missing/null durations stay unknown
even without in-horizon starts. An empty duration map explicitly models an
unknown side, rather than borrowing durations from the other side.

Text retains the start summary and adds the two known lower bounds, overall
peaks (`unknown` when incomplete), and overlap delta (`unknown` unless complete).
It includes each side's unknown-duration count. Without an enabled comparison
gate, a valid partial report exits 0; `--strict` emits the report then exits 2 for unsupported records on either side
(including disabled unsupported records), or incomplete overlap.

Both duration options are required together. Unpaired options, unknown IDs,
invalid durations/schema/files and exceeded bounds exit 2 with empty stdout.
Sidecar bounds and negative cases are identical to standalone `overlap`:
regular nonsymlink UTF-8 JSON, <=1 MiB, unique keys, version integer 1,
only `version` and `durations_seconds`, integer 0..604800 or null values (zero means no running load),
<=2000 matching IDs; no booleans, floats, strings, nonfinite values or extras.
Sidecars must be outside scanned export directories.

The same exact-second half-open UTC model includes carry-in, elapsed durations,
DST folds/gaps and deterministic peak merging. `--window` affects only the
existing start-count metrics. Across the entire comparison, start forecasts and
both duration lookbacks share the eight-million candidate budget. Start-count
forecasts retain their combined one-million firing and 10000 hazard bounds;
both overlap sides together allow one million executions (including carry-in)
and 100000 peak intervals. Existing 100000 added/removed bound remains.

`--max-peak K` gates only the after side's minute-start peak in the selected
window, even with duration sidecars. Zero-duration starts still count; an
unknown duration does not make the start peak unknown. The full report is
emitted unchanged, exit 1 means `after.peak > K`, and strict exit 2 takes
precedence. This option does not gate `overlap.after.peak` or `known_peak`.
No running-overlap threshold, capacity, queueing, runtime measurement or
automatic edits.

`--max-peak-increase K` gates the top-level signed minute-start peak delta.
`--max-added-firings K` gates the full added-event count in the selected horizon;
windows and durations do not change that count, and removals do not offset it.
`--max-removed-firings K` independently gates the full removed-event count;
additions never offset removals, including net-positive changes. Windows and
durations do not change this count either. Any enabled gate can cause exit 1,
with strict exit 2 taking precedence. All retain full reports. See [schedules.md](../schedules.md#ci-gates) for
argument and coverage contracts.
