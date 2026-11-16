> **Part of [flowwrench](../../README.md).** nrflowtest is the Node.js companion of
> flowwrench: it needs the real Node-RED runtime, so it is not in the Python package.
> `flowwrench nodered-test` prints how to run it. All commands below run from this
> directory (`packages/nrflowtest/`), e.g. `cd packages/nrflowtest && npm ci && npm test`.

# nrflowtest

Run offline, headless tests against real Node-RED flow exports. Inject JSON
messages, replace I/O nodes with declarative stubs, assert messages at node
boundaries, and fail CI on flow errors instead of waiting for a misleading timeout.
No editor, Node-RED server, external service, or credentials are needed.

## Install and run

Requires **Node.js ≥22.9**, npm, and Linux. The original brief proposed Node ≥20;
pinned `node-red@5.0.9` actually requires ≥22.9. Node 20 is not supported by this
release. Development and acceptance tests ran on Node 24.22.2.

From this repository:

```sh
npm ci
npm test
node bin/nrflowtest.js run \
  --flows fixtures/orders/flows.json \
  --tests 'fixtures/orders/*.test.yaml'
```

The included redistributable export is an order pipeline:
`inject → switch → change → http request → debug`, with a rejected-order branch.
Each case replaces the HTTP node, so the fixture's `.example` URL is never used.
The command prints three named passes followed by:

```text
3 passed, 0 failed
```

Build an installable local package (no registry publishing):

```sh
npm pack
# Choose a local install directory; this example stays inside the checkout.
mkdir -p local-install
tar -xzf nrflowtest-0.1.0.tgz -C local-install
(cd local-install/package && npm ci --omit=dev --ignore-scripts)
./local-install/package/bin/nrflowtest.js --help
./local-install/package/bin/nrflowtest.js run \
  --flows fixtures/orders/flows.json --tests 'fixtures/orders/*.test.yaml'
```

Use this **package-root installation** to preserve the distributable lockfile and
security overrides. Installing the tarball as another project's dependency with
`npm install`/`npx` ignores dependency-level overrides and is not the verified
installation method for this release. `npm run test:package` verifies the locked
package-root installation offline (after the first `npm ci` has filled npm's
cache), including licenses, patch versions, 3 order cases, and a hanging-input /
hanging-cleanup fixture followed by a healthy case, plus passing/failing multi-port
sequence and JSON Pointer assertions with parsed JUnit diagnostics, plus declared
and unexpected caught subflow errors with structured count/source records. It requires
Linux `tar` and `/proc`. It also sends SIGINT/SIGTERM to the installed CLI during
hanging input/cleanup, checks exit 130/143 and interrupted JSON/JUnit, and verifies
that the worker is reaped and its runtime directory removed before parent exit.
The verifier also checks structured resource-limit lower bounds and exact-cap
boundaries, including setup/cleanup errors and UTF-8 JSON bytes.
No registry release is published.

The executable also works when invoked from a directory without `package.json`.
To inspect and test your own synthetic export:

```sh
node bin/nrflowtest.js run --flows flows.json --tests 'tests/**/*.yaml' \
  --strict-types --reporter json
node bin/nrflowtest.js run --flows flows.json --tests 'tests/*.yaml' \
  --reporter junit --junit-file out/junit.xml
node bin/nrflowtest.js run --flows flows.json --tests 'tests/*.yaml' --dry-run
```

Quote globs, and repeat `--tests PATTERN` to select multiple paths. `*`, `?`,
character classes and `**` are supported; brace expansion and extglob are disabled.
Patterns are passed to a glob library, never a shell. All paths are relative to
the invocation directory unless an explicit absolute input path is supplied.

| Option | Behavior |
| --- | --- |
| `--reporter spec` | Named results, failure details and pass/fail summary (default) |
| `--reporter json` | One JSON object with case results, structured message/error mismatches, errors, durations and warnings |
| `--reporter junit` | JUnit XML only on stdout; warnings go to stderr |
| `--junit-file out/junit.xml` | Also write JUnit atomically; path must stay beneath cwd, contain no symlinks/traversal, and not overwrite an input |
| `--strict-types` | Reject unsupported types unless explicitly stubbed; recommended for CI |
| `--allow-function` | Opt in to executing **trusted** Function-node JavaScript, including initialization/finalization |
| `--dry-run` | Static schema, selectors, types, stubs and subflow mapping checks only; no runtime, scripts, steps, timers or report files are started/written |

Exit **0** means all cases passed, or a dry-run validated. Exit **1** means a test
failed (message mismatch, count, timeout, unexpected error, resource limit or
worker crash). Exit **2** means a usage/load/report error (including malformed
files, unknown/ambiguous selectors or inactive targets). Load/usage errors go to
stderr and do not create JUnit files. Dry-run cannot be combined with
`--junit-file`. A normal JUnit run contains a `testsuites`/`testsuite` hierarchy
with one `testcase` per YAML case and escaped `failure` text.

SIGINT (Ctrl-C) exits **130**; SIGTERM exits **143**. During a run the parent
force-terminates the active worker, waits for process/stdio closure, removes its
temporary runtime directory, then writes the selected report and optional JUnit
file before exiting. The active case fails with `Run interrupted by SIGINT`
(or `SIGTERM`), its last known phase and elapsed duration. Completed cases retain
their results; later cases do not run and are omitted from JSON/JUnit totals.
JSON adds `interrupted: "SIGINT"` (or `"SIGTERM"`); stderr also explains the
interruption. An existing JUnit file is atomically replaced with this interrupted
report, so it cannot retain an earlier pass after a successful report write.
Repeated signals during cleanup keep the first signal's exit code. Report-write
errors are printed to stderr; an interrupted run retains exit 130/143.

## Case format

A test file has `version: 1` and a nonempty `cases` array. Unknown fields,
duplicate YAML keys, aliases, custom tags and non-JSON data are errors.

