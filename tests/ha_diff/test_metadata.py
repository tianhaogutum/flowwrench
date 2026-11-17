import copy

import pytest

from flowwrench.ha_diff import TraceError, compare_traces, parse_trace


def compare(left, right, **options):
    return compare_traces(parse_trace(left), parse_trace(right), **options)


def test_run_context_actor_is_compared_while_generated_ids_are_ignored(trace):
    left = copy.deepcopy(trace)
    right = copy.deepcopy(trace)
    right["context"]["id"] = "another-id"
    right["context"]["parent_id"] = "another-parent"
    assert compare(left, right)["status"] == "no_recorded_difference"
    right["context"]["user_id"] = "another-user"
    report = compare(left, right)
    assert report["summary"]["run_changes"] == 1
    change = report["run_changes"][0]
    assert change["field"] == "/context/user_id"
    assert change["right"]["pointer"] == "/context/user_id"
    assert compare(left, right, exact=True)["summary"]["run_changes"] == 3


@pytest.mark.parametrize("location", ["changed_variables", "result"])
def test_wait_trigger_context_policy_preserves_actor_identity(trace, location):
    left = copy.deepcopy(trace)
    left["trace"]["action/0"][0][location]["wait"] = {
        "trigger": {"event": {"context": {"id": "one", "parent_id": None, "user_id": "user"}}}
    }
    right = copy.deepcopy(left)
    context = right["trace"]["action/0"][0][location]["wait"]["trigger"]["event"]["context"]
    context["id"] = "two"
    assert compare(left, right)["status"] == "no_recorded_difference"
    context["user_id"] = "other-user"
    assert compare(left, right)["summary"]["field_changes"] == 1


def test_unknown_field_named_unfinished_does_not_change_coverage(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["unfinished"] = "custom data, not run state"
    report = compare(trace, right)
    assert report["coverage"] == "recorded"
    assert report["first_divergence"] == "action/0#0"
    assert any("Unknown step fields" in warning for warning in report["warnings"])


@pytest.mark.parametrize("state", [None, "future-state"])
def test_missing_or_unknown_run_completion_state_is_partial(trace, state):
    if state is None:
        trace.pop("state")
    else:
        trace["state"] = state
    report = compare(trace, trace)
    assert report["coverage"] == "partial"
    assert report["status"] == "inconclusive"


@pytest.mark.parametrize(
    "key,value",
    [("state", {}), ("script_execution", []), ("not_triggered", 0), ("timestamp", "bad")],
)
def test_malformed_run_metadata_is_a_clean_input_error(trace, key, value):
    trace[key] = value
    with pytest.raises(TraceError):
        parse_trace(trace)


def test_actor_added_to_an_id_only_context_references_the_actor_field(trace):
    left = copy.deepcopy(trace)
    left["context"] = {"id": "generated"}
    right = copy.deepcopy(left)
    right["context"]["user_id"] = "actor"
    report = compare(left, right)
    change = report["run_changes"][0]
    assert change["field"] == "/context/user_id"
    assert not change["left"]["present"]
    assert change["right"]["pointer"] == "/context/user_id"


def test_only_generated_variables_do_not_create_a_container_presence_difference(trace):
    left = copy.deepcopy(trace)
    left["trace"]["action/0"][0].pop("changed_variables")
    right = copy.deepcopy(left)
    right["trace"]["action/0"][0]["changed_variables"] = {"context": {"id": "generated"}}
    assert compare(left, right)["status"] == "no_recorded_difference"


def test_genuine_empty_application_container_is_still_recorded(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["changed_variables"]["application_empty"] = {}
    report = compare(trace, right)
    change = report["steps"][1]["changes"][0]
    assert change["field"] == "/changed_variables/application_empty"
    assert change["right"]["value"] == {}


@pytest.mark.parametrize(
    "finish", [False, "not-a-time", "2026-10-07T04:57:43", "2020-01-01T00:00:00Z"]
)
def test_invalid_or_reversed_final_timestamp_cannot_confirm_complete_coverage(trace, finish):
    right = copy.deepcopy(trace)
    right["timestamp"]["finish"] = finish
    right["trace"]["action/0"][0]["error"] = "changed"
    report = compare(trace, right)
    assert report["coverage"] == "partial"
    assert report["first_divergence"] is None
