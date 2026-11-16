# flowwrench n8n2ap

`flowwrench n8n2ap` converts an exported **n8n workflow** into an **Activepieces flow template**, together with a migration report that says, node by node, what moved, what moved only partly and what you still have to rebuild by hand. Use it to find out what can be migrated before you start rebuilding a workflow.

The command was previously the standalone Node.js tool `n8n2ap`. It runs offline and uses only the Python standard library. It never executes workflow code, HTTP requests, schedules or any other workflow step, and it never contacts n8n or Activepieces. Upstream relationships and sources are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench          # or: pipx install flowwrench
flowwrench n8n2ap --version     # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench n8n2ap convert ./workflow.json --dry-run
```

`python -m flowwrench n8n2ap` is equivalent to `flowwrench n8n2ap`. The sample inputs below live in `examples/n8n2ap/` in the flowwrench repository; run the commands from the repository root (or adjust the paths). They are synthetic: their `.test` URLs are deliberately not live APIs.

## Convert

```text
flowwrench n8n2ap convert INPUT -o OUTPUT.json [--report REPORT.json|REPORT.md]
    [--trigger NAME_OR_ID] [--strict] [--dry-run] [--force]
    [--output-root DIRECTORY] [--connection-map FILE.json]
```

```sh
mkdir -p out
flowwrench n8n2ap convert examples/n8n2ap/webhook-http-if.n8n.json \
  -o out/flow.json --report out/report.md --strict
```

The webhook sample converts **4/4** nodes (100% coverage) into a schema-28 Activepieces template plus a Markdown report, and exits 0 even with `--strict`. The command prints a one-line summary (`Converted 4/4 nodes (100%); partial 0, unsupported 0.`) followed by the absolute flow and report paths.

```sh
# Machine-readable report; confine both output paths to a chosen directory.
flowwrench n8n2ap convert examples/n8n2ap/schedule-set.n8n.json \
  -o out/daily.json --report out/daily-report.json --output-root out

