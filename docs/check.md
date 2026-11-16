# flowwrench check

`flowwrench check` scans a folder of exported workflows and reports **what will
break before you import or deploy them**: n8n expressions that reference a node
that no longer exists, Node-RED wires to deleted nodes, Activepieces pieces that
are not pinned, Huginn agents nobody links to, and jobs on different platforms
that all start in the same minute or fall into a DST gap.

It is the front door to the other flowwrench commands. It does not reimplement
their analysis: each check calls the read-only Python API of one tool and turns
the result into one report. It never writes a file, never runs a workflow step
and never opens a network connection.

```sh
uvx flowwrench check .                      # or: pipx install flowwrench
flowwrench check exports/ flows.json        # several files or folders
flowwrench check . --format sarif > flowwrench.sarif
```

## Try it on the demo exports

The repository ships a small synthetic set of exports in
[`examples/check/`](../examples/check/): two n8n workflows, a Node-RED
`flows.json`, an Activepieces flow, a Windmill flow, a Huginn scenario and a
crontab. Every name, URL and connection in it is made up.

```sh
cd examples/check
flowwrench check --from 2026-11-16
```

`--from` pins the start of the schedule horizons so the output is the same on
any day. The run reports 7 files, 3 errors, 9 warnings and 7 info notices, among
them:

```text
n8n/order-sync.json  n8n workflow
  error  n8n-refs/R001            Post summary: references missing node 'Enrich'
  warn   schedules/herd           4 schedules start at once (Mon 00:00 UTC) across 4 files: Order sync / Every night

node-red/flows.json  Node-RED flows
  error  nodered/dangling-wire    Parse reading: wire to node 'debug-old' that does not exist
  error  nodered/missing-config   Publish: uses config node 'broker-home', which is not in this export

cron/crontab  crontab
  warn   schedules/dst-skipped    02:30 Europe/Berlin does not exist on 2027-03-28 (DST gap): the run is skipped  line 4
```

The herd is a cross-platform finding: an n8n schedule at 01:00 Europe/Berlin, a
Node-RED inject, a crontab line and a Huginn agent all fire at 00:00 UTC.
`tests/check/test_cli.py::test_readme_demo_output` pins this output.

## What is detected

Files are recognised by their **content**, not their name, so you can point
`check` at a whole repository. Only `*.json`, `*.yaml`/`*.yml` and crontab files
(`crontab`, `.crontab`, `*.cron`, `*.crontab`) are opened. Dot-directories,
`node_modules`, `venv`, `site-packages` and `__pycache__` are skipped, and
symlinked directories are not followed.

| Kind | Recognised by |
| --- | --- |
| n8n workflow | an object with a `nodes` array and a `connections` object |
| n8n workflow bundle | an array of such workflows (`n8n export:workflow --all`) |
| Node-RED flows | a flat array of nodes with string `id`/`type`, wires, tabs or subflows |
| Activepieces flow / template | a `trigger` of type `PIECE_TRIGGER`/`EMPTY` at the top level, under `version` or `template`, or in every entry of `flows` |
| Windmill flow | an OpenFlow object (`summary` + `value.modules`), YAML or JSON; `!inline` tags are tolerated |
| Windmill schedule | `*.schedule.yaml`, or JSON with `schedule` and `script_path` |
| Huginn scenario | `schema_version: 1` with an `agents` array, or a YAML kit directory (`scenario.yaml` + `agents/`) |
| crontab | by file name, as above |

Files that are not workflow exports (`package.json`, CI configuration,
Kubernetes manifests, ...) are skipped silently. A JSON file whose name looks
like an export (it contains `flow`, `workflow`, `n8n`, `scenario`, `template`,
`huginn`, `windmill` or `node-red`) but does not parse is reported as
`parse/invalid-json`, because a truncated or hand-edited export would otherwise
vanish from the report. Files you name explicitly are always reported if they
cannot be parsed or are not recognised.

## Checks

`flowwrench check --list-checks` prints this list. All checks run by default.

| Check | Uses | Reports |
| --- | --- | --- |
| `parse` | (built in) | Exports that cannot be read or parsed |
| `n8n-refs` | [`n8n-refs`](n8n-refs.md) | `R001` missing node, `R002` not upstream, `R003` not on every path, `R004` paired-item hazard, `R005` disabled node, `R006` duplicate name or dangling connection, `R007` not statically analysable |
| `n8n2ap` | [`n8n2ap`](n8n2ap.md) | Info only: how many nodes convert to Activepieces as-is, which are partial or unsupported |
| `nodered` | [`nodered-merge`](nodered-merge.md) | Wires to missing, config or cross-tab nodes; missing config, tab, group or subflow nodes; more wire ports than outputs |
| `ap-promote` | [`ap-promote`](ap-promote.md) | Connection references to remap (info), piece versions that are not pinned to `x.y.z`, leftover test sample data (info) |
| `windmill` | [`windmill-dryrun`](windmill-dryrun.md) | Flows outside the supported OpenFlow subset or structurally invalid (duplicate ids, bad transforms, unsupported module types) |
| `huginn` | [`huginn`](huginn.md) | Invalid scenarios (dangling links, bad agents), unknown schedules, link cycles, orphan agents, template variables still to render |
| `schedules` | [`schedules`](schedules.md) | Herds (N or more schedules starting in the same minute, across all files and platforms), DST-skipped and DST-doubled runs, schedules that cannot be forecast, schedules without a timezone |

