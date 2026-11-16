"""Port of test/subflow-summary.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from flowwrench.nodered_merge.flows import diff_flows, merge_flows, summarize_merge

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FILES = [str(FIXTURES / "subflow-summary" / f"{role}.json") for role in ("base", "ours", "theirs")]


def parse(file: str | Path) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


BASE, OURS, THEIRS = (parse(file) for file in FILES)


def sub(flow: list[dict[str, Any]]) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == "sub")


def entry(diff: dict[str, Any]) -> dict[str, Any]:
    return next(n for n in diff["changed"] if n["id"] == "sub")


def endpoint(node_id: str, name: str, port: int | None = None, pass_through: bool = False, reason: str | None = None) -> dict[str, Any]:
    return {"id": node_id, "name": f"{name} [{node_id}]", "port": port, "passThrough": pass_through, "valid": reason is None, "reason": reason}


def alpha(port: int | None, reason: str | None = None) -> dict[str, Any]:
    return endpoint("a", "Alpha", port, False, reason)


def beta(port: int | None) -> dict[str, Any]:
    return endpoint("b", "Beta", port)


def direct(port: int, reason: str | None = None) -> dict[str, Any]:
    return endpoint("sub", "Reusable", port, True, reason)


def group(kind: str, port: int, removed: list[Any], added: list[Any]) -> dict[str, Any]:
    return {"kind": kind, "property": "/status/wires" if kind == "status" else f"/{kind}/{port}/wires", "port": port, "removed": removed, "added": added}


def write(directory: Path, name: str, value: Any) -> str:
    file = directory / f"{name}.json"
    file.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    return str(file)


def test_subflow_summaries_name_in_out_status_endpoints_and_direct_input_pass_through_with_exact_ports() -> None:
    original = copy.deepcopy([BASE, OURS, THEIRS])
    assert entry(diff_flows(BASE, OURS, ignore_layout=True))["subflowWireChanges"] == [
        group("in", 0, [alpha(None)], [beta(None)]),
        group("out", 1, [alpha(0)], [direct(0)]),
    ]
    assert entry(diff_flows(BASE, THEIRS))["subflowWireChanges"] == [group("status", 0, [alpha(1)], [direct(0)])]
    result = merge_flows(BASE, OURS, THEIRS, ignore_layout=True)
    assert result["clean"] is True
    assert entry(result["summary"]["merged"])["subflowWireChanges"] == [
        group("in", 0, [alpha(None)], [beta(None)]),
        group("out", 1, [alpha(0)], [direct(0)]),
        group("status", 0, [alpha(1)], [direct(0)]),
    ]
    assert result["summary"] == summarize_merge(BASE, OURS, THEIRS, result["flows"], ignore_layout=True)
    assert [BASE, OURS, THEIRS] == original


def test_text_and_json_diffs_expose_both_enclosing_and_endpoint_port_indexes(run_cli) -> None:
    text = run_cli(["diff", FILES[0], FILES[1], "--ignore-layout"])
    assert text.returncode == 0, text.stderr
    assert re.search(r"subflow in port 0 - Alpha \[a\] \(port unspecified\)", text.stdout)
    assert re.search(r"subflow in port 0 \+ Beta \[b\]", text.stdout)
    assert re.search(r"subflow out port 1 - Alpha \[a\] port 0", text.stdout)
    assert re.search(r"subflow out port 1 \+ Reusable \[sub\] input port 0 \(pass-through\)", text.stdout)
    status = run_cli(["diff", FILES[0], FILES[2]])
    assert re.search(r"subflow status port 0 - Alpha \[a\] port 1", status.stdout)
    assert re.search(r"subflow status port 0 \+ Reusable \[sub\] input port 0 \(pass-through\)", status.stdout)
    as_json = run_cli(["diff", FILES[0], FILES[1], "--format", "json", "--ignore-layout"])
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == diff_flows(BASE, OURS, ignore_layout=True)


def test_added_removed_definitions_expose_all_edges_and_only_subflow_definitions_are_interpreted() -> None:
    without = [n for n in BASE if n["id"] != "sub"]
    expected = [group("in", 0, [], [alpha(None)]), group("out", 1, [], [alpha(0)]), group("status", 0, [], [alpha(1)])]
    flipped = [{**g, "removed": g["added"], "added": []} for g in expected]
    assert diff_flows(without, BASE)["added"][0]["subflowWireChanges"] == expected
    assert diff_flows(BASE, without)["removed"][0]["subflowWireChanges"] == flipped
    ordinary = [{"id": "custom", "type": "custom", "in": {"wires": "opaque"}, "status": {"wires": "opaque"}}]
    assert "subflowWireChanges" not in diff_flows([], ordinary)["added"][0]
    converted = copy.deepcopy(BASE)
    sub(converted)["type"] = "custom"
    assert entry(diff_flows(BASE, converted))["subflowWireChanges"] == flipped


def test_port_identity_includes_enclosing_index_endpoint_index_and_omitted_versus_explicit_zero() -> None:
    after = copy.deepcopy(BASE)
    sub(after)["in"][0]["wires"] = [{"id": "a", "port": 0}]
    sub(after)["out"][0]["wires"] = [{"id": "a", "port": 1}]
    sub(after)["out"][1]["wires"] = [{"id": "a", "port": 1}]
    assert entry(diff_flows(BASE, after))["subflowWireChanges"] == [
        group("in", 0, [alpha(None)], [alpha(0)]),
        group("out", 0, [], [alpha(1)]),
        group("out", 1, [alpha(0)], [alpha(1)]),
    ]
    moved = copy.deepcopy(BASE)
    sub(moved)["out"].reverse()
    assert entry(diff_flows(BASE, moved))["subflowWireChanges"] == [
        group("out", 0, [], [alpha(0)]),
        group("out", 1, [alpha(0)], []),
    ]


def test_direct_pass_through_uses_the_input_index_even_when_no_such_output_index_exists() -> None:
    before = copy.deepcopy(BASE)
    sub(before)["in"].append({"wires": []})
    sub(before)["out"] = [{"wires": []}]
    sub(before)["status"]["wires"] = []
    after = copy.deepcopy(before)
    sub(after)["out"][0]["wires"] = [{"id": "sub", "port": 1}]
    sub(after)["status"]["wires"] = [{"id": "sub", "port": 1}]
    result = merge_flows(before, after, before)
    assert result["clean"] is True
    assert entry(result["summary"]["merged"])["subflowWireChanges"] == [
        group("out", 0, [], [direct(1)]),
        group("status", 0, [], [direct(1)]),
    ]
    # A self-ID in an input wire is not a pass-through edge.
    sub(after)["in"][1]["wires"] = [{"id": "sub", "port": 0}]
    assert entry(diff_flows(before, after))["subflowWireChanges"][0] == group(
        "in", 1, [], [endpoint("sub", "Reusable", 0, False, "self-input-reference")]
    )
    assert merge_flows(before, after, before)["integrity"][0]["kind"] == "dangling-subflow-wire"


def test_nested_coordinates_metadata_ordered_arrays_and_empty_ports_remain_raw_semantic_changes() -> None:
    after = copy.deepcopy(BASE)
    sub(after)["x"] = 500
    sub(after)["in"][0]["x"] = 999
    sub(after)["in"][0]["wires"][0]["custom"] = "preserve"
    sub(after)["out"].append({"x": 20, "y": 30, "wires": []})
    sub(after)["status"]["y"] = 222
    sub(after)["status"]["label"] = "changed"
    diff = entry(diff_flows(BASE, after, ignore_layout=True))
    assert "subflowWireChanges" not in diff
    assert [p["property"] for p in diff["properties"]] == ["/in", "/out", "/status/label", "/status/y"]
    assert diff["properties"][0]["after"]["value"][0]["wires"][0]["custom"] == "preserve"
    assert diff["properties"][0]["after"]["value"][0]["x"] == 999
    assert len(diff["properties"][1]["after"]["value"]) == 3
    assert all(not p["layout"] for p in diff["properties"])


def test_object_wire_order_duplicates_and_target_names_do_not_create_false_edge_changes_deltas_are_deterministic() -> None:
    before, after = copy.deepcopy(BASE), copy.deepcopy(BASE)
    sub(before)["out"][1]["wires"] = [{"id": "b", "port": 1}, {"id": "a", "port": 0}]
    sub(after)["out"][1]["wires"] = [{"id": "a", "port": 0}, {"id": "b", "port": 1}, {"id": "a", "port": 0}]
    next(n for n in after if n["id"] == "a")["name"] = "Renamed"
    assert "subflowWireChanges" not in entry(diff_flows(before, after))
    empty = copy.deepcopy(BASE)
    sub(empty)["out"][1]["wires"] = []
    annotated = entry(diff_flows(empty, after))["subflowWireChanges"]
    assert annotated == [group("out", 1, [], [endpoint("a", "Renamed", 0), beta(1)])]
    after.reverse()
    sub(after)["out"][1]["wires"].reverse()
    assert entry(diff_flows(empty, after))["subflowWireChanges"] == annotated


def test_missing_and_invalid_edges_are_described_in_branch_summaries_merged_summaries_reflect_pruning() -> None:
    after = copy.deepcopy(BASE)
    sub(after)["out"][1]["wires"] = [{"id": "missing", "port": 3}, {"id": "sub", "port": 8}]
    sub(after)["status"]["wires"] = [{"id": "a", "port": 9}]
    diff = entry(diff_flows(BASE, after))
    assert diff["subflowWireChanges"] == [
        group("out", 1, [alpha(0)], [endpoint("missing", "(missing)", 3, False, "missing-target"), direct(8, "input-port-out-of-range")]),
        group("status", 0, [alpha(1)], [alpha(9, "output-port-out-of-range")]),
    ]
    result = merge_flows(BASE, after, BASE)
    assert result["clean"] is False
    assert len(result["integrity"]) == 3
    assert entry(result["summary"]["ours"])["subflowWireChanges"] == diff["subflowWireChanges"]
    assert entry(result["summary"]["merged"])["subflowWireChanges"] == [
        group("out", 1, [alpha(0)], []),
        group("status", 0, [alpha(1)], []),
    ]
    assert sub(result["flows"])["out"][1]["wires"] == []


def test_atomic_subflow_array_conflicts_retain_decisions_and_resolution_summarizes_actual_selected_edges(run_cli, sandbox: Path) -> None:
    out, manifest = sandbox / "out.json", sandbox / "decisions.json"
    other = copy.deepcopy(BASE)
    sub(other)["out"][1]["wires"] = [{"id": "b", "port": 1}]
    other_file = write(sandbox, "other", other)
    inputs = [FILES[0], FILES[1], other_file]
    result = run_cli(["merge", *inputs, "-o", out, "--manifest", manifest, "--ignore-layout"])
    assert result.returncode == 1, result.stderr
    report = parse(f"{out}.conflicts.json")
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["property"] == "/out"
    assert entry(report["summary"]["merged"])["subflowWireChanges"][1] == group("out", 1, [alpha(0)], [direct(0)])
    document = parse(manifest)
    document["conflicts"][0]["decision"] = {"choice": "theirs"}
    manifest.write_text(json.dumps(document), encoding="utf-8")
    resolved = run_cli(["resolve", *inputs, "-o", out, "--manifest", manifest, "--ignore-layout"])
    assert resolved.returncode == 0, resolved.stderr
    resolved_summary = parse(f"{out}.conflicts.json")["summary"]["merged"]
    assert entry(resolved_summary)["subflowWireChanges"][1] == group("out", 1, [alpha(0)], [beta(1)])
    assert sub(parse(out))["in"][0]["x"] == 50
    assert sub(parse(out))["x"] == 150


def test_malformed_object_wires_fail_before_replacing_merge_outputs_and_emit_no_partial_diff(run_cli, sandbox: Path) -> None:
    out = sandbox / "out.json"
    report = Path(f"{out}.conflicts.json")
    out.write_text("KEEP OUT", encoding="utf-8")
    report.write_text("KEEP REPORT", encoding="utf-8")
    for kind, wires in (
        ("in", ["a"]),
        ("in", [{"id": "a", "port": -1}]),
        ("out", [{"id": "a"}]),
        ("status", [{"id": "a", "port": 0.5}]),
        ("status", [{"id": "", "port": 0}]),
    ):
        bad = copy.deepcopy(BASE)
        if kind == "status":
            sub(bad)["status"]["wires"] = wires
        else:
            sub(bad)[kind][0]["wires"] = wires
        file = write(sandbox, "bad", bad)
        diff = run_cli(["diff", FILES[0], file, "--format", "json"])
        assert diff.returncode == 2
        assert diff.stdout == ""
        assert run_cli(["merge", FILES[0], file, FILES[0], "-o", out]).returncode == 2
        assert out.read_text(encoding="utf-8") == "KEEP OUT"
        assert report.read_text(encoding="utf-8") == "KEEP REPORT"
