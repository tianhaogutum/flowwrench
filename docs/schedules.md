# flowwrench schedules

`flowwrench schedules` builds **one offline inventory of the schedules in your n8n,
Node-RED, Huginn, Windmill, Activepieces and crontab exports**. It finds
simultaneous starts, shows DST gaps and duplicates, compares two inventories over
a finite horizon, models running-job overlap from durations you supply, and
generates a reproducible forward jitter plan, all without touching your workflows.

It uses only the Python standard library at runtime. Every command is read-only:
it never edits exports, executes jobs, makes network calls or changes a service.
"Cross-platform" refers to the six automation formats. The tool is developed and
tested on Linux (CI also runs on macOS) and needs a host IANA timezone database
readable by Python `zoneinfo` (normally present on Linux and macOS).

This command was previously the standalone tool `cronherd`.

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench              # or: pipx install flowwrench
flowwrench schedules --version      # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench schedules analyze ./exports --from 2026-11-01 --days 7
```

`python -m flowwrench schedules` is equivalent to `flowwrench schedules`.

## Quick start

The synthetic inputs below live in `tests/schedules/fixtures/` and
`tests/schedules/examples/` in the flowwrench repository; run the commands from
the repository root (or adjust the paths).

```sh
flowwrench schedules extract tests/schedules/fixtures/ --format json
flowwrench schedules analyze tests/schedules/fixtures/ --from 2026-11-01 --days 7
flowwrench schedules dst tests/schedules/fixtures/ --from 2026-11-01 --days 370
flowwrench schedules suggest tests/schedules/fixtures/ --from 2026-11-01 --days 7 --max-shift 10m
flowwrench schedules compare --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1
flowwrench schedules overlap tests/schedules/examples/overlap/jobs.crontab \
  --durations tests/schedules/examples/overlap/durations.json --from 2026-11-01 --days 1