Rule ids are `check/rule`, for example `n8n-refs/R001` or `schedules/herd`; the
JSON and SARIF output use the same ids.

### Severities

- **error**: the workflow will fail or misbehave as exported (a reference to a
  node that does not exist, a wire to a deleted node, an invalid scenario).
- **warning**: likely wrong or fragile (a reference that is empty on some
  branches, an unpinned piece version, a herd, a daily job in a DST gap).
- **info**: worth knowing, not a defect (connections to remap, migration
  readiness, an assumed timezone, an hourly job that skips one run a year).

### How the schedules check works

Schedules are extracted file by file with the `schedules` extractor (so one
malformed file only loses its own records) and then analysed together:

- **Herd**: the busiest UTC minute over the 7 days from `--from`. If it has at
  least `--herd-threshold` starts (default 3), each file with a schedule in that
  minute gets a warning naming them. `flowwrench schedules suggest` produces a
  read-only jitter plan.
- **DST**: every local fire time over 370 days that does not exist (spring
  forward) or happens twice (fall back). One finding per schedule and kind; a
  warning when the schedule fires once a day, info when it fires several times.
- Schedules without an exported timezone use `--default-timezone` (default
  `UTC`) and get one info notice per file. Interval schedules without a start
  phase (Node-RED `repeat`, Activepieces `every_x_minutes`) cannot be forecast
  and are listed as `schedules/not-forecast`.

## Output

### Text (default)

Findings are grouped by file, most severe first, and end with a one-line
summary:

```text
7 files · 3 errors · 9 warnings · 7 info
```

`-v` adds a fix hint and the JSON Pointer under each finding and lists the clean
files. Colour is used on a terminal unless `NO_COLOR` is set; force it with
`--color always|never`. Labels from your exports are printed with control
characters escaped.

### JSON (`--format json`)

```json
{
  "format": "flowwrench.check/v1",
  "tool": {"name": "flowwrench", "version": "0.1.0"},
  "paths": ["."],
  "checks": ["parse", "n8n-refs", "n8n2ap", "nodered", "ap-promote", "windmill", "huginn", "schedules"],
  "files": [{"path": "n8n/order-sync.json", "kind": "n8n", "platform": "n8n", "findings": 3}],
  "findings": [{
    "check": "n8n-refs", "rule": "R001", "ruleId": "n8n-refs/R001", "severity": "error",
    "message": "references missing node 'Enrich'", "file": "n8n/order-sync.json",
    "pointer": "/nodes/3/parameters/text", "line": null, "node": "Post summary",
    "hint": "rename the reference to an existing node, or use `flowwrench n8n-refs rename` next time"
  }],
  "summary": {"files": 7, "inspected": 7, "error": 3, "warning": 9, "info": 7},
  "failOn": "error",
  "failed": true
}
```

`file` is `null` only for run-level notices (for example a schedule analysis
limit). `pointer` is a JSON Pointer into the export's data model; `line` is set
for crontab findings.

### SARIF (`--format sarif`)

SARIF 2.1.0 with driver name `flowwrench`, one rule per `ruleId`, levels
`error`/`warning`/`note`, relative artifact URIs (`uriBaseId: %SRCROOT%`), the
JSON Pointer as a logical location and the crontab line as the region. Upload it
to GitHub code scanning:

```yaml
- run: uvx flowwrench check exports/ --format sarif > flowwrench.sarif || true
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: flowwrench.sarif
```

## Options

| Option | Meaning |
| --- | --- |
| `PATH ...` | Files and directories to scan (default `.`) |
| `--format text\|json\|sarif` | Report format (default `text`) |
| `--fail-on error\|warning\|never` | Exit 1 when a finding is at least this severe (default `error`) |
| `--only CHECK[,CHECK]` | Run only these checks (repeatable) |
| `--skip CHECK[,CHECK]` | Do not run these checks (repeatable) |
| `--list-checks` | List the checks with the tool each one uses, and exit |
| `--default-timezone IANA` | Timezone for schedules that export none (default `UTC`) |
| `--from YYYY-MM-DD` | UTC start of the schedule horizons (default today) |
| `--herd-threshold N` | Starts in one minute that count as a herd (default 3, minimum 2) |
| `--color auto\|always\|never` | Colour for text output |
| `-v`, `--verbose` | Hints, pointers and clean files in text output |

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | No finding at or above `--fail-on` (also when nothing was found) |
| 1 | At least one finding at or above `--fail-on` |
| 2 | Usage or input error: unknown option or check, missing path, unknown timezone |

## When a check crashes

Each check runs in isolation. If one raises an unexpected exception, the report
gets a `CHECK/internal` **error** for that file (the exception type only, never
its message, which could quote your data) and every other check still runs.
Set `FLOWWRENCH_CHECK_DEBUG=1` to get the traceback instead, and please open an
issue.

## Limits

- `check` sees exports, not running instances: it cannot know which nodes are
  installed, which credentials exist in the target, or what a server's timezone
  really is (use `--default-timezone`).
- The Windmill check validates the subset that `windmill-dryrun` simulates;
  a warning there can mean "valid in Windmill, but not dry-runnable".
- JSON files above 16 MiB and YAML files above 4 MiB are not inspected; each
  tool also applies its own size limits.
- To act on a finding, use the tool behind it: `n8n-refs rename`, `n8n2ap`,
  `nodered-merge`, `ap-promote plan/apply/scrub`, `huginn render`,
  `schedules suggest`, `windmill-dryrun run`.