```yaml
version: 1
cases:
  - name: creates an order
    timeout: 200
    stubs:
      - node: {id: api}
        delay: 5
        outputs:
          - payload: {orderId: sample-001}
            statusCode: 201
    inject:
      node: {name: New order}
      msg: {payload: {amount: 42, currency: EUR}}
    expect:
      - node: api
        event: input
        msg: {topic: orders/create, payload: {amount: 42}}
      - node: api
        event: output
        output: 0
        count: 1
        msg: {statusCode: 201}
      - node: Accepted
        msg: {payload: {orderId: sample-001}}
    expectNoMessage:
      - node: Rejected
```

A node selector is an id/name string, `{id: ...}` or `{name: ...}`. String
selectors try an exact id first, then a unique exact name; ambiguous names are
errors. Selectors refer to the original export, before replacements. Exported
message nodes must have `x` and `y`, as in real editor exports; nodes without
coordinates are config nodes and can only be selected for stubbing. Structural
nodes (tabs, groups, definitions, comments, global config) cannot be targets.

- `timeout`: the case's observation window in milliseconds, default 200,
  range 10–30000. The whole window is observed even after an expectation matches,
  so extra messages and late errors inside the window fail. This is a finite
  window, not a claim about activity after it.
- `hardTimeout`: optional whole-worker wall-clock budget in milliseconds,
  integer 10–60000. It covers process startup, deployment, the observation window,
  and cleanup together. The default is `12000 + timeout`; an explicit value
  replaces that entire budget. Allow enough time for startup and cleanup: a value
  shorter than `timeout` is valid but can terminate even a healthy case. The
  parent enforces the deadline even if flow code blocks the worker's event loop.
  Expiry fails only that case (exit 1), reports the budget, last known phase
  (`startup`, `observation`, or `cleanup`) and elapsed time, kills/reaps the worker,
  removes its runtime directory, and proceeds to the next case. Phase updates do
  not extend the deadline. This is separate from per-expectation timeouts.
- `inject`: one object or an array of `{node, msg, after?}`. `msg` must be a JSON
  object; `after` defaults to 0 and is milliseconds from the start of observation,
  less than the case timeout. An empty list supports absence/setup-error tests.
  Injecting into a node delivers to its **input**, not to its downstream wires.
- `stubs`: array of `{node, outputs?, error?, delay?}`. On each input a stub emits
  a fresh clone of its canned outputs, raises `node.error(error, msg)`, or drops
  the input if neither is supplied. Outputs and error are mutually exclusive.
  `delay` defaults to 0, range 0–30000 ms. Config stubs are inert placeholders;
  they do not implement a broker, TLS configuration or connection behavior.
- `expect`: array of `{node, event?, output?, count?, msg?, fields?, sequence?, timeout?}`. Event
  defaults to `input`; `output` selects a zero-based port for `event: output`.
  Omit `output` to count all output ports. Count defaults to 1 (or the length of
  `sequence`) and is the **exact total at that boundary**, not the number matching the partial pattern.
  Every observed message must match `msg` if supplied. An optional timeout is
  a prefix of the case window (1 through case timeout).
- `fields`: nonempty object mapping JSON Pointer strings to **exact JSON values**.
  Every selected message must contain each path and equal its value, including
  object keys and array length/order. Other message fields are ignored. Pointers
  start with `/`; `~1` escapes `/` and `~0` escapes `~` in keys. The empty pointer
  `''` selects the entire message. Array indexes are zero-based decimal integers
  without leading zeroes. Missing paths fail even when the expected value is
  `null`. URI fragments, wildcards and expressions are not supported.
- `sequence`: nonempty ordered array (maximum 1000 entries) of
  `{output?, msg?, fields?}`. Entry 1 matches observed message 1, and so on;
  missing/extra messages fail the exact count. An explicit `count` must equal
  the array length. Common `msg`/`fields` on the expectation apply to **every**
  observed message as well. An empty entry `{}` accepts any message at that
  position. For zero messages, use `expectNoMessage`.
  A sequence uses the expectation's node, event, port filter and time window.
  Per-entry `output` checks a zero-based port and requires `event: output`.
  Omit the outer `output` to assert order across a node's output ports; set it
  to assert the order on one port. Order means observation order at that boundary,
  not a timing guarantee or a global order across different nodes.
- `expectNoMessage`: array of `{node, event?, output?, timeout?}`. Exact count 0
  over that window, independent of payload; there is no pattern filter.