```

No platform installation, API key, network request, running service, workflow
execution or production data is involved.

The synthetic fixture set contains **14 schedules across all six formats**: nine
hourly jobs at minute zero, some daily schedules and one disabled Huginn agent.
For 2026-11-01 through 2026-11-08 UTC with a 10-minute forward allowance,
`suggest` takes the peak from **9 to 1** while keeping **1541 → 1541 firings**;
the sum of squared bucket counts drops from 13637 to 1541. This is a reproducible
load-distribution comparison on synthetic data, not a runtime/CPU benchmark or a
prediction for a production deployment.

## Commands and options

| Command | Purpose |
| --- | --- |
| `extract PATH...` | Normalized inventory (`--format text\|json\|snapshot`) |
| `analyze (PATH... \| --snapshot FILE)` | Simultaneous-start histogram, hot minutes, per-platform peaks, DST hazards |
| `dst (PATH... \| --snapshot FILE)` | Skipped and ambiguous local fire times |
| `suggest (PATH... \| --snapshot FILE)` | Deterministic forward jitter plan |
| `overlap (PATH... \| --snapshot FILE) --durations JSON` | Running-job concurrency from explicit durations |
| `compare --before ... --after ...` | Before/after inventory comparison over one horizon |

Options shared by all commands:

- `--format text|json` (`extract` also accepts `snapshot`); default `text`.
- `--default-timezone IANA`: explicit assumption for a missing platform/server
  timezone (default `UTC`). Applies only to source paths, never to snapshots.
- `--strict`: exit 2 if any record is unsupported (for `overlap` and
  duration-aware `compare`, also if any enabled supported record has an unknown
  duration).

Options for everything except `extract`:

- `--from DATE`: UTC start date, inclusive (default today, UTC).
- `--days N`: number of elapsed 24-hour days, end exclusive (default 7; `dst`: 370).
- `--window SECONDS` (`analyze`, `suggest`, `compare`): trailing firing window, a
  multiple of 60 in 60–86400 (default 60).
- `--max-peak K` (`analyze`, `compare`): exit 1 above this start-count peak
  (`compare`: after side).
- `compare` only: `--before PATH...` or `--before-snapshot JSON`, `--after PATH...`
  or `--after-snapshot JSON` (exactly one per side), `--before-durations JSON` and
  `--after-durations JSON` (both or neither), `--max-peak-increase K`,
  `--max-added-firings K`, `--max-removed-firings K`.
- `suggest` only: `--max-shift MINUTES` (default 10; `10m` also accepted),
  `--objective fire-density|concurrency` (default `fire-density`), and
  `--durations JSON` (requires `--objective concurrency`).
- `overlap` only: `--durations JSON` (required).

## Inputs and normalized output

Pass files or directories (directories are scanned recursively). Multiple paths
are supported:

```sh
flowwrench schedules extract exports/n8n.json exports/flows.json jobs.crontab --format json
flowwrench schedules analyze exports/ --default-timezone Europe/Berlin --from 2027-03-28 --days 1 --format json
```

Source path tokens must be nonempty. An explicit empty token (`""`) in any
`PATH...` list or comparison `--before` / `--after` group (including `--before=`
and `--after=`) is a usage error: stderr explains the empty source path, exit
status is 2, and stdout is empty without a traceback. The entire argument list
is parsed before any source, saved inventory or duration sidecar is read, so an
empty token cannot silently select the current directory, even in a mixed list.
Use an explicit `.` to scan the current directory. Filenames are not trimmed.

`extract --format json` returns an object with `schedules`, coverage, warnings,
and assumptions. Each record includes:

```json
{
  "platform": "n8n",
  "file": "tests/schedules/fixtures/n8n-berlin.json",
  "workflow": "Berlin daily report",
  "node": "Daily 02:30[0:0]",
  "kind": "cron",
  "expr": "30 2 * * *",
  "timezone": "Europe/Berlin",
  "enabled": true,
  "supported": true,
  "error": null
}
```

Additional fields include a stable `id`, `source_expr`, `source_dialect`, fixed
`second`, `day_mode`, `timezone_assumed` and `enabled_assumed`. Cron `expr` is
normalized to five fields. Fixed seconds in n8n/Windmill expressions are retained
as metadata but counted in their containing UTC minute. IDs hash the platform,
relative filename, workflow and node/rule identity, so unchanged input trees give
stable plans even if supplied in a different order or under a different root.
The same physical file passed twice is read once. Duplicate identities from
separate roots cause an explicit error; use their common parent instead.

| Input | Supported mappings and boundaries |
| --- | --- |
| n8n workflow JSON, or array of workflows | `n8n-nodes-base.scheduleTrigger` (`rule.interval`) and legacy `n8n-nodes-base.cron` (`triggerTimes.item`); custom cron, daily/weekly/monthly single-period presets, hourly single-period and minute intervals dividing 60. Node/workflow disable flags are respected. Missing clock fields, multi-day/week/month/hour recurrence and non-dividing minute recurrence are flagged because hidden state/randomization cannot be reconstructed. `typeVersion` is not a server version. |
| Node-RED `flows.json` array | Inject `crontab`, node `d`, tab `disabled`. `repeat` takes precedence over `crontab` and is flagged as a relative interval with unknown phase. Startup-only injects are flagged; a cron inject's additional `once` fire is explicitly warned about and excluded. The runtime timezone must be supplied with `--default-timezone`. |
| Huginn scenario JSON | Agent `schedule` enums, `disabled`, `deactivated`, `never`. Mirrors the enum meanings: `every_2d` is odd month-days, `every_5h` is hours `0,5,10,15,20`, `every_7d` is Monday. `never` is inventoried as disabled with an inert normalized cron and never contributes fires. The instance timezone must be supplied. SchedulerAgent options/control targets are warned about as outside v1. |
| Windmill `*.schedule.yaml` / `*.schedule.yml`, or schedule JSON | Top-level `schedule` (also `cron`), `timezone`, `enabled`, `script_path`, `path`. Six fields are seconds-first; a seventh year field must be `*`. Only top-level scalar metadata is parsed; nested `args` and other payloads are ignored. Selected metadata rejects YAML tags, anchors, aliases, block scalars, collections, duplicate keys and multiple documents. Quote cron strings. This is deliberately not a general YAML parser. |
| Activepieces export, `version` / `flowVersion`, template `flows` | Schedule piece triggers `cron_expression`, `every_hour`, `every_day`, `every_week`, `every_month`, with the actual snake_case input fields, weekends and flow status. `every_hour` is always UTC and defaults to weekdays. `every_x_minutes` is a relative interval and is flagged. Combined restricted day-of-month/day-of-week constraints are flagged to avoid guessing behavior across server versions. |
| `crontab`, `.crontab`, `*.crontab`, `*.cron` | Five-field rows, the usual `@hourly`/`@daily`/`@weekly`/`@monthly`/`@yearly` aliases, and `CRON_TZ` affecting subsequent rows. Environment values and commands are discarded. `TZ` is a job environment variable, not the schedule zone, and produces a warning. System crontabs can be read because everything after five fields is opaque, but user columns and commands are not inventoried. `@reboot` is flagged. |

JSON must be UTF-8 (a BOM is allowed). Duplicate keys, NaN/Infinity, invalid
structures and malformed known files are errors. Unrecognized files are skipped
with warnings. Symlinks and non-regular files are skipped; hidden directories,
`.git`, `.venv`, `node_modules` and caches are not traversed. No archives are opened.

Absent activation state in n8n/Activepieces/Windmill exports is **assumed enabled**
and reported. Absent timezone is **assumed UTC**, or the explicit
`--default-timezone`. These are visible assumptions, not inferred server settings.
Group exports by instance and analyze them separately when their implicit
timezones differ. Explicit unknown timezone identifiers fail even on disabled records.

## Herd analysis and exit codes

```sh
flowwrench schedules analyze tests/schedules/fixtures/ --from 2026-11-01 --days 7 --window 60 --max-peak 8
# Reports peak 9 and exits 1, suitable for CI.
```

`--from` is midnight **UTC**, inclusive; `--days` is an elapsed number of 24-hour
days, with an exclusive end. Supported start years are 1970–2098, days 1–730.

`analyze` reports total firings, a sparse per-minute UTC `histogram`,
`zero_minutes`, the top 20 hot minutes (top 10 shown in text), per-platform
summaries, DST hazards, and the full normalized `schedules` (including excluded
records) in the selected horizon. Schedules, exclusions and hazards use stable ID
order. Omitted histogram minutes are zero. `--window` measures trailing bucket
counts including the current minute. A longer window adds `window_histogram`; the
beginning is truncated at the horizon start (no warm-up). `peak` and `--max-peak`
refer to this window. This measures **simultaneous starts**; use `overlap` to
model running jobs from explicit durations. Worker capacity is not modeled.

Cron supports numbers, month/weekday names, inclusive ranges, lists, positive
steps (`*/15`, `5/20`) and Sunday as `0` or `7`. Not supported: wraparound ranges,
Quartz `? L W #`, `H`, year constraints, multiple firings per minute, templates
and expressions. Day matching is explicit in `day_mode`: Vixie-style for crontab
(a field starting with `*` participates as a wildcard), full-value wildcard/otherwise
OR for n8n, literal `*` wildcard/otherwise OR for Node-RED, and AND for
Windmill/Huginn. Activepieces supports only one restricted day field. Invalid
syntax is flagged **per record**; supported records continue to be analyzed.

