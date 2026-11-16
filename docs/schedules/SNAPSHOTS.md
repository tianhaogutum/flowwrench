# flowwrench schedules: inventory snapshot v1 contract

Part of [`flowwrench schedules`](../schedules.md). Commands below are subcommands of
`flowwrench schedules` (for example `flowwrench schedules extract PATH... --format snapshot`).
The `cronherd.inventory` schema identifier is kept from the former standalone tool
`cronherd` so existing snapshots remain valid.

`extract PATH... --format snapshot` emits UTF-8 JSON to stdout. Existing text and
JSON extraction formats are unchanged. A snapshot has exactly these fields:

```json
{
  "schema": "cronherd.inventory",
  "version": 1,
  "schedules": [
    {
      "id": "0900cd3c1c7b2a35c90d",
      "platform": "crontab",
      "file": "tests/schedules/examples/comparison/before/jobs.crontab",
      "workflow": "jobs",
      "node": "line 2",
      "kind": "cron",
      "expr": "30 2 * * *",
      "timezone": "Europe/Berlin",
      "enabled": true,
      "supported": true,
      "error": null,
      "source_expr": "30 2 * * *",
      "source_dialect": "standard",
      "second": 0,
      "day_mode": "or",
      "timezone_assumed": false,
      "enabled_assumed": false
    }
  ],
  "warnings": [],
  "assumptions": {
    "default_timezone": "UTC",
    "timezone_assumed_records": 0,
    "enabled_assumed_records": 0
  }
}
```

Every record field is required, including null `error`/`source_expr` when relevant.
IDs are unique lowercase 20-digit hexadecimal strings, preserved as opaque source
identities; they are not recalculated from the snapshot filename. Original file,
workflow, node, source expression/dialect, fixed seconds, day mode, enabled/support
state and assumption flags remain intact. Original source paths are metadata only:
they are never opened. Snapshots contain no commands, payloads or durations.

Each comparison side requires exactly one of `--before PATH...` and
`--before-snapshot FILE` (likewise `--after`/`--after-snapshot`). Export and snapshot
sides can be mixed. Snapshots cannot be merged with exports or other snapshots on
one side. Ordinary `extract --format json` and analysis/comparison reports are not
snapshot inputs. No automatic format detection is performed.

Inventory selectors may occur only once: `--snapshot` for standalone analysis, DST, suggestions or
overlap, and each of `--before`, `--before-snapshot`, `--after`, `--after-snapshot`
for comparison. Repeating a selector is an exit-2 usage error with empty stdout,
even with the same filename or when an earlier filename is missing or invalid.
This is checked before any inventory or sidecar is read; the last argument must
not silently replace an earlier input. Multiple source paths belong in a single
`--before PATH...` or `--after PATH...` group. One snapshot on each comparison side
is valid, including using the same file for both sides. These rules also apply to
`--snapshot=FILE` and the equivalent comparison option syntax.

Concrete acceptance example: snapshot `tests/schedules/examples/comparison/before`, then compare
it to source `tests/schedules/examples/comparison/after` over 2026-10-25 UTC, one day. Expected:
two removed Berlin 02:30 folds (00:30Z/01:30Z), one added 03:30 fire (02:30Z), one
unchanged fire, and minute peaks 1 -> 2. All schedules and event/DST metadata match
the source-to-source comparison. Either/both sides can be snapshots, and the
comparison still works after the original baseline exports have been removed.

The output recomputes coverage and forecasts from records. Per-side warnings and
assumptions equal those saved at extraction, including the saved default timezone;
the comparison's `--default-timezone` only applies to source export sides. On a
snapshot side, stderr calls the timezone a saved assumption. `--strict` emits the
report then exits 2 if either side contains unsupported records, including disabled
ones, as with source comparison. Unsupported/disabled-only sides are valid and
contribute zero fires with explicit coverage. Empty inventories are rejected.

Validation is atomic: invalid inputs exit 2, put diagnostics on stderr, and emit
no stdout JSON. Reject missing/extra fields at any schema level, unknown schema or
version (including boolean/floating versions), wrong types, duplicate JSON keys,
nonfinite constants, malformed UTF-8/JSON, invalid Unicode strings, duplicate IDs,
unknown platform/dialect/day mode/kind, invalid timezone even on excluded records,
and inconsistent assumption counts or assumed timezones. Flags must be booleans;
seconds must be integer 0..59. Platform dialect/day mode must match the supported
adapter (early unsupported n8n structural errors retain `standard`/`or`, as emitted
by the extractor). A supported record must be cron, have no error, and have a
valid normalized five-field expression/second matching `source_expr` under its dialect. Activepieces
combined restricted day fields remain unsupported. Unsupported records must retain
a nonempty error, and are never promoted by reparsing their source expression.

