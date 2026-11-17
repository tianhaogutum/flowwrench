# flowwrench ha-diff

An automation worked yesterday and failed today, or a script takes a different branch on one run: Home Assistant's trace UI shows one run at a time, so finding what actually changed between two runs means reading two JSON trees side by side. `flowwrench ha-diff` (formerly `ha-trace-diff`) compares two downloaded Home Assistant automation or script traces offline and reports the recorded differences in trigger data, changed variables, condition results, branch choices, errors, service parameters and reached paths, each with an RFC 6901 pointer into its original file.

Use it when you have a good and a bad run of the same automation or script and want the first recorded divergence, or in a test pipeline to check that two saved traces still agree. It is a thin adapter around [DeepDiff](https://github.com/seperman/deepdiff) 8.6 and [python-json-pointer](https://github.com/stefankoegl/python-json-pointer). No Home Assistant credentials, connection or engine is needed, and nothing is uploaded.

## Quick start

`ha-diff` needs the `ha` extra (DeepDiff and jsonpointer, both pure Python):

```sh
pip install 'flowwrench[ha]'
```

The example inputs live in [`examples/ha-diff/`](../examples/ha-diff/), so run these from a clone of the repository:

```sh
flowwrench ha-diff examples/ha-diff/motion-success.json examples/ha-diff/motion-blocked.json
flowwrench ha-diff examples/ha-diff/motion-success.json examples/ha-diff/motion-blocked.json \
  --format html --output comparison.html
flowwrench ha-diff examples/ha-diff/theme-success.json examples/ha-diff/theme-error.json \
  --format json --output comparison.json
```

In Home Assistant, open the automation's Traces, select the successful run, use the menu to download its JSON, and repeat for the failing run. Then pass the files as `LEFT RIGHT`.

`python -m flowwrench ha-diff` is equivalent to `flowwrench ha-diff`. `--version` prints the bare flowwrench version.

## Options

| Option | Meaning |
| --- | --- |
| `LEFT`, `RIGHT` | Baseline/successful and comparison/failing trace JSON files |
| `--left-pointer`, `--right-pointer` | RFC 6901 pointer selecting one trace inside each file (default: the whole document) |
| `--format text\|json\|html` | Report format (default `text`) |
| `--output PATH` | Write the report to a file instead of stdout |
| `--exact` | Also compare generated context and child run IDs |
| `--ordinal-repeats` | Disable directly recorded repeat-index anchors; use uncertain occurrence ordinals |
| `--ignore-field POINTER` | Ignore an exact field pointer relative to every step (repeatable) |
| `--show-equal` | Include equal rows in the text report |
| `--check` | Exit 1 on differences, 3 on partial evidence |

`--check` returns 0 for no recorded difference, 1 for differences, 2 for invalid input/I/O errors, and 3 for partial evidence (takes precedence over differences). Without `--check`, a completed report returns 0, including reports containing differences. Input and I/O errors always exit 2 with a `flowwrench ha-diff: ...` message on stderr.

The motion example is synthetic and controlled: lux changes from 12 to 125, the recorded condition changes from true to false, and the light action is absent. The report highlights the first variable difference and separately identifies the first condition/branch/error/path difference. The theme example uses a real upstream failing trace plus an explicitly counterfactual successful variant; see [examples/ha-diff/PROVENANCE.md](../examples/ha-diff/PROVENANCE.md). [`examples/ha-diff/motion-comparison.html`](../examples/ha-diff/motion-comparison.html) is the HTML report for the motion pair.

HTML reports are self-contained and script-free. Open the saved file directly; they load no resources or original files. Output files are written atomically with private permissions; failures preserve existing reports, and input aliases/hardlinks cannot be overwritten.

The JSON report format (`ha-trace-diff/v1`) is documented by [report.schema.json](ha-diff/report.schema.json) (JSON Schema 2020-12) and validated in the test suite. The [library integration guide](ha-diff/API.md) explains status, coverage and original-field references.

## Formats and integration

Accepts native downloads (`{"trace": FULL_TRACE, ...}`), raw `trace/get` objects, successful WebSocket result envelopes, and a single saved-trace record (`{"extended_dict": FULL_TRACE, ...}`). To choose one record from a Home Assistant saved-traces storage file:

```sh
flowwrench ha-diff tests/ha_diff/fixtures/upstream/automation_saved_traces.json \
  tests/ha_diff/fixtures/upstream/automation_saved_traces.json \
  --left-pointer /data/automation.moon/0 --right-pointer /data/automation.moon/1
```

All original envelope segments remain in emitted pointers. Use `jsonpointer.resolve_pointer(original_document, reference["pointer"])` to retrieve the exact reported value. Aligned row references omit redundant full-step values; individual changes include their raw values. File inputs include SHA-256 digests to bind references to the exact bytes inspected. Missing fields have `present: false` and no pointer; missing is different from JSON `null`.

Home Assistant's native raw downloads work directly. Automation Testkit's downloaded fixture wrapper also works. HA Trace Viewer's privacy-reviewed summary export deliberately excludes variables and service arguments; use native raw downloads instead. This command complements HA Trace Viewer's timing comparison and CCA Trace Compare's blueprint summaries; see [prior art](ha-diff/PRIOR-ART.md).

## Comparison rules and limits

- Same domain and item_id required; successful/failed status comes from recorded fields and is never guessed.
- Exact paths align with directly recorded, unique, increasing repeat.index anchors for a single loop when available; otherwise they use zero-based occurrence ordinals. Nested or unanchored repeats are explicitly uncertain. No fuzzy action matching across configuration edits. See [alignment rules](ha-diff/ALIGNMENT.md).
- Changed steps link to their raw configuration and aliases across legacy/modern formats; unsupported paths retain an explicit unresolved suffix. See [configuration rules](ha-diff/CONFIGURATION.md). Configuration and blueprint inputs are compared separately. Configuration changes warn that a matching path can refer to a different action.
- First divergence uses recorded step start timestamps in each run. Equal/non-monotonic timestamps, missing timestamps, uncertain iteration alignment or disagreement between runs do not produce an invented unique first step. It is an observed difference, not proof of causality or the instant a result was computed.
- Variable maps are compared as recorded deltas. No global state or execution engine is reconstructed. State timestamps, trigger IDs, user IDs and application variables named `id`/`timestamp` remain meaningful evidence.
- By default, only step path/timestamp and generated `id`/`parent_id` in known HA context locations, plus child run IDs, are ignored. The same generated-ID rule applies to the run context and recorded wait-trigger contexts; actor user IDs remain compared. Unknown run-level extensions are listed as outside the comparison scope, while unknown step fields are compared raw. `--exact` includes generated IDs. `--ignore-field /changed_variables/this/last_updated` explicitly ignores one exact per-step field; repeat as needed. The report records the entire policy.
- Empty runs, unknown completion states and missing/invalid/reversed run time bounds produce partial coverage. A missing step means not recorded in that file, not proof of the reason it did not run. Traces alone cannot establish device behavior or evaluate templates.
- UTF-8 JSON only, 16 MiB per input, 20,000 recorded steps, 80 nesting levels, 4,096 characters / 160 segments per step path, 10,000 DeepDiff operations per compared object, 5,000 retained changes globally, 1,000 digits per integer, 250,000 JSON objects, 1,000,000 object members and 2,000,000 JSON nodes (including object keys). Unexamined rows are explicit and partial evidence cannot produce a unique first-divergence claim. Duplicate keys, non-finite numbers and wrong shapes are rejected. Files are read locally; reports can contain the same private data as the inputs.

## Measured bounds

A synthetic 20,000-step trace with 20 variables per step (12.4 MB per input) took about 4.3 seconds in the comparison phase and 1.9 seconds in validation/adaptation, with about 158 MiB peak RSS on one development Mac. Its final compact JSON report is about 9.0 MB. These measurements use in-memory generated objects rather than end-to-end file I/O; the complete file-to-report CLI path for the same workload took about 8.4 seconds and 237 MiB peak RSS, including JSON decoding, hashing, comparison, pretty JSON output and fsync. A dense-change workload stops retaining changes at the documented budget and reports partial coverage. Measurements are not cross-machine guarantees.

## Tests and fixtures

Tests under `tests/ha_diff/` include unmodified upstream Home Assistant saved traces (Apache-2.0) and Automation Testkit's public trace fixture (GPL-3.0); see [fixture provenance](../tests/ha_diff/fixtures/PROVENANCE.md). The property tests use [Hypothesis](https://hypothesis.readthedocs.io/) and the report-contract tests use jsonschema; both are in the `test` extra. No Home Assistant, browser, Docker, watcher or dev server is started.

## License

`ha-diff` was originally released under the MIT license; that notice is kept in [licenses/ha-trace-diff-MIT.txt](../licenses/ha-trace-diff-MIT.txt). Upstream fixture licenses and notices apply to those files. See [THIRD_PARTY.md](../THIRD_PARTY.md#flowwrench-ha-diff-formerly-ha-trace-diff).
