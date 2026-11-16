# flowwrench windmill-dryrun

`flowwrench windmill-dryrun` runs offline mapping and control-flow tests for **Windmill OpenFlow** files kept in git. Catch a wrong `input_transforms` field, an unreachable branch, a missing mock or a runaway loop before you start a workspace job. Every script and subflow result is mocked: workflow source code is never executed and no Windmill API is contacted.

The command was previously the standalone Node.js tool `openflow-dryrun`. It is based on the [OpenFlow specification v1.868.0](https://github.com/windmill-labs/windmill/blob/v1.868.0/openflow.openapi.yaml) and complements `wmill lint`: it is a bounded simulator with assertable runtime values, not a replacement for upstream schema validation or a worker emulator. Upstream relationships are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer (with PyYAML, installed automatically) and **Node.js 20 or newer on `PATH`**. OpenFlow expressions are JavaScript, so each one is evaluated by a sandboxed `node` process; `run`, `test` and `replay` exit 2 with `windmill-dryrun needs Node.js 20+ to evaluate JavaScript expressions` when no suitable `node` is found. Only `--help` and `--version` work without it. Linux is the tested platform.

```sh
pip install flowwrench                   # or: pipx install flowwrench
node --version                           # v20 or newer
flowwrench windmill-dryrun --version     # prints the bare version, e.g. 0.1.0
```

`python -m flowwrench windmill-dryrun` is equivalent to `flowwrench windmill-dryrun`. The synthetic examples below live in `examples/windmill_dryrun/` in the flowwrench repository; run the commands from the repository root (or adjust the paths).

## Try it

```sh
flowwrench windmill-dryrun run examples/windmill_dryrun/order-flow/flow.yaml \
  --input examples/windmill_dryrun/order-flow/input.json \
  --mocks examples/windmill_dryrun/order-flow/mocks.yaml
flowwrench windmill-dryrun test examples/windmill_dryrun/order-flow/cases/*.yaml
```

The run prints JSON with `status: "ok"`, the final result `{"outcome":{"sent":true}}`, resolved arguments, a per-attempt trace and coverage. The test command prints one `PASS`/`FAIL` line per case and then:

```text
4 cases, 4 passed, 0 failed; modules 8/8 executed, branches 3/3
```

## Commands and options

```text
flowwrench windmill-dryrun run FLOW --input in.json [--mocks mocks.yaml] [--trace out.json]
flowwrench windmill-dryrun test CASES... [--junit results.xml] [--report suite.json] [--bundle saved.json]
flowwrench windmill-dryrun replay BUNDLE --case N [--junit results.xml] [--report suite.json]
```

| Option (`run` and `test`) | Default and range |
| --- | --- |
| `--unmocked error\|null\|echo-args` | `error`; explicit fallback for script, rawscript and flow steps only |
| `--max-iterations N` | 100, range 1–10000, per-loop ceiling |
| `--timeout-ms N` | 100, range 1–2000, per expression |
| `--max-steps N` | 1000, range 1–10000; attempts and skipped events count |
| `--require-all-mocks-used true\|false` | `false`; fail after execution if an external mock sequence has unused responses |
| `--output-dir DIR` | Current directory; confines every requested output file (also accepted by `replay`) |

Flow and mock files are YAML or JSON; `--input` is a JSON object. Flow files need the OpenFlow wrapper `summary: ...` and `value: {modules: [...]}`. Flags also accept `--name=value`. Cases are file arguments (your shell expands globs), and paths inside a case resolve relative to that case file. CLI options override a case's `options`.

Exit codes: **0** successful run, or all assertions pass (including expected-error cases); **1** runtime mapping/control/mock error or assertion mismatch; **2** invalid input, unsupported fields, invalid CLI usage, missing Node.js or output I/O failure. Errors carry a module id; no host stack traces are printed.

`run` always prints the report, including on runtime failure, and `--trace` writes the same JSON. `test --report` includes all reports and failed-assertion diffs, and `--junit` writes escaped JUnit XML for CI. Coverage unions executed module ids and selected branch ids across cases, per canonical flow path; a default arm counts as a branch even if empty. Coverage shows which paths ran, not that they are correct.

Output files must be **new**, in existing directories beneath `--output-dir`. Existing files and symlink leaves are rejected, and parent symlinks are resolved and checked for escape. Output paths come only from CLI arguments. Reports contain your inputs, arguments and mock values: use synthetic data in CI.

## Flows, mocks and test cases

An OpenFlow script step maps its inputs like this:

```yaml
summary: Notify a customer
value:
  modules:
    - id: notify
      value:
        type: script
        path: f/demo/notify
        input_transforms:
          customer: {type: javascript, expr: flow_input.customer}
          channel: {type: static, value: email}
```

Mock files map module ids to explicit outcomes. A single outcome repeats; a `sequence` consumes exactly one outcome per invocation or attempt across loops, branches and retries, and running out fails instead of reusing the last value:

```yaml
notify: {echo_args: true}
# or: notify: {result: {sent: true}}
# or a deterministic transient failure followed by success:
# notify:
#   sequence:
#     - error: {name: Temporary, message: retry me}
#     - result: {sent: true}
```

A sequence response may add `expect_args: {...}` to check its fully resolved arguments by exact deep JSON equality; a mismatch consumes the response, records `MockArgsMismatch` in `mock_errors` and fails the run.

A test case inlines or references its flow, input and mocks, and needs at least one assertion:

```yaml
name: customer notification arguments
flow: flow.yaml
input: {customer: alice}
mocks:
  notify: {echo_args: true}
options: {maxIterations: 20, timeoutMs: 100}
expect:
  status: ok
  args:
    notify: [{customer: alice, channel: email}]
  results:
    notify: {customer: alice, channel: email}
  path: [notify]
  result: {customer: alice, channel: email}
```

All comparisons use exact deep JSON equality (object key order is irrelevant; list order, counts and types matter). Unknown expectation keys or unknown module ids are format errors (exit 2), never passing assertions. A runtime failure fails a case unless `status: error` or `error` is expected.

| `expect` key | Asserts |
| --- | --- |
| `status`, `result`, `stopped`, `failure_result` | Final run status, root result, stop flag, failure-handler result |
| `args.<id>` | Exact list of fully resolved argument objects, including retries |
| `results.<id>` | Last successful or continued result of that module |
| `path` | Exact module invocation order, containers before children |
| `error`, `failure_error` | Supplied fields of the primary / failure-handler error: `name`, `message`, `step_id`, `call`, `expected_args`, `actual_args` |
| `simulated_delay_seconds` | Exact total of scheduled retry delays (no sleeping) |
| `retry_delays` | Ordered `{module_id, attempt, retry_delay_seconds}` records |
| `attempt_errors`, `attempt_results` | Ordered `{module_id, attempt, error\|result}` from every trace event that owns one |
| `branch_choices` | Ordered `{module_id, attempt, branch}` selections (`'0'`, `'1'`, …, `default`) |
| `skipped_steps` | Ordered `{module_id, attempt, path}` of events with `status: skipped` |
| `mock_calls` | Ordered `{module_id, source, call}` of every mock lookup |
| `mock_usage.<id>` | `{expected, calls, consumed, remaining, extra}` of an external sequence |
| `mock_errors` | Every collected mock contract error (`MockArgsMismatch`, `MockExhausted`, `MockUnused`), in order |

Assertions never change runtime behavior: list-style assertions with a wrong shape exit 2, valid but different values fail with `<key>: expected <full value>, got <full value>` (exit 1) in the console, JSON, JUnit and replay. More runnable cases:

```sh
flowwrench windmill-dryrun test examples/windmill_dryrun/retry-delay/cases/*.yaml       # exit 0
flowwrench windmill-dryrun test examples/windmill_dryrun/mock-consumption/cases/*.yaml  # exit 0, including expected failures
flowwrench windmill-dryrun run examples/windmill_dryrun/mock-consumption/flow.yaml \
  --input examples/windmill_dryrun/mock-consumption/input.json \
  --mocks examples/windmill_dryrun/mock-consumption/mocks.yaml --require-all-mocks-used true
```

The last run exits 0 with `[{"sent":"alice"},{"sent":"bob"}]` and `mock_usage.send` equal to `{"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}`. With `input-one.json` instead, the gate fails with `MockUnused` at `send` (consumed 1 of 2, exit 1).

## Save and replay case diagnostics

`test --bundle saved.json` checkpoints every completed case (passing, failing or expected-error) into one self-contained JSON bundle **before loading the next case**. Each case stores the resolved flow, input, mocks, assertions and effective options, SHA-256 hashes of those parsed values and of the outcome, the full report with assertion diffs, selected branches, mock calls and a JUnit link. `--bundle` also writes `saved.json.junit.xml` unless `--junit` names another path; the same XML is embedded in the bundle.

```sh
mkdir -p .test-tmp/diagnostics
flowwrench windmill-dryrun test examples/windmill_dryrun/diagnostics/failure.yaml \
  --bundle .test-tmp/diagnostics/saved.json
# Exit 1: result: expected 99, got 7
flowwrench windmill-dryrun replay .test-tmp/diagnostics/saved.json --case 1 \
  --report .test-tmp/diagnostics/replay.json --junit .test-tmp/diagnostics/replay.xml
# Exit 1: same assertion failure, plus "REPLAY case 1: reproduced"
```

Copying **only the JSON bundle** to another machine with flowwrench and Node.js is enough to rerun that case offline: the original case, flow and mock paths are metadata and are never loaded. Replay reruns the selected case from the beginning with the saved options (execution overrides are rejected) and compares the complete new outcome with the saved one. A reproduced failure exits 1 and a reproduced pass exits 0; changed behavior adds `ReplayMismatch` and exits 1. Invalid selectors, hash mismatches, unsupported versions and malformed bundles exit 2.

The bundle's `checkpoint` records `total`, `completed` and `complete`. If a later case is invalid, the command exits 2, keeps the completed prefix and records `terminal_error: {case, path, message}`. Each update is written to a mode-0600 temporary file and installed atomically, so an interruption cannot truncate the previous bundle (an interrupted write may leave a `.openflow-dryrun-*.tmp` file). Bundles are capped at 64 MiB.

The bundle format (`openflow-dryrun-bundle` v1, tool `openflow-dryrun` 0.1.0, OpenFlow v1.868.0) is unchanged from the original tool, so bundles replay with either implementation. It records the Node.js version used (or `unknown`); use the same Node release for the closest reproduction. Expressions that depend on time, randomness or timing may differ, and replay reports that rather than claiming a reproduction. Bundles keep the supplied values and have no redaction.

## Supported simulation semantics

| Construct | Behavior |
| --- | --- |
| `rawscript`, `script`, `flow` | Resolve `value.input_transforms` and return the mock; path, lock and source content are never loaded |
| `identity` | Return `previous_result`; `value.flow: true` returns the current `flow_input` |
| `branchone` | The first boolean-true predicate wins, otherwise `default`; returns the selected body's last result |
| `branchall` | Run every branch in order and return their results; `skip_failure` gives null for a failed branch |
| `forloopflow` | Sequential body over an array; `flow_input.iter = {index, value}`; returns the last body results |
| `whileloopflow` | Repeat until stopped; the limit is min(`--max-iterations`, `value.max_iterations`) |
| `skip_if` | Boolean predicate before mapping; no mock is consumed |
| `stop_after_if`, `stop_after_all_iters_if` | Stop conditions after a step, a loop iteration or the collected loop results |
| `continue_on_error` | After retries, produce `{error: {name, message, step_id}}` and continue |
| Loop `skip_failures` | Record failed iterations as null and continue |
| `failure_module` | Root failure handler (`failure`, script/rawscript) receiving `error`; the original run stays failed |
| `retry` | Constant, then exponential attempt budgets with optional boolean `retry_if`; delays are computed, never slept |

Expressions see `flow_input`, `results`, `previous_result`, `result`, `error` and standard JavaScript intrinsics. A reference to an **unknown result id always throws**, even `results.typo?.value`; a known id that is not in scope is undefined, so use `??` or optional chaining for intentional fallbacks. Predicates must return booleans. `$res:...` and `$var:...` strings stay literal.

## Boundaries and limitations

This is a deliberately limited OpenFlow subset. Unsupported behavioral fields fail before execution: approvals/suspend, sleep, preprocessors, parallel execution, squash, duration-based loops, AI transforms and modules, cache, concurrency, worker and security settings, scheduling and server resource resolution. Parallel configurations must be adapted to sequential ones in a test copy; they are never serialized silently. Schema metadata is accepted but input JSON Schema validation is not performed.

There is no Windmill worker parity, durable jobs, real error stacks, network or script execution, API integration or filesystem includes. Module ids must be globally unique and use only letters, digits, `_` and `-`. Keys `__proto__`, `constructor` and `prototype`, YAML aliases and custom tags, duplicate keys and multi-document YAML are rejected; YAML is read with YAML 1.2 core-schema rules. JSON data must be finite and at most 64 levels deep. Source files are capped at 1 MiB, expressions at 16 KiB and flows at 1000 modules and 32 nesting levels. Per-expression scope and result are capped at 1 MiB, and the accumulated trace at 16 MiB.

Each expression runs in `node:vm` inside a fresh `node` process with a 64 MiB heap, Node permissions denying filesystem, child-process and worker access, code generation disabled, no host objects, a minimal environment (no inherited credentials), a VM timeout and an outer process deadline. No `process`, `require`, fetch or timers are available; async results and dynamic imports fail. This is defense in depth for local test expressions, not OS-level isolation against unknown Node.js or V8 vulnerabilities: use a patched Node release. Starting one process per expression favors isolation over speed.

## Tests

```sh
uv run --extra test pytest tests/windmill_dryrun
```

The suite needs Node.js 20 or newer on `PATH` for expression evaluation and runs offline with synthetic fixtures.

## License

Apache-2.0; see [LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md). flowwrench is not affiliated with or endorsed by Windmill Labs. No AGPL or proprietary Windmill server, worker or CLI code is copied, required or executed.