| Exit | Meaning |
| --- | --- |
| 0 | Report produced; check `complete`, `excluded`, warnings and assumptions for coverage. |
| 1 | Analyzed peak is above `--max-peak`, or any enabled comparison peak/increase/added/removed-firing gate fails. |
| 2 | Invalid arguments/input/timezone, malformed file, no schedules, no enabled supported schedules (except `compare`/`overlap`), resource limit, or `--strict` with any unsupported record (also unknown active durations for `overlap` or duration-aware `compare`). |

Use `--strict` in CI to reject partial inventories. Disabled records stay in the
inventory and exclusions but do not contribute fires. `complete` means no enabled
unsupported records; it does not certify platform runtime parity or assumptions.
For `overlap` and duration-aware `compare`, top-level `complete` additionally
requires known durations for every enabled supported schedule on the relevant
sides. JSON goes to stdout; diagnostics go to stderr. Invalid-input failures
produce no partial JSON. `--strict` emits a report and then exits 2; it takes
precedence over a peak threshold failure.

## Running-job overlap with explicit durations

`overlap` reads source exports or one v1 saved inventory, plus a versioned JSON
duration sidecar. First use `extract --format json` to get stable schedule IDs.
Enter a constant elapsed duration in **integer seconds** for each job you can
model; omit an ID or use `null` for an unknown duration. There is no inferred or
default duration, and zero explicitly means no running load. Durations must be in
0–604800 seconds (up to seven days); zero-duration jobs still count as starts.

The synthetic `tests/schedules/examples/overlap/jobs.crontab` contains:

