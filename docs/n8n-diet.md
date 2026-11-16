# flowwrench n8n-diet

`flowwrench n8n-diet` (formerly `n8n-exec-diet`) works offline on self-hosted n8n SQLite databases. It finds the workflows that store the most execution data, simulates retention policies, and recovers already freed disk space into a verified compact copy. **It never prunes or replaces the source.** You do not need an n8n server, credentials, network calls, or any runtime Python packages beyond the standard library.

n8n itself is source-available under the Sustainable Use License; it is not OSI open source. No n8n code is bundled. See [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer, SQLite 3.31+ (supplied by Python's `sqlite3`), a POSIX system with a local filesystem that supports POSIX byte-range locks (the tool was developed and tested on Linux), and a backup taken while n8n was stopped.

```sh
pip install flowwrench          # or: pipx install flowwrench
flowwrench n8n-diet --version   # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench n8n-diet analyze ./database.sqlite
```

`python -m flowwrench n8n-diet` is equivalent to `flowwrench n8n-diet`. The command has no runtime dependencies. Comparison output records the SQLite version it ran with; nothing is downloaded.

## Try it with synthetic data

Use fresh output paths, because existing files are refused:

```sh
flowwrench n8n-diet fixture gen --out /tmp/n8n-demo.sqlite --seed 42
flowwrench n8n-diet analyze /tmp/n8n-demo.sqlite --top 5
flowwrench n8n-diet analyze /tmp/n8n-demo.sqlite --json
flowwrench n8n-diet plan /tmp/n8n-demo.sqlite \
  --max-age-days 30 --max-count 200 --keep-failed \
  --as-of 2026-11-01T12:00:00Z --json
flowwrench n8n-diet reclaim-plan /tmp/n8n-demo.sqlite --out /tmp/reclaim.json
flowwrench n8n-diet reclaim /tmp/n8n-demo.sqlite \
  --plan /tmp/reclaim.json --out /tmp/compact.sqlite
flowwrench n8n-diet verify-copy /tmp/compact.sqlite --plan /tmp/reclaim.json
```

The generator creates five workflows and 2,000 executions. It then deletes 60% of them, cascading the delete to execution data, which leaves 800 executions and some free pages. All timestamps and data are synthetic, and the default seed is 42. For smaller experiments, use `--executions`, `--deleted-percent` and `--payload-bytes`. Use `--overwrite` to replace an existing output and `--json` for a machine-readable summary. On the same Python/SQLite runtime, output is checked to be identical byte for byte. File layout and the random number generator can differ between runtime versions.

## Commands and output

### `analyze DB [--top N] [--max-workflows N] [--json] [--no-dbstat]`

`analyze` reports the file size, the page size and page count, the number of freelist pages, and the exact free-page capacity (`page_size × freelist_count`). Per-table `dbstat` allocation includes each table's indexes. When `dbstat` is unavailable, or you turn it off with `--no-dbstat`, `allocated_bytes` is null. `value_bytes_estimate` then sums the UTF-8 or BLOB lengths of the stored values and leaves out page, record and index overhead. This fallback is an estimate, not physical disk accounting.

All workflows are listed in order of stored payload bytes, with execution counts, status counts and names where available. `--top` limits only the list of largest executions, not the list of workflows. Names of deleted or missing workflows stay null. Missing execution-data rows and orphan execution-data rows are counted separately. Orphan rows are not assigned to a workflow, and `plan` never selects them.

**How names are loaded.** `analyze` fetches names only for the original workflow IDs that appear in `execution_entity` on the same locked snapshot. That includes protected executions and executions without data. Unused definitions and orphan data never cause names to be fetched or workflow rollups to be added. An empty or orphan-only input fetches no names. If a name is missing or the catalog is absent, the name is null. Null IDs, empty strings and the literal strings `"None"` and `"null"` are kept distinct. IDs are matched on their raw values, without type or case coercion, and the workflow order does not change. A single set-based SELECT loads the names, so there is no query per workflow and no parameter list that grows with the number of workflows.

This keeps unneeded names out of Python, but the tool makes no claim about total memory or timing. Matching names can be arbitrarily long. Workflow and status aggregation and the largest-executions report still use memory. SQLite may scan catalog and execution-ID metadata and build temporary structures, and its allocations are not limited. Table storage accounting still covers **all** definitions, including unused ones, in both the `dbstat` and the fallback measurement.

#### `--max-workflows N`

`analyze --max-workflows N` optionally caps the number of **workflow entries in the report**. The Python API takes the same limit as `analyze(..., max_workflows=None)`, and `analyze_connection` accepts it too.

- **Values.** Leaving it out, or passing `None`, means no limit. N must be a non-negative integer. The API rejects booleans, floats, strings, containers and negative integers. Zero allows an empty or orphan-only input.
- **What counts.** Protected executions and executions with missing data count toward their workflow. Unused definitions and orphan data do not count. Null, empty and literal IDs keep the meanings described above. The count uses the same SQLite workflow/status groups and Python key equality as the report itself, including the existing collation and choice of group representative. It does not add a new rule for what counts as a distinct ID.
- **Within the limit.** JSON and human-readable reports are identical to the unlimited output. All names, aggregates, ordering, largest executions, source accounting and `dbstat` or fallback fields are still present. `--top` still limits only the largest executions.
- **Over the limit.** The hand-authored ten-execution fixture has three workflows in its report. With that fixture, `--max-workflows 3` succeeds with unchanged output. `--max-workflows 2` exits 2, prints nothing on stdout, and writes only this diagnostic to stderr:

```text
flowwrench n8n-diet: observed workflow count 3 exceeds max_workflows 2
```

**Order of checks.** The CLI parses the integer before touching the source, and the API validates the limit before accessing the source (or, for `analyze_connection`, the schema). The checks on the source, locks, rollback journal, WAL/SHM files and supported schema all run before the count check. When `dbstat` is requested, this includes the check for a schema object shadowing `dbstat`. `--force` never bypasses any of those refusals. The count check runs on the same locked snapshot, before names are loaded, largest executions are selected or the report is built. A refusal contains only counts, never identities, names or payload values, and the source bytes stay unchanged.

**Cost.** Setting a limit adds a full aggregate scan and a Python set of the report keys, which run before the existing report scans. This is **not a bound on evaluation memory, process memory or serialized size**. The key set, matching names, workflow/status aggregation, largest-executions list, schema metadata, and SQLite's selection, grouping and temporary allocations are all unlimited. Table storage still accounts for all definitions. The tool makes no claim about scan speed and publishes no benchmark.

#### Payload bytes

`payload_bytes` is exactly the stored byte length of `execution_data.data` plus `workflowData`. It is **not** the JSON character length, the total row size, the uncompressed data size, or a share of database pages. Payloads are never decoded, evaluated or printed. When `storedAt` locations are present, their counts are reported. Data stored in the filesystem, S3 or Azure, and external binary files, are not measured. Example JSON fields:

```json
{
  "storage": {"page_size": 4096, "freelist_count": 3248, "freelist_bytes": 13303808},
  "totals": {"executions": 800},
  "table_storage": {"method": "dbstat", "includes_indexes": true}
}
```

### `plan DB --max-age-days N --max-count M [--keep-failed] [--as-of TIME] [--json]`

You must give at least one limit. `plan` is always a dry run: it never issues DELETE statements and never calls n8n, its APIs or workflow steps. The policy it simulates is an explicit storage scenario. It does not reproduce n8n's internal pruning job.

1. **Eligible rows.** Only `success`, `error`, `crashed` and `canceled` executions with a valid `stoppedAt` at or before the reference time are eligible. Other statuses, null stopped times and stopped times in the future are protected. A malformed timestamp on an eligible row is an error and is never treated as old.
2. **`--keep-failed`** protects `error`, `crashed` and `canceled` executions from both limits.
3. **The limits.** The age limit selects eligible rows **strictly earlier** than `as_of - N days`. The count limit keeps the newest M eligible rows across all workflows. Rows are ordered by stopped time descending, and ties are broken by execution ID compared as a string, descending. Protected rows do not use up the count budget. Zero is valid for either limit.
4. **Result.** The selection is the union of both limits. The reason counts `age`, `count` and `both` do not overlap. The report gives the selected execution and data rows, their exact payload bytes, and selection totals for each workflow.

Times can be ISO-8601 with an offset or SQLite `YYYY-MM-DD HH:MM:SS[.sss]`. Timestamps without a timezone are read as UTC. `--as-of` defaults to the current UTC time, so set it explicitly when you want to replay a plan. Age is limited to 365,000 days and the cutoff must fit in the datetime range.

The size projection is `projected_size_estimate_bytes = max(page_size, allocated_page_bytes - selected_payload_bytes)`. It assumes the selected rows are eventually deleted **and** the database is compacted. It is a heuristic, not a predicted output file size. Record and index overhead, B-tree packing, related n8n tables and external files are not included. The simulation covers only the two execution tables, and no option applies the plan.

### `compare DB --baseline baseline.json --candidate candidate.json --as-of TIME [--summary-only] [--max-workflows N] [--max-members N] [--json]`

`compare` evaluates two explicit retention policies on **one locked snapshot** at the same required reference time. Each policy file is a UTF-8 JSON object that may contain only `max_age_days`, `max_count` and `keep_failed`. Limits may be left out or set to null, but at least one must be non-null. Limits must be integers, not booleans or fractions. `keep_failed` must be a boolean and defaults to false. The limits, eligibility rules, strict age boundary and count ordering are the same as for `plan`. In the report, `keep` means eligible but within the limits, and `protected` means excluded from the limits.

For a complete offline example, run this from the repository root. It builds a fresh database with ten executions from the hand-authored integration fixture:

```sh
demo=$(mktemp -d)
python3 - "$demo/input.sqlite" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3, sys
with closing(sqlite3.connect(sys.argv[1])) as conn:
    conn.executescript(Path('tests/n8n_diet/fixtures/split-schema.sql').read_text())
PY
printf '%s\n' '{"max_count":2}' > "$demo/baseline.json"
printf '%s\n' '{"max_age_days":20,"max_count":1,"keep_failed":true}' > "$demo/candidate.json"
flowwrench n8n-diet compare "$demo/input.sqlite" \
  --baseline "$demo/baseline.json" --candidate "$demo/candidate.json" \
  --as-of 2026-11-01T00:00:00Z --json
```

The report contains all nine transitions (empty cells included), the group totals for each policy, and a signed `drop_delta` (candidate minus baseline). Each transition has execution and data-row counts, exact payload bytes, and a `members` list. Each member gives the execution ID, workflow ID, bytes and the reason from each policy. Groups and transitions always appear in the order `keep`, `drop`, `protected`. Members are sorted by execution ID in ascending lexicographic order. The example's non-empty cells are:

| Baseline → candidate | Execution IDs | Data rows | Payload bytes |
| --- | --- | ---: | ---: |
| keep → keep | 4 | 1 | 9 |
| keep → drop | 3 | 1 | 21 |
| drop → drop | 1 | 1 | 10 |
| drop → protected | 2, 8, 9 | 3 | 36 |
| protected → protected | 10, 5, 6, 7 | 2 | 8 |

The totals are 10 executions, 8 data rows and 84 payload bytes, and `drop_delta` is `{"executions":-2,"data_rows":-2,"payload_bytes":-15}`. Missing data rows count as zero bytes. Orphan data, auxiliary tables and external files are not included. Payload bytes measure stored `data` plus `workflowData`, not disk space saved. The comparison makes no file-size projection and never deletes anything.

#### Per-workflow rollups

`workflows` groups the same decisions by `workflow_id`. Each entry contains `workflow_name`, `totals`, `baseline.groups`, `candidate.groups`, `drop_delta` and `transitions`. Totals, groups, deltas and cells all use the same three metrics: `executions`, `data_rows` and `payload_bytes`. Every workflow has all nine cells, zeros included, in the same order as the global list. Workflow cells leave out `members` so execution identities are not repeated. For example, wf-a's drop → protected cell is:

```json
{"from":"drop","to":"protected","executions":1,"data_rows":1,"payload_bytes":23}
```

A workflow's cells add up to its own totals and policy groups, and the same cell summed across workflows equals the global cell. Workflow drop deltas add up to the global delta. The example above gives:

| Workflow ID | Name | Executions | Data rows | Payload bytes | Drop delta (executions / data rows / bytes) |
| --- | --- | ---: | ---: | ---: | --- |
| missing-workflow | null | 1 | 0 | 0 | 0 / 0 / 0 |
| wf-a | Email | 4 | 3 | 37 | -1 / -1 / -23 |
| wf-b | 订单 | 5 | 5 | 47 | -1 / -1 / +8 |

Count budgets stay **global**, and rollups never rerun a policy inside a single workflow. In the example, wf-a has no `keep` executions under the baseline, even though the baseline keeps two executions overall.

- **Order.** Workflows are sorted by string ID in ascending order. If the schema allows a null ID, it comes first.
- **IDs and names.** A null ID is distinct from an empty ID or the literal `"None"`. A name is null when the optional workflow table or the matching row is absent.
- **Which workflows appear.** Only IDs that appear in executions are listed. This includes workflows whose executions are all protected or have missing data. An empty execution table gives `"workflows":[]`. Unused workflow definitions and orphan data add no rollups.

The human-readable output also shows each workflow's totals, its nine cells and its drop delta. Payload bytes are printed as exact integers, and IDs and names are escaped.

#### `--summary-only`

`--summary-only` leaves out the execution members and keeps everything else: every global and per-workflow total, policy group, transition cell, workflow ID and name, and signed drop delta.

- **JSON.** The report adds a top-level `"members_included":false` and removes the `members` key from all nine global cells, including the empty ones. Execution identities and decision reasons are not moved anywhere else. Empty and orphan-only inputs still give nine zero global cells and `"workflows":[]`.
- **Human-readable output.** The output is labelled with `members_included=false`, keeps the aggregate lines and leaves out the execution member lines.
- **Default output.** Without the flag, nothing changes: full JSON keeps `members` and has no `members_included` field.

For the same fixture and policies as above:

```sh
flowwrench n8n-diet compare "$demo/input.sqlite" \
  --baseline "$demo/baseline.json" --candidate "$demo/candidate.json" \
  --as-of 2026-11-01T00:00:00Z --summary-only --json
```

Python callers can use `flowwrench.n8n_diet.core.compare(..., summary_only=True)`. The keyword defaults to `False` and must be a boolean. Summary-only output is smaller because the member lists are gone; how much smaller depends on the input. It skips building the member records, but it **does not limit evaluation memory**. Execution metadata, sorting and both policies' decisions still use memory proportional to the number of executions, and so do the workflow rollups and matching names. Both modes evaluate the same policies and apply the same refusals.

#### How names are loaded

`compare` loads names the same way `analyze` does. It fetches names only for the original workflow IDs that appear in `execution_entity` on the same locked snapshot, including protected executions and executions without data. Unused definitions and orphan data never cause names to be fetched or rollups to be added. An empty or orphan-only input fetches no names. A missing definition or an absent names table gives a null name. Null IDs, empty strings and the literal strings `"None"` and `"null"` stay distinct, and the order and names are the same in both modes. IDs are matched on their raw values, without SQL type or case coercion. A single set-based SELECT filters the names, so there is no query per workflow and no parameter list that grows with the number of workflows.

This adds SQLite selection work over the execution IDs, and SQLite may scan catalog and ID metadata and build temporary structures. Only matching name values enter Python, but their length, SQLite's allocations and total evaluation memory are not limited. The tool makes no claim about timing or memory and publishes no benchmark.

#### `--max-workflows N`

`--max-workflows N` refuses a report whose `workflows` list would have more than N entries. In Python, use `compare(..., max_workflows=N)`.

- **Values.** Leaving it out, or passing `None` to the API, means no limit. N must be a non-negative integer, and the API rejects booleans, floats and strings. Zero allows an empty execution table, including one that has only orphan data.
- **What counts.** The count is the number of distinct original workflow IDs **that appear in executions on the same snapshot**. Null, empty string, `"None"` and `"null"` stay distinct. Workflows whose executions are all protected, and executions without data, count. Unused workflow definitions and orphan data do not.
- **Within the limit.** Full and summary reports, JSON and human-readable, are identical to the unlimited output and have no extra fields.
- **Over the limit.** In the example above, `--max-workflows 3` succeeds with unchanged output. `--max-workflows 2` exits 2, prints nothing on stdout, and writes only this diagnostic to stderr:

```text
flowwrench n8n-diet: observed workflow count 3 exceeds max_workflows 2
```

#### `--max-members N`

`--max-members N` caps the total number of **execution members in the report**, summed across the nine global transition cells. In Python, use `compare(..., max_members=N)`.

- **Values.** Leaving it out, or passing `None` to the API, means no limit. N must be a non-negative integer. The API rejects booleans, floats, strings and containers, even in summary mode.
- **What counts.** Full output has one member per execution on the snapshot, including protected executions and executions with missing data. Orphan data adds no members. Empty and orphan-only inputs accept zero. Summary-only output has no members, so it accepts `--max-members 0` whatever the number of executions, and all its aggregates stay complete.
- **Within the limit.** JSON and human-readable output are identical to the unlimited output, with no extra fields or notes.
- **Over the limit.** For the ten-execution example, `--max-members 10` succeeds with unchanged output. `--max-members 9` exits 2, prints nothing on stdout, and writes only this diagnostic to stderr:

```text
flowwrench n8n-diet: emitted execution member count 10 exceeds max_members 9
```

#### Combining limits and order of checks

You can combine the limits. For the same fixture, `--max-workflows 3 --max-members 10` and `--summary-only --max-workflows 3 --max-members 0` both succeed.

**What the limits do not cap.** `max_workflows` caps only the workflow entries in the report, and `max_members` caps only the execution members. Neither limits the memory used by matching workflow names, evaluation memory, SQLite allocations or serialized size. All rows and both policies' decision lists are still built in memory, so these are **not limits on evaluation memory**. Names, IDs and reason strings can be any length.

**When the count checks run.** Both count checks run before names are loaded and rollups are built, but **after both policies' timestamps are validated**. A malformed timestamp that either policy needs is reported ahead of an exceeded count. Rows that both policies protect still skip timestamp parsing. The source bytes are unchanged whether the command succeeds or refuses.

**Full order of checks.**

1. The CLI parses all arguments, including both limits, before loading the policy files. Invalid options are reported in argument order.
2. The API checks `summary_only`, then `max_workflows`, then `max_members`, then the as-of time and the baseline and candidate policy values, all before opening the source.
3. The stopped-backup, lock and journal/WAL/SHM safeguards and the schema validation run before any rows are loaded or classified.
4. The rows are classified for the baseline, then for the candidate.
5. The workflow-count check runs, then the member-count check. If both limits are exceeded, the workflow diagnostic is reported, in either mode.

Neither limit bypasses a refusal about the source, even with zero, summary-only or `--force`. An invalid N gets a generic diagnostic that does not echo the value you passed. A count refusal contains no execution or workflow identities, decision reasons or payload values.

#### Errors and report size

A malformed `stoppedAt` that **either** policy needs stops the whole comparison with exit 2 and no report on stdout. For example, if execution 1's stopped time is `invalid`, the comparison fails instead of treating that execution as old. As in `plan`, a row protected by its status or by `keep_failed` skips timestamp parsing for that policy. It must still parse if the other policy needs it. Null stopped times stay protected.

The comparison also fails explicitly on invalid JSON, duplicate or unknown keys, invalid policy types, cutoff overflow, and policy files over 64 KiB. `--force` reads committed WAL data through the same private-snapshot and locking safeguards as `plan`.

By default, reports include execution IDs but never payload contents. A full report grows with the number of executions plus nine cells per workflow that appears in executions. Unchanged members are included so the accounting is complete. Summary-only reports keep the nine global cells and nine cells per workflow, without member lists. In either mode, metadata memory also includes the matching workflow names.

### `reclaim-plan DB --out plan.json [--content] [--force] [--json]`

`reclaim-plan` saves a version-1 `n8n-exec-diet/reclaim-plan` JSON artifact used to **reclaim free space that already exists**. It is separate from the retention simulator `plan`, and neither command deletes rows. The source must pass an integrity check first.

The artifact contains:

- the resolved source path
- the SHA-256 and byte length of the whole main file
- the SHA-256 and length of a non-empty WAL, when `--force` admits one
- a schema SHA-256
- the row counts of all ordinary tables
- page and free-space statistics

The schema digest covers every table, index, trigger and view definition, plus `user_version`, `application_id` and the encoding. It leaves out physical root-page locations and `schema_version`, because VACUUM changes them. SHM bytes are coordination state and are not part of the fingerprint. Fingerprinting holds the same source locks as the checks that follow and reads the whole database and WAL, so it is not a cheap metadata check.

The artifact is published atomically with mode 0600. The command refuses existing paths, aliases of the source and source sidecars. Plans are limited to 1 MiB. Keep the artifact somewhere you trust: it is a consistency guard, not a signed authorization token.

`--content` saves a version-2 artifact with typed row digests for each table. Version 1 stays the default and supports structural checks only. Version 2 also contains `content: {"algorithm":"sha256-typed-rows-v1","tables":{...}}`, with one SHA-256 for every ordinary table, including auxiliary, internal and empty tables. Building it requires a full content scan. Payload values are read into memory as bytes, but they are never decoded as application data, evaluated or printed.

### `reclaim DB --out compact.sqlite [--plan plan.json] [--overwrite] [--json]`

**Binding to a saved plan.** With `--plan`, the command checks the following against the plan **before it creates a staging file**: the exact source path, the main database and committed WAL fingerprints, the schema digest and the row counts.

- **Unchanged source.** With the synthetic database and plan from above, an unchanged source gives `"plan_verified": true`, a valid copy and equal row counts.
- **Changed source.** If you rename a workflow, change a schema object or commit another WAL transaction after planning, the command exits 2 with `stale reclamation plan`. No new output is created, and an existing output stays intact even with `--overwrite`.
- **Moved or checkpointed database.** Moving the database or checkpointing it also requires a fresh plan.
- **No bypass.** `--force` and `--overwrite` never bypass this check, and the saved plan cannot itself be the output.
- **Without `--plan`.** Reclamation without a plan still works and reports `"plan_verified": false`.

**Building and publishing the copy.**

1. The command checks the integrity of the source.
2. It runs `VACUUM INTO` a private staging file in the output directory.
3. It checks the copy: `integrity_check`, the schema digest and **the row counts of all ordinary tables**.
4. With a plan that has content digests, the source's row digests must match before staging, and the copy's row digests must match before publication. The report then shows `"content_verified": true`; without content digests it shows false.
5. It fsyncs the file and publishes it atomically with mode 0600.

**Output paths.** An existing output needs `--overwrite`. The command refuses source paths, hard-link aliases, symlinks, source sidecar paths, locked output databases, and destinations that have SQLite sidecars.

**Failures.** A failure before publication removes the staging files and leaves an existing output untouched. If the link or rename succeeds but the directory fsync fails, the command exits 2 with a message that says `output published` and `directory durability unconfirmed` and names the visible output. That verified copy is kept, so do not simply retry with `--overwrite`.

**Disk space.** You need enough temporary and output space for a whole compact database.

This recovers existing free pages without deleting any execution rows. The copy can save more than the exact freelist bytes, because SQLite also repacks partly filled B-trees, and other datasets can pack differently. SQLite keeps the application tables, indexes, triggers and views. In tables without an explicit INTEGER PRIMARY KEY, implicit rowids can change during VACUUM. The tool never replaces or restores the source file automatically. Keep backups, and stop n8n before any later restore.

### `verify-copy COPY --plan plan.json [--content] [--json]`

`verify-copy` reopens a published output read-only, under source locks. It runs `integrity_check` and compares the schema digest and every table count with the saved plan. On success it exits 0 and reports `"verified": true` with the SHA-256 of the output's main file. Corruption or a schema or count mismatch exits 2. Use it after an ambiguous publication error, or after an interrupted command whose output exists. You do not need the original source, which may have changed since the copy was published. A non-empty WAL is refused: verify the standalone copy that `VACUUM INTO` produced.

When the plan has content digests, they are **always** checked, even without `--content`. The flag makes them mandatory: with a version-1 plan the command exits 2 with `plan has no content digests` and never quietly falls back to structural checks. For example, with fresh output paths:

```sh
flowwrench n8n-diet reclaim-plan /tmp/n8n-demo.sqlite \
  --content --row-hash-memory-bytes 64 --out /tmp/content-plan.json
flowwrench n8n-diet reclaim /tmp/n8n-demo.sqlite \
  --plan /tmp/content-plan.json --row-hash-memory-bytes 64 --out /tmp/content-copy.sqlite
flowwrench n8n-diet verify-copy /tmp/content-copy.sqlite \
  --plan /tmp/content-plan.json --content --row-hash-memory-bytes 64 --json
```

The 64-byte budget is deliberately tiny. It forces multiple sort runs on any table with more than two rows, including the synthetic fixture. Keep the default for normal use.

On success the command exits 0 and reports `verified: true`, `content_verified: true` and the matching `content` object. Content changes fail even when the counts and schema still match. These include:

- an edit to a BLOB or TEXT value
- a change to a declared key
- a change to a value's type
- a change in how many times a duplicate row occurs

In that case the command exits 2 with `copy content verification failed` and prints no report on stdout. The copy and the plan are not modified. With a version-1 plan, `content_verified` is false and `content` is null, because matching counts and schema alone do not prove the row contents are equal.

#### `--report-content-mismatches`

For a per-table diagnosis, add `--report-content-mismatches`. In the API, use `verify_copy(..., report_content_mismatches=True)`; the default is `False`. The flag needs a version-2 plan with content digests, even without `--content`. With a version-1 plan the command refuses before opening the copy. The API accepts only an exact boolean. It validates the value after the existing content-limit arguments and before any access to the plan or source.

```sh
flowwrench n8n-diet verify-copy /tmp/content-copy.sqlite \
  --plan /tmp/content-plan.json --report-content-mismatches --json
```

**Unchanged copy.** An unchanged copy gives **exactly the normal success report** and exits 0.

**Changed content.** This applies when integrity, schema and all ordinary-table counts match but the content differs.

- **API.** The API returns a dictionary with `verified: false`, `content_verified: false`, `output`, `integrity_check: "ok"`, `schema_equal: true`, `row_counts_equal: true`, `content_algorithm: "sha256-typed-rows-v1"` and `content_mismatches`.
- **`content_mismatches`.** This list is sorted by table name, and each entry contains exactly `table`, `expected_sha256` (from the trusted plan) and `actual_sha256` (from the copy). Every changed table is listed, and unchanged tables are not.
- **CLI with `--json`.** The command writes this report to stdout and **exits 2 with empty stderr**.
- **CLI without `--json`.** The human-readable output starts with `Copy content mismatch:` and shows the same escaped table and digest metadata. It never claims that the copy verified.

Check the exit code, not whether anything was printed on stdout.

**What the report reveals.** This mode never prints row payloads, row hashes or row identities. It reuses the single complete typed-row digest pass, under the existing locks. That pass covers forced spills, duplicate counts, binary and invalid-text values and generated-column values. There is no second fetch of payloads to explain a mismatch. Table names and digests are still **sensitive metadata**: a hash can reveal content that is easy to guess. An editable plan, even one you trust, gives you consistency, not authenticity.

**Other failures.** These failures still stop the command with **empty stdout**, exit 2 and a generic message on stderr:

- safety, lock, WAL or journal checks
- the plan
- integrity, schema or count checks
- content-policy limits

An interruption exits 130. The existing order of the count and byte policies still applies. That includes refusing in a later table after an earlier table has already changed: no partial mismatch list is printed. With the flag, low-level SQLite or OS errors become `copy verification failed; no report emitted`.

**Guarantees and limits.** The source, plan and copy are not touched, and spill files are closed on success, error or interruption. The flag adds no limit on memory, scanning, disk use or performance. It does not diagnose structural failures and does not locate changed rows. Without the flag, the existing reports, artifacts, digests, errors and exit codes are unchanged.

### Content digests

Digests cover every declared column in schema order, including explicit primary keys and generated values. They leave out implicit rowids and scan order, so they tolerate rowid renumbering during VACUUM and still account for how often a duplicate row occurs.

- **Types.** NULL, INTEGER, REAL, TEXT and BLOB values are kept distinct. Empty values and embedded NUL bytes are covered.
- **TEXT** is hashed as SQLite's UTF-8 bytes, without decoding into a Python string.
- **BLOB** is hashed as its exact bytes.
- **REAL** is hashed as its binary64 value, with signed zero normalized.

So that you can reproduce the digests, `sha256-typed-rows-v1` is defined as follows. Each row is encoded as:

1. the ASCII prefix `n8n-exec-diet/row/v1`, followed by a NUL byte
2. the column count, as an unsigned 64-bit big-endian integer
3. for each value: a one-byte tag (`n`, `i`, `r`, `t` or `b`), the value's byte length as an unsigned 64-bit big-endian integer, and the value bytes

Integers are encoded as signed 64-bit big-endian and reals as big-endian IEEE 754 binary64. NULL has zero bytes. A table digest hashes the prefix `n8n-exec-diet/table/v1` and a NUL byte, then the row count as an unsigned 64-bit big-endian integer, then the sorted 32-byte SHA-256 digests of the rows, **duplicates included**.

### Content limits and memory

Content mode scans every value and sorts the row hashes one table at a time. `reclaim-plan`, `reclaim` and `verify-copy` all take the options in this section. The policies apply only to the run where you set them: they are not saved in the plan, so pass them on every command. The examples below add each option to the three content commands shown above.

The behavior on refusal is the same for every option. A refusal exits 2, prints nothing on stdout and publishes nothing. On a refusal before publication, an error or an interruption, the source, plan and any existing output stay intact, and private spill and staging files are closed or removed. Diagnostics never include values. Setting a policy adds no content scans to structural-only operations, changes no artifact or report fields or digests, and works with any valid row-hash budget, including one that forces spilling.

#### `--row-hash-memory-bytes N`

This option (API: `row_hash_memory_bytes`) sets the memory budget for each sort run. The default is **8388608** (8 MiB). N must be an integer of at least 32. An invalid value fails before the source or plan is read, even in structural-only mode, where a valid budget is otherwise unused.

Each run holds at most `floor(N / 32)` raw 32-byte hashes. The budget does not include Python object, list and sort overhead, SQLite memory, the row's full values or the fixed merge state. It is **not a cap on process memory**, and large BLOBs are still read in full.

#### `--max-content-value-bytes N`

This option (API: `max_content_value_bytes=None`) refuses large individual values. Without it, full-row hashing is unlimited.

- **Values.** N must be an integer from 0 to 9223372036854775807, and the API rejects booleans. An invalid limit fails before the source or plan is accessed, even in structural-only mode.
- **What it counts.** The limit applies to TEXT and BLOB values; NULL, INTEGER and REAL values are exempt. Zero allows only empty TEXT and BLOB values. A value of exactly N bytes is accepted, and a longer value is refused.
- **How it checks.** Before a table's payload rows are selected into Python, the check runs SQLite `typeof` and `length(CAST(column AS BLOB))` on every declared column. It counts stored bytes, including embedded NUL bytes and invalid UTF-8 TEXT, rather than characters. TEXT is measured in the database encoding. Auxiliary and internal ordinary tables and generated columns are included. The source locks stay held through the check and the hashing.
- **Cost and limits.** The check can add one scan per column, and SQLite can allocate memory while it evaluates values. Rows that pass are still read in full. This is **a limit per value, not a cap on whole rows or process memory**: several allowed values can still make a large row, and schema metadata and SQLite memory are outside the limit.

For example, `--max-content-value-bytes 1048576` allows values up to 1 MiB. With a plan that has content digests, the check applies to the source and the staged copy in `reclaim`, and to `verify-copy` even without `--content`.

#### `--max-content-row-bytes N`

This option (API: `max_content_row_bytes=None`) limits the stored TEXT and BLOB bytes in each row. Without it, no row check runs.

- **Values.** The integer range and validation rules are the same as for `--max-content-value-bytes`.
- **What it counts.** SQLite adds up the stored TEXT and BLOB bytes across **every declared column of each row**, including auxiliary tables and virtual or stored generated columns. NULL, INTEGER and REAL values count as zero. Implicit rowids and record or encoding overhead are not counted. Invalid UTF-8, embedded NUL bytes and the database TEXT encoding follow the stored-byte rules above.
- **Boundaries.** A row that is empty or has only numeric values passes even with a limit of zero, and a row of exactly N bytes is allowed. Duplicate rows are checked separately and keep their count in the digest.
- **How it checks.** The check runs before the table's payload `SELECT *`, under the same locks. On refusal it returns only a constant to Python. It uses balanced integer addition for wide tables, so there is no conversion to floating point, and SQLite's hard limits on columns and value size rule out overflow. If you set both this and the per-value limit, the per-value check runs first for each table.

An oversized row is refused with this message:

```text
flowwrench n8n-diet: content row exceeds --max-content-row-bytes; no output published
```

For example, `--max-content-row-bytes 2097152` together with the 1 MiB value limit caps each row at 2 MiB. The check covers the same places as the per-value limit: the source and staged copy for plans with content digests, and `verify-copy` even without `--content`. The diagnostic for this limit contains no values, sizes or identities.

This adds a full SQLite check on each table. It is **not a limit on total scanning, SQLite allocations or process memory**. Expressions and generated columns may allocate memory inside SQLite before a refusal, rows that pass still enter Python in full, and metadata, Python object overhead and hash and spill state are not counted. The tool makes no claim about memory or performance and publishes no benchmark.

#### `--max-content-rows N`

This option (API: `max_content_rows=None`) limits the total number of rows admitted to **each content hashing pass**. Without it, no row-count check runs.

- **Values.** N must be an exact non-negative integer, and the API rejects booleans. There is no upper limit on N.
- **Boundaries.** Zero allows a database whose ordinary tables have no rows. Exactly N rows is allowed. More than N is refused before **any** table's payload `SELECT *` or byte check, with this message:

```text
flowwrench n8n-diet: content row count exceeds --max-content-rows; no output published
```

- **How it counts.** On the same locked snapshot, the check runs `COUNT(*)` on each ordinary table and adds the results with exact Python integers, stopping as soon as the total exceeds N.
- **What it counts.** Rows that are null or numeric only, duplicates, orphan data, auxiliary tables and internal ordinary tables such as `sqlite_sequence` all count. Empty tables count as zero. Views and indexes add no rows, and generated columns and implicit rowids are not counted as extra rows.

For example, add `--max-content-rows 100000`. The diagnostic for this limit contains no counts, payloads or identities.

`COUNT(*)` can itself make SQLite scan tables. This is **not a limit on scanned bytes, SQLite allocations or process memory**. An allowed row can still be arbitrarily large within SQLite's limits, and allowed payloads enter Python in full. Generated expressions, metadata, Python objects and hash and spill state are outside this count. Combine it with the byte limits and the row-hash budget as you need; none of them limits total process memory. The tool makes no claim about performance or memory and publishes no benchmark.

#### `--max-content-bytes N`

This option (API: `max_content_bytes=None`) caps the total stored TEXT and BLOB bytes across **each content hashing pass**. Without it, no total-byte check runs.

- **Values.** N must be an exact non-negative integer, and the API rejects booleans, floats, strings and containers. There is no upper limit on N. An invalid limit fails before the source or plan is accessed, even in structural-only mode.
- **Boundaries.** Zero allows empty TEXT and BLOB values and any number of rows that are null or numeric only. Exactly N bytes is allowed. More than N is refused with this message:

```text
flowwrench n8n-diet: content bytes exceed --max-content-bytes; no output published
```

- **What it counts.** The total covers every declared column and every row of every ordinary table. That includes auxiliary and internal tables (such as `sqlite_sequence`), orphan data, duplicates, and virtual or stored generated columns. NULL, INTEGER and REAL values count as zero, and views, indexes and implicit rowids add nothing.
- **How bytes are measured.** TEXT and BLOB lengths use `length(CAST(column AS BLOB))`, which includes embedded NUL bytes and invalid UTF-8. TEXT is measured in the database encoding, so **stored UTF-16 byte counts can differ from the UTF-8 bytes Python hashes**. The total is neither the physical file size nor the total size of the encoded digest input, which also has type and length tags and numeric encodings.

**How the check runs.** The check streams one integer row size per row into Python. It adds them with exact Python integers across all rows and tables and stops as soon as the total exceeds N. Each row's size is computed with balanced SQLite integer addition, within SQLite's hard limits on columns and value size. There is no SQLite `SUM` across rows and no conversion to REAL, so the total cannot overflow or be rounded.

**When it runs.** The whole-pass check finishes before **any** payload `SELECT *`, per-table value or row check, or hash spill. If you also set a row-count limit, that check runs first. Each pass uses the same locked snapshot as its hashing, and the existing checks on the plan, source, paths, locks, WAL/SHM/journal, schema and integrity keep their order. A SQLite or OS error during this check gives only `content byte preflight failed; no output published`. An interruption gives the usual generic exit-130 message.

For example, `--max-content-bytes 1073741824` allows at most 1 GiB of stored TEXT and BLOB bytes in each pass. Planning, the source and staged-copy passes in `reclaim`, and `verify-copy` each apply the budget separately, including for version-2 plans without `--content`. The diagnostics for this limit contain no values, counts or identities.

This adds a scan of every declared value in the ordinary tables. It is **not a limit on scanned bytes, SQLite allocations or process memory**. SQLite may read pages and allocate memory while it evaluates expressions or generated columns before a refusal, and allowed payloads still enter Python in full. Metadata, Python object overhead and hash and spill state are not counted. Combine it with the per-value, per-row, row-count and row-hash limits as you need; none of them limits total process memory. The tool makes no claim about scan speed or memory and publishes no benchmark.

#### Order of content checks

The API validates its arguments in this order, before any access to the source or plan:

1. the row-hash budget
2. the per-value byte limit
3. the per-row byte limit
4. the total row count
5. the total content bytes

The existing checks on the plan, source, locks, WAL/SHM/journal, schema, paths and integrity keep their order and run before any content pass. Within each pass, the checks run in this order:

1. the total row count
2. the optional total-byte check
3. for each table, the per-value byte check and then the per-row byte check, before the table's payloads are selected

This applies separately to content planning, to the source and staged copy in `reclaim`, and to verifying a plan that has content digests, even without `--content`. Structural-only operations validate N but add no checks.

#### Spill files

When a table has more hashes than one run can hold, the sorted hashes spill to two anonymous temporary files with mode 0600 in the standard temporary directory (`TMPDIR` on Linux). Each merge pass combines two runs and holds only two current hashes and two spill file descriptors, so there is no growing list of run paths. Tables that fit in one run use no spill files.

- **Disk space.** Spill files can use up to 64 bytes per row of the largest table. That is on top of any WAL snapshot and the space for the compact copy.
- **Speed.** A very small budget means more merge passes and more I/O. The tool makes no claim that spilling is faster.
- **Memory.** Row-hash memory grows with the configured budget, not with the number of rows in a table. Sorting still costs O(R log R) for a table with R rows.
- **Effect on output.** The budget is not recorded in the artifact and does not change any digest.

Spill files contain row hashes, never raw row values. They have no lasting path name and are closed on success, on a content refusal, on an I/O error or on interruption. On Linux their storage is also reclaimed if the process dies; removing them is not secure forensic erasure. The source locks stay held for the whole scan and merge. Content mode is optional and can still take a long time and need a lot of row memory and temporary space.

#### What the hashes prove

The hashes show that the content is consistent with a **trusted plan**, as far as SHA-256 is collision resistant. They do not authenticate a plan that someone could edit, and they do not hide values that are easy to guess. You can store the returned whole-file hash separately and use it for later byte-level comparisons.

Verification does not fsync the directory, prove the copy survives a crash, replace the source or restore n8n. Every output still needs the separate restore procedure, done from a stopped backup, described above.

### Locks and WAL

**Locking.** Every database command refuses immediately if a writer is active. The tool holds shared POSIX locks on the pending, reserved and shared region of SQLite's main file and, when present, on the lock bytes of the WAL-index SHM file. It opens the source through an immutable, read-only SQLite URI. It never checkpoints the source and never changes its journal mode. `trusted_schema` is off, and loadable extensions and user SQL are not enabled. Virtual tables, and any schema object that shadows `dbstat`, are rejected.

**Non-empty WAL.** A **non-empty WAL is refused by default**. `analyze`, `plan`, `compare`, `reclaim-plan` and `reclaim` accept `--force`, which reads committed WAL data from a private temporary copy of the database and WAL while keeping the locks on the original database and SHM file. `--force` never bypasses a lock, rollback journal, schema or output check. A WAL snapshot needs an existing regular SHM lock file and temporarily needs space for the database plus the WAL. The tests check that the source database, WAL and SHM hashes stay unchanged.

**WAL mode with no SHM file.** A database in WAL mode that has **no SHM** file is refused explicitly; some stopped or copied WAL databases are like this. The tool cannot keep a newly starting WAL writer out without creating a sidecar next to the source. Instead, use a separate offline SQLite backup converted to DELETE journal mode. Never change a live production source just to satisfy this tool.

**Rollback journals and symlinks.** A non-empty rollback journal must be recovered separately first. Symlinked sidecars are not supported.

These conservative failures keep the read-only promise instead of silently ignoring committed WAL data.

## Supported formats and limits

- **Schema detection.** The tool detects the schema from its structure; it does not guess the n8n version.
  - `execution_entity`, without a table prefix, needs `id` (a single primary key), `workflowId`, `status`, `startedAt` and `stoppedAt`.
  - `execution_data` needs `executionId` (a single primary key), `data` and `workflowData`.
  - The optional `workflow_entity` table must have an `id` primary key and a `name`.

  Extra ordinary tables and columns are kept. The hand-authored [SQL fixture](../tests/n8n_diet/fixtures/split-schema.sql) exercises this real interface.
- **Unsupported databases.** Older combined execution tables, custom table prefixes, SQLCipher, virtual tables, Postgres and MySQL are not supported and fail explicitly.
- **What it never does.** The tool never deletes data, cleans binary directories or external objects, calls the n8n API, discovers production instances, prompts for credentials or runs workflows.
- **Safe environment.** Use a local filesystem with normal SQLite and POSIX locking, a parent directory you trust, and one CLI operation per process. NFS, hostile concurrent path replacement, custom SQLite VFS or locking, and other SQLite connections in the same process are outside the safety model. Long reads hold locks that can block n8n writers for a while, so use a stopped backup.
- **Memory.** Analysis scans payload lengths and table counts. Policy simulation keeps metadata for every execution in memory so it can order timestamps. These commands do not load payload strings into Python. Optional content verification reads raw row values and sorts each table's hashes, as described above. The tool runs as a single process, with no resident service.
- **Exit codes.** 0 means success. 2 means bad input, an unsupported schema, a SQLite or safety error, or a diagnosed content mismatch. 130 means the command was interrupted.
- **Sensitive data.** Reports include workflow names and IDs. Payloads are left out, but still treat reports and database copies as potentially sensitive metadata.

## Development

From the repository root:

```sh
uv run --extra test pytest tests/n8n_diet
```

The tests cover:

- the CLI end to end on synthetic databases
- two-policy transition partitions, with workflow cells, groups and deltas that add up, and global count budgets
- names loaded only for observed workflows, null workflow IDs, empty and orphan-only input, and escaped human-readable rollups
- exact UTF-8 and BLOB accounting, malformed timestamps on either side, replay and tie ordering, and strict policy JSON
- saved-plan round trips, rejection of stale main files, schemas and WALs, and recovery from publication failures
- independent typed-digest test vectors, BLOB edits, duplicate counts, and invariance to rowid and row order
- bounded multi-run merges, private spill permissions, and spill cleanup on I/O errors and interruption
- short reads and writes, budget validation, refusals when content digests are required, and content checks before publication
- policy arithmetic and boundaries, and deterministic fixtures
- real WAL snapshots and writer conflicts from a separate process
- malformed schemas, output paths and atomic cleanup
- full row equality after compaction, and unchanged source hashes

All tests run offline, without n8n.

## License

Apache-2.0 as part of flowwrench; see [LICENSE](../LICENSE),
[NOTICE](../NOTICE), and [THIRD_PARTY.md](../THIRD_PARTY.md).
