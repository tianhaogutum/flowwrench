"""Port of test/wire-validity.test.js."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows, summarize_merge

_UNDEFINED = object()  # JavaScript ``undefined``: the key is removed
_DEFAULT_TARGET = {"x": 0, "z": "flow"}


def source(flow: list[dict[str, Any]]) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == "source")


def entry(diff: dict[str, Any]) -> dict[str, Any] | None:
    return next((n for n in [*diff["changed"], *diff["added"], *diff["removed"]] if n["id"] == "source"), None)


def edges(diff: dict[str, Any]) -> Any:
    return entry(diff)["wireChanges"]


def graph(source_fields: dict[str, Any] | None = None, target_fields: Any = _UNDEFINED) -> list[dict[str, Any]]:
    """``target_fields`` defaults to ``{x: 0, z: 'flow'}``; ``None`` spreads nothing (JS ``...null``)."""
    if target_fields is _UNDEFINED:
        target_fields = _DEFAULT_TARGET
    src = {"id": "source", "type": "function", "name": "Source", "z": "flow", "wires": [[]], **(source_fields or {})}
    return [
        {"id": "flow", "type": "tab"},
        {"id": "other", "type": "tab"},
        {key: value for key, value in src.items() if value is not _UNDEFINED},
        {"id": "target", "type": "custom", "name": "Target", **copy.deepcopy(target_fields or {})},
    ]


def wired(flow: list[dict[str, Any]]) -> list[dict[str, Any]]:
    full = copy.deepcopy(flow)
    source(full)["wires"] = [["target"]]
    return full


def remove_target(flow: list[dict[str, Any]]) -> None:
    flow.pop(next(i for i, n in enumerate(flow) if n["id"] == "target"))


CASES: list[tuple[str, dict[str, Any], dict[str, Any] | None, str | None]] = [
    ("missing", {}, None, "missing-target"),
    *[
        case
        for type_ in ("tab", "subflow", "group", "config")
        for case in (
            (f"{type_} same flow", {}, {"type": type_, "z": "flow"}, "non-runtime-target"),
            (f"{type_} foreign flow priority", {}, {"type": type_, "z": "other"}, "non-runtime-target"),
            (f"{type_} absent flow", {}, {"type": type_}, "non-runtime-target"),
        )
    ],
    ("runtime different flow", {}, {"x": 0, "z": "other"}, "wrong-flow"),
    ("runtime same flow", {}, {"x": 0, "z": "flow"}, None),
    ("runtime missing target z", {}, {"x": 0}, None),
    ("runtime empty target z", {}, {"x": 0, "z": ""}, None),
    ("runtime missing source z", {"z": _UNDEFINED}, {"x": 0, "z": "other"}, None),
    ("runtime empty source z", {"z": ""}, {"x": 0, "z": "other"}, None),
    ("runtime both absent", {"z": _UNDEFINED}, {"x": 0}, None),
    ("runtime both empty", {"z": ""}, {"x": 0, "z": ""}, None),
    ("runtime y only", {}, {"y": 0, "z": "flow"}, None),
    ("runtime wires only no outputs", {}, {"wires": [], "outputs": 0, "z": "flow"}, None),
    ("config even with z and outputs", {}, {"outputs": 2, "z": "flow"}, "non-runtime-target"),
]


@pytest.mark.parametrize(("name", "fields", "target", "reason"), CASES, ids=[name for name, _, _, _ in CASES])
def test_ordinary_wire_validity_each_version_and_node_lifecycle(
    name: str, fields: dict[str, Any], target: dict[str, Any] | None, reason: str | None
) -> None:
    empty = graph(fields, target or {})
    if target is None:
        remove_target(empty)
    full = wired(empty)
    original = copy.deepcopy(full)
    expected = {"id": "target", "name": "(missing)" if target is None else "Target [target]", "valid": reason is None, "reason": reason}
    without_source = [n for n in empty if n["id"] != "source"]
    for before, after, direction in (
        (empty, full, "added"),
        (full, empty, "removed"),
        (without_source, full, "added"),
        (full, without_source, "removed"),
    ):
        assert edges(diff_flows(before, after))[0][direction] == [expected]
    checked = copy.deepcopy(full)
    issues = check_integrity(checked)
    assert issues == (
        [{"kind": "dangling-wire", "nodeId": "source", "property": "/wires/0", "target": "target", "action": "pruned", "reason": reason}]
        if reason
        else []
    )
    assert source(checked)["wires"] == ([[]] if reason else [["target"]])
    merged = merge_flows(empty, full, empty)
    assert merged["clean"] is (reason is None)
    assert edges(merged["summary"]["ours"])[0]["added"] == [expected]
    assert merge_flows(empty, empty, full)["summary"]["theirs"] == merged["summary"]["ours"]
    assert merged["summary"] == summarize_merge(empty, full, empty, checked)
    if reason:
        assert entry(merged["summary"]["merged"]) is None
    else:
        assert edges(merged["summary"]["merged"])[0]["added"] == [expected]
    assert full == original


def test_changed_edges_use_their_own_source_and_target_versions_retain_raw_wires_and_port_identity() -> None:
    before = wired(graph())
    after = copy.deepcopy(before)
    source(after)["z"] = "other"
    source(after)["wires"] = [[], ["target", "target"]]
    next(n for n in after if n["id"] == "target")["name"] = "New name"
    d = entry(diff_flows(before, after))
    assert d["wireChanges"] == [
        {"port": 0, "removed": [{"id": "target", "name": "Target [target]", "valid": True, "reason": None}], "added": []},
        {"port": 1, "removed": [], "added": [{"id": "target", "name": "New name [target]", "valid": False, "reason": "wrong-flow"}]},
    ]
    prop = next(p for p in d["properties"] if p["property"] == "/wires")
    assert prop["before"] == {"present": True, "value": [["target"]]}
    assert prop["after"] == {"present": True, "value": [[], ["target"]]}
    assert prop["ports"] == d["wireChanges"]
    # Move the target across flows instead of the source: removals still use before.
    source(after)["z"] = "flow"
    next(n for n in after if n["id"] == "target")["z"] = "other"
    assert entry(diff_flows(before, after))["wireChanges"] == d["wireChanges"]


def _rename(flow: list[dict[str, Any]]) -> None:
    next(n for n in flow if n["id"] == "target")["name"] = "Rename"


def _retype(flow: list[dict[str, Any]]) -> None:
    next(n for n in flow if n["id"] == "target")["type"] = "tab"


def _move_source(flow: list[dict[str, Any]]) -> None:
    source(flow)["z"] = "other"


def test_unchanged_ids_never_rewire_for_target_deletion_rename_type_or_source_flow_changes_nested_values_stay_visible() -> None:
    before = wired(graph())
    for mutate in (_rename, remove_target, _retype, _move_source):
        after = copy.deepcopy(before)
        mutate(after)
        source(after)["meta"] = {"x": 123, "nested": {"wires": [["hidden"]]}}
        source(after)["x"] = 99
        d = entry(diff_flows(before, after, ignore_layout=True))
        assert "wireChanges" not in d
        assert not any(p["property"] == "/x" for p in d["properties"])
        assert next(p for p in d["properties"] if p["property"] == "/meta")["after"]["value"] == source(after)["meta"]


def test_invalid_base_removals_reflect_base_version_final_summary_records_actual_pruning() -> None:
    base = wired(graph({}, None))
    remove_target(base)
    ours = copy.deepcopy(base)
    source(ours)["name"] = "Changed"
    result = merge_flows(base, ours, base)
    assert result["clean"] is False
    assert "wireChanges" not in entry(result["summary"]["ours"])
    assert edges(result["summary"]["merged"])[0]["removed"] == [{"id": "target", "name": "(missing)", "valid": False, "reason": "missing-target"}]
    assert source(result["flows"])["wires"] == [[]]


def test_cli_ordinary_invalid_references_are_read_only_in_diff_branch_proposals_and_actual_pruning_differ(run_cli, sandbox: Path) -> None:
    base = graph()
    ours = copy.deepcopy(base)
    ours.extend([{"id": "cfg", "type": "config", "z": "other"}, {"id": "cross", "type": "custom", "x": 0, "z": "other"}])
    source(ours)["wires"] = [["missing", "cfg", "cross", "target"]]

    def write(name: str, value: Any) -> Path:
        p = sandbox / f"{name}.json"
        p.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        return p

    a, b = write("base", base), write("ours", ours)
    out = sandbox / "out.json"
    report = Path(f"{out}.conflicts.json")
    data = [f.read_bytes() for f in (a, b)]
    out.write_text("KEEP", encoding="utf-8")
    report.write_text("REPORT", encoding="utf-8")
    text = run_cli(["diff", a, b])
    assert text.returncode == 0, text.stderr
    for reason in ("missing-target", "non-runtime-target", "wrong-flow"):
        assert f"(invalid: {reason})" in text.stdout
    assert "port 0 + Target [target]\n" in text.stdout
    as_json = run_cli(["diff", a, b, "--format", "json"])
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == diff_flows(base, ours)
    assert [f.read_bytes() for f in (a, b)] == data
    assert out.read_text(encoding="utf-8") == "KEEP"
    assert report.read_text(encoding="utf-8") == "REPORT"
    merge = run_cli(["merge", a, b, a, "-o", out])
    assert merge.returncode == 1, merge.stderr
    r = json.loads(report.read_bytes())
    assert r["summary"]["ours"] == json.loads(as_json.stdout)
    assert len(r["integrity"]) == 3
    assert r["conflicts"] == []
    assert edges(r["summary"]["merged"])[0]["added"] == [{"id": "target", "name": "Target [target]", "valid": True, "reason": None}]
    protected = [f.read_bytes() for f in (out, report)]
    for wires in (None, ["target"], [[None]], [[{}]], [[""]]):
        bad = copy.deepcopy(ours)
        source(bad)["wires"] = wires
        p = write("bad", bad)
        d = run_cli(["diff", a, p, "--format", "json"])
        assert d.returncode == 2, d.stderr
        assert d.stdout == ""
        assert run_cli(["merge", a, p, a, "-o", out]).returncode == 2
        assert [f.read_bytes() for f in (out, report)] == protected
    assert [f.read_bytes() for f in (a, b)] == data


def test_target_existence_and_runtime_classification_come_from_each_endpoint_version() -> None:
    for target in (None, {"type": "config"}, {"x": 0, "z": "other"}):
        before = wired(graph({}, target))
        if target is None:
            remove_target(before)
        after = graph()
        source(after)["wires"] = [[], ["target"]]
        groups = edges(diff_flows(before, after))
        reason = "missing-target" if target is None else ("non-runtime-target" if target.get("type") == "config" else "wrong-flow")
        assert groups[0]["removed"] == [
            {"id": "target", "name": "(missing)" if target is None else "Target [target]", "valid": False, "reason": reason}
        ]
        assert groups[1]["added"] == [{"id": "target", "name": "Target [target]", "valid": True, "reason": None}]