```text
CRON_TZ=UTC
0 0 * * * echo first
5 0 * * * echo second
```

Its `tests/schedules/examples/overlap/durations.json` is:

```json
{
  "version": 1,
  "durations_seconds": {
    "0900cd3c1c7b2a35c90d": 600,
    "855f4270cceb30e02189": 300
  }
}
```

```sh
flowwrench schedules overlap tests/schedules/examples/overlap/jobs.crontab \
  --durations tests/schedules/examples/overlap/durations.json --from 2026-11-01 --days 1 --format json
```

Expected: `peak=2`, `known_peak=2`, `known_firings=2`, `carry_in_firings=0`,
`complete=true`, and exactly one `known_peak_intervals` entry:
`{"from":"2026-11-01T00:05:00Z","until_exclusive":"2026-11-01T00:10:00Z"}`.
Text output begins `Known-duration peak 2; overall peak 2`.

To keep modeling after removing the exports, save a v1 inventory:

```sh
flowwrench schedules extract tests/schedules/examples/overlap/jobs.crontab --format snapshot > inventory.json
flowwrench schedules overlap --snapshot inventory.json \
  --durations tests/schedules/examples/overlap/durations.json --from 2026-11-01 --days 1 --format json
```

This returns the same report. Use exactly one `--snapshot FILE` or a nonempty
group of source paths. The snapshot preserves IDs, source metadata, warnings and
saved assumptions; original exports are never read. `--default-timezone` applies
only to source paths and cannot override saved assumptions. Metrics are recomputed
for the requested horizon and the external sidecar. Invalid snapshots or sidecars
exit 2 with no partial JSON. Keep both files outside export directories.

Each execution occupies `[start, start + duration)` in UTC; a run that ends
exactly when another starts does not overlap it. The deterministic sweep groups
equal timestamps and merges adjacent intervals at the same peak, even if the
contributing jobs change. Repeated executions of one job may overlap each other.
Fixed source seconds are honored exactly, before timezone conversion. Durations
are elapsed time through DST; gaps are skipped and both ambiguous folds produce
executions. These are modeling assumptions, not promises about platform execution
policy.

For each known-duration job the model looks back by its duration, includes every
execution still running at the horizon start, and clips intervals at both horizon
edges. `known_firings` counts known-duration starts inside the horizon;
`carry_in_firings` counts earlier executions intersecting it. Both consume the
shared one-million execution bound, and lookback consumes the eight-million
candidate budget. At most 100,000 peak intervals are returned.

Replace the sidecar with `tests/schedules/examples/overlap/partial.json` to leave
the second job's duration unknown. Expected: `known_peak=1`, `peak=null`,
`complete=false`, that ID in `unknown_duration_ids`, and its
`duration_seconds=null` in `schedules`. `known_peak_intervals` always describes
only the known-duration contribution; `known_peak` is a lower bound when
incomplete. If all durations are unknown it is 0 with no intervals, while `peak`
stays `null`. Unknown durations stay unknown even when their schedule has no
starts in this horizon, because carry-in cannot be bounded. Disabled and
unsupported records remain visible but contribute no intervals; enabled
unsupported records also make the overall peak unknown.
`schedule_coverage_complete` keeps the usual unsupported-record coverage meaning.

Sidecar IDs must match the inventory exactly. Unknown IDs, booleans,
fractional/string/negative/oversized durations, duplicate JSON keys, nonfinite
numbers, unknown schema fields/versions, malformed UTF-8/JSON and sidecars over
1 MiB fail with exit 2 and no partial JSON. Only regular files are accepted;
symlinks are rejected. A valid partial report exits 0; `--strict` emits the report
and then exits 2 for any unsupported record or unknown enabled supported duration.
All-disabled inventories may report a known zero. `overlap` has no threshold flag
and no capacity/queue simulation. Supplied durations describe hypothetical
constant runtimes, not measured runtimes.

Duration sidecar selectors are single-use: `overlap --durations`,
`compare --before-durations` / `--after-durations`, and `suggest --durations`.
Repeating one (same or different path, missing or empty earlier path, or
`--option=FILE` syntax) is a usage error: exit 2 with empty stdout before any
export, snapshot or duration file is read.

## Compare inventories over a finite horizon

`compare` reads supported **source exports** or versioned inventory snapshots.
Both `--before` and `--after` accept multiple files/directories. Use equivalent
roots and preserve relative filenames and schedule identities.

