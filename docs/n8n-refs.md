# flowwrench n8n-refs

`flowwrench n8n-refs` checks the data references in exported **n8n workflows** offline, so you catch "node does not exist", "node is not upstream" and "node only runs on some branches" before you import or run the workflow. It can also rename a node and every literal reference to it in one safe step. Every finding has an RFC 6901 JSON Pointer, and reports come as text, JSON or SARIF 2.1.0 for local review and CI.

The command was previously the standalone tool `n8n-refcheck`. Runtime and unit tests use only the Python standard library. It never evaluates expressions, runs Code nodes, external commands or workflows, and never contacts the n8n API. Upstream relationships and sources are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench          # or: pipx install flowwrench
flowwrench n8n-refs --version   # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench n8n-refs check ./workflow.json
```

`python -m flowwrench n8n-refs` is equivalent to `flowwrench n8n-refs`. The sample inputs below live in `tests/n8n_refs/fixtures/` in the flowwrench repository; run the commands from the repository root (or adjust the paths).

## Check

```sh
flowwrench n8n-refs check tests/n8n_refs/fixtures/branch-ref.json --format text
flowwrench n8n-refs check tests/n8n_refs/fixtures/clean.json tests/n8n_refs/fixtures/branch-ref.json --format json
flowwrench n8n-refs check tests/n8n_refs/fixtures/*.json --format sarif > report.sarif
flowwrench n8n-refs check tests/n8n_refs/fixtures/branch-ref.json --fail-on warning
```

In the branch sample, `Enrich` only runs on the IF node's true branch, while `Notify` can also be reached from the other branch. The first command prints:

```text
tests/n8n_refs/fixtures/branch-ref.json:/nodes/5/parameters/text: R003 warning: referenced node is not executed on every structural path to consumer (reference 'Enrich', consumer 'Notify')
```

| Rule | Severity | Meaning |
| --- | --- | --- |
| R001 | error | The referenced node name does not exist |
| R002 | error | The node is not a strict upstream of the consumer (self-reference, downstream or unconnected node) |
| R003 | warning | The referenced node does not dominate the consumer: some structural path bypasses it |
| R004 | warning | `.item`, `.itemMatching()` or legacy `$node[...]` paired-item lookup crosses a known risky node |
| R005 | error | The reference points at a disabled node |
| R006 | error | Duplicate node names, or a connection source/target does not exist |
| R007 | error | Syntax that cannot be covered statically, a non-`main` connection, or an analysis resource limit; never treat it as a pass |

The input must be a **single exported workflow object** with a non-empty `nodes` array and a `connections` object. Bulk export arrays, execution history and API wrapper objects are not supported and exit 2. Nodes need at least `name` and `type`; `parameters` defaults to an empty object. Connections keep n8n's channel → output array → edge array shape, and each edge has `node`, `type` and a non-negative integer `index`.

Exit codes: **0** no finding at the threshold; **1** findings at the threshold; **2** argument, read, JSON/structure errors or a refused rename. The default is `--fail-on error`, so R003/R004 are shown but do not fail CI on their own. Every finding has an RFC 6901 JSON Pointer; `offset` (when present) is the zero-based character offset, in the *decoded* source string, of the opening quote of the node-name literal (or of the property identifier for dot access). It is not a byte offset into the JSON file.

JSON output contains `version`, `reports: [{file, findings}]` and `errors`. SARIF writes the JSON Pointer both to the logical location and to `properties.jsonPointer`, and identifies artifacts by file URI; on input errors `executionSuccessful=false`. JSON line numbers are never invented. When one of several inputs is broken, the remaining files are still checked and the final exit code is 2. Errors on stderr are prefixed `flowwrench n8n-refs: `.

The SARIF `tool.driver.name` is still `n8n-refcheck`, and the fingerprint namespace is still `n8n-refcheck/comparisonIdentity/v1`. Both are kept unchanged so that existing code-scanning alerts and fingerprints stay stable across the move to flowwrench.

### Fingerprints in `check` SARIF

Plain `check --format sarif` uses the same `n8n-refcheck/comparisonIdentity/v1` fingerprints as a complete `compare` of the same problem (including new/unchanged/absent and occurrence counts of repeated problems). To get them, a file must have no R007, all node names must be unique, and every ID present must be a unique non-empty string; nodes without an ID fall back to their exact name. Any duplicate name, duplicate or invalid ID, or R007 makes **all results of that file omit fingerprints**, without affecting other files in the batch. R006 for a dangling connection can still be fingerprinted. This eligibility check adds no diagnostics and does not change text/JSON output, `executionSuccessful` or the exit code: an invalid ID alone still does not fail `check`, and R007 still exits 1 as an error. `check` does not emit `baselineState` or comparison identity properties. Fingerprints are not globally unique across workflows.

Standalone `check` SARIF also explains each input's eligibility in `runs[0].properties.fingerprintEligibility`. For one good file and one unreadable file:

```json
{
  "namespace": "n8n-refcheck/comparisonIdentity/v1",
  "artifacts": [
    {"uri": "file:///work/good.json", "eligible": true, "reasonCodes": []},
    {"uri": "file:///work/missing.json", "eligible": false, "reasonCodes": ["load-error"]}
  ]
}
```

`artifacts` lists one entry per CLI file argument, in order, keeping duplicate paths; the URI equals the result artifact URI. `eligible=true` means v1 fingerprints can be generated, even if the workflow has no findings; `false` means all results of that input omit fingerprints. Eligibility is independent of gating and does not mean the workflow is error-free. `reasonCodes` is de-duplicated, contains every applicable reason in the order of this table, and never contains node values or source:

| reasonCode | Contract |
| --- | --- |
| `duplicate-node-name` | Two nodes have exactly the same name; shares the identity check with `compare`. |
| `invalid-node-id` | An ID is present but is not a string, or is empty/all whitespace; a missing ID falls back to the name. |
| `duplicate-node-id` | Two valid non-empty string IDs are identical; whitespace is not trimmed when comparing. |
| `incomplete-analysis` | The file has R007; reported even when identity errors are also present. |
| `load-error` | Reading, JSON/structure validation or analysis failed, so there is no usable report. Reported alone: `eligible=false` means eligibility could not be established, not that identity is wrong or R007 is present. |

The array is empty when there is no reason. Invalid IDs are not counted as duplicate valid IDs. Inputs that fail to load still get metadata and their original error notification, but no findings. The `check` CLI uses the `artifacts` form; the `compare` CLI uses the `sides` form described below. Low-level `sarif(reports, errors)` / `comparison_sarif(...)` rendering without workflow identity context keeps its original contract. None of this changes the v1 hash input, result content, text/JSON or exit codes 0/1/2.

## Compare a baseline and a candidate workflow

```sh
flowwrench n8n-refs compare tests/n8n_refs/fixtures/comparison/baseline.json tests/n8n_refs/fixtures/comparison/candidate.json
flowwrench n8n-refs compare tests/n8n_refs/fixtures/comparison/baseline.json tests/n8n_refs/fixtures/comparison/candidate.json \
  --format json --fail-on-new
flowwrench n8n-refs compare baseline.json candidate.json --fail-on-new --fail-on warning
flowwrench n8n-refs compare tests/n8n_refs/fixtures/comparison/baseline.json tests/n8n_refs/fixtures/comparison/candidate.json \
  --format sarif --fail-on-new > comparison.sarif
```

Both inputs are raw exported single-workflow objects; no prior check report is needed. In the sample, the consumer keeps `id=consumer`, is renamed from `Use` to `Renamed Use` and moved to the front of the array; the old reference `MissingOld` is replaced by `MissingNew`. The expected result is **1 new R001, 1 resolved R001, 1 unchanged R005**, and the consumer rename is reported. The first command only reports and exits 0; the second exits 1 because of the new error. Existing or resolved errors do not fail `--fail-on-new`; by default only new errors gate, and `--fail-on warning` also includes new warnings.

A comparison identity consists of the rule, location kind, consumer/connection-source identity, relative JSON Pointer, reference target identity and occurrence count of the same kind. Nodes are identified by their unique non-empty string `id`; without an `id`, only the exact node name is used. Node array order, reference character offsets, quoting, and the names of nodes that have IDs are not part of the identity, so safe renames and unrelated text edits do not create new problems. Adding one more problem of the same kind in the same field still produces one `new`. Diagnostics keep the original JSON Pointers of both sides so each can be located.

A rename is reported only when a node has the same unique ID in both inputs and its name changed; nothing is guessed from position, type or similar content. Reference targets are also matched by ID, so a stale old name left after a rename becomes a new R001. Renames without IDs, and adding/removing/changing an ID, are treated as delete plus add. Exported IDs are assumed to denote the same logical node; deliberately reused IDs cannot be detected. Duplicate names, duplicate IDs or present-but-invalid IDs refuse the comparison with exit 2.

JSON contains the `baseline` and `candidate` file names, `comparison: {complete, new, resolved, unchanged, renames}` and `errors`. Each change has `identity`, `baseline` and `candidate`, with `null` for the missing side. Inside the identity, `node`/`reference` are `{kind: id|name|missing, value}`; `occurrence` starts at 1. text/json/sarif are supported with the same gating semantics. Read/structure errors exit 2 with `comparison: null`. If either side has R007, `complete=false`: known results are still shown, but the command always exits 2 and cannot serve as proof of a passing gate or a full resolution.

In SARIF 2.1.0, new → `baselineState: "new"`, unchanged → `"unchanged"`, resolved → `"absent"`. The primary `locations` of new/unchanged results point at the candidate file, absent results at the baseline file; unchanged results also keep the baseline location in `relatedLocations[0]`. Files are absolute, correctly URI-escaped `file:` URIs. The primary location's JSON Pointer, node name, reference name and source offset are in the result `properties`; the related baseline location stores the same fields in its own `properties`. Both Pointers are written to their own logical locations. `offset` is a character position in the decoded source, **not a JSON line, column or byte position**; no `region` or invented `startLine` is emitted, even for minified JSON, multi-line source or Unicode escapes.

`runs[0].properties.comparison` contains the `baseline`/`candidate` URIs, `complete` and `renames`; each result also keeps `properties.identity` and the primary location's `comparisonSide`. The example above produces three results (new R001, absent R001, unchanged R005) and exits 1. Input/identity errors produce valid SARIF with empty results, error notifications and `executionSuccessful=false`, and exit 2. An R007-incomplete comparison still emits the known diagnostics with their sources, but **all results omit `baselineState`**; `complete=false`, `executionSuccessful=false`, exit 2. This follows the complete-comparison requirement of [SARIF 2.1.0 §3.27.24](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html) and never reports a missed scan as resolved.

What is compared is the rule category and count at each location; changes in the list of intermediate risky nodes in R004 are not new problems on their own, and wording changes of diagnostics with the same rule do not count. Array indexes inside parameters and connection port/edge array indexes are still part of the path, so reordering those arrays can show up as delete plus add. Runtime semantics are not evaluated, and no input file is modified.

The `compare` CLI also adds overall and per-side eligibility in `runs[0].properties.fingerprintEligibility`, without modifying the existing `comparison` property. For example, with a good baseline and a candidate containing R007:

```json
{
  "namespace": "n8n-refcheck/comparisonIdentity/v1",
  "eligible": false,
  "sides": {
    "baseline": {"uri": "file:///work/before.json", "eligible": true, "reasonCodes": []},
    "candidate": {"uri": "file:///work/after.json", "eligible": false, "reasonCodes": ["incomplete-analysis"]}
  }
}
```

Top-level `eligible=true` requires that the comparison was formed and is complete; when `false`, all results omit fingerprints and `baselineState`. `sides.baseline` / `sides.candidate` describe each file's own eligibility, with the same five reason codes and ordering as the table above; a good side never gets a spurious reason because the other side failed. Both side URIs match the existing comparison URIs, and the same path keeps both roles. A complete workflow without findings can be eligible; missing IDs fall back to exact names, and a dangling-edge R006 alone does not affect eligibility.

Even when the comparison cannot be formed because of identity errors, or a read/JSON/structure error on the other side, the loadable side is still checked independently and all its identity problems and R007 are reported (they can co-occur). A side that cannot be loaded or analyzed reports only `load-error`, meaning eligibility could not be established; unseen identity errors are not inferred. Reason codes carry no node values or source. This evaluation adds no diagnostic notifications and does not change the first message of existing identity errors, findings, locations, renames, v1 hash bytes, text/JSON or the 0/1/2 gate. Low-level `comparison_sarif` calls without eligibility context do not add the property.

Every SARIF result of a complete comparison (new, unchanged, absent) has `partialFingerprints: {"n8n-refcheck/comparisonIdentity/v1": "<64 lowercase hex SHA-256>"}`, using [SARIF's versioned namespaces](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html). The v1 input is the full `properties.identity` object (`rule`, `scope`, `node`, `path`, `reference`, `occurrence`), serialized as Python `json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"))` (recursively sorted keys, non-ASCII escaped, no whitespace), then SHA-256 over the UTF-8 bytes, with no prefix or newline. Incompatible changes to the identity or encoding will use a new namespace version.

File names/URIs, comparison direction and state, node array indexes, messages, source offsets, quoting and the display names of nodes with IDs do not enter the hash. The value is the same whether an identity appears only in the baseline, only in the candidate, or in both; repeated problems in the same field differ by `occurrence`. Nodes without IDs are still distinguished by exact name. Fingerprints inherit the comparison identity's limits: parameter array/connection edge indexes are part of the identity, deliberately reused IDs are not detected, and they are not globally unique across workflows. All results of an R007-incomplete comparison omit both `partialFingerprints` and `baselineState`.

## SARIF CI stream identity (`check` / `compare`)

```sh
flowwrench n8n-refs check tests/n8n_refs/fixtures/branch-ref.json --format sarif \
  --sarif-automation-id 'CI/references/check/'
flowwrench n8n-refs compare tests/n8n_refs/fixtures/comparison/baseline.json tests/n8n_refs/fixtures/comparison/candidate.json \
  --format sarif --sarif-automation-id 'CI/references/compare/' --fail-on-new
```

The two commands exit 0 and 1; their SARIF `runs[0].automationDetails` are `{"id":"CI/references/check/"}` and `{"id":"CI/references/compare/"}`. Later runs of the same CI stream should reuse the same stable ID; independent check/compare streams should use explicitly different IDs, even when their findings are identical.

`--sarif-automation-id ID` applies only to `check`/`compare` and requires `--format sarif`. The ID is **1-128 ASCII characters** fully matching `[A-Za-z0-9][A-Za-z0-9._/-]*`; it is case-sensitive and must start with a letter or digit. Slashes can express the caller's hierarchy and a trailing slash is optional; the value is an identity string and is never resolved or normalized as a path. Empty values, whitespace, Unicode, control characters and any other characters are rejected; nothing is trimmed, escaped into another identity, guessed or generated. A missing or invalid value, or using the option with default text, explicit text or JSON, is an argument error that exits **2** before any input is read, writing only to stderr with empty stdout (no SARIF error document). `rename` does not accept this option.

A valid ID is preserved verbatim in complete, R007-incomplete, identity-refused, read/JSON/structure-failure and mixed multi-file reports. Only `runs[0].automationDetails.id` is added; v1 fingerprint bytes, eligibility reasons, findings, source locations, comparison states, notifications, `executionSuccessful` and the 0/1/2 gate are unchanged. Without the option, `automationDetails` is omitted entirely and the report bytes stay the same; no identity is inferred from workflow content, file names or paths. How results are grouped depends on the downstream SARIF consumer; this command only emits the identity field.

## SARIF source-root mapping (`check` / `compare`)

```sh
flowwrench n8n-refs check tests/n8n_refs/fixtures/branch-ref.json --format sarif \
  --sarif-source-root . --sarif-automation-id 'CI/references/check/'
flowwrench n8n-refs compare tests/n8n_refs/fixtures/comparison/baseline.json tests/n8n_refs/fixtures/comparison/candidate.json \
  --format sarif --sarif-source-root . --fail-on-new
```

The two commands exit 0 and 1. `--sarif-source-root DIR` is only valid with an explicit `--format sarif` on `check` / `compare`; DIR must be non-empty and an existing directory (a symlink to a directory is fine). Empty or missing values, nonexistent/non-directory/broken/looping symlinks, NUL in the path, and use with text/JSON/default format or `rename` are argument errors: the command reports on stderr before reading any workflow, prints nothing to stdout, exits 2 and produces no SARIF.

The root and every input path are made absolute relative to **the current working directory at invocation** and lexically normalized for `.` / `..`; `~` is not expanded, symlinks are not resolved, and case follows the host's path rules. A root alias and the real directory are two separate path namespaces; a symlink inside the root pointing outside is still reported by its in-root path. The option describes the caller's checkout path; it does not prove physical containment and is not a security sandbox. Inputs need not exist: eligibility locations for missing or failed inputs use the same rules.

With this option the run always declares `originalUriBaseIds.SRCROOT: {"uri": "file:///absolute/checkout/"}` (an absolute file URI that keeps the trailing `/`). Artifacts inside the root use POSIX relative paths, e.g. `{"uri":"nested/%E9%9B%AA%20%23%25.json","uriBaseId":"SRCROOT"}`; the root itself is `./`. Spaces, Unicode, `#`, `%`, colons and so on are percent-encoded. Files outside the root (including sibling directories with the same prefix) use normalized absolute file URIs without `uriBaseId`. Consumers can map the same relative path onto another CI checkout; only the base URI moves with the checkout.

POSIX file names are percent-encoded from their raw filesystem bytes: the `os.fsdecode` / `os.fsencode` surrogateescape round trip keeps non-UTF-8 bytes. For example, a real `flow-\xff.json` (byte `FF`) becomes `flow-%FF.json`, UTF-8 `flow-ÿ.json` stays `flow-%C3%BF.json`, and a literal `%FF` becomes `%25FF`. The decoded URI bytes equal the raw bytes of the lexical relative path; the root and out-of-root file URIs keep their bytes too. Missing files and symlinks (including broken ones) keep the caller's lexical path instead of the target. Notification messages keep the original path, JSON-escaped to ASCII. Arbitrary file-name bytes are not promised on non-POSIX platforms; the real non-UTF-8 file-name tests are skipped there while ordinary Unicode tests still run. Default output is unchanged.

The mapping covers each result's `physicalLocation.artifactLocation`, the baseline `relatedLocations` of unchanged results, `check`'s `fingerprintEligibility.artifacts`, `compare`'s `fingerprintEligibility.sides`, and the `locations` of file-failure notifications, in complete, incomplete and load/JSON/structure-failure reports. Eligibility objects keep `eligible` / `reasonCodes`; only their URI changes and `uriBaseId` is added when applicable. **Only when this option is given**, `properties.comparison.baseline` / `candidate` change from the historical URI strings to artifact-location objects (out-of-root ones are `{"uri":"file:///..."}`) so each side can carry its base ID. Without the option, absolute URIs are used, `comparison` keeps strings and no base mapping is added; file-failure notifications still carry the artifact locations described next.

The automation ID, v1 fingerprints/identities, eligibility decisions/reasons, findings, comparison states, notification text and exit codes 0/1/2 are unchanged. Notifications keep the caller's original file names, so the text of failure reports can still contain the original checkout path: this is not a privacy feature that strips all absolute paths. How base IDs are used is up to the consumer; upload to a hosted SARIF service has not been verified.

## SARIF file-failure notification locations (`check` / `compare`)

Missing, unreadable, invalid-JSON or structurally invalid CLI inputs keep their notification in `toolExecutionNotifications` and gain an artifact-only `locations`:

```json
{
  "level": "error",
  "message": {"text": "candidate flows/bad.json: invalid UTF-8 JSON or excessive nesting"},
  "locations": [{"physicalLocation": {"artifactLocation": {"uri": "flows/bad.json", "uriBaseId": "SRCROOT"}}}]
}
```

The example assumes an explicit source root containing the input; without a source root, and for out-of-root paths, absolute file URIs are used. Locations reuse the lexical path, URI escaping and symlink rules of the previous section; missing files need not exist and symlink targets never replace the location. No line/column, JSON Pointer, source snippet, raw exception or credential is guessed or attached.

`check` keeps notifications in the order of the failing arguments, repeating duplicate failing paths; good files get no notification. `compare` reports file errors in baseline, candidate order; even when both sides use the same path, both role notifications are kept and each points at its input path. The role stays in the existing message prefix. **Errors without a file path**, such as identity refusal and R007-incomplete comparisons, never borrow a side's location and get no `locations`.

Only failure notifications that carry an original CLI file path gain this field; notification text bytes, level, count/order, `executionSuccessful`, the 0/1/2 gate, text/JSON output, findings, identities, fingerprints and eligibility are unchanged. Complete SARIF reports without file errors keep their bytes. Messages still keep the caller's original path; the location is not path anonymization. Error text for OS failures depends on the host platform.

## Workflow load-failure categories (`check` / `compare`)

Real load failures in JSON `errors` carry a stable `category`; the matching SARIF file notification gets `properties.category`. For example:

```json
{"side": "candidate", "file": "flows/bad.json", "message": "invalid UTF-8 JSON or excessive nesting", "category": "invalid-json"}
```

| category | Origin |
|---|---|
| `file-not-found` | FileNotFoundError while reading bytes, including broken symlinks |
| `permission-denied` | PermissionError while reading bytes |
| `file-read` | Any other read OSError, including a directory input |
| `invalid-utf8` | UTF-8-sig decoding failed |
| `invalid-json` | JSON syntax, duplicate object keys, non-finite numbers |
| `invalid-workflow` | Workflow structure or field validation failed, including zero nodes |
| `input-limit` | 16 MiB, 2000 nodes, 20000 edges, 100 nesting levels, or a JSON parser RecursionError |

Only the first failure is reported, and the category is never inferred from message text. The original order is kept: read → byte size → UTF-8 decode → JSON parse (including hook/recursion failures) → structure validation. Structure validation checks the top-level shape first, then node count, overall depth, node fields, pinData and connection traversal; exceeding the edge limit is reported before that edge's fields are validated. An empty node list is a structure error and too many nodes is a limit error; both keep the same original message but differ in category. Whether the parser hits the recursion limit first depends on Python and the platform; no extra pre-scan changes the original first-error behavior.

Only file failures classified at their origin carry the field. Comparison identity refusals and R007-incomplete comparisons remain file-less errors with no `category` or notification `properties`; R007 analysis resource limits remain findings. Successful files, low-level unclassified errors and general analysis exceptions never get an inferred load category. Categories contain no source, node values or raw exceptions. Original message bytes, file/side, notification order/locations, text, findings, identities/fingerprints, eligibility and the 0/1/2 gate keep their contract. JSON only adds `category`, SARIF only adds `properties.category`.

## Safe rename

```sh
flowwrench n8n-refs rename tests/n8n_refs/fixtures/rename.json \
  --from 'Fetch / A' --to 'Customer' -o renamed.json
flowwrench n8n-refs check renamed.json --fail-on warning
flowwrench n8n-refs rename renamed.json \
  --from 'Customer' --to 'Fetch / A' --in-place
```

Rename rewrites the node `name`, every connection source key and target `node`, top-level `pinData` keys, and the **node-name literals** of references in executable parameters and Code source. Comments, ordinary string content, fixed parameter values, pinData contents, credential metadata and node IDs are never changed. The library API never modifies the source file; CLI output is JSON with two-space indentation. Rewritten literals are normalized to double quotes, keeping escapes and Unicode semantically equal.

JavaScript static dot access `$node.Fetch.json` is also supported, for node names that are unescaped ASCII identifiers `[A-Za-z_$][A-Za-z0-9_$]*`. A rename always converts it to quoted bracket form, so the new name may contain spaces, slashes, quotes and Unicode; e.g. `Fetch` → `Customer / A` turns `$node.Fetch.json` into `$node["Customer / A"].json`. Comments and whitespace around the dot are kept: `$node /* lookup */ . /* name */ Fetch` becomes `$node /* lookup */ [ /* name */ "Customer / A"]`.

```sh
flowwrench n8n-refs rename tests/n8n_refs/fixtures/rename-dot.json \
  --from Fetch --to 'Customer / A' -o renamed-dot.json
