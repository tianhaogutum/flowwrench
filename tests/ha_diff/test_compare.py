import copy

import pytest
from jsonpointer import resolve_pointer

from flowwrench.ha_diff import TraceError, compare_traces, parse_trace


def compare(left, right, **options):
    return compare_traces(
        parse_trace(left, source="left.json"), parse_trace(right, source="right.json"), **options
    )


def assert_all_references(report, left, right):
    def check(change):
        for side, original in (("left", left), ("right", right)):
            ref = change[side]
            if ref["present"]:
                actual = resolve_pointer(original, ref["pointer"])
                if "value" in ref:
                    assert actual == ref["value"]
                else:
                    assert isinstance(actual, dict)
            else:
                assert ref["pointer"] is None
                assert "value" not in ref

    for key in ("configuration_changes", "run_changes"):
        for change in report[key]:
            check(change)
    for row in report["steps"]:
        check(row)
        for change in row["changes"]:
            check(change)


def test_real_download_compares_service_arguments(testkit):
    other = copy.deepcopy(testkit)
    other["trace"]["run_id"] = "other-run"
    other["trace"]["trace"]["action/0"][0]["result"]["params"]["service_data"]["name"] = "night"
    report = compare(testkit, other)
    assert report["first_divergence"] == "action/0#0"
    assert report["summary"]["field_changes"] == 1
    change = report["steps"][1]["changes"][0]
    assert change["field"] == "/result/params/service_data/name"
    assert change["left"]["value"] == "waves"
    assert_all_references(report, testkit, other)


def test_recorded_conditions_and_unreached_paths(trace):
    left = copy.deepcopy(trace)
    step = {
        "path": "condition/0",
        "timestamp": "2026-10-07T04:57:43.847300+00:00",
        "result": {"result": True, "state": "on", "wanted_state": "on"},
    }
    left["trace"]["condition/0"] = [step]
    right = copy.deepcopy(left)
    right["trace"]["condition/0"][0]["result"]["result"] = False
    del right["trace"]["action/0"]
    right["script_execution"] = "failed_conditions"
    report = compare(left, right)
    assert report["first_divergence"] == "condition/0#0"
    assert report["summary"]["different_rows"] == 2
    assert report["steps"][1]["changes"][0]["category"] == "condition"
    missing = report["steps"][2]["changes"][0]
    assert missing["kind"] == "step_not_recorded_right"
    assert missing["right"]["present"] is False
    assert_all_references(report, left, right)


def test_timestamps_and_generated_context_ids_are_not_semantic_noise(trace):
    right = copy.deepcopy(trace)
    right["run_id"] = "another"
    for entries in right["trace"].values():
        for step in entries:
            step["timestamp"] = step["timestamp"].replace("2026-10-07", "2026-10-08")
            context = step.get("changed_variables", {}).get("context")
            if context:
                context["id"] = "different"
                context["parent_id"] = "different-parent"
    report = compare(trace, right)
    assert report["status"] == "no_recorded_difference"
    assert report["first_divergence"] is None
    exact = compare(trace, right, exact=True)
    assert exact["status"] == "different"


def test_application_ids_and_timestamps_remain_evidence(trace):
    right = copy.deepcopy(trace)
    right["trace"]["trigger/0"][0]["changed_variables"].update(
        {"id": "meaningful", "timestamp": "application-value", "__private": True}
    )
    report = compare(trace, right)
    fields = {change["field"] for row in report["steps"] for change in row["changes"]}
    assert fields >= {
        "/changed_variables/id",
        "/changed_variables/timestamp",
        "/changed_variables/__private",
    }
    assert_all_references(report, trace, right)