```sh
flowwrench schedules compare \
  --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after \
  --from 2026-10-25 --days 1 --format json
```

The synthetic example has these `jobs.crontab` inputs:

```text
# before/jobs.crontab
CRON_TZ=Europe/Berlin
30 2 * * * echo report
30 3 * * * echo unchanged

# after/jobs.crontab
CRON_TZ=Europe/Berlin
30 3 * * * echo report
30 3 * * * echo unchanged
```

Expected changes on Berlin's fall-back day: remove `2026-10-25T00:30Z` and
`2026-10-25T01:30Z` (both local 02:30, folds 0/1, UTC offsets 7200/3600 seconds),
add `2026-10-25T02:30Z` (local 03:30), and keep the unchanged job at 02:30Z.
The report has `before.peak=1`, `after.peak=2`, `peak_delta=1`,
`before.total_firings=3`, `after.total_firings=2`, `total_firings_delta=-1`, and
`unchanged_firings=1`. Text output begins:

```text
Peak 1 -> 2 in trailing 60s; 2 removed; 1 added; 1 unchanged
```

JSON `added` and `removed` arrays are ordered by UTC minute, ID and fixed source
second. Each event includes `id`, `utc_minute`, `source_second`, `timezone`,
`local_time`, `utc_offset_seconds`, PEP 495 `fold`, `ambiguous`, and
`possible_utc_minutes` (both alternatives for a fold, even if one is outside the
horizon). Peaks remain **minute-bucket start counts**, with optional
`--window SECONDS` trailing counts and no warm-up.

Matching uses `(stable schedule ID, UTC minute, fixed source second)`. A fixed
second change produces a removal and an addition even when minute peaks stay
equal. Different records never cancel each other's changes. Renaming files,
workflows or nodes, or reordering index-identified rules/crontab lines, can change
IDs and produce removals/additions; there is no fuzzy identity matching. A
timezone or expression change with identical modeled fires remains unchanged;
both sides' full `schedules` metadata still records that change.

Each side also keeps coverage, exclusions, warnings, timezone/activation
assumptions, DST hazards and peak summaries (without full histograms). Gap times
produce no fire events but remain visible as `skipped` hazards. Ambiguity hazards
are kept whenever either possible instant is in the horizon. A side with only
disabled or unsupported records has zero fires; inspect `complete` and exclusions
before interpreting a reduction. `--strict` checks **both** sides. The default
timezone assumption applies to both inputs; explicit source timezones stay
independent.

Malformed input, unknown timezone, an empty/no-schedule input, invalid horizon or
window, duplicate identities within a side, and exceeded resource bounds exit 2
with no partial JSON. Without a gate, a valid comparison exits 0 even if peaks
increase.

### CI gates

All gates take a nonnegative integer K, change only the exit status (the full
JSON/text report is unchanged), work with source, saved and mixed inputs, and can
be combined: any failed gate exits 1. `--strict` exit 2 takes precedence and
still prints the report. Negative, noninteger or missing values are argument
errors (stderr diagnostic, exit 2, empty stdout) before any input is read.
Partial inventories gate only the observed supported starts/events; use
`--strict` to reject incomplete coverage. Duration sidecars never change these
metrics.

- `--max-peak K`: exit 1 when `after.peak > K` (start counts in the selected
  horizon and trailing `--window`, including zero-duration jobs). Equality and
  a zero-active after side with K=0 pass. It does not gate `overlap.after.peak`.
- `--max-peak-increase K`: exit 1 when the signed `peak_delta = after.peak -
  before.peak` is strictly greater than K. Equal or decreasing peaks pass with K=0.
- `--max-added-firings K`: exit 1 when `len(added) > K`. Removals never offset
  additions: same-total replacements, fixed-second edits and changed identities
  can fail even with unchanged peaks or a negative net firing delta.
- `--max-removed-firings K`: exit 1 when `len(removed) > K`. Additions never
  offset removals; pure additions pass with K=0.

```sh
flowwrench schedules compare --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1 --max-peak 1
# Complete report, after.peak=2, exit 1. Use --max-peak 2 to exit 0.

flowwrench schedules compare --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1 --max-peak-increase 0
# Complete report, peak_delta=1, exit 1. Use --max-peak-increase 1 to exit 0.

flowwrench schedules compare --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1 --max-added-firings 0
# Complete report, 1 added event, exit 1. Use --max-added-firings 1 to exit 0.

flowwrench schedules compare --before tests/schedules/examples/comparison/before \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1 --max-removed-firings 1
# Complete report, 2 removed events, exit 1. Use --max-removed-firings 2 to exit 0.
```

