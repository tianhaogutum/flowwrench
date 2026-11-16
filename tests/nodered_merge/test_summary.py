"""Port of test/summary.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge import flows

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUTS = [str(FIXTURES / "summary" / f"{role}.json") for role in ("base", "ours", "theirs")]


def parse(file: str | Path) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


VERSIONS = [parse(file) for file in INPUTS]


def find(diff: dict[str, Any], node_id: str = "a") -> dict[str, Any]:
    return next(n for n in diff["changed"] if n["id"] == node_id)


def node(node_id: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": "custom", "x": 0, "y": 0, "wires": [], **(props or {})}


def cfg(node_id: str, name: str | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": "config", "name": node_id if name is None else name}


def ref(prop: str, ref_id: str, name: str | None = None, valid: bool = True) -> dict[str, Any]:
    return {
        "property": prop, "id": ref_id, "name": f"{ref_id if name is None else name} [{ref_id}]",
        "valid": valid, "reason": None if valid else "missing-target",
    }


def write(directory: Path, name: str, value: Any) -> str:
    file = directory / f"{name}.json"
    file.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    return str(file)


def test_three_way_reports_distinguish_branch_edits_final_topology_and_layout_only_movement(run_cli, sandbox: Path) -> None:
    out = sandbox / "out.json"
    merged = run_cli(["merge", *INPUTS, "-o", out])
    assert merged.returncode == 0, merged.stderr
    summary = parse(f"{out}.conflicts.json")["summary"]
    assert summary, "merge report must include a semantic summary"
    assert summary["ignoreLayout"] is False
    assert [p["property"] for p in find(summary["ours"])["properties"]] == ["/broker", "/x"]
    assert [n["id"] for n in summary["ours"]["layoutOnly"]] == ["b"]
    assert find(summary["theirs"])["wireChanges"] == [{
        "port": 0,
        "removed": [{"id": "b", "name": "Console [b]", "valid": True, "reason": None}],
        "added": [{"id": "c", "name": "Archive [c]", "valid": True, "reason": None}],
    }]
    assert find(summary["merged"])["configReferences"] == {
        "removed": [ref("/broker", "primary", "Primary")], "added": [ref("/broker", "backup", "Backup")]
    }
    before = out.read_text(encoding="utf-8")
    filtered = run_cli(["merge", *INPUTS, "-o", out, "--ignore-layout"])
    assert filtered.returncode == 0, filtered.stderr
    s = parse(f"{out}.conflicts.json")["summary"]
    assert s["ignoreLayout"] is True
    assert s["ours"]["layoutOnly"] == []
    assert [p["property"] for p in find(s["merged"])["properties"]] == ["/broker", "/wires"]
    assert out.read_text(encoding="utf-8") == before, "filtering report layout must preserve merged coordinates"


def test_diff_names_config_references_in_json_and_text_including_explicit_never_present_targets(run_cli) -> None:
    result = run_cli(["diff", INPUTS[0], INPUTS[1], "--ignore-layout", "--format", "json"])
    assert result.returncode == 0, result.stderr
    assert find(json.loads(result.stdout))["configReferences"] == {
        "removed": [ref("/broker", "primary", "Primary")], "added": [ref("/broker", "backup", "Backup")]
    }
    text = run_cli(["diff", INPUTS[0], INPUTS[1], "--ignore-layout"])
    assert re.search(r"config /broker - Primary \[primary\]", text.stdout)
    assert re.search(r"config /broker \+ Backup \[backup\]", text.stdout)
    before = [node("a", {"servers": ["missing-old"]})]
    after = [node("a", {"servers": ["missing-new"]})]
    diff = flows.diff_flows(before, after, config_refs=["custom.servers"])
    assert find(diff)["configReferences"] == {
        "removed": [ref("/servers/0", "missing-old", "(missing)", False)],
        "added": [ref("/servers/0", "missing-new", "(missing)", False)],
    }


def test_reference_inference_handles_nested_pointers_arrays_and_deletion_without_interpreting_names_code() -> None:
    before = [
        cfg("old", "Old"),
        cfg("new", "New"),
        node("a", {"name": "old", "func": "old", "label": "old", "options": {"a/b~c": ["old"]}, "servers": ["old"]}),
    ]
    after = copy.deepcopy(before)
    after[2].update({"name": "new", "func": "new", "label": "new", "options": {}, "servers": ["new"]})
    diff = flows.diff_flows(before, after)
    assert find(diff)["configReferences"] == {
        "removed": [ref("/options/a~1b~0c/0", "old", "Old"), ref("/servers/0", "old", "Old")],
        "added": [ref("/servers/0", "new", "New")],
    }
    with pytest.raises(flows.InputError):
        flows.diff_flows(before, after, config_refs=["invalid"])


def test_added_and_removed_nodes_expose_outgoing_wire_and_config_reference_changes() -> None:
    other = node("b", {"name": "Sink"})
    a = node("a", {"broker": "cfg", "wires": [["b"]]})
    before = [other, cfg("cfg", "Broker")]
    after = [*before, a]
    added = flows.diff_flows(before, after)["added"][0]
    assert added["wireChanges"] == [{"port": 0, "removed": [], "added": [{"id": "b", "name": "Sink [b]", "valid": True, "reason": None}]}]
    assert added["configReferences"] == {"removed": [], "added": [ref("/broker", "cfg", "Broker")]}
    removed = flows.diff_flows(after, before)["removed"][0]
    assert removed["wireChanges"] == [{"port": 0, "removed": [{"id": "b", "name": "Sink [b]", "valid": True, "reason": None}], "added": []}]
    assert removed["configReferences"] == {"removed": [ref("/broker", "cfg", "Broker")], "added": []}


def test_summary_compares_the_actual_fallback_and_pruned_graph_with_all_three_input_config_histories(run_cli, sandbox: Path) -> None:
    base = [node("a", {"func": "base", "broker": "", "wires": [["b"]]}), node("b")]
    ours = [node("a", {"func": "ours", "broker": "cfg", "wires": [["b"]]}), cfg("cfg", "Branch config")]
    theirs = [node("a", {"func": "theirs", "broker": "cfg", "wires": [["b"]]}), node("b")]
    files = [write(sandbox, str(i), flow) for i, flow in enumerate([base, ours, theirs])]
    out = sandbox / "out.json"
    result = run_cli(["merge", *files, "-o", out, "--prefer", "theirs"])
    assert result.returncode == 1, result.stderr
    report = parse(f"{out}.conflicts.json")
    assert report["summary"], "unclean reports also need summaries"
    merged_a = find(report["summary"]["merged"])
    assert next(p for p in merged_a["properties"] if p["property"] == "/func")["after"]["value"] == "theirs"
    assert [n["id"] for n in merged_a["wireChanges"][0]["removed"]] == ["b"]
    assert find(report["summary"]["theirs"])["configReferences"]["added"] == [ref("/broker", "cfg", "Branch config", False)]
    assert merged_a["configReferences"]["added"] == [ref("/broker", "cfg", "Branch config")]
    assert report["integrity"][0]["kind"] == "dangling-wire"


def test_layout_filtering_keeps_flow_membership_nested_coordinates_arrays_and_port_counts_semantic() -> None:
    before = [node("a", {"z": "one", "g": "first", "options": {"x": 1}, "rules": [1], "wires": [[]]})]
    after = [node("a", {"z": "two", "g": "second", "options": {"x": 2}, "rules": [2], "wires": [[], []], "x": 100})]
    diff = flows.diff_flows(before, after, ignore_layout=True)
    assert [p["property"] for p in find(diff)["properties"]] == ["/g", "/options/x", "/rules", "/wires", "/z"]
    assert callable(flows.summarize_merge), "library must expose the three-way summary"
    assert flows.summarize_merge(before, after, before, after, ignore_layout=True)["merged"] == diff


def test_resolved_reports_summarize_reviewed_custom_decisions_and_retain_manifest_config_rules(run_cli, sandbox: Path) -> None:
    out, manifest = sandbox / "out.json", sandbox / "decisions.json"
    files = [write(sandbox, name, [node("a", {"server": name, "x": i})]) for i, name in enumerate(("base", "ours", "theirs"))]
    generated = run_cli(["merge", *files, "-o", out, "--manifest", manifest, "--config-ref", "custom.server"])
    assert generated.returncode == 1, generated.stderr
    document = parse(manifest)
    for c in document["conflicts"]:
        c["decision"] = {"choice": "ours"}
    # Clear the reference explicitly so resolution has no remaining integrity issue.
    next(c for c in document["conflicts"] if c["property"] == "/server")["decision"] = {"choice": "custom", "present": True, "value": ""}
    manifest.write_text(json.dumps(document), encoding="utf-8")
    resolved = run_cli(["resolve", *files, "-o", out, "--manifest", manifest, "--ignore-layout"])
    assert resolved.returncode == 0, resolved.stderr
    report = parse(f"{out}.conflicts.json")
    assert [p["property"] for p in find(report["summary"]["merged"])["properties"]] == ["/server"]
    assert find(report["summary"]["merged"])["configReferences"] == {
        "removed": [ref("/server", "base", "(missing)", False)], "added": []
    }
    assert parse(out)[0]["server"] == ""


def test_layout_filtering_retains_conflict_decisions_and_does_not_reinterpret_nested_subflow_arrays(run_cli, sandbox: Path) -> None:
    out = sandbox / "out.json"
    files = [write(sandbox, str(x), [node("a", {"x": x})]) for x in (0, 1, 2)]
    result = run_cli(["merge", *files, "-o", out, "--ignore-layout"])
    assert result.returncode == 1, result.stderr
    report = parse(f"{out}.conflicts.json")
    assert report["conflicts"][0]["property"] == "/x"
    assert parse(out)[0]["x"] == 1
    assert report["summary"]["merged"] == {"added": [], "removed": [], "changed": [], "layoutOnly": []}
    before = [{"id": "sub", "type": "subflow", "in": [{"x": 0, "y": 0, "wires": []}]}]
    after = copy.deepcopy(before)
    after[0]["in"][0]["x"] = 10
    assert [p["property"] for p in flows.diff_flows(before, after, ignore_layout=True)["changed"][0]["properties"]] == ["/in"]


def test_summaries_are_deterministic_do_not_mutate_inputs_and_preserve_raw_functional_values() -> None:
    original = copy.deepcopy(VERSIONS)
    merged = flows.merge_flows(*VERSIONS)["flows"]
    summary = flows.summarize_merge(*VERSIONS, merged)
    reordered = [[dict(reversed(list(n.items()))) for n in reversed(flow)] for flow in VERSIONS]
    assert flows.summarize_merge(*reordered, list(reversed(merged))) == summary
    assert VERSIONS == original
    assert next(p for p in find(summary["merged"])["properties"] if p["property"] == "/broker") == {
        "property": "/broker",
        "before": {"present": True, "value": "primary"},
        "after": {"present": True, "value": "backup"},
        "layout": False,
    }


def test_invalid_summary_reference_rules_and_malformed_inputs_leave_output_report_untouched(run_cli, sandbox: Path) -> None:
    out = sandbox / "out.json"
    report = Path(f"{out}.conflicts.json")
    bad = sandbox / "bad.json"
    bad.write_text("[broken", encoding="utf-8")
    out.write_text("KEEP OUT", encoding="utf-8")
    report.write_text("KEEP REPORT", encoding="utf-8")
    for args in (
        ["merge", *INPUTS, "-o", out, "--ignore-layout", "--config-ref", "bad"],
        ["merge", INPUTS[0], INPUTS[1], bad, "-o", out, "--ignore-layout"],
    ):
        assert run_cli(args).returncode == 2
        assert out.read_text(encoding="utf-8") == "KEEP OUT"
        assert report.read_text(encoding="utf-8") == "KEEP REPORT"
    assert run_cli(["diff", INPUTS[0], INPUTS[1], "--config-ref", "bad"]).returncode == 2