flowwrench n8n-refs check renamed-dot.json --fail-on warning
```

This synthetic sample contains Code source, parameter expressions, comments, ordinary strings and pinData. Only real accesses, the node name, connections and pinData keys are rewritten, and both commands are expected to exit 0. `$node[name]`, `$node['Fetch' + suffix]`, escaped or non-ASCII dot names, and Python dot access still report R007; rename then exits 2, leaves the input unchanged and creates no output file. Non-ASCII names can use quoted bracket access.

JavaScript static optional-chaining bracket access `$node?.["A"]` is supported with the same single-quote, static backtick and name-escape rules. `?.` must be contiguous, with optional whitespace and comments around it; e.g. renaming `A` to `Customer / 雪` turns only `'A'` in `$node /* lookup */ ?. [ /* key */ 'A' /* end */ ]?.json` into `"Customer / 雪"` and keeps every other source character. The diagnostic offset still points at the opening quote of the original literal; optional chaining does not exempt R001-R005 or the R004 paired-item risk of legacy `$node`.

```sh
flowwrench n8n-refs rename tests/n8n_refs/fixtures/rename-optional.json \
  --from A --to 'Customer / 雪' -o renamed-optional.json
flowwrench n8n-refs check renamed-optional.json --fail-on warning
```

Both commands are expected to exit 0. `$node?.[name]`, `$node?.["A" + suffix]`, dynamic template keys with `${...}` and directly computed selector properties still report R007; rename exits 2, keeps the input bytes and creates no output. Python optional chaining is not supported and reports R007.

JavaScript static optional dot access `$node?.A.json` is supported too, again for unescaped ASCII identifiers. `A` → `Customer / 雪` turns it into `$node?.["Customer / 雪"].json`; `$node /* lookup */ ?. /* key */ A.json` becomes `$node /* lookup */ ?. /* key */ ["Customer / 雪"].json`. Only the identifier is replaced by a quoted bracket key; the contiguous `?.` and all comments and whitespace are kept, and the diagnostic offset points at the start of the original identifier. This applies to Code source and parameter expression blocks; ordinary strings, comments, `obj.$node?.A` on other objects and static text outside blocks are not rewritten, and R001-R005 and paired-item risks still apply.

```sh
flowwrench n8n-refs rename tests/n8n_refs/fixtures/rename-optional-dot.json \
  --from A --to 'Customer / 雪' -o renamed-optional-dot.json