Adapter metadata pairs in v1:

| Platform | `source_dialect` | `day_mode` |
| --- | --- | --- |
| crontab | standard | or |
| n8n | n8n | or-values |
| node-red | standard | or-literal |
| huginn | standard | and |
| windmill | windmill | and |
| activepieces | standard | or |

Limits: 1..2000 records, 8 MiB snapshot JSON, 10000 warnings, 8192 characters per
warning, 4096 per source filename, 512 per workflow/node label and 1024 per
expression/error. Only regular files are read; directories, FIFOs and symlinks
are rejected. Snapshot generation enforces the same schema/size limits before
writing stdout. Forecast and change bounds are unchanged. JSON key order is not
significant; generated record order is stable by ID, warnings keep source order.

Snapshots preserve metadata, not authenticated provenance or a timezone database.
They are editable inputs, not signatures of original exports. Forecasts use the
current host tzdb and the existing conservative gap/fold model; reusing a snapshot
does not freeze historical prediction results across tzdb updates. Paths and labels
may be sensitive; sanitize them before sharing.

## Standalone analysis from a saved inventory

`analyze (--snapshot FILE | PATH...) --from DATE --days N [--window SECONDS]
[--format json|text] [--strict] [--max-peak K]` requires exactly one input:
one v1 snapshot or a nonempty group of source paths. Neither input, both inputs,
an empty snapshot path, or an unreadable snapshot is an exit-2 error with empty
stdout. Standalone `dst`, `suggest` and `overlap` also accept snapshots (below).

Analysis uses the same v1 loader and all schema, regular-file and resource bounds
above. It recomputes the existing minute-start metrics, coverage, exclusions and
DST hazards for the requested horizon/window; it never opens saved source paths.
Analysis JSON additionally includes full normalized `schedules`, including excluded
records, sorted by stable ID. Source and snapshot analysis use that same stable
order for exclusions and hazards. Text keeps the existing analysis format.
Warnings and assumptions are preserved verbatim in JSON; stderr describes assumed
timezones as `saved default_timezone`. The CLI `--default-timezone` applies only
to source paths, and is ignored for snapshots, even if the supplied value is not
an IANA identifier. It cannot replace saved assumptions or record timezones.

Concrete regression: save a synthetic inventory with Berlin 02:30 and 02:31 cron
jobs, a 02:30 Node-RED cron with an excluded startup warning, an unsupported repeat,
a fixed-second Windmill 02:30 job with missing activation state, and a disabled job.
Use saved default Europe/Berlin; analyze 2026-10-25 UTC, one day, window 120s.
Expect 8 firings, peak 4 at 00:31Z, four ambiguous hazards, one disabled and one
unsupported record, and incomplete coverage. After deleting every export, snapshot
analysis must equal source analysis, including schedules, warnings and assumptions.
Changing the horizon to 2027-03-28 recomputes four skipped hazards and zero fires.

A partial report exits 0 unless `--strict` rejects any unsupported record (even
disabled ones): it still emits the report then exits 2. Threshold violation exits
1, equality exits 0; strict takes precedence over threshold failure. As with source
analysis, all-disabled or all-unsupported input exits 2 without a report. Invalid
schema/version/JSON, inconsistent normalized data or assumptions, invalid horizon,
window or threshold, and exceeded forecast/output limits exit 2 without partial
JSON. Snapshot input does not relax any existing bounds or promote unsupported
records. Ordinary extraction JSON remains invalid as a snapshot.

## Standalone overlap from a saved inventory

`overlap (--snapshot FILE | PATH...) --durations JSON --from DATE --days N
[--format json|text] [--strict]` requires exactly one v1 snapshot or a nonempty
source-path group. The duration sidecar remains external and is validated against
saved IDs; it is never embedded or inferred. Neither input, both selectors, an
empty/unreadable snapshot path, malformed schema/version/IDs, inconsistent saved
records/assumptions or an invalid sidecar exit 2 with no stdout. Snapshot and
sidecar validators and all existing byte/record/work/execution/interval bounds
are reused. Original source paths are never opened.

Output is the existing overlap report: exact-second half-open elapsed intervals,
known peaks and peak intervals, carry-in, coverage, exclusions and full schedules
with nullable `duration_seconds`. Saved source metadata, warnings and assumptions
remain intact; `--default-timezone` is ignored for a snapshot, including invalid
CLI timezone values. Changing horizon or sidecar recomputes the report. Missing
and null durations stay unknown, even for a job with no horizon starts. A valid
partial report exits 0; strict emits the report then exits 2 for unknown enabled
supported durations or any unsupported record, including disabled ones.
All-disabled supported input reports a complete zero without durations.