Forecast work, firings and DST hazards are bounded across both sides together,
and the combined added/removed output is limited to 100,000 events.
File/record/byte limits apply per input side. Shorten the horizon or split
inventories when a limit is exceeded.

### Save a baseline without keeping workflow exports

```sh
flowwrench schedules extract tests/schedules/examples/comparison/before --format snapshot > baseline.json
flowwrench schedules compare --before-snapshot baseline.json \
  --after tests/schedules/examples/comparison/after --from 2026-10-25 --days 1 --format json
```

This produces the same Berlin comparison as above: two removed fires, one added,
one unchanged, minute peaks 1 -> 2. The baseline exports are no longer needed.
Save the snapshot outside directories you scan for source exports.

A saved inventory can also be analyzed, checked for DST hazards, or used for a
jitter plan directly:

```sh
flowwrench schedules analyze --snapshot baseline.json --from 2026-10-25 --days 1 --window 120 --format json
# three firings, peak 1, one ambiguous Berlin DST hazard
flowwrench schedules dst --snapshot baseline.json --from 2027-03-28 --days 1 --format json
# one skipped Berlin 02:30 local time with no UTC instants
flowwrench schedules suggest --snapshot baseline.json --from 2026-10-25 --days 1 --max-shift 10m --window 120 --format json
```

Each side of `compare` accepts either source paths or one snapshot
(`--before-snapshot FILE`, `--after-snapshot FILE`); either or both sides may use
snapshots. `analyze`, `dst`, `suggest` and `overlap` accept exactly one
single-use `--snapshot FILE` or a nonempty source-path group. Metrics, coverage
and hazards are recomputed for the requested horizon/window; saved IDs, warnings
and assumptions are kept and `--default-timezone` cannot override them. Original
paths are never opened. Inventory selectors may each be given only once
(including `--snapshot=FILE`); repeats exit 2 with empty stdout before any file
is read. Put multiple comparison source paths in one `--before PATH...` or
`--after PATH...` group.

Snapshot output has `schema: "cronherd.inventory"` and integer `version: 1`
(the schema identifier is kept from the former standalone tool so existing
snapshots stay valid), plus full `schedules`, `warnings` and `assumptions`.
`extract --format json` is not a snapshot, and analysis/comparison reports are
not accepted as snapshots. Validation rejects unknown/missing fields or versions,
wrong types, duplicate keys or IDs, nonfinite JSON, malformed UTF-8, invalid
timezones, inconsistent records/assumptions, empty inventories and exceeded
bounds, with exit 2 and no JSON output. Only regular files up to 8 MiB and 2000
records are read.

A snapshot preserves editable source metadata, not authenticated provenance or a
timezone database: results still depend on the current host tzdb. Paths and
labels may be sensitive; sanitize them before sharing. See
[the full v1 snapshot schema and negative-case contract](schedules/SNAPSHOTS.md).

### Compare durations independently of schedule changes

Supply both per-side duration sidecars to add exact-second running-job metrics
alongside the minute-start comparison. Either side may use a snapshot. Sidecar
IDs are validated against their own inventory; durations are never copied
between sides or saved into snapshots.

```sh
flowwrench schedules compare \
  --before tests/schedules/examples/overlap/jobs.crontab --after tests/schedules/examples/overlap/jobs.crontab \
  --before-durations tests/schedules/examples/overlap/durations.json \
  --after-durations tests/schedules/examples/overlap/short.json \
  --from 2026-11-01 --days 1 --format json
```

The schedules are identical: two unchanged starts, minute peaks 1 -> 1 and
`peak_delta=0`. Before durations are 600/300 seconds; after durations are
300/300. `overlap.before.peak=2`, `overlap.after.peak=1`,
`overlap.peak_delta=-1` and `overlap.complete=true`. Text adds
`Overlap peak 2 -> 1; delta -1` and each side's known lower bound.