flowwrench n8n-refs check renamed-optional-dot.json --fail-on warning
```

Both commands are expected to exit 0. Escaped identifiers `$node?.A` / `$node?.AB`, non-ASCII identifiers `$node?.雪`, split `$node? .A`, dynamic keys `$node?.[name]` / `$node?.A?.[property]` and Python `$node?.A` / `_node?.A` all report R007; rename exits 2, keeps the input bytes and creates no output file (including refusing `--in-place`). This support is offline source analysis and does not verify that a particular n8n runtime accepts the syntax.

JavaScript static optional calls `$?.("A")` and `$items?.("A", 0, 0)` are supported in Code source and parameter expression blocks. Node names follow the existing single-quote, double-quote, static backtick and escape rules. For `A` → `Customer / 雪`, `$ /* lookup */ ?. ( /* name */ 'A' /* end */ ).item.json` only changes `'A'` into `"Customer / 雪"`, and `$items?.("A", 0, 0)` becomes `$items?.("Customer / 雪", 0, 0)`. `?.` must be contiguous, with optional comments and whitespace around it; call punctuation, later arguments and all trivia are kept character for character, and the diagnostic offset points at the original opening quote. Optional calls do not exempt R001-R005; `.item` / `.itemMatching()` still carry paired-item risk, while `.first()` and `$items` are not paired lookups. Ordinary strings, comments, `obj.$?.(...)` / `obj?.$items?.(...)` and text outside expression blocks are unchanged.

```sh
flowwrench n8n-refs rename tests/n8n_refs/fixtures/rename-optional-call.json \
  --from A --to 'Customer / 雪' -o renamed-optional-call.json