# See the gaps without writing anything: the JSON report goes to stdout.
flowwrench n8n2ap convert examples/n8n2ap/schedule-set.n8n.json --dry-run --strict
```

| Option | Meaning |
| --- | --- |
| `INPUT` | **One** n8n export object with `nodes`, `connections` and optionally `settings`. Export arrays and archives are rejected. Node names must be unique. |
| `-o`, `--output` | Activepieces template envelope with one entry in `flows`. Import it with Activepieces **Import flow**, then review the draft. Required unless `--dry-run`. |
| `--report` | `.json` or `.md`; the default is `OUTPUT.json.report.json`. |
| `--trigger` | Choose an enabled supported trigger by exact name or ID. Required when the workflow has more than one trigger; unselected nodes stay visible in the report. |
| `--strict` | Artifacts are still written, but exit **2** if any node is partial or unsupported, workflow settings need review, or connection placeholders remain unresolved. |
| `--dry-run` | Convert and validate, print the JSON report, write **nothing** (not even output directories). |
| `--force` | Replace existing regular output files. Inputs and symlinks are never replaced. |
| `--output-root` | Optional boundary: both destinations must be inside this directory. |
| `--connection-map` | Bind exact source credentials to named, unresolved placeholders (see below). |

The report lists every source node as `converted`, `partial` or `unsupported`, with its generated step IDs, whether it was emitted, the review issues, and the credentials you need to recreate (names and types only, never IDs). Coverage is fully converted nodes divided by all source nodes, including annotations, so a disconnected or unselected execution node lowers it. A report fragment:

```json
{
  "complete": false,
  "counts": {"converted": 0, "partial": 2, "unsupported": 1},
  "coveragePercent": 0,
  "credentials": [
    {"node": "CRM", "type": "exampleApi", "name": "Example CRM connection"}
  ]
}
```

Exit codes: **0** conversion succeeded (an honestly reported partial migration also exits 0 without `--strict`); **1** input, usage or I/O error; **2** `--strict` and the migration needs review or configuration. Errors are printed as one line prefixed with `flowwrench n8n2ap:`; they never echo input values.

Output files are created with mode `0600`. Existing outputs need `--force`. Both paths are checked first: overwriting the workflow or mapping input, hard links to either input and symlinked output paths are refused. Workflow fields never control file paths. Each forced replacement is atomic, but the flow/report pair is not a cross-file transaction.

## Mapping boundaries

| n8n node | Activepieces representation | Supported behavior |
| --- | --- | --- |
| `webhook` | `catch_webhook` trigger + CODE guard | Unauthenticated GET/POST; the original method is enforced and body/headers/query are normalized. Update callers to the new URL. |
| `scheduleTrigger`, `cron` | `cron_expression` trigger | One rule with an explicit workflow timezone; see [Schedules](#schedules) |
| `manualTrigger` | EMPTY trigger + failing guard | Partial: test entry only; configure a real trigger before publishing |
| `httpRequest` v3/v4 | HTTP `send_request` piece + response guard | HTTP(S), method, header/query pairs or JSON, JSON body, timeout; default redirect behavior; a single JSON object response is required |
| `if` v1/v2 | Type guard + ROUTER | AND/OR; text equality/inequality/contains/not-contains/starts/ends; numeric equal/greater/less; boolean true/false; true and fallback branches |
| `set` v1/v2/v3 | Generated CODE | Legacy typed values or modern manual assignments; unique simple field names; optional input preservation; runtime type checks |
| `noOp` | Pass-through CODE | Preserves the input JSON |
| `stickyNote` | Activepieces note | Text and geometry preserved; a disconnected annotation is allowed |
| `code` | Explicitly failing CODE | Partial: the original n8n code is omitted and must be ported by hand |
| Unknown, Merge or community node | Explicitly failing CODE | Unsupported; its parameters are not exported |

Supported node-version ceilings are explicit: webhook 2.1, scheduleTrigger 1.3, cron/manual/noOp/stickyNote 1, HTTP 4.4, IF 2.3, Set 3.4, Code 2. Newer versions require review, and HTTP nodes older than v3 are partial. Unsupported options, including explicit false-valued options, are reported.

Linear chains and structured IF branches are converted. A diamond join is emitted once after its router, **as a failing step**, because n8n's multi-input and repeated-execution semantics cannot be represented faithfully. Non-IF fan-out is blocked at its source and the omitted downstream nodes are reported. Unstructured graphs that would require duplicating nodes are rejected with an error. All connections, including disconnected and AI channels, take part in cycle detection. AI channels, retries, continue-on-error, disabled execution nodes and workflow error handlers require manual review.

### Expressions

Only simple property references inside n8n expression delimiters are translated:

| n8n input | Example output in the webhook sample |
| --- | --- |
| `={{ $json.amount }}` after HTTP/IF | `{{step_2.output.body.amount}}` |
| `={{ $('Webhook').item.json.body.id }}` | `{{webhook_method_guard.output.body.id}}` |
| `={{ $node["Webhook"].json.query.region }}` | `{{webhook_method_guard.output.query.region}}` |
| `=Order {{ $json.id }}` | Interpolated string using the current data source |

Downstream of an IF, `$json` still refers to the input item, not to a nonexistent router output. A named source must be guaranteed to have run on that path: sibling-branch, forward, unknown and disconnected references are flagged. Arithmetic, function calls, bracket paths, array indexing, `$env`, `$items`, escaped node names and arbitrary JavaScript require manual migration. Literal `{{…}}` strings are flagged as well, so the target never evaluates them by accident. An affected node becomes a failing placeholder, so unsupported logic never turns silently into a literal, an empty value or an unconditional branch. Nothing is evaluated: no `eval`, shell or subprocess is involved.

## Connection placeholders

`--connection-map FILE.json` keeps the structure of HTTP Basic or Bearer authentication without accepting any credential:

```sh
flowwrench n8n2ap convert examples/n8n2ap/http-basic.n8n.json \
  --connection-map examples/n8n2ap/http-basic.connections.json \
  -o out/bound-flow.json --report out/bound-report.md --strict
