# Library and report integration

`flowwrench ha-diff` is also available as a Python library (install `flowwrench[ha]`). It does not import
Home Assistant, evaluate templates or make network requests.

```python
from flowwrench.ha_diff import compare_traces, load_trace
from flowwrench.ha_diff.render import render_html

left = load_trace("examples/ha-diff/motion-success.json")
right = load_trace("examples/ha-diff/motion-blocked.json")
report = compare_traces(left, right)

assert report["first_divergence"] == "trigger/0#0"
assert report["first_control_divergence"] == "condition/0#0"
html = render_html(report)
```

`load_trace(path, select="/data/automation.example/0")` selects one record from
an enclosing JSON object using an RFC 6901 pointer. `parse_trace(document,
source="name.json", select="...")` accepts an already-decoded object. Do not
mutate that object during comparison: references point to the original data.
File loading includes a byte SHA-256; the in-memory API has no original byte
stream, so its digest is null.

`compare_traces(left, right, exact=False, ignored_fields=(...),
align_repeats=True)` accepts exact per-step ignore pointers. `exact=True`
includes generated context/child run IDs; it still excludes structural step
paths and timestamps from the field diff. `align_repeats=False` explicitly uses
uncertain ordinal alignment for loop paths. Every effective policy is emitted
in the report.

## Interpreting the contract

The report format is [ha-trace-diff/v1](report.schema.json). Consumers should
inspect `status` and `coverage` together:

| Field | Meaning |
| --- | --- |
| `status: different` | At least one compared recorded field or reached step differs. |
| `status: no_recorded_difference` | All compared evidence matched within the stated policy and budgets. It does not establish identical device behavior. |
| `status: inconclusive` | No confirmed difference was retained, but empty/incomplete/limited evidence prevents a full comparison. |
| `coverage: partial` | Input completion is unconfirmed, a trace has no steps, or a safety budget stopped comparison. |
| `first_divergence: null` | No unique earliest step is established. This can mean no step difference, uncertain time/alignment, or partial coverage; inspect candidates and warnings. |
| `comparison_complete: false` | This row was not fully compared. Never treat its empty changes array as equality. |

`first_control_divergence` considers recorded conditions, branch choices, errors
and reached-path differences separately from variable/trigger data. It is not
a root-cause claim. A service-argument difference remains a result-field
difference and is visible in its step; the field categories state exactly which
evidence was compared.

Changed-variable maps are deltas emitted by Home Assistant. An absent key means
“not recorded at this step”, **not** “undefined in the execution engine”. No
global snapshot is reconstructed across nested or parallel scopes.

## Checking original references

Individual changes include raw values plus original source pointers. A
`compared_value`, when present, shows the value after filtering; `value` always
comes from the unmodified input. Aligned-row references omit redundant values.
For example:

```python
from jsonpointer import resolve_pointer

documents = {"left": left.document, "right": right.document}
for step in report["steps"]:
    for change in step["changes"]:
        for side in ("left", "right"):
            ref = change[side]
            if ref["present"]:
                assert resolve_pointer(documents[side], ref["pointer"]) == ref["value"]
```

`present: false` has no pointer or value. It distinguishes absent recorded
evidence from JSON null. Repeat anchors can point to different raw array
ordinals on each side; never reconstruct source pointers from a displayed row
ID. Use the emitted pointer and verify the file digest if the input may have
changed since the report was generated.

The CLI's normal exit 0 means a report was produced. In automation, use `--check`:
0 means no recorded difference, 1 means a difference, 2 means invalid input/I/O
failure, and 3 means partial evidence. Exit 3 takes precedence over exit 1.