flowwrench n8n-refs check renamed-optional-call.json --fail-on warning
```

Both commands are expected to exit 0. Dynamic names `$?.(name)`, concatenated names `$items?.("A" + suffix, 0, 0)`, interpolated templates ``$?.(`A${suffix}`)``, split forms `$? .("A")` / `$items? /* gap */ .("A")` / `$?.("A")? /* gap */ .item`, directly computed selector properties `$?.("A")?.[property]` and Python `$?.("A")` / `_?.("A")` / `$items?.("A")` / `_items?.("A")` all report R007; rename exits 2, keeps the input bytes, and neither creates the `-o` file nor writes `--in-place`. Whether n8n's runtime provides these optional calls is not verified.

By default the new JSON goes to stdout only; `--dry-run` prints JSON and never writes files, even combined with `--in-place` or `-o`. Stdout contains the full workflow with its original metadata. No step is ever executed.

An existing target node, the same name, a missing source node, pinData key collisions, R006 and R007 are all refused before writing. `-o` requires a path that does not exist yet and never overwrites an existing file or symlink; its directory must already exist. `--in-place` only accepts regular files, replaces atomically via a temporary file in the same directory, keeps permissions, and checks whether the file changed while being read; it is not a lock against concurrent editing. New output files get mode 0600. Temporary files are cleaned up. Node names are never used to build disk paths. Shell redirection onto the source (such as `> input.json`) truncates the file before the program starts; use `--in-place` instead.

## Scanner and graph model limits

- Strings in `nodes[*].parameters` that start with `=` are scanned recursively, but only inside `{{ ... }}` expression blocks; text outside blocks, and strings without blocks, are static text. Common forms are `={{ $('A').item.json }}` and `=Hello {{ $node["A"].json.name }}`.
- The official Code node's `jsCode` / `pythonCode` are scanned in full, as is the legacy Function/Function Item `functionCode`. Recognized forms are `$()`, `$node["..."]` and `$items("...", ...)`; JavaScript also supports the static optional calls `$?.("...")` / `$items?.("...", ...)` above, static `$node.Name` dot access and `$node?.Name` / `$node?.["..."]` static optional chaining. `$node` access also counts toward R004 paired-item risk. Python source additionally recognizes the legacy proxies `_()`, `_node[...]` and `_items(...)`; this does not guarantee those proxies work on every n8n/Python runtime version.
- The lexical scanner skips JS `//` / `/* */` and Python `#` comments and quoted strings, including Python triple-quoted text. It supports quote/backslash/common Unicode escapes, JS static backticks and real references inside template `${...}`; reference-like samples inside strings are never rewritten. Direct, grouped and optional-chained paired-item properties and `["item"]` are supported.
- Dynamic node names, directly computed selector properties, visible accessor aliases/indirection, unsupported name escapes, Python f-strings and unterminated lexical constructs report R007. Regular expressions are supported in clear positions (assignment, `return`, expression start); a `/` after `)` / `}` cannot be told apart from division versus a statement regex and conservatively reports R007; use an intermediate variable instead.
- **This is not a full JS/Python parser**: it does not check all language syntax, and does not analyze eval/generated code, accessors reached by reflection such as `globalThis`, cross-variable data flow or guard conditions. Only the direct reference forms documented here are promised; indirectly constructed forms are never claimed to be rename-safe. `obj.$(...)` on ordinary objects is not treated as the n8n global.
- Only `main` execution edges are modeled; AI/tool and other connection channels report R007 so that resource wiring is not mistaken for execution order. IF/Switch outputs and multiple triggers are merged conservatively by structure. A source strongly connected component enters the graph through its known trigger if it has one; otherwise every member may be an entry. Feedback loops are not unrolled per iteration.
- Branch conditions are not evaluated, and Merge synchronization/mode/version behavior, error routing, zero-item outputs, manual executions, pinData and cross-workflow run history are not simulated. R003 means "not structurally guaranteed"; `isExecuted` guards and read-only configuration access such as `.params` get no special exemption and may need human judgment.
- R004 is a risk hint. Known risky nodes are Merge, Aggregate, Summarize, legacy Item Lists, Function, and Code not in `runOnceForEachItem` mode. It fires only when such a node lies between the reference source and the consumer; the consumer's own output behavior does not count as input risk. `pairedItem` repair logic, indirect destructuring of `.item` and community node internals are not analyzed; no R004 does not prove pairing is correct. `.first()` / `.all()` / `$items()` do not depend on item pairing but are still checked for reachability.
- Limits: 16 MiB per file, 2000 nodes, 20000 edges, JSON depth 100; 1048576 characters per source field, 100000 tokens per lexical scan, template nesting 40, 20000 static references per workflow. File/structure limits exit 2; scan/reference limits report R007, so results may cover only part of the source but never pass. Duplicate JSON keys and NaN/Infinity literals are rejected.

