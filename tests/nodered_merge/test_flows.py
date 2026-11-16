"""Port of test/flows.test.js."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import (
    InputError,
    check_integrity,
    diff_flows,
    merge_flows,
    normalize,
    serialize,
    validate_flow,
)
from flowwrench.nodered_merge.jsjson import parse as js_parse

TAB = {"id": "tab", "type": "tab", "label": "Test"}
A = {"id": "a", "type": "function", "z": "tab", "name": "Alpha", "func": "return msg;", "x": 100, "y": 100, "wires": [["b"]]}
B = {"id": "b", "type": "debug", "z": "tab", "name": "Beta", "x": 300, "y": 100, "wires": []}
BASE = [TAB, A, B]


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def edit(flow: list[dict[str, Any]], node_id: str, props: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**node, **props} if node["id"] == node_id else clone(node) for node in flow]


def node(result: dict[str, Any], node_id: str = "a") -> dict[str, Any] | None:
    return next((n for n in result["flows"] if n["id"] == node_id), None)


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == node_id)


def test_diff_classifies_additions_removals_property_changes_and_layout_only_changes() -> None:
    after = [n for n in edit(BASE, "a", {"func": "return null;", "x": 101}) if n["id"] != "b"]
    after.append({"id": "c", "type": "debug", "z": "tab", "x": 20, "y": 20, "wires": []})
    after[0]["label"] = "Renamed"
    result = diff_flows(BASE, after)
    assert [n["id"] for n in result["added"]] == ["c"]
    assert [n["id"] for n in result["removed"]] == ["b"]
    assert [p["property"] for p in find(result["changed"], "a")["properties"]] == ["/func", "/x"]
    moved = diff_flows(BASE, edit(BASE, "a", {"x": 999}))
    assert moved["layoutOnly"][0]["id"] == "a"
    assert diff_flows(BASE, edit(BASE, "a", {"x": 999}), ignore_layout=True) == {
        "added": [], "removed": [], "changed": [], "layoutOnly": []
    }
    filtered = diff_flows(BASE, after, ignore_layout=True)
    assert [p["property"] for p in find(filtered["changed"], "a")["properties"]] == ["/func"]


def test_diff_wires_include_port_numbers_and_both_old_and_new_target_names() -> None:
    after = edit(BASE, "a", {"wires": [["c"]]})
    after.append({**B, "id": "c", "name": "Gamma"})
    p = diff_flows(BASE, after)["changed"][0]["properties"][0]["ports"][0]
    assert p["port"] == 0
    assert p["removed"] == [{"id": "b", "name": "Beta [b]", "valid": True, "reason": None}]
    assert p["added"] == [{"id": "c", "name": "Gamma [c]", "valid": True, "reason": None}]


def test_ordering_object_key_order_and_duplicate_wire_targets_do_not_produce_differences() -> None:
    after = list(reversed(clone(BASE)))
    find(after, "a")["wires"] = [["b", "b"]]
    assert diff_flows(BASE, after)["changed"] == []


def test_disjoint_node_edits_and_same_node_different_property_edits_merge_cleanly() -> None:
    ours = edit(BASE, "a", {"func": "return null;", "x": 150})
    theirs = edit(edit(BASE, "b", {"name": "Renamed"}), "a", {"y": 200})
    result = merge_flows(BASE, ours, theirs)
    assert result["clean"] is True
    assert node(result)["func"] == "return null;"
    assert node(result)["x"] == 150
    assert node(result)["y"] == 200
    assert node(result, "b")["name"] == "Renamed"


def test_nested_plain_objects_merge_by_property_arrays_remain_atomic() -> None:
    start = edit(BASE, "a", {"options": {"one": 1, "two": 2}, "rules": [1]})
    result = merge_flows(
        start,
        edit(start, "a", {"options": {"one": 3, "two": 2}, "rules": [2]}),
        edit(start, "a", {"options": {"one": 1, "two": 4}, "rules": [3]}),
    )
    assert node(result)["options"] == {"one": 3, "two": 4}
    assert result["conflicts"][0]["property"] == "/rules"
    assert result["clean"] is False


def test_wire_set_merge_combines_additions_and_respects_deletions_on_each_port() -> None:
    start = [*edit(BASE, "a", {"wires": [["b", "c"], []]}), {**B, "id": "c"}, {**B, "id": "d"}, {**B, "id": "e"}]
    ours = edit(start, "a", {"wires": [["b", "d"], ["e"]]})
    theirs = edit(start, "a", {"wires": [["b", "c", "e"], ["d"]]})
    result = merge_flows(start, ours, theirs)
    assert result["clean"] is True
    assert node(result)["wires"] == [["b", "d", "e"], ["d", "e"]]


def test_output_port_count_changes_concurrent_with_wire_edits_conflict() -> None:
    result = merge_flows(BASE, edit(BASE, "a", {"wires": []}), edit(BASE, "a", {"wires": [["a"]]}))
    assert result["conflicts"][0]["kind"] == "wire-port-count"
    assert node(result)["wires"] == []


def test_identical_edits_and_identical_additions_do_not_conflict() -> None:
    after = [*edit(BASE, "a", {"func": "return null;"}), {**B, "id": "c"}]
    assert merge_flows(BASE, after, after)["clean"] is True


def test_overlapping_property_conflict_records_presence_and_selected_value() -> None:
    result = merge_flows(BASE, edit(BASE, "a", {"func": "ours"}), edit(BASE, "a", {"func": "theirs"}), prefer="theirs")
    assert result["clean"] is False
    assert node(result)["func"] == "theirs"
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", result["conflicts"][0]["id"])
    assert [{k: v for k, v in c.items() if k != "id"} for c in result["conflicts"]] == [{
        "kind": "property",
        "nodeId": "a",
        "property": "/func",
        "base": {"present": True, "value": "return msg;"},
        "ours": {"present": True, "value": "ours"},
        "theirs": {"present": True, "value": "theirs"},
        "selected": "theirs",
    }]
    js_parse(serialize(result["flows"]))


def test_property_deletion_is_distinguished_from_json_null_and_nested_pointer_escaping_is_correct() -> None:
    start = edit(BASE, "a", {"options": {"a/b~c": 1}, "nullable": None})
    ours = edit(start, "a", {"options": {}})
    del find(ours, "a")["nullable"]
    theirs = edit(start, "a", {"options": {"a/b~c": 2}, "nullable": False})
    result = merge_flows(start, ours, theirs)
    assert [c["property"] for c in result["conflicts"]] == ["/nullable", "/options/a~1b~0c"]
    assert result["conflicts"][0]["ours"] == {"present": False}
    assert result["conflicts"][0]["base"] == {"present": True, "value": None}
    assert "nullable" not in node(result)


def test_delete_versus_modify_conflicts_in_both_directions_including_preferred_deletion() -> None:
    removed = [n for n in BASE if n["id"] != "a"]
    changed = edit(BASE, "a", {"name": "Changed"})
    first = merge_flows(BASE, removed, changed)
    assert first["conflicts"][0]["kind"] == "modify/delete"
    assert node(first) is None
    second = merge_flows(BASE, changed, removed)
    assert node(second)["name"] == "Changed"
    assert second["clean"] is False
    assert node(merge_flows(BASE, changed, removed, prefer="theirs")) is None


def test_add_add_id_collisions_conflict_one_sided_addition_deletion_merge() -> None:
    ours = [*BASE, {**B, "id": "new", "name": "Ours"}]
    theirs = [*BASE, {**B, "id": "new", "name": "Theirs"}]
    assert merge_flows(BASE, ours, theirs)["conflicts"][0]["kind"] == "add/add"
    assert merge_flows(BASE, BASE, ours)["clean"] is True
    assert merge_flows(BASE, [n for n in BASE if n["id"] != "a"], BASE)["clean"] is True


def test_dangling_wires_are_pruned_and_keep_the_result_unclean() -> None:
    result = merge_flows(BASE, [n for n in BASE if n["id"] != "b"], BASE)
    assert node(result)["wires"] == [[]]
    assert result["clean"] is False
    assert result["integrity"][0] == {
        "kind": "dangling-wire", "nodeId": "a", "property": "/wires/0", "target": "b", "action": "pruned", "reason": "missing-target"
    }


def test_missing_tabs_subflow_definitions_and_group_references_are_reported() -> None:
    ours = edit([n for n in BASE if n["id"] != "tab"], "a", {"type": "subflow:gone", "g": "group"})
    result = merge_flows(BASE, ours, BASE)
    kinds = {i["kind"] for i in result["integrity"]}
    assert "missing-tab" in kinds
    assert "missing-subflow" in kinds
    assert "missing-group" in kinds


def test_deleted_known_config_references_are_detected_but_function_text_is_not_interpreted() -> None:
    config = {"id": "cfg", "type": "mqtt-broker", "name": "Broker"}
    start = [*edit(BASE, "a", {"broker": "cfg", "func": "cfg"}), config]
    result = merge_flows(start, [n for n in start if n["id"] != "cfg"], start)
    assert [i["property"] for i in result["integrity"] if i["kind"] == "missing-config"] == ["/broker"]
    assert result["clean"] is False


def test_explicit_config_rules_detect_never_present_references_and_non_config_targets() -> None:
    start = edit(BASE, "a", {"broker": "never-present"})
    assert merge_flows(start, start, start)["clean"] is True
    result = merge_flows(start, start, start, config_refs=["function.broker"])
    assert result["integrity"][0]["kind"] == "missing-config"
    flow = edit(BASE, "a", {"broker": "b"})
    assert check_integrity(flow, [flow], ["function.broker"])[0]["kind"] == "missing-config"
    with pytest.raises(InputError):
        merge_flows(BASE, BASE, BASE, config_refs=["invalid"])
    with pytest.raises(InputError):
        merge_flows(BASE, BASE, BASE, prefer="unknown")


def test_subflow_input_output_and_status_object_wires_are_validated_and_pruned() -> None:
    sub = {
        "id": "sub", "type": "subflow", "name": "Reusable",
        "in": [{"x": 10, "y": 10, "wires": [{"id": "inside"}]}],
        "out": [{"x": 200, "y": 10, "wires": [{"id": "inside", "port": 0}]}],
        "status": {"x": 200, "y": 50, "wires": [{"id": "inside", "port": 1}]},
    }
    inside = {**A, "id": "inside", "z": "sub", "wires": [[]]}
    flow = [sub, inside, TAB, {**B, "type": "subflow:sub"}]
    assert len(check_integrity(clone(flow))) == 1  # status refers to nonexistent port 1
    result = merge_flows(flow, [n for n in flow if n["id"] != "inside"], flow)
    assert len([i for i in result["integrity"] if i["kind"] == "dangling-subflow-wire"]) == 3
    assert node(result, "sub")["in"][0]["wires"] == []


def test_normalize_is_idempotent_deterministic_and_does_not_mutate_input_or_reorder_semantic_arrays() -> None:
    original = clone(BASE)
    flow = list(reversed(BASE))
    flow[1] = {**flow[1], "rules": [{"z": 2, "a": 1}, {"a": 0}], "wires": [["b", "a", "b"], []]}
    once = serialize(flow)
    assert serialize(js_parse(once)) == once
    assert find(js_parse(once), "a")["rules"][0]["z"] == 2
    assert BASE == original
    result = merge_flows(BASE, BASE, BASE)
    result["flows"][0]["name"] = "mutation"
    assert BASE == original


def test_special_json_keys_are_data_and_cannot_pollute_object_prototype() -> None:
    flow = js_parse('[{"id":"n","type":"test","__proto__":{"polluted":true},"constructor":{"x":1}}]')
    result = merge_flows(flow, flow, flow)
    # Python dicts have no prototype; keep the Node assertion's intent.
    assert "polluted" not in {}
    assert js_parse(serialize(result["flows"]))[0]["__proto__"]["polluted"] is True
    assert normalize(flow)[0]["constructor"] == {"x": 1}


INVALID_FLOWS = [
    ("object wrapper", {"flows": []}),
    ("null", None),
    ("non-node", [None]),
    ("missing type", [{"id": "a"}]),
    ("duplicate IDs", [A, A]),
    ("non-string z", [{**A, "z": None}]),
    ("wire object", [{**A, "wires": [[{"id": "b"}]]}]),
    ("wire port not array", [{**A, "wires": ["b"]}]),
    ("invalid subflow wire", [{"id": "s", "type": "subflow", "in": [{"wires": [{"id": "a", "port": -1}]}]}]),
    ("nested subflow format", [{"id": "s", "type": "subflow", "nodes": []}]),
]


@pytest.mark.parametrize(("name", "invalid"), INVALID_FLOWS, ids=[name for name, _ in INVALID_FLOWS])
def test_validation_rejects(name: str, invalid: Any) -> None:
    with pytest.raises(InputError):
        validate_flow(invalid)


def test_deeply_nested_json_is_rejected_before_recursive_processing() -> None:
    value: Any = 1
    for _ in range(70):
        value = {"child": value}
    with pytest.raises(InputError, match="maximum depth"):
        validate_flow([{"id": "a", "type": "test", "value": value}])


def test_numeric_overflow_cannot_silently_become_json_null() -> None:
    with pytest.raises(InputError, match="non-finite"):
        validate_flow(js_parse('[{"id":"a","type":"test","value":1e400}]'))


def test_cross_flow_wires_are_pruned_and_config_to_runtime_type_changes_invalidate_references() -> None:
    flow = [TAB, {"id": "tab2", "type": "tab"}, A, {**B, "z": "tab2"}]
    assert check_integrity(clone(flow))[0]["kind"] == "dangling-wire"
    cfg = {"id": "cfg", "type": "mqtt-broker"}
    start = [*edit(BASE, "a", {"broker": "cfg"}), cfg]
    changed = edit(start, "cfg", {"x": 100, "y": 100, "wires": [], "type": "debug"})
    assert merge_flows(start, changed, start)["integrity"][0]["kind"] == "missing-config"


def test_direct_subflow_input_pass_through_to_outputs_status_is_preserved_invalid_input_index_is_pruned() -> None:
    sub = {
        "id": "sub", "type": "subflow",
        "in": [{"x": 0, "y": 0, "wires": []}],
        "out": [{"x": 100, "y": 0, "wires": [{"id": "sub", "port": 0}]}],
        "status": {"x": 100, "y": 100, "wires": [{"id": "sub", "port": 0}]},
    }
    assert merge_flows([sub], [sub], [sub])["clean"] is True
    invalid = {**sub, "out": [{"wires": [{"id": "sub", "port": 1}]}]}
    result = merge_flows([invalid], [invalid], [invalid])
    assert result["integrity"][0]["kind"] == "dangling-subflow-wire"
    assert result["flows"][0]["out"][0]["wires"] == []


def test_built_in_mqtt_checks_missing_broker_without_history_and_explicit_reference_arrays_deduplicate_findings() -> None:
    mqtt = {"id": "a", "type": "mqtt in", "broker": "missing", "wires": [], "x": 1, "y": 1}
    assert merge_flows([mqtt], [mqtt], [mqtt])["integrity"][0]["kind"] == "missing-config"
    cfg = {"id": "cfg", "type": "test-config"}
    start = [{**mqtt, "broker": "cfg", "servers": ["cfg"]}, cfg]
    result = merge_flows(start, start[:1], start, config_refs=["mqtt in.servers", "mqtt in.broker"])
    assert len([i for i in result["integrity"] if i["kind"] == "missing-config"]) == 2


def test_wires_beyond_declared_output_count_are_reported_for_review() -> None:
    start = edit(BASE, "a", {"outputs": 0})
    result = merge_flows(start, start, start)
    assert result["integrity"][0]["kind"] == "output-port-count"
    assert result["clean"] is False


def test_incompatible_combined_schema_preserves_a_complete_valid_side_and_reports_conflict() -> None:
    start = [{"id": "s", "type": "custom", "in": []}]
    ours = [{"id": "s", "type": "subflow", "in": []}]
    theirs = [{"id": "s", "type": "custom", "in": {"customOption": True}}]
    result = merge_flows(start, ours, theirs)
    assert result["conflicts"][0]["kind"] == "schema"
    assert result["clean"] is False
    assert result["flows"] == ours
    validate_flow(result["flows"])


def test_node_count_limit_fails_before_processing_an_oversized_flow() -> None:
    with pytest.raises(InputError, match="too many nodes"):
        validate_flow([{"id": "x", "type": "test"}] * 100001)