# Artifacts written, complete=false, flow valid=false, exit 2.
```

The mapping format is deliberately exact. No IDs, secrets or target account connections belong in this file:

```json
{
  "formatVersion": 1,
  "bindings": [
    {
      "source": {"node": "Fetch", "type": "httpBasicAuth", "name": "Example CRM"},
      "placeholder": "crm_basic"
    }
  ]
}
```

This matches an n8n HTTP Request v3/v4 with `authentication: "genericCredentialType"`, `genericAuthType: "httpBasicAuth"` and `credentials.httpBasicAuth.name: "Example CRM"`. For Bearer, use `httpBearerAuth` for both type fields. The generated HTTP `send_request` action uses `authType: "BASIC"` with `{{connection_crm_basic.output.username}}` and `{{connection_crm_basic.output.password}}` (Bearer uses `authType: "BEARER_TOKEN"` and a `token` field). A preceding CODE step named `connection_crm_basic` always throws an actionable configuration error: in the imported draft, replace it with a securely configured step that provides those output fields, keeping its name and position.

The converter does not retrieve secrets, create Activepieces connections or treat an alias as a resolved credential. Every binding stays `unresolved` in the report's `connectionPlaceholders`, and `complete` and flow validity stay false, so coverage may be 100% while connection setup still blocks execution.

Several HTTP nodes may share one placeholder only when each node has its own exact binding, the credential type and name match, and every reference carries the **same nonempty source credential ID**. IDs are compared locally and never written to the map, template, reports or error messages; equal names alone do not prove a shared identity. Conflicting IDs for one credential type/name are refused anywhere in a mapped export. With a mapping file, every credential reference in the export needs exactly one binding, including disconnected nodes; missing, stale, wrong, duplicate or conflicting bindings fail with exit 1 before anything is written. Aliases must match `[a-z][a-z0-9_]{0,63}`.

## Schedules

Schedule triggers are converted only when the result fires at exactly the same times:

- A known workflow timezone is required; a missing timezone blocks the trigger instead of guessing the instance timezone.
- Minute intervals must divide 60 and hour intervals must divide 24. Seconds, multiple rules and multi-day intervals are refused.
- Modern presets (minutes, hours, days, one-week cadence, months with intervals 1, 2, 3, 4, 6 or 12) follow the pinned n8n defaults, including clocks derived from the exported workflow and node IDs. The IDs are only used locally and never copied into the output.
- Modern presets also have an identity-derived **second**. If the IDs are missing or the second is not zero, the five-field target cron cannot preserve the phase: the trigger is partial and `--strict` exits 2.
- Custom five-field crons and zero-second six-field crons are supported, including the English weekday names `SUN`…`SAT` in the weekday field (`0 17 8 * * MON-FRI` becomes `17 8 * * 1-5`). A weekday field covering the whole week becomes `0-7` to keep n8n's day-of-month semantics.
- Legacy `cron` v1 presets (`everyMinute`, `everyHour`, `everyDay`, `everyWeek`) pick a random second at activation that the export does not contain, so they stay partial. Choose an explicit zero-second custom expression if that phase change is acceptable.

A blocked schedule becomes an EMPTY trigger with empty settings, an invalid flow and a throwing `migration_guard`, so it can never be published by accident.

## Safety and limits

This is a **single-JSON-payload** migration tool, not an n8n emulator. Item arrays, paired items, binary data, pagination, loops, multi-input Merge, custom webhook responses, webhook `params` and execution metadata, and real credential migration are out of scope. A generated guard rejects HTTP array or scalar responses.

Credential IDs and objects, `pinData`, `staticData`, raw Code sources and source workflow IDs are never copied. Heuristics flag secret-named fields, authentication headers, URL credentials or token query parameters, private keys and common token patterns (also inside JSON bodies). When something is detected, every parameter value of that node is omitted and its execution is blocked; a credentialed webhook becomes an EMPTY trigger with a failing guard. This is **not a proof that no secret is present**: review literals, names, URLs and both artifacts before sharing them. Reports contain node names, credential names/types and safe field paths, never expression or secret values.

Limits: 5 MiB input files, 500 nodes, 5,000 edges, 40 levels of JSON nesting and 200,000 values. Destinations must be trusted local directories: symlinks are refused, but the command is not a sandbox against another process replacing parent directories concurrently. Run it as an ordinary unprivileged user.

The target format is pinned to Activepieces commit `bf145a07489f5bc32a83e35e68564b861389aaed`, schema **28**, webhook piece **0.1.43**, HTTP **0.13.3** and schedule **0.1.22**; another installation may need matching pieces or adjustments. The output is checked offline against a strict JSON Schema of the emitted subset, written for this tool from the public non-enterprise Activepieces contracts. No live Activepieces import or workflow execution is performed: `complete: true` means "converted within these bounds", not behavioral equivalence or a deployment guarantee.

## Library API

```python
from flowwrench.n8n2ap import convert, report_markdown

result = convert(workflow, trigger="Webhook")   # connection_map=... is optional
print(result["report"]["coveragePercent"])
markdown = report_markdown(result["report"])     # write the template/report wherever you choose
```

The API is deterministic and does not mutate its input. Every source node appears in the report, and generated timestamps are fixed for reproducibility. The generated metadata keeps the `n8n2ap` tool name and version so outputs stay byte-compatible with the original tool.

## Tests

```sh
uv run --extra test pytest tests/n8n2ap
```

The suite runs offline: complete golden fixtures compared byte for byte, real CLI subprocesses, the expression allowlist, routing and fan-in boundaries, per-node mappings, generated Set/guard behavior, secret omission, strict exit codes, path protection and schema validation of valid and deliberately invalid outputs. The schedule suites replay captured outputs of the pinned n8n cron, default and recurrence helpers, so no upstream checkout or n8n installation is needed. The optional `jsonschema` test extra adds an independent validation of the template schema.

## License

Apache-2.0; see [LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md). n8n is source-available under its Sustainable Use License; the consulted Activepieces contracts are MIT. flowwrench contains no n8n implementation code and has no runtime dependency on either product.