Checks never print parameter source or credential fields; diagnostics contain the file, node name, reference name and JSON Pointer. This is not a general linter, expression evaluator, full language parser or n8n API client, and it never executes workflows.

## Tests

```sh
uv run --extra test pytest tests/n8n_refs
flowwrench n8n-refs check tests/n8n_refs/fixtures/*.json --format sarif
```

`tests/n8n_refs/fixtures/*.json` contains clean, branch, Merge pairing and rename samples; the SARIF acceptance command exits 0 with **3 warnings**. `tests/n8n_refs/fixtures/rules/` has a positive and a negative sample for each of R001-R007; the deliberately broken samples exit 1 with the default threshold, as expected.

## Optional: real n8n engine rename acceptance

An explicitly opt-in integration recipe, independent of the offline CLI, lives in [tests/n8n_refs/integration/n8n/](../tests/n8n_refs/integration/n8n/). It pins **n8n 2.47.4, Node 24.22.2 and npm 11.19.1**, with dependencies and integrity locked in `package-lock.json`. These npm dependencies exist only for this recipe; they are not a flowwrench runtime dependency and n8n is not bundled. Installing them needs network access and plenty of disk space.

The three self-authored [runtime fixtures](../tests/n8n_refs/fixtures/runtime) are each `Manual Trigger → Set v2 A → Set v2 Result`, with no credentials, Code or external network nodes; they reference `A` as `$('A').first().json.value`, `$node['A'].json.value` and `$node?.['A']?.json.value`. Each fixture is renamed (A → `Customer / 雪`) with the installed CLI, then the original, renamed and a deliberately broken (`Missing`) copy are imported, executed and re-exported through the official n8n Server CLI, each in a fresh temporary HOME, `N8N_USER_FOLDER` and SQLite database with an allowlisted environment. Business output must be `[{"result":"fixed / 雪"}]` before and after the rename, and the broken copy must fail the gate with R001 (exit 1) and in the real engine.

```sh
npm ci --prefix tests/n8n_refs/integration/n8n --no-audit --no-fund
python3 tests/n8n_refs/integration/n8n/run.py --refcheck-python /path/to/python-with-flowwrench \
  --output-dir <new evidence dir>

# Or the full local CI example (installs the locked npm deps and flowwrench into a fresh venv):
python3 tests/n8n_refs/integration/n8n/ci.py --output-dir <new evidence dir>
```

The output directory must not exist yet. Both entry points exit **0=PASS, 77=SKIP, 2=ERROR**: missing prerequisites are SKIP, while version mismatches, install/upstream failures and acceptance mismatches are ERROR; CI must not treat 77 as a pass. All commands, raw logs, exports and summaries are kept in the output directory. This proves only the three exact expressions, node types/versions and linear single-item workflows above; every other form documented here is **offline static support** only.

## License

Apache-2.0; see [LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md).
