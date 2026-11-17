"""Property checks for evidence fidelity, not snapshots of implementation output."""

import copy
import json

from hypothesis import given, settings
from hypothesis import strategies as st
from jsonpointer import JsonPointer, resolve_pointer

from flowwrench.ha_diff import compare_traces, parse_trace

keys = st.one_of(
    st.sampled_from(
        ["a/b", "a~b", "0", "*", "['x']", "a'\"b", "\n", "\x1b[31m", "__private", "温度"]
    ),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=12),
)
scalars = (
    st.none()
    | st.booleans()
    | st.integers(-(10**30), 10**30)
    | st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=24)
)
json_values = st.recursive(
    scalars,
    lambda values: st.lists(values, max_size=4) | st.dictionaries(keys, values, max_size=4),
    max_leaves=25,
)


def payload(variables):
    return {
        "domain": "automation",
        "item_id": "property",
        "run_id": "one",
        "state": "stopped",
        "script_execution": "finished",
        "config": {"actions": []},
        "timestamp": {"start": "2026-11-11T00:00:00Z", "finish": "2026-11-11T00:00:01Z"},
        "trace": {
            "trigger/0": [
                {
                    "path": "trigger/0",
                    "timestamp": "2026-11-11T00:00:00Z",
                    "changed_variables": variables,
                }
            ]
        },
    }


def assert_references(report, left, right):
    for row in report["steps"]:
        for change in row["changes"]:
            for side, document in (("left", left), ("right", right)):
                ref = change[side]
                if ref["present"]:
                    actual = resolve_pointer(document, ref["pointer"])
                    # JSON encoding distinguishes bool/int and preserves Unicode escapes.
                    assert json.dumps(actual, sort_keys=True) == json.dumps(
                        ref["value"], sort_keys=True
                    )


@settings(max_examples=500, deadline=None, derandomize=True)
@given(json_values)
def test_arbitrary_recorded_json_compares_equal_to_itself(value):
    document = payload({"observed": value})
    original = copy.deepcopy(document)
    trace = parse_trace(document)
    report = compare_traces(trace, trace, exact=True)
    assert report["status"] == "no_recorded_difference"
    assert report["coverage"] == "recorded"
    assert document == original


@settings(max_examples=500, deadline=None, derandomize=True)
@given(keys, json_values)
def test_arbitrary_key_mutations_have_resolvable_symmetric_evidence(key, distractor):
    left = {"trace": payload({"observed": {key: [distractor, "left"]}})}
    right = copy.deepcopy(left)
    right["trace"]["trace"]["trigger/0"][0]["changed_variables"]["observed"][key][1] = "right"
    pristine_left, pristine_right = copy.deepcopy(left), copy.deepcopy(right)
    forward = compare_traces(parse_trace(left), parse_trace(right), exact=True)
    backward = compare_traces(parse_trace(right), parse_trace(left), exact=True)
    assert forward["status"] == backward["status"] == "different"
    assert forward["summary"]["field_changes"] == backward["summary"]["field_changes"] == 1
    assert forward["first_divergence"] == backward["first_divergence"] == "trigger/0#0"
    assert_references(forward, left, right)
    assert_references(backward, right, left)
    assert left == pristine_left and right == pristine_right


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.lists(json_values, min_size=2, max_size=8), st.integers(0, 6))
def test_ignored_array_slots_preserve_other_original_indices(values, seed):
    ignored = seed % len(values)
    changed = (ignored + 1) % len(values)
    left = payload({"array": values})
    right = copy.deepcopy(left)
    right["trace"]["trigger/0"][0]["changed_variables"]["array"][ignored] = "ignored mutation"
    right["trace"]["trigger/0"][0]["changed_variables"]["array"][changed] = {
        "nonignored-witness": "always unique"
    }
    report = compare_traces(
        parse_trace(left),
        parse_trace(right),
        ignored_fields=(f"/changed_variables/array/{ignored}",),
    )
    assert report["status"] == "different"
    assert_references(report, left, right)
    changes = report["steps"][0]["changes"]
    assert all(
        not change["field"].startswith(f"/changed_variables/array/{ignored}/") for change in changes
    )
    assert any(
        change["field"].startswith(f"/changed_variables/array/{changed}") for change in changes
    )


@settings(max_examples=500, deadline=None, derandomize=True)
@given(keys, json_values)
def test_filtered_container_shells_preserve_added_leaf_presence(key, actor):
    left = payload({"custom": {key: {"id": "generated"}}})
    right = copy.deepcopy(left)
    right["trace"]["trigger/0"][0]["changed_variables"]["custom"][key]["actor"] = actor
    ignored = str(JsonPointer.from_parts(["changed_variables", "custom", key, "id"]))
    report = compare_traces(parse_trace(left), parse_trace(right), ignored_fields=(ignored,))
    changes = report["steps"][0]["changes"]
    assert len(changes) == 1
    expected = str(JsonPointer.from_parts(["changed_variables", "custom", key, "actor"]))
    assert changes[0]["field"] == expected
    assert not changes[0]["left"]["present"]
    assert changes[0]["right"]["present"]
    assert_references(report, left, right)
