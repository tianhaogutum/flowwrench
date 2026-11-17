import copy

import pytest
from jsonpointer import resolve_pointer

from flowwrench.ha_diff import compare_traces, parse_trace
from flowwrench.ha_diff.configuration import configuration_for_path


@pytest.mark.parametrize(
    "config,path,pointer,label",
    [
        (
            {"action": {"alias": "Legacy singleton", "service": "light.turn_on"}},
            "action/0",
            "/config/action",
            "Legacy singleton",
        ),
        (
            {"actions": [{"action": "light.turn_on"}]},
            "action/0",
            "/config/actions/0",
            "light.turn_on",
        ),
        ({"sequence": [{"event": "done"}]}, "sequence/0", "/config/sequence/0", "done"),
        ({"conditions": "{{ true }}"}, "condition/0", "/config/conditions", None),
        (
            {
                "actions": [
                    {
                        "if": [{"alias": "Dark", "condition": "template"}],
                        "then": [{"event": "light"}],
                    }
                ]
            },
            "action/0/if/condition/0",
            "/config/actions/0/if/0",
            "Dark",
        ),
        (
            {"actions": [{"repeat": {"sequence": [{"service": "light.turn_on"}]}}]},
            "action/0/repeat/sequence/0",
            "/config/actions/0/repeat/sequence/0",
            "light.turn_on",
        ),
        (
            {
                "actions": [
                    {
                        "choose": [
                            {
                                "conditions": [
                                    {"condition": "state", "entity_id": ["light.a", "light.b"]}
                                ]
                            }
                        ]
                    }
                ]
            },
            "action/0/choose/0/conditions/0/entity_id/1",
            "/config/actions/0/choose/0/conditions/0/entity_id/1",
            "state",
        ),
        (
            {
                "triggers": [
                    {"trigger": "state"},
                    {"triggers": [{"trigger": "event"}, {"triggers": {"platform": "time"}}]},
                ]
            },
            "trigger/2",
            "/config/triggers/1/triggers/1/triggers",
            "time",
        ),
    ],
)
def test_configuration_paths_retain_real_format_and_original_pointers(
    trace, config, path, pointer, label
):
    trace["config"] = config
    document = parse_trace(trace)
    result = configuration_for_path(document, path)
    assert result["status"] == "exact"
    assert result["reference"]["pointer"] == pointer
    assert result["label"] == label
    resolve_pointer(trace, pointer)
    if result["label_reference"]:
        ref = result["label_reference"]
        assert resolve_pointer(trace, ref["pointer"]) == ref["value"] == label


def test_manual_trigger_is_not_mapped_to_a_configured_trigger(trace):
    result = configuration_for_path(parse_trace(trace), "trigger")
    assert result["status"] == "manual_trigger"
    assert result["reference"] is None


def test_unknown_nested_path_is_explicitly_partial(trace):
    result = configuration_for_path(parse_trace(trace), "action/0/future_feature/0")
    assert result["status"] == "partial"
    assert result["unresolved_path"] == "future_feature/0"
    assert result["reference"]["pointer"] == "/config/actions/0"


def test_changed_rows_include_configuration_and_labels(trace):
    other = copy.deepcopy(trace)
    other["trace"]["action/0"][0]["error"] = "different"
    report = compare_traces(parse_trace({"trace": trace}), parse_trace({"trace": other}))
    config = report["steps"][1]["configuration"]
    assert config["left"]["reference"]["pointer"] == "/trace/config/actions/0"
    assert config["left"]["label"] == "frontend.set_theme"
    assert "configuration" not in report["steps"][0]


def test_parallel_single_action_has_an_implicit_sequence_wrapper(trace):
    trace["config"] = {"actions": [{"parallel": [{"service": "light.turn_on"}]}]}
    result = configuration_for_path(parse_trace(trace), "action/0/parallel/0/sequence/0")
    assert result["status"] == "exact"
    assert result["reference"]["pointer"] == "/config/actions/0/parallel/0"


@pytest.mark.parametrize("index", ["01", "9" * 5000])
def test_noncanonical_or_excessive_indices_remain_partial(trace, index):
    result = configuration_for_path(parse_trace(trace), "action/" + index)
    assert result["status"] == "partial"
