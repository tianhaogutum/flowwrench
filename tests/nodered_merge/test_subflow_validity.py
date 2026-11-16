"""Port of test/subflow-validity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows, summarize_merge


def sub(flow: list[dict[str, Any]]) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == "sub")


def entry(diff: dict[str, Any]) -> dict[str, Any] | None:
    return next((n for n in [*diff["changed"], *diff["added"], *diff["removed"]] if n["id"] == "sub"), None)


def changes(diff: dict[str, Any]) -> Any:
    return entry(diff)["subflowWireChanges"]


def graph() -> list[dict[str, Any]]:
    return [
        {"id": "sub", "type": "subflow", "name": "Reusable", "in": [{"wires": []}, {"wires": []}], "out": [{"wires": []}], "status": {"wires": []}},
        {"id": "inside", "type": "function", "name": "Inside", "z": "sub", "x": 10, "outputs": 2, "wires": [[], []]},
        {"id": "other", "type": "subflow", "in": [], "out": []},
        {"id": "cross", "type": "function", "z": "other", "x": 20, "wires": [[]]},
        {"id": "config", "type": "config", "z": "sub"},
        {"id": "group", "type": "group", "z": "sub"},
        {"id": "tab", "type": "tab", "z": "sub"},
        {"id": "nested", "type": "subflow", "z": "sub", "in": [], "out": []},
    ]


def wire_port(node: dict[str, Any], kind: str) -> dict[str, Any]:
    return node["status"] if kind == "status" else node[kind][0]


def wired(flow: list[dict[str, Any]], kind: str, wire: dict[str, Any]) -> list[dict[str, Any]]:
    result = copy.deepcopy(flow)
    wire_port(sub(result), kind)["wires"] = [copy.deepcopy(wire)]
    return result


CASES: list[tuple[str, str, dict[str, Any], str | None]] = [
    ("internal unspecified input", "in", {"id": "inside"}, None),
    ("internal explicit input ignores output count", "in", {"id": "inside", "port": 99}, None),
    ("internal output", "out", {"id": "inside", "port": 1}, None),
    ("internal status", "status", {"id": "inside", "port": 1}, None),
    ("direct second input despite one output", "out", {"id": "sub", "port": 1}, None),
    ("status from second input", "status", {"id": "sub", "port": 1}, None),
    ("self input unspecified", "in", {"id": "sub"}, "self-input-reference"),
    ("self input explicit", "in", {"id": "sub", "port": 0}, "self-input-reference"),
    ("self output beyond inputs", "out", {"id": "sub", "port": 2}, "input-port-out-of-range"),
    ("self status beyond inputs", "status", {"id": "sub", "port": 2}, "input-port-out-of-range"),
    *[
        (f"{kind} {name}", kind, wire, reason)
        for kind in ("in", "out", "status")
        for name, wire, reason in (
            ("missing", {"id": "missing", "port": 0}, "missing-target"),
            ("cross flow", {"id": "cross", "port": 0}, "wrong-flow"),
            ("config", {"id": "config", "port": 0}, "non-runtime-target"),
            ("group", {"id": "group", "port": 0}, "non-runtime-target"),
            ("tab", {"id": "tab", "port": 0}, "non-runtime-target"),
            ("definition", {"id": "nested", "port": 0}, "non-runtime-target"),
        )
    ],
    ("internal output beyond count", "out", {"id": "inside", "port": 2}, "output-port-out-of-range"),
    ("internal status beyond count", "status", {"id": "inside", "port": 2}, "output-port-out-of-range"),
]


@pytest.mark.parametrize(("name", "kind", "wire", "reason"), CASES, ids=[name for name, _, _, _ in CASES])
def test_endpoint_validity_in_each_version(name: str, kind: str, wire: dict[str, Any], reason: str | None) -> None:
    empty = graph()
    full = wired(empty, kind, wire)
    original = copy.deepcopy(full)
    if wire["id"] == "missing":
        label = "(missing) [missing]"
    else:
        target = next(n for n in full if n["id"] == wire["id"])
        label = f"{target.get('name') or target['type']} [{wire['id']}]"
    expected = {
        "id": wire["id"], "name": label, "port": wire.get("port"),
        "passThrough": wire["id"] == "sub" and kind != "in", "valid": reason is None, "reason": reason,
    }
    without_sub = [n for n in empty if n["id"] != "sub"]
    assert changes(diff_flows(empty, full))[0]["added"] == [expected]
    assert changes(diff_flows(full, empty))[0]["removed"] == [expected]
    assert changes(diff_flows(without_sub, full))[0]["added"] == [expected]
    assert changes(diff_flows(full, without_sub))[0]["removed"] == [expected]
    checked = copy.deepcopy(full)
    issues = check_integrity(checked)
    assert len(issues) == (1 if reason else 0)
    assert wire_port(sub(checked), kind)["wires"] == ([] if reason else [wire])
    merged = merge_flows(empty, full, empty)
    assert merged["clean"] is (not reason)
    assert changes(merged["summary"]["ours"])[0]["added"] == [expected]
    assert merge_flows(empty, empty, full)["summary"]["theirs"] == merged["summary"]["ours"]
    assert merged["summary"] == summarize_merge(empty, full, empty, checked)
    assert full == original


COUNTS: list[tuple[str, dict[str, Any], int, str | None]] = [
    ("unknown count", {"x": 0}, 999, None),
    ("noninteger outputs unknown", {"x": 0, "outputs": "opaque"}, 999, None),
    ("wires fallback valid", {"wires": [[], []]}, 1, None),
    ("wires fallback exceeded", {"wires": [[]]}, 1, "output-port-out-of-range"),
    ("noninteger outputs uses wires fallback", {"outputs": 1.5, "wires": [[]]}, 1, "output-port-out-of-range"),
    ("integer outputs wins over wires length", {"outputs": 2, "wires": [[]]}, 1, None),
    ("zero outputs wins over wires length", {"outputs": 0, "wires": [[]]}, 0, "output-port-out-of-range"),
    ("negative integer count", {"outputs": -1, "x": 0}, 0, "output-port-out-of-range"),
    ("zero wires", {"wires": []}, 0, "output-port-out-of-range"),
]


@pytest.mark.parametrize(("name", "fields", "port", "reason"), COUNTS, ids=[name for name, _, _, _ in COUNTS])
def test_output_count_contract(name: str, fields: dict[str, Any], port: int, reason: str | None) -> None:
    empty = graph()
    empty.append({"id": "unknown", "type": "custom", "z": "sub", **copy.deepcopy(fields)})
    for kind in ("out", "status"):
        full = wired(empty, kind, {"id": "unknown", "port": port})
        endpoint = changes(diff_flows(empty, full))[0]["added"][0]
        assert endpoint["valid"] is (reason is None)
        assert endpoint["reason"] == reason
        checked = copy.deepcopy(full)
        issues = [i for i in check_integrity(checked) if i["kind"] == "dangling-subflow-wire"]
        assert len(issues) == (1 if reason else 0)


def test_reason_precedence_and_absent_input_count_use_local_integrity_semantics() -> None:
    empty = graph()
    sub(empty)["in"] = []
    sub(empty)["out"].append({"wires": []})
    empty.append({"id": "foreign-config", "type": "config"})
    after = wired(empty, "out", {"id": "sub", "port": 0})
    sub(after)["status"]["wires"] = [{"id": "foreign-config", "port": 99}]
    assert [g["added"][0]["reason"] for g in changes(diff_flows(empty, after))] == ["input-port-out-of-range", "wrong-flow"]
    del sub(empty)["in"]
    status_diff = diff_flows(empty, wired(empty, "status", {"id": "sub", "port": 0}))
    assert changes(status_diff)[0]["added"][0]["reason"] == "input-port-out-of-range"


def test_unchanged_edge_ids_ports_do_not_rewire_on_validity_or_name_changes_nested_data_stays_visible() -> None:
    before = wired(graph(), "out", {"id": "inside", "port": 1})
    after = copy.deepcopy(before)
    inside = next(n for n in after if n["id"] == "inside")
    inside["outputs"] = 1
    inside["name"] = "Renamed"
    sub(after)["out"][0]["x"] = 123
    sub(after)["out"][0]["wires"][0]["metadata"] = {"x": 456}
    diff = entry(diff_flows(before, after, ignore_layout=True))
    assert "subflowWireChanges" not in diff
    assert [p["property"] for p in diff["properties"]] == ["/out"]
    assert diff["properties"][0]["after"]["value"][0]["wires"][0]["metadata"]["x"] == 456
    assert diff["properties"][0]["after"]["value"][0]["x"] == 123
    without_target = [n for n in after if n["id"] != "inside"]
    assert "subflowWireChanges" not in entry(diff_flows(before, without_target))
    result = merge_flows(before, after, before)
    assert changes(result["summary"]["merged"])[0]["removed"][0]["valid"] is True
    assert changes(result["summary"]["merged"])[0]["added"] == []


def test_cli_diff_annotates_invalid_references_read_only_merge_report_shows_actual_pruning(run_cli, sandbox: Path) -> None:
    def write(name: str, value: Any) -> Path:
        file = sandbox / f"{name}.json"
        file.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        return file

    before = graph()
    after = wired(before, "out", {"id": "sub", "port": 1})
    sub(after)["status"]["wires"] = [{"id": "missing", "port": 3}, {"id": "inside", "port": 9}]
    sub(after)["in"][1]["wires"] = [{"id": "sub", "port": 0}]
    a, b = write("before", before), write("after", after)
    data = [f.read_bytes() for f in (a, b)]
    out = sandbox / "out.json"
    report = Path(f"{out}.conflicts.json")
    out.write_text("KEEP", encoding="utf-8")
    report.write_text("KEEP REPORT", encoding="utf-8")
    text = run_cli(["diff", a, b])
    assert text.returncode == 0, text.stderr
    assert re.search(r"input port 1 \(pass-through\)(?! \(invalid)", text.stdout)
    for reason in ("self-input-reference", "missing-target", "output-port-out-of-range"):
        assert f"(invalid: {reason})" in text.stdout
    as_json = run_cli(["diff", a, b, "--format", "json"])
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == diff_flows(before, after)
    assert [f.read_bytes() for f in (a, b)] == data
    assert out.read_text(encoding="utf-8") == "KEEP"
    assert report.read_text(encoding="utf-8") == "KEEP REPORT"
    merge = run_cli(["merge", a, b, a, "-o", out])
    assert merge.returncode == 1, merge.stderr
    result = json.loads(report.read_bytes())
    assert len(result["conflicts"]) == 0
    assert len(result["integrity"]) == 3
    assert result["summary"]["ours"] == json.loads(as_json.stdout)
    assert [g["kind"] for g in changes(result["summary"]["merged"])] == ["out"]
    assert changes(result["summary"]["merged"])[0]["added"][0]["valid"] is True
    merged = json.loads(out.read_bytes())
    assert sub(merged)["out"][0]["wires"] == [{"id": "sub", "port": 1}]
    assert sub(merged)["status"]["wires"] == []