def test_missing_is_distinct_from_null(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["changed_variables"]["response"] = None
    report = compare(trace, right)
    change = report["steps"][1]["changes"][0]
    assert not change["left"]["present"]
    assert change["right"]["present"] and change["right"]["value"] is None


def test_boolean_is_not_a_number(trace):
    left = copy.deepcopy(trace)
    left["trace"]["trigger/0"][0]["changed_variables"]["answer"] = 1
    right = copy.deepcopy(left)
    right["trace"]["trigger/0"][0]["changed_variables"]["answer"] = True
    report = compare(left, right)
    assert report["summary"]["field_changes"] == 1
    assert report["steps"][0]["changes"][0]["kind"] == "type_changes"


def test_array_indexes_and_escaped_json_pointer_keys(trace):
    left = copy.deepcopy(trace)
    left["trace"]["action/0"][0]["changed_variables"]["rooms/a~b"] = ["kitchen", "bedroom"]
    right = copy.deepcopy(left)
    right["trace"]["action/0"][0]["changed_variables"]["rooms/a~b"] = ["hall", "bedroom", "office"]
    report = compare(left, right)
    assert report["summary"]["field_changes"] == 2
    assert_all_references(report, left, right)
    assert {change["field"] for change in report["steps"][1]["changes"]} == {
        "/changed_variables/rooms~1a~0b/0",
        "/changed_variables/rooms~1a~0b/2",
    }


def test_configuration_change_is_separate_and_visible(trace):
    right = copy.deepcopy(trace)
    right["config"]["actions"][0]["data"]["name"] = "different-config"
    report = compare(trace, right)
    assert report["status"] == "different"
    assert report["summary"]["different_rows"] == 0
    assert report["first_divergence"] is None
    assert report["configuration_changes"][0]["field"] == "/config/actions/0/data/name"
    assert any("Configuration differs" in warning for warning in report["warnings"])
    assert_all_references(report, trace, right)


def test_repeated_occurrences_are_not_collapsed(trace):
    left = copy.deepcopy(trace)
    path = "action/0/repeat/sequence/0"
    left["trace"][path] = [
        {
            "path": path,
            "timestamp": f"2026-10-07T04:57:43.847{n}00+00:00",
            "changed_variables": {"repeat": {"index": n + 1}, "sensor": n},
        }
        for n in range(3)
    ]
    right = copy.deepcopy(left)
    right["trace"][path][2]["changed_variables"]["sensor"] = 99
    report = compare(left, right)
    assert report["first_divergence"] == f"{path}@repeat=3"
    assert next(row for row in report["steps"] if row["changes"])["alignment"]["repeat_index"] == 3
    assert_all_references(report, left, right)


def test_parallel_reordering_does_not_invent_a_first_divergence(trace):
    left = copy.deepcopy(trace)
    left["trace"] = {
        path: [
            {
                "path": path,
                "timestamp": f"2026-10-07T04:57:43.847{n}00+00:00",
                "result": {"result": True},
            }
        ]
        for n, path in enumerate(
            ("action/0/parallel/0/sequence/0", "action/0/parallel/1/sequence/0")
        )
    }
    right = copy.deepcopy(left)
    for n, entries in enumerate(reversed(list(right["trace"].values()))):
        entries[0]["timestamp"] = f"2026-10-07T04:57:43.847{n}00+00:00"
        entries[0]["result"]["result"] = False
    report = compare(left, right)
    assert report["first_divergence"] is None
    assert len(report["first_divergence_candidates"]) == 2


def test_equal_timestamps_are_ambiguous(trace):
    left = copy.deepcopy(trace)
    right = copy.deepcopy(trace)
    for doc in (left, right):
        for entries in doc["trace"].values():
            entries[0]["timestamp"] = "2026-10-07T04:57:43.847400+00:00"
    for entries in right["trace"].values():
        entries[0]["result"] = {"new": True}
    report = compare(left, right)
    assert report["first_divergence"] is None
    assert len(report["first_divergence_candidates"]) == 2


def test_missing_timestamp_prevents_first_claim(trace):
    right = copy.deepcopy(trace)
    right["trace"]["trigger/0"][0].pop("timestamp")
    right["trace"]["action/0"][0]["error"] = "changed"
    report = compare(trace, right)
    assert report["first_divergence"] is None
    assert any("Chronology is uncertain" in warning for warning in report["warnings"])


def test_empty_or_running_runs_are_partial(trace):
    right = copy.deepcopy(trace)
    right["trace"] = {}
    report = compare(trace, right)
    assert report["coverage"] == "partial"
    right = copy.deepcopy(trace)
    right["state"] = "running"
    right["timestamp"]["finish"] = None
    assert compare(trace, right)["coverage"] == "partial"


def test_different_automation_rejected(trace):
    right = copy.deepcopy(trace)
    right["item_id"] = "another"
    with pytest.raises(TraceError, match="different automations"):
        compare(trace, right)


def test_explicit_ignore_field_is_reported(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["error"] = "changed"
    report = compare(trace, right, ignored_fields=("/error",))
    assert report["status"] == "no_recorded_difference"
    assert "/error" in report["policy"]["ignored_step_fields"]


@pytest.mark.parametrize("value", ["not-a-pointer", ""])
def test_invalid_ignore_field_rejected(trace, value):
    with pytest.raises(TraceError, match="ignore-field"):
        compare(trace, trace, ignored_fields=(value,))


def test_ignore_pointer_can_select_an_array_index(trace):
    left = copy.deepcopy(trace)
    left["trace"]["action/0"][0]["changed_variables"]["levels"] = [10, 20, 30]
    right = copy.deepcopy(left)
    right["trace"]["action/0"][0]["changed_variables"]["levels"][1] = 99
    report = compare(left, right, ignored_fields=("/changed_variables/levels/1",))
    assert report["status"] == "no_recorded_difference"


def test_ignored_context_only_addition_does_not_hide_a_real_variable(trace):
    left = copy.deepcopy(trace)
    left["trace"]["trigger/0"][0]["changed_variables"] = {"temperature": 20}
    right = copy.deepcopy(left)
    right["trace"]["trigger/0"][0]["changed_variables"]["context"] = {"id": "generated"}
    report = compare(left, right)
    assert report["status"] == "no_recorded_difference"
    right["trace"]["trigger/0"][0]["changed_variables"]["temperature"] = 30
    report = compare(left, right)
    assert report["summary"]["field_changes"] == 1
    assert report["steps"][0]["changes"][0]["field"] == "/changed_variables/temperature"


def test_real_upstream_automation_saved_runs(automation_records):
    records = automation_records["data"]["automation.moon"]
    assert len(records) >= 2
    left, right = records[0], records[1]
    report = compare(left, right)
    assert report["status"] == "different"
    assert report["summary"]["configuration_changes"] == 0
    assert_all_references(report, left, right)


def test_real_upstream_script_trace(script_records):
    record = script_records["data"]["script.moon"][0]
    right = copy.deepcopy(record)
    right["extended_dict"]["trace"]["sequence/0"][0]["result"]["event"] = "different-event"
    report = compare(record, right)
    assert report["first_divergence"] == "sequence/0#0"
    assert_all_references(report, record, right)


def test_global_report_budget_is_explicit_and_does_not_claim_equal_rows(trace, monkeypatch):
    import flowwrench.ha_diff.compare as comparison

    monkeypatch.setattr(comparison, "MAX_TOTAL_CHANGES", 1)
    right = copy.deepcopy(trace)
    for entries in right["trace"].values():
        entries[0]["error"] = "changed"
    report = compare(trace, right)
    assert report["coverage"] == "partial"
    assert report["summary"]["field_changes"] == 1
    assert report["summary"]["uncompared_rows"] == 1
    assert report["steps"][1]["status"] == "not_compared_limit"
    assert report["first_divergence"] is None
    assert_all_references(report, trace, right)


def test_deepdiff_visit_limit_is_not_silently_treated_as_equal(trace, monkeypatch):
    import flowwrench.ha_diff.compare as comparison

    monkeypatch.setattr(comparison, "MAX_FIELD_DIFFS", 1)
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["error"] = "changed"
    report = compare(trace, right)
    assert report["coverage"] == "partial"
    assert report["first_divergence"] is None
    assert report["status"] in ("different", "inconclusive")


def test_two_empty_traces_are_inconclusive(trace):
    trace["trace"] = {}
    assert compare(trace, trace)["status"] == "inconclusive"
