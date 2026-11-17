import copy

from flowwrench.ha_diff import compare_traces, parse_trace

PATH = "action/0/repeat/sequence/0"


def loop_trace(trace, indices):
    result = copy.deepcopy(trace)
    result["trace"] = {
        PATH: [
            {
                "path": PATH,
                "timestamp": f"2026-10-07T04:57:43.847{position}00+00:00",
                "changed_variables": {"repeat": {"index": index}, "observed": index * 10},
            }
            for position, index in enumerate(indices)
        ]
    }
    return result


def compare(left, right, **options):
    return compare_traces(parse_trace(left), parse_trace(right), **options)


def test_explicit_repeat_indices_prevent_shifted_false_pairs(trace):
    left, right = loop_trace(trace, [1, 3]), loop_trace(trace, [1, 2, 3])
    report = compare(left, right)
    assert report["summary"]["aligned_rows"] == 3
    assert report["summary"]["different_rows"] == 1
    changed = next(row for row in report["steps"] if row["changes"])
    assert changed["id"] == f"{PATH}@repeat=2"
    assert changed["left_occurrence"] is None
    assert changed["right_occurrence"] == 1
    assert changed["changes"][0]["kind"] == "step_not_recorded_left"
    assert report["first_divergence"] == changed["id"]


def test_anchored_same_iteration_can_have_different_raw_array_indices(trace):
    left, right = loop_trace(trace, [1, 3]), loop_trace(trace, [1, 2, 3])
    right["trace"][PATH][2]["changed_variables"]["observed"] = 999
    report = compare(left, right)
    row = next(row for row in report["steps"] if row["id"] == f"{PATH}@repeat=3")
    change = row["changes"][0]
    assert change["left"]["pointer"].endswith("/1/changed_variables/observed")
    assert change["right"]["pointer"].endswith("/2/changed_variables/observed")
    assert change["left"]["value"] == 30
    assert change["right"]["value"] == 999


def test_missing_repeat_anchor_falls_back_without_claiming_a_unique_first(trace):
    left = loop_trace(trace, [1, 2])
    left["trace"][PATH][1]["changed_variables"].pop("repeat")
    right = copy.deepcopy(left)
    right["trace"][PATH][1]["changed_variables"]["observed"] = 500
    report = compare(left, right)
    row = next(row for row in report["steps"] if row["changes"])
    assert row["alignment"]["confidence"] == "ordinal_only"
    assert report["first_divergence"] is None
    assert report["first_divergence_candidates"] == [f"{PATH}#1"]


def test_nested_loop_duplicate_indices_are_not_assumed_unique(trace):
    left = loop_trace(trace, [1, 2, 1, 2])
    right = copy.deepcopy(left)
    right["trace"][PATH][3]["changed_variables"]["observed"] = -1
    report = compare(left, right)
    assert all(row["alignment"]["strategy"] == "occurrence" for row in report["steps"])
    assert report["first_divergence"] is None


def test_nonloop_user_variable_named_repeat_does_not_realign_steps(trace):
    left = copy.deepcopy(trace)
    right = copy.deepcopy(left)
    left["trace"]["action/0"][0]["changed_variables"]["repeat"] = {"index": 1}
    right["trace"]["action/0"][0]["changed_variables"]["repeat"] = {"index": 3}
    report = compare(left, right)
    assert report["summary"]["aligned_rows"] == 2
    assert report["first_divergence"] == "action/0#0"
    assert report["steps"][1]["alignment"]["strategy"] == "occurrence"


def test_partial_running_trace_does_not_claim_a_unique_first(trace):
    right = copy.deepcopy(trace)
    right["state"] = "running"
    right["timestamp"]["finish"] = None
    right["trace"]["action/0"][0]["error"] = "changed"
    report = compare(trace, right)
    assert report["coverage"] == "partial"
    assert report["first_divergence"] is None


def test_explicit_ordinal_mode_is_available_and_reported(trace):
    left, right = loop_trace(trace, [1, 3]), loop_trace(trace, [1, 2, 3])
    report = compare(left, right, align_repeats=False)
    assert all(row["alignment"]["strategy"] == "occurrence" for row in report["steps"])
    assert report["summary"]["different_rows"] == 2


def test_unique_inner_repeat_index_does_not_identify_outer_iteration(trace):
    left = loop_trace(trace, [2, 3])
    nested_path = "action/0/repeat/sequence/0/repeat/sequence/0"
    left["trace"][nested_path] = left["trace"].pop(PATH)
    for entry in left["trace"][nested_path]:
        entry["path"] = nested_path
    right = copy.deepcopy(left)
    right["trace"][nested_path][1]["changed_variables"]["observed"] = 999
    report = compare(left, right)
    assert all(row["alignment"]["confidence"] == "ordinal_only" for row in report["steps"])
    assert report["first_divergence"] is None


def test_reversed_repeat_indices_and_timestamps_are_not_trusted(trace):
    left = loop_trace(trace, [2, 1])
    right = copy.deepcopy(left)
    right["trace"][PATH][0]["changed_variables"]["observed"] = 999
    report = compare(left, right)
    assert report["first_divergence"] is None
    left = loop_trace(trace, [1, 2])
    left["trace"][PATH][0]["timestamp"], left["trace"][PATH][1]["timestamp"] = (
        left["trace"][PATH][1]["timestamp"],
        left["trace"][PATH][0]["timestamp"],
    )
    parsed = parse_trace(left)
    assert parsed.chronological is False