Concrete regression: save six synthetic records, with UTC jobs at 23:59 (120s)
and 00:00 (60s), a Berlin 02:30 Node-RED job (7200s) with an excluded startup
warning, an unsupported repeat, a Berlin Windmill 02:30:17 job (20s) with assumed
activation, and a disabled job. Save default Europe/Berlin. For 2026-10-25 UTC,
one day: 6 known starts, 1 carry-in, known peak 3 exactly in
[01:30:17Z,01:30:37Z), 6 records/4 analyzed/1 disabled/1 unsupported, overall
peak null and incomplete coverage. Deleting exports must leave snapshot output
identical to source output, including saved warnings, assumptions, IDs and metadata.

## Standalone DST hazards from a saved inventory

`dst (--snapshot FILE | PATH...) --from DATE --days N [--format json|text]
[--strict]` requires exactly one single-use snapshot selector or a nonempty
source-path group. Neither input, both groups, repeated selectors (including
`--snapshot=FILE`), empty paths, bad files, malformed schema/version/IDs/records
or inconsistent assumptions exit 2 with empty stdout. The existing strict v1
loader, 8 MiB/2000-record limits, horizon limits, 8 million transition-day
candidate budget and 10000-hazard limit apply unchanged. Original export paths
remain metadata; they are never opened.

Output keeps the existing DST shape: horizon, coverage/exclusions, hazards with
stable IDs and source platform/workflow/node/timezone, saved warnings and saved
assumptions. Hazards sort by local time then ID; exclusions sort by ID for both
source and snapshot reports. Changing the requested horizon recomputes hazards
from normalized records using the host tzdb. The CLI `--default-timezone` is
ignored for snapshots, even if invalid; saved assumptions cannot be overridden.
Text output is unchanged. A valid partial report exits 0; strict emits it then
exits 2 for any unsupported record, including disabled ones. All-disabled or
all-unsupported input exits 2 without a report, matching source DST behavior.

Concrete regression: save the six-record Berlin inventory described for analysis
above, including excluded startup warning, unsupported repeat, disabled job and
assumed timezone/activation state. Over 2026-10-25 UTC, one day, expect four
ambiguous hazards at 02:30/02:31, each with two UTC instants; over 2027-03-28,
four skipped hazards with no UTC instants. Coverage is 6 records/4 analyzed/
1 disabled/1 unsupported, incomplete. Save source reports for both dates, delete
all exports, and reproduce identical full JSON from the snapshot, including
warnings and assumptions. A nontransition horizon has no hazards.

## Standalone jitter suggestions from a saved inventory

`suggest (--snapshot FILE | PATH...) --from DATE --days N [--max-shift MINUTES]
[--window SECONDS] [--format json|text] [--strict]` requires exactly one single-use
snapshot selector or nonempty source-path group. Neither input, both groups,
repeated selectors (also `--snapshot=FILE`), empty/missing/nonregular/symlink files,
malformed v1 schema/IDs/normalized records/assumptions/JSON/UTF-8 and exceeded
limits exit 2 with empty stdout. Ordinary extraction JSON is not a snapshot.

Output is the existing read-only forward jitter plan, before/after minute-start
metrics, coverage and exclusions, plus original warnings and saved assumptions.
Plans and exclusions sort by stable ID for source/snapshot equality independent
of record order. Saved source paths are metadata and never opened. The CLI
`--default-timezone` is ignored for snapshots, even invalid zones. Nothing is
written back to the snapshot or exports. Reports are recomputed for the requested
horizon/window/shift using the host tzdb; snapshots do not store plans.

The existing greedy algorithm, SHA-256 tie breaks, fixed source seconds/year
wildcard, same-hour forward shifts and DST fire-count preservation remain intact.
A valid partial report exits 0; strict emits it then exits 2 for any unsupported
record, even disabled ones. All-disabled/all-unsupported inventories exit 2 with
no report. Bounds remain 0..59 minutes of shift, 60..86400 seconds in whole-minute
windows, horizon 1970..2098 and 1..730 days, v1 8 MiB/2000 records, 8 million
forecast candidates, 20 million optimizer score work and 1 million result firings.

Concrete regression: save the six-record Berlin analysis inventory above (the
Windmill source uses second 17 and a wildcard year). Generate source plans for
2026-10-25 (8 fires), 2027-03-28 (0 fires), and 2026-11-01 (4 fires), one day,
window 120s, max shift 10m. Delete every export and reverse saved record order;
the entire saved report must equal source output, including plan, before/after
metrics, 6/4/1/1 coverage, warnings and timezone/activation assumptions. Repeating
must produce identical output. Zero shift leaves expressions/metrics unchanged;
minute 59 cannot shift and no-fire schedules stay unchanged. Lord Howe half-hour
DST gap/fold cases must reject candidates that change modeled fire counts.