- `expectError`: array of `{node, message, messageMatch?, count?, source?}`. Message is a
  literal, case-sensitive string, not a regex/expression. Omitted `messageMatch` and
  `messageMatch: substring` require the retained text to contain it; `messageMatch: exact`
  requires the whole retained text to equal it (see
  [Exact retained messages](#exact-retained-messages)). Count is an integer from
  **0 to 10000**, default 1. Explicit `count: 0` asserts no **remaining retained**
  errors match that selector; it does not assert that no matching error was emitted.
  Each error can satisfy only one entry, in declaration order. All other errors
  fail, **including errors handled by a catch node**. For errors inside subflows,
  select the exported instance; diagnostics retain the concrete source id.
  Optional `source` further restricts that selection to exact `errors[].node`
  equality. It is a nonempty case-sensitive literal string, without name lookup,
  regex, suffix matching or expansion of internal definition ids. Unknown source
  ids are valid filters and yield no candidates. The existing `node` selector
  still must resolve to an exported message node/instance.

At least one message/absence/error expectation is required. Objects match
recursively by specified properties; extra actual object keys are allowed.
Arrays require equal length and ordered recursive matching. Numbers and strings
are distinct. Messages are observed as JSON snapshots before downstream mutation;
non-JSON/circular/BigInt messages fail observation. Buffer output uses Node's JSON
`{type: Buffer, data: [...]}` representation. Mismatches show a property path plus
expected and actual values.

### Ordered messages and JSON Pointer fields

The synthetic `test/fixtures/output-sequence.json` runs the pinned real Switch
node. Three injected messages contain `{payload: {id: A, accepted: true}}`,
then `B/true`, then `C/false`. The expected output is exactly `A`, `B` on port 0,
then `C` on port 1. Ignore generated metadata and assert only selected fields:

```yaml
expect:
  - node: router
    event: output
    sequence:
      - output: 0
        fields: {'/payload/id': A}
      - output: 0
        fields: {'/payload/id': B}
      - output: 1
        fields: {'/payload/id': C, '/payload/accepted': false}
```

Run the complete passing example (exit **0**, one case passed):

```sh
node bin/nrflowtest.js run --flows test/fixtures/output-sequence.json \
  --tests test/fixtures/output-sequence.test.yaml --strict-types --reporter json
```

The positive fixture also checks exact per-port counts, ordered sink input, and
`/payload/a~1b/~0key` selecting `payload['a/b']['~key']`. Existing `msg` patterns
remain recursive partial matches; `fields` compares each selected value exactly.

The companion negative fixture deliberately expects four messages, reverses
the first two IDs, expects the second message on the wrong port, and selects a
missing field. Run it to inspect both reporters (exit **1**, one failed case):

```sh
node bin/nrflowtest.js run --flows test/fixtures/output-sequence.json \
  --tests test/fixtures/output-sequence.fail.yaml --strict-types \
  --reporter json --junit-file out/sequence-junit.xml
```

JSON `cases[0].failures` and JUnit failure text include the same details, such as:

```text
Timeout at node router output after 80ms: expected 4 message(s), got 3
Message mismatch at node router output, message 1, JSON Pointer "/payload/id": expected 'B<&', got 'A'
Sequence mismatch at node router output, message 2: expected output 1, got 0
Message mismatch at node router output, message 3, JSON Pointer "/payload/missing": missing; expected null
```

Message indexes in diagnostics start at 1; port indexes start at 0. Malformed
Pointers, empty sequences and conflicting counts fail preflight with exit **2**
and no report file, including in dry-run validation.

### Structured JSON mismatches

Each JSON case result includes `mismatches`, an array alongside the existing
human-readable `failures`. The negative example above produces five records:
one count, two field values, one output port, and one missing field. For example:

```json
{
  "kind": "field",
  "assertion": "expect",
  "expectationIndex": 0,
  "sequenceIndex": 2,
  "messageIndex": 2,
  "node": "router",
  "event": "output",
  "output": 1,
  "pointer": "/payload/missing",
  "expected": {"value": null},
  "actual": {"missing": true}
}
```

- `assertion` is `expect` or `expectNoMessage`; `expectationIndex` indexes that
  original YAML array. All structured indexes are **zero-based**, including
  `messageIndex` within the expectation's filtered port/time window.
- `sequenceIndex` identifies the sequence entry, or is `null` for common
  patterns/counts. `messageIndex` is `null` for counts. Missing/extra emissions
  produce a count record, rather than invented message/value records.
- `node` is the resolved exported id and `event` is `input` or `output`.
  `output` is the actual port for a message record, the selected port for a
  count, or `null` for input/all-port counts. An `output` mismatch stores the
  expected/actual ports in the value wrappers.
- `kind` is `count`, `output`, `message` (partial `msg`), or `field` (exact
  `fields`). `pointer` is a JSON Pointer for message/field values, including
  `''` for the root; otherwise it is `null`. Partial patterns record the first
  difference; array-length differences select the whole array, not a synthetic
  `/length` property. Escaped keys are unambiguous even if human paths contain dots.
- `expected` and `actual` contain `{value: ...}` with the original JSON value,
  without diagnostic-text truncation. An absent actual property is
  `{missing: true}`, distinct from `{value: null}` or `{value: {missing: true}}`.

Error diagnostics also use `mismatches`, with two additional kinds:

- `errorCount`: `assertion: "expectError"`, the zero-based original
  `expectationIndex`, selected exported `node`, and literal `errorMessage`
  substring. When supplied, `source` carries the literal source filter; it is
  absent when omitted. `errorIndexes` lists the matching indexes in the case's
  existing `errors` array. `expected` and `actual` wrap integer counts as `{value: ...}`.
  `captureComplete` says whether error-entry capture discarded any errors.
  `actual.value` always counts captured matching entries remaining at this
  declaration, never total emitted errors; when capture is incomplete, even a
  zero match does not establish that the flow emitted no matching error.
  No match is `{value: 0}` with `errorIndexes: []`, never an invented error.
  `messageFidelity` separately describes retained strings at this selector, as
  detailed below; it does not turn these captured counts into emitted totals.
- `unexpectedError`: `assertion` and `expectationIndex` are `null`;
  `errorIndex` selects the original `errors` entry. `node` is its concrete source
  id, or `null` for a runtime error. `expected: {missing: true}` means no error
  was expected; `actual: {value: ...}` contains that recorded error object,
  including its `node`, `instances`, `message`, `messageTruncated` and
  `messageOriginalLength`. Existing capture limits apply (100 errors and 8000
  JavaScript string code units per message); records do not recover discarded data.

Both error kinds have `event: "error"` and `sequenceIndex`, `messageIndex`,
`output` and `pointer` set to `null`. Error-count records follow declaration
order, then unexpected records follow error order. Each expectation consumes
all remaining matching errors even if its count fails, preserving the existing
declaration-order semantics. Unexpected errors still suppress consequent message
assertions; a count mismatch without unexpected errors permits message checks.
Message records then follow failure order: count, each observed message's common
patterns, then its sequence port/patterns. Passing cases have `mismatches: []`.
Deadlines, interruptions and unexpected worker exits also have `termination`
metadata, described below; other worker failures retain existing text diagnostics.
Spec output, `errors`, human-readable failures and JUnit text are unchanged.
Use case `status`/`failures` to determine success; an empty `mismatches` array
alone does not imply a pass. Full values can make JSON reports larger than text.

Run the synthetic error examples with trusted Function execution enabled:

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-mismatches.json \
  --tests test/fixtures/error-mismatches.test.yaml --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/error-mismatches.json \
  --tests test/fixtures/error-mismatches.fail.yaml --allow-function \
  --reporter json --junit-file out/errors-junit.xml
```

The first exits **0**, with one pass: two real `node.error` calls inside a subflow
are declared at `instance`, and a real Catch delivers both to `sink`. The second
exits **1**, with five intentional failures: missing error, extra declared errors
plus a message count mismatch, wrong case-sensitive substring, an already consumed
error, and an undeclared caught error. Missing errors report count `0`; unexpected
records retain source `instance-inner` and `instances: ["instance"]`. The last
case observes Catch delivery but suppresses its consequent message-count failure.
Human failure strings and independently parsed JUnit text remain identical.

### Restricting expected errors to a concrete source

Two internal nodes can emit the same message inside one selected instance.
Copy a concrete id from `errors[].node` into `source` to distinguish them:

```yaml
expectError:
  - node: outer
    source: outer-middle-left
    message: shared
  - node: outer
    source: outer-middle-right
    message: shared
```

Both node/instance membership and literal source equality must hold. For a direct
exported node, `source` can equal that node's id; a different source cannot bypass
the node selection. A source such as `left` does not expand to
`outer-middle-left`, and a source from another instance does not match `outer`.
Normal node-selection restrictions still apply to internal or cloned ids.

The combined filter applies to `candidateErrorIndexes`, `errorIndexes`,
`nonmatchingErrorIndexes`, `messageFidelity` and `previouslyConsumed`. Original
capture indexes and declaration-order all-match consumption are preserved,
including failed declarations. A truncated nonmatch affects only fidelity for
its selected source. Unknown helper metadata remains unknown (`null`); no
candidates means fidelity `true`. Global `captureComplete` remains independent.
No discarded errors, suffix matches or emitted totals are inferred.

Outcomes and `errorCount` mismatches include `source` only when supplied.
A source-filtered count failure identifies it in spec, JSON failure strings and
JUnit text, for example:
`Error expectation at node outer, source 'left': expected 1 containing 'shared', got 0`.
Declarations without `source` retain their existing evidence and diagnostics.
Unavailable worker evaluations still omit outcomes; dry-run returns no cases.
Invalid empty/nonstring sources fail loading with exit **2**, without JUnit output.

The passing fixture exercises two sources in a nested instance, another instance,
direct nodes, zero/default/positive counts and truncated nonmatches (exit **0**,
four passes). The negative fixture checks failed-zero consumption, prior consumers,
nonmatching source evidence and unexpected-error priority, followed by a healthy
case (exit **1**, three failures and one pass):

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-source.json \
  --tests test/fixtures/error-source.test.yaml --strict-types --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/error-source.json \
  --tests test/fixtures/error-source.fail.yaml --strict-types --allow-function \
  --reporter json --junit-file out/source-junit.xml
```

### Exact retained messages

Omit `messageMatch` for the default substring behavior, or write
`messageMatch: substring` explicitly. `messageMatch: exact` requires the **entire
retained message** to equal `message`, using JavaScript string equality: case-sensitive,
without trimming, regex, expression or Unicode normalization. Multiline, Unicode and
combining-character literals are compared exactly as written:

```yaml
expectError:
  - {node: plain, message: "line\n雪🙂 <&", messageMatch: exact}
  - {node: plain, message: short, messageMatch: exact, count: 0}
```

Any other `messageMatch` value (for example `EXACT`, `regex`, `null` or `false`) fails
loading with exit **2** and writes no JUnit file, as does an empty or nonstring `message`. Exact equality uses the same predicate for matches, nonmatches,
`previouslyConsumed` and failure text (`exactly matching`). A `short` declaration does
not match retained `short tail` or `pre short`. Candidate arrays, `source` filtering,
declaration-order consumption and unavailable-evaluation omission are unchanged.
Outcomes and `errorCount` mismatches include `messageMatch` only when it was declared.

Retained equality is independent of original-message fidelity. If `message` equals a
truncated 8000-unit retained text, the declaration can pass, but `messageFidelity.complete`
is `false` and that entry appears in `truncatedErrorIndexes`, because the original message
may have continued past the retained cap. A longer declared message that merely begins
with the retained prefix does not match, and no discarded suffix or emitted total is
inferred.

The exact fixtures use the synthetic source flow. The passing fixture passes four cases
(exit **0**). The failing fixture passes one healthy case and fails four intentional cases
(exit **1**): a retained-zero consumption, an unexpected nonmatch, a prefix/suffix
count mismatch, and a truncated retained-equality case that also trips the observation
resource limit:

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-source.json \
  --tests test/fixtures/error-exact.test.yaml --strict-types --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/error-source.json \
  --tests test/fixtures/error-exact.fail.yaml --strict-types --allow-function \
  --reporter json --junit-file out/exact-junit.xml
```

### Structured worker termination

Every JSON case has `termination: null` when the worker returns an ordinary
result, including failed message or error assertions. A hard deadline,
interruption or unexpected child exit instead supplies an object, for example:

```json
{
  "reason": "deadline",
  "phase": "observation",
  "hardTimeout": 1500,
  "signal": null,
  "exitCode": null,
  "exitSignal": "SIGKILL"
}
```

- `reason` is `deadline`, `interrupted` or `workerExit`. A child exiting with
  code **0 without a result** is still a failed `workerExit`.
- `phase` is the last parent-observed phase: `startup`, `observation` or
  `cleanup`. It does not infer progress the parent has not received over IPC.
- `hardTimeout` is the effective whole-worker budget for a deadline, including
  the computed default when no override is set; otherwise it is `null`.
- `signal` is the received parent signal for an interruption (`SIGINT` or
  `SIGTERM`); otherwise it is `null`. It differs from `exitSignal`, which is the
  **observed child close signal**, typically `SIGKILL` after cancellation.
- `exitCode` and `exitSignal` are the actual child close values, with `null`
  for an unavailable value. Sending a kill request does not establish an exit
  signal. A pre-aborted `runWorker` call starts no child and reports both as
  `null`; the CLI omits cases it has not started after cancellation.

Elapsed time remains in the case's `durationMs`. Existing failure strings,
spec/JUnit text, interruption priority, deadline continuation and cleanup are
unchanged. Terminal failures do not invent message mismatches or recorded node
errors. Usage/load errors still exit **2** without a case report.

The deadline example below exercises real Node-RED observation and cleanup
hangs. `test/termination.test.js` additionally substitutes three controlled real
child processes from parent-only test instrumentation: exit 23, termination by
SIGTERM and exit 0 without a result, followed by a healthy real Node-RED case.
This checks child exit reporting without a production worker override or a
Function VM escape. The installed-package verification runs the same check.

### Structured resource limits

Ordinary JSON assertion results include `resourceLimits: []` when no resource
breach was witnessed. A resource failure retains `termination: null` and reports
one record per witnessed resource, in first-breach order. For example, 103 raised
errors with an expectation accepting the first 100 still fail with:

```json
{"resourceLimits": [{"resource": "errors", "limit": 100, "captured": 100, "observedAtLeast": 101}], "errorCapture": {"limit": 100, "captured": 100, "observedAtLeast": 101, "complete": false}}
```

`resource` is `observations` (limit 10000), `deliveries` (100000),
`observationBytes` (8388608), or `errors` (100). `observedAtLeast` is the first
witnessed over-limit count, or cumulative attempted UTF-8 JSON bytes at the first
byte breach. It is a **lower bound**, never an exact final total. Exactly at a
limit does not fail. Repeated breaches of the same resource do not add records.
`captured` is the amount retained at the first breach in the resource's units:
observation snapshots, error entries, or UTF-8 bytes of retained JSON messages
(excluding the observation envelope). A rejected message contributes to the byte
lower bound but not to captured bytes. For `deliveries`, `captured` means accepted
input deliveries, not stored snapshots; deliveries beyond the cap are rejected.

The existing shared capture stop remains: any breach stops further message
snapshots; errors can still be retained up to 100, and over-limit deliveries are
rejected. Independently witnessed breaches can produce multiple records, but
uncrossed limits are not inferred after capture stops. `observedMessages` and
`errors` describe retained data, not attempted totals; message/error mismatches
still compare that retained data. Setup and cleanup errors count toward the same
error cap. Resource records describe the worker's capture lifetime, rather than
claiming an observation phase for setup/cleanup errors.

Every completed ordinary worker result also includes `errorCapture` with `limit`,
`captured`, `observedAtLeast` and `complete`. When no error entry was discarded,
`complete` is true and the lower bound equals the captured count (including zero
and exactly 100). Once an error is discarded, `complete` is false and the lower
bound is 101, even if more errors were raised. This global lower bound cannot
establish attempted counts for individual nodes or message substrings.
`complete` describes entry-count capture only: the existing 8000-code-unit
(JavaScript string length) cap still applies to each retained message. A message
resource breach alone does not make error-entry capture incomplete.
Error-count mismatches expose the same boolean as `captureComplete`; declaration
order and consumption still apply to their captured counts and `errorIndexes`.

Each retained `errors` entry has `messageTruncated` (boolean) and
`messageOriginalLength` (integer). Length is measured in UTF-16 code units, as in
JavaScript `string.length`, on the normalized error message immediately before
the existing `slice(0, 8000)`. It is not a byte, code-point or grapheme count.
Strings and string-valued `error.message` properties are used as before; other
values use the existing inspection conversion. The length describes that
converted string, not the underlying object's full contents.

At exactly 8000 code units, `messageTruncated` is false; at 8001 it is true.
A short message still has its original length and false; an empty message has
length 0 and false. For example, an 8001-unit message retains 8000 units and adds
`"messageTruncated": true, "messageOriginalLength": 8001`. Truncation can split
a surrogate pair: JSON preserves the retained lone surrogate, while existing
UTF-8 spec output and XML sanitization retain their behavior. No discarded text
is reconstructed. Expected-error substring matching still uses the retained
message, so a substring only in the discarded suffix cannot match. Even with
`captureComplete: true`, truncated strings can hide matching text.

Each `errorCount` mismatch also includes `messageFidelity`:

```json
{"complete": false, "truncatedErrorIndexes": [0]}
```

Candidates are the remaining retained errors whose concrete `node` or
`instances` contain the selected node, **before this declaration consumes
matches**. This includes substring nonmatches: discarded suffixes can hide the
substring. `truncatedErrorIndexes` lists known truncated candidates by their
original indexes in `errors`, in capture order. Unrelated nodes/instances and
previously consumed errors are excluded. `complete` is false if any candidate
is truncated, true if all candidates are untruncated (including no candidates),
and null if metadata is unavailable for any candidate and none is known
truncated. Ordinary worker captures always have boolean truncation metadata;
null supports assertion-helper inputs without that metadata. Length alone is
never used to infer fidelity.

For example, selecting `TAIL` against a retained 8000-unit prefix whose original
message ended with discarded `TAIL` still yields `actual: {value: 0}` and
`errorIndexes: []`, but `messageFidelity.complete: false`. If a preceding
declaration consumed that error using its retained prefix, a later declaration
has no such candidate and reports true with `truncatedErrorIndexes: []`.
False describes string loss, even when the substring already matched the prefix;
it does not mean a particular count is wrong or that a missing substring exists.

`messageFidelity.complete` describes only these retained strings. It can be
true while global `captureComplete` is false: discarded entries have unknown
nodes and text, so neither field establishes selector-specific emitted-data
completeness or an exact emitted count. No fidelity records are invented for
unavailable worker captures.

Each `errorCount` mismatch also includes `previouslyConsumed`, in original
`errors` capture order:

```json
[{"errorIndex": 0, "expectationIndex": 0}, {"errorIndex": 1, "expectationIndex": 2}]
```

Each entry identifies a retained error that an earlier `expectError` declaration
consumed and that matches the current node/instance **and retained literal
substring**. `expectationIndex` is that consumer's original zero-based declaration
index, including successful declarations that have no mismatch record. Counts
consume every match even when they fail; each error has one original consumer.
Consumption order can differ from capture order.

For example, errors `alpha shared` and `beta shared` consumed by declarations
0 (`alpha`, count 1) and 1 (`beta`, count 2) leave a later `shared` declaration
with `actual: {"value": 0}`, `errorIndexes: []`, and
`previouslyConsumed: [{"errorIndex": 0, "expectationIndex": 0},
{"errorIndex": 1, "expectationIndex": 1}]`. The second consumer failed its count
but still consumed its match. The current declaration's matches are excluded
from its own provenance.

`[]` means no witnessed earlier-consumed retained error matches this selector.
Unrelated nodes/instances, substring nonmatches (including a substring only in a
discarded suffix), discarded entries and absent consumers contribute no entries.
No truncation metadata is needed to witness a retained substring match. Missing
worker captures get no invented provenance records. This field changes neither
remaining `actual.value`/`errorIndexes` nor `messageFidelity` or global
`captureComplete`; empty provenance does not establish that no matching error
was emitted. An explicit expected count of 0 can pass after earlier consumers.

Each completed case also has `errorExpectations`: one outcome for **every**
`expectError` declaration, in original declaration order, including successful
counts. For example, one short error matching the first declaration produces:

```json
{
  "assertion": "expectError", "expectationIndex": 0,
  "node": "a", "errorMessage": "short", "status": "passed",
  "expected": {"value": 1}, "actual": {"value": 1}, "errorIndexes": [0],
  "candidateErrorIndexes": [0], "nonmatchingErrorIndexes": [],
  "captureComplete": true,
  "messageFidelity": {"complete": true, "truncatedErrorIndexes": []},
  "previouslyConsumed": []
}
```

`node` is the normalized concrete node or subflow instance selector;
`errorMessage` is its literal, case-sensitive substring. `expectationIndex` is
zero-based in the original `expectError` list; the default expected count is 1.
`actual.value` and `errorIndexes` describe remaining retained matches **before**
this declaration consumes them. Both passing and failing counts consume all their
matches, including when an explicit `count: 0` fails on retained matches. Later
overlapping positive declarations can therefore fail with actual count 0 and
`errorIndexes: []`, while `previouslyConsumed` points to earlier passing or failing
consumers. Error indexes refer to the case's original `errors` array, whose node
and instances retain concrete/subflow attribution.

`candidateErrorIndexes` lists **all** remaining retained errors selected by that
concrete node or any entry in `instances`, before consumption. The ordered subset
`nonmatchingErrorIndexes` lists those whose retained message does not include the
literal, case-sensitive substring; `errorIndexes` lists the complementary matches.
All three use original zero-based `errors` capture order, regardless of earlier
consumption order. Previously consumed and unrelated errors are excluded.

For example, retained messages `alpha`, `short`, and an 8000-unit `x` prefix at
one selected instance yield `candidateErrorIndexes: [0, 1, 2]` and
`nonmatchingErrorIndexes: [1, 2]` for substring `alpha`. That declaration can pass
with `errorIndexes: [0]` even though its remaining candidates include nonmatches.
A following `TAIL` declaration has candidates and nonmatches `[1, 2]`, with zero
matches: a truncated prefix does not establish whether its discarded suffix
contained `TAIL`. After earlier declarations consume both remaining errors, a
later declaration has both new arrays empty instead. Thus a zero count can be
inspected without reconstructing consumption. Uppercase `ALPHA` does not match
retained lowercase `alpha`.

These arrays describe only retained evidence, even with `captureComplete: false`;
they infer no discarded entries, hidden suffix matches or emitted totals. They
remain available when helper truncation metadata makes fidelity null. They are
added to outcomes only; existing mismatch objects keep their shape. Like outcomes,
the arrays are unavailable when worker evaluation did not complete.

`status` is `passed` exactly when that captured count equals the declared count,
otherwise `failed`. It describes this declaration alone: an outcome can pass in a
case failed by another assertion, unexpected error or resource overflow. Even with
`captureComplete: false`, a retained count of 100 can pass an expected count of
100; this does **not** establish an emitted matching total. Fidelity and provenance
have the same semantics as on `errorCount` mismatches, including false for a
truncated remaining candidate, null for unavailable helper metadata, and no
inference about hidden suffixes or discarded entries. Every failed outcome's
count/evidence fields come from the same evaluation as its existing mismatch;
`mismatches` still contains failures only, with its existing shape.

`errorExpectations: []` means a completed evaluation had no declarations. It is
present even when unexpected errors suppress message assertions. Deadline,
interruption and unexpected-worker-exit cases omit the field because there was no
completed evaluation. Load errors still exit 2 without a suite JSON result;
dry-run starts no workers and returns no case outcomes.

To assert retained absence, use an explicit zero:

```yaml
expectError:
  - {node: worker, message: fatal, count: 0}
  - {node: worker, message: expected warning}
```

The first declaration passes if no remaining retained message at `worker` contains
lowercase `fatal`. It consumes nothing on a pass. A retained `fatal` match fails
the zero and is consumed, so later declarations cannot reuse it. Nonmatches remain
available to later declarations and fail as unexpected errors if never consumed.
Earlier passing or failing consumers can also leave a zero declaration passing;
`previouslyConsumed` distinguishes those witnessed matches from no retained matches.

Inspect `candidateErrorIndexes`, `nonmatchingErrorIndexes`, `captureComplete` and
`messageFidelity` alongside the outcome. Zero can pass with truncated nonmatching
prefixes or incomplete entry capture. It says nothing about discarded suffixes or
discarded entries; an independent resource-limit breach still fails the case.
No completed worker evaluation means no zero outcome, just as for positive counts.

The zero-count fixtures cover empty captures, short/truncated nonmatches, matching
errors, declaration-order consumption, real nested instances and incomplete capture:

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-zero-count.json --tests test/fixtures/error-zero-count.test.yaml --allow-function --strict-types --reporter json
# exit 0: three passing cases
node bin/nrflowtest.js run --flows test/fixtures/error-zero-count.json --tests test/fixtures/error-zero-count.fail.yaml --allow-function --strict-types --reporter json --junit-file zero-junit.xml
# exit 1: four intentional failures, then one healthy passing case
```

The following extra negative example exits **1**: its first declaration passes
with 100 truncated retained matches and incomplete entry capture; the second has
zero remaining matches and identifies all 100 prior consumptions. The case also
fails the existing resource limit.

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-selector-fidelity.json \
  --tests test/fixtures/error-expectations.fail.yaml --allow-function --reporter json
```

Run the selector examples (exits **0** and **1**):

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-selector-fidelity.json \
  --tests test/fixtures/error-selector-fidelity.test.yaml --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/error-selector-fidelity.json \
  --tests test/fixtures/error-selector-fidelity.fail.yaml --allow-function --reporter json \
  --junit-file out/selector-junit.xml
```

The negative example intentionally fails eight cases covering direct and subflow
selectors, unrelated truncated instances, suffix misses, consumption, short
messages, multiple prior consumers and entry loss with short or truncated strings; its final healthy case
passes. The positive example passes both cases.

String truncation alone is not a resource breach and does not make entry-count
capture incomplete. Entries discarded by the 100-error cap have no per-string
metadata. When the worker capture is unavailable, there are no invented error
entries, lengths or truncation flags.

Run the trusted synthetic string-boundary examples (exits **0** and **1**):

```sh
node bin/nrflowtest.js run --flows test/fixtures/error-message-capture.json \
  --tests test/fixtures/error-message-capture.test.yaml --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/error-message-capture.json \
  --tests test/fixtures/error-message-capture.fail.yaml --allow-function --reporter json \
  --junit-file out/strings-junit.xml
```

The first passes two cases: declared boundary errors and a later healthy case.
The second intentionally fails four cases (undeclared errors, discarded-suffix
matching, declaration-order consumption and 101 long errors), then passes a
healthy case. They cover ASCII, multibyte BMP/astral Unicode, a surrogate pair
crossing the cap, and ordinary short/empty messages.

The existing `Observation limit exceeded (...)` failure, spec/JUnit text,
continuation and cleanup are unchanged. Deadline, interruption and unexpected-exit
results omit `resourceLimits` and `errorCapture`: the parent has no completed
worker capture, so empty or complete metadata would be misleading.

Run synthetic exact-cap and over-cap examples (exits **0** and **1**):

```sh
node bin/nrflowtest.js run --flows test/fixtures/resource-limits.json \
  --tests test/fixtures/resource-limits.test.yaml --allow-function --reporter json
node bin/nrflowtest.js run --flows test/fixtures/resource-limits.json \
  --tests test/fixtures/resource-limits.fail.yaml --allow-function \
  --reporter json --junit-file out/limits-junit.xml
```

The first passes three cases at the observation, UTF-8 byte and error caps. The
second intentionally fails six cases (including independent breaches and
setup/cleanup errors), then passes a healthy case. Source and installed-package
checks additionally verify the delivery cap with real Node-RED deliveries.
Additional capture examples intentionally fail three cases (101 errors with the
first 100 declared, incomplete declaration-order counts, and a byte overflow after
a retained UTF-8 prefix), with the same human/JUnit failures:

```sh
node bin/nrflowtest.js run --flows test/fixtures/resource-limits.json \
  --tests test/fixtures/resource-capture.fail.yaml --allow-function --reporter json
```

Those delivery stress checks use a test-only preload to reset unused helper
metric-spy history; without it, helper history can exhaust the worker's 192 MiB
heap before 100000 deliveries. Production runtime/capture limits are unchanged;
heap exhaustion remains an unexpected worker exit when no result is returned.

Stub output uses Node-RED's per-port array convention:

```yaml
stubs:
  - node: api
    outputs:
      - null                    # no output on port 0
      - - {payload: first}      # two messages on port 1
        - {payload: second}
```

Canned messages **replace** the input; there is no automatic `_msgid`/topic/header
merge, substitution, script, request matching or sequential response queue.
To model an error, use `error: synthetic service unavailable` and assert it in
`expectError`. See the third case in the orders fixture for a complete example.

## Real runtime behavior and boundaries

Each case gets a fresh sequential child process, memory-only Node-RED storage and
context, a temporary `.nrflowtest-*` directory beneath cwd, and a hard deadline.
The directory is removed after the child exits. No helper `startServer` call is
made. Socket connections/listeners and DNS entry points are additionally blocked
inside the worker. Node-RED is unloaded before results return; a child process
cannot leave flow timers or listeners running after normal completion.

Parent SIGINT/SIGTERM also cleans up a worker whose event loop is blocked in
Function code: it uses SIGKILL on the worker instead of waiting for graceful
Node-RED unload. Flow finalizers are not guaranteed on cancellation. SIGKILL of
the **parent**, OS crashes and arbitrary descendant processes created by trusted
code are outside this cleanup guarantee.

Run the synthetic signal regression (Linux, no external services):

```sh
node --test --test-concurrency=1 test/signals.test.js
```

The six checks send each signal to the CLI alone during startup, a blocked
asynchronous Function callback, and stalled Function cleanup. Each run first
completes a healthy case, cancels the active case, and proves a later case never
starts. Expected: six tests pass, exit 130/143 from the interrupted CLI processes,
two reported cases with one failure, no worker PID in `/proc`, and no temporary
runtime directory when the parent exits. Both fresh and pre-existing JUnit
destinations are covered. `npm run test:package` repeats the hanging-input and
hanging-cleanup examples against the installed package's CLI entrypoint.

The shipped synthetic deadline example deliberately blocks an asynchronous
Function callback and leaves a Function close callback unfinished, then runs a
healthy case with fresh context. Run it with trusted Function execution enabled:

```sh
node bin/nrflowtest.js run --flows test/fixtures/deadline-isolation.json \
  --tests test/fixtures/deadline-isolation.test.yaml --allow-function \
  --reporter junit --junit-file out/deadline-junit.xml
```

Expected: exit **1**, three JUnit testcases with **two failures** containing
`hard deadline exceeded after 1500ms` and their observation/cleanup phase, and
one later pass. The first two cases use `timeout: 60` and `hardTimeout: 1500`;
the healthy case allows 3000 ms. These are functional deadline checks, not
performance measurements. All intervals in the fixture end with their worker.

The following pinned upstream nodes execute their real implementations:
`function` (opt-in), `switch`, `change`, `range`, `template`, `delay`, `trigger`,
`rbe`, `json`, `csv`, `split`, `join`, `sort`, `batch`, `catch`, `status`, `complete`,
`link in`, `link out`, `link call`, and `junction`. Tabs, groups, comments and
literal environment settings are preserved. Global module declarations and
external Function libraries are rejected rather than installed.

`inject` is an inert, explicit message entry point: configured repeat/once/cron
schedules and configured payload generation never run. `debug` is a quiet sink
that can always be observed, regardless of its display toggle. Both are
recognized in strict mode. HTTP/MQTT/TCP, exec, filesystem, credentials and all
other unsupported node types become **drop-input stubs with a warning**, or a
load error in strict mode. An explicit stub works in strict mode. No third-party
nodes are discovered from cwd or installed.

Subflow definitions and instances, including nested instances, use the actual
Node-RED runtime. Instance output expectations resolve the definition's exposed
ports to their concrete internal sources, including direct-input outputs; they
do not confuse input forwarding with output. Inject/assert against a top-level
instance boundary. Direct injection/observation of a definition's internal node
id is rejected because multiple instances create different clones. An explicit
stub on an internal definition id applies to **every instance** of that
definition. Stubbing an entire instance is not supported and fails explicitly.
Recursive subflows and nesting beyond 32 levels fail preflight. Subflows have at
most one input port. See `test/fixtures/subflow.*` and nested-instance tests.

Only test assertions, not an arbitrary expression language, are interpreted by
nrflowtest. **Function JavaScript is denied unless `--allow-function` is given**;
Node-RED's VM is not a security sandbox. Even with stripped worker environment,
no injected modules, short VM execution limits and socket guards, do not opt in
for hostile/untrusted flows. Use an OS/container sandbox if you need that.
Other allowed nodes retain their normal declarative semantics (for example
Node-RED's JSONata expressions and Mustache templates).

The worker does not inherit host environment variables/credentials, preload
hooks, user settings, credential files or persisted context. Flow environment
entries must be literal `str`/`num`/`bool`/`json`; dynamic/credential entries and
inline `credentials` blocks are rejected. Use synthetic exports/messages, not
production data: diagnostic diffs intentionally contain actual test values.
Runtime setup messages emitted before deployment completes are outside the
observation window; setup errors are captured. Errors during cleanup also fail.

Limits: 5 MiB flow JSON with up to 10000 nodes; 1 MiB per YAML file and 10 MiB total;
100 patterns of at most 2048 characters, traversal depth 32; 1000 files/cases;
JSON nesting depth 64. Each case allows 10000 observed messages, 100000 input
deliveries, 8 MiB of observed JSON and 100 recorded errors. Exceeding a runtime
limit fails the case. Worker V8 heap is capped at 192 MiB. Function synchronous
execution is capped at one second; the parent's default whole-worker deadline is
10 seconds startup allowance + case timeout + 2 seconds cleanup allowance,
overridable per case with `hardTimeout`. Case timing uses a
monotonic clock, but this is not a real-time scheduler or performance benchmark.

This first release does not provide editor UI, real I/O, credential injection,
third-party node installation, binary message assertions, cross-case state,
performance tests, or a hostile-code sandbox. It depends on pinned helper/runtime
internals for instrumentation and subflow wiring; upgrading Node-RED requires
rerunning the integration tests. Node-RED's bundled npm dependency chain still
has moderate audit advisories (see `research/dependency-audit.json`); patch
overrides remove the observed high-severity issues. nrflowtest never invokes
that npm installation service or exposes the editor/HTTP server.

## Verification and provenance

`npm test` runs offline and in one test process. It executes actual child-process
CLI success/failure paths, real exported order/subflow fixtures, nested subflows,
HTTP stubs, multi-output mapping, context isolation, synchronous/asynchronous
errors, configurable hard-deadline isolation through input and cleanup hangs,
parent SIGINT/SIGTERM cleanup and truthful interrupted reports,
absence/count/timing behavior, exact retained-message equality, schema/path safety,
ordered multi-port sequences, exact JSON Pointer values and escaping,
structured mismatch attribution, missing/null distinctions and untruncated values,
and independent parsing of JUnit XML. Tests use synthetic local inputs only.
`STATUS.json` records the executed acceptance commands and results.

This is an independent Apache-2.0 project, not an official Node-RED plugin.
Node-RED and node-red-node-test-helper are Apache-2.0 runtime dependencies.
See [LICENSE](LICENSE), [NOTICE](NOTICE) and [THIRD_PARTY.md](THIRD_PARTY.md).
The supplied research documents remain unchanged for traceability.