With `--after-durations tests/schedules/examples/overlap/partial.json`, the after
known peak is 1, its overall peak is `null`, `overlap.peak_delta=null`, and both
`overlap.complete` and top-level `complete` are false. The overlap delta is
numeric only when **both** overlap sides are complete. The existing
`before.complete` / `after.complete` fields describe schedule coverage; duration
completeness is in `overlap.before/after.complete`. See
[the duration-aware comparison contract](schedules/OVERLAP_COMPARISON.md).

## Suggest shifts

`suggest --max-shift 10m` (also `10`) produces proposed five-field expressions,
source-dialect expressions, offsets, and before/after metrics. It tries forward
shifts in stable ID order, minimizes `(peak, sum of squared bucket counts)`, and
uses SHA-256 for deterministic tie breaks. It includes offset zero, never worsens
the measured objective, preserves the number of fires per record in the horizon,
and leaves unobserved schedules unchanged. It is a bounded greedy plan, **not**
a global optimum or a guarantee outside the measured dates.

The maximum shift is 0–59 minutes. Shifts stay in the same hour: `55 * * * *` may
move at most four minutes and `59 * * * *` cannot move. This prevents incorrect
hour/day/month rollover rewrites. Non-minute fields and fixed source seconds are
preserved. Dense schedules may have no useful shift. Preset-only platforms may
need a custom cron trigger; Huginn's enum itself cannot encode arbitrary offsets.
Dependencies, timing SLAs and platform edit support need manual review. Applying a
proposal is always a separate manual step.

### Using modeled running durations

Opt in with **both** `--objective concurrency` and an explicit duration sidecar:

```sh
flowwrench schedules suggest tests/schedules/examples/overlap/jobs.crontab \
  --objective concurrency --durations tests/schedules/examples/overlap/durations.json \
  --from 2026-11-01 --days 1 --max-shift 10m --format json
# Or replace the source path with: --snapshot inventory.json
```

This example has two starts and a minute-start peak of **1 → 1**, but a modeled
running peak of **2 → 1**. JSON reports `objective: "concurrency"` and adds full
`overlap.before` / `overlap.after` reports. The objective minimizes the known
running peak, then the integral of squared concurrency over elapsed seconds
(`overlap.before_squared_job_seconds=1500` → `after_squared_job_seconds=900`).
Top-level `before`/`after` continue to describe minute-start density.

Every enabled supported job needs a known duration, **even without horizon
fires**; missing/null values fail with exit 2 and empty stdout. `--objective
fire-density` is the explicit default; a sidecar with that objective, or the
concurrency objective without a sidecar, is rejected. See
[the duration-aware suggestion contract](schedules/DURATION_SUGGEST.md).

## DST

`dst` reports `skipped` and `ambiguous` **local** matching fire times with their
possible UTC instants. It uses `zoneinfo` fold round trips and transition-day
checks, including half-hour transitions:

```sh
flowwrench schedules dst tests/schedules/fixtures/n8n-berlin.json --from 2027-03-28 --days 1
# skipped: 2027-03-28T02:30 Europe/Berlin
```

Forecasts deliberately skip nonexistent times and count both folds of ambiguous
times. Platforms may deduplicate, defer, catch up or skip differently; these are
hazards, not claims about a particular runtime's DST execution policy. Host tzdb
updates may change future predictions.

## Limits and safety

Bounds: 2,000 files, 2,000 schedule records, 8 MiB per recognized file, 64 MiB
total, 512-character labels, 1,024-character cron expressions, 8 million
candidate local minutes, 20 million optimizer score work units, 1 million
firings and 10,000 DST hazards. Exceeding a bound fails explicitly; split inputs
or shorten the horizon. No shell, `eval`, YAML object construction or workflow
expression evaluation is used. Command strings, payloads and environment secrets
are not kept in reports; labels, paths and cron metadata are, so sanitize those
before sharing. Untrusted labels are escaped in text output.

## Development

```sh
uv run --extra test pytest tests/schedules
```

Tests are offline. They cover parser tables, every adapter, CLI exit codes,
per-minute counts against an independent UTC-scan oracle, Berlin/New York skipped
and ambiguous times, Lord Howe's half-hour transition, deterministic plan replay,
snapshots, overlap, comparison gates, malicious/unsupported inputs and resource
bounds.

## License

Apache-2.0, as part of flowwrench. All fixtures are synthetic; no upstream
application source is distributed. See [THIRD_PARTY.md](../THIRD_PARTY.md) for
provenance and attribution.
