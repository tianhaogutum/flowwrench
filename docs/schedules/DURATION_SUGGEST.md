# flowwrench schedules: duration-aware suggestion contract

Part of [`flowwrench schedules`](../schedules.md); `suggest`, `overlap` and `compare`
below are `flowwrench schedules` subcommands.

Input: `suggest PATH...` or `suggest --snapshot FILE`, plus BOTH
`--objective concurrency --durations FILE`. Sidecars retain version 1 and their
existing strict ID/schema/file/size validation. Known durations are integer elapsed
seconds in 0..604800; zero explicitly means an empty running interval, never unknown.
This shared extension also applies to overlap and duration comparisons. Missing or
null durations for ANY enabled supported record fail even if it has no horizon fires.
Disabled/unsupported records need no duration. Repeated suggestion `--durations`
is rejected before reads. A sidecar without this objective, or this objective
without a sidecar, exits 2 with empty stdout. Default `fire-density` preserves the
existing algorithm and report shape exactly.

Objective: lexicographically minimize `(known_peak, squared_job_seconds)`, where
squared_job_seconds is the integral of the square of modeled concurrency over the
finite UTC horizon. Intervals are exact-second, half-open, constant elapsed durations;
carry-in is included using the existing overlap lookback. No per-second expansion.
Equal objective values prefer offset zero, then SHA-256(ID:offset) among improving
nonzero candidates. Process stable IDs once. Preserve existing 0..59 minute bounds,
same-hour constraint, fixed source seconds, source dialect, timezone assumptions and
per-record horizon fire counts (including exact-second counts). No observed starts
means no shift; zero duration and no-improvement schedules remain unchanged.

Output retains existing `before`/`after` minute-start summaries, plan IDs/metadata,
coverage, exclusions, warnings and assumptions, and adds `objective: concurrency`
and `overlap`: full standalone `before`/`after` reports plus
`before_squared_job_seconds` and `after_squared_job_seconds`. Both overlap reports
use original identities; after schedules carry proposed expressions/source expressions.
Text identifies the objective and before/after modeled running peaks. Unsupported
enabled records keep `complete=false` and nullable overlap `peak`; known_peak and
objective scores are lower bounds over supported records only. They are NOT zero
load assumptions. `--strict` emits this explicitly partial report then exits 2 for
any unsupported record, including disabled ones, as in existing suggest.

Failing example: two hourly jobs at minutes 0 and 5, durations 600 and 300s.
Both have minute-start peak 1 and 48 total daily starts. Original running peak 2
and squared_job_seconds 36000; a bounded shift of the second to minute >=10 or
first past the second's end reduces running peak to 1 and squared_job_seconds
to 21600, with 48 starts. Fire density alone does not describe this distinction.

All existing forecast/optimizer/output limits remain. Duration lookbacks and all
candidate interval enumerations and final reports share forecast work and a combined
1-million execution budget; event scoring charges sorting work to the optimizer
budget. Exceeded limits or invalid sidecars fail atomically without JSON. Snapshot
assumptions are never overridden, and no source file/snapshot/sidecar is edited.
This is a bounded greedy manual proposal, no global optimum, outside-horizon promise,
measured-runtime prediction, or scheduler runtime parity.
