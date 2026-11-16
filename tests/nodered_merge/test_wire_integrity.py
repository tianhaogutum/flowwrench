"""Port of test/wire-integrity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows

_UNDEFINED = object()  # JavaScript ``undefined``: the key is removed
TABS = [{"id": "flow", "type": "tab"}, {"id": "other", "type": "tab"}]


def source(props: dict[str, Any] | None = None) -> dict[str, Any]:
    node = {"id": "source", "type": "function", "z": "flow", "wires": [["target", "target"], ["target"]], **(props or {})}
    return {key: value for key, value in node.items() if value is not _UNDEFINED}


def target(props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "target", "type": "custom", "x": 0, "z": "flow", **(props or {})}


def issue(reason: str, port: int = 0, target_id: str = "target") -> dict[str, Any]:
    return {"kind": "dangling-wire", "nodeId": "source", "property": f"/wires/{port}", "target": target_id, "action": "pruned", "reason": reason}


def entry(d: dict[str, Any]) -> dict[str, Any] | None:
    return next((n for n in [*d["changed"], *d["added"], *d["removed"]] if n["id"] == "source"), None)


def wire_changes(d: dict[str, Any]) -> Any:
    """``entry(d)?.wireChanges`` (``None`` for undefined)."""
    return (entry(d) or {}).get("wireChanges")


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == node_id)


CASES: list[tuple[str, dict[str, Any], dict[str, Any] | None, str | None]] = [
    ("missing", {}, None, "missing-target"),
    *[
        (f"{type_} z={'undefined' if z is None else z}", {}, {"id": "target", "type": type_, **({} if z is None else {"z": z})}, "non-runtime-target")
        for type_ in ("tab", "subflow", "group", "config")
        for z in ("flow", "other", None)
    ],
    ("runtime wrong flow", {}, target({"z": "other"}), "wrong-flow"),
    ("same flow", {}, target(), None),
    ("target absent z", {}, {"id": "target", "type": "custom", "x": 0}, None),
    ("target empty z", {}, target({"z": ""}), None),
    ("source absent z", {"z": _UNDEFINED}, target({"z": "other"}), None),
    ("source empty z", {"z": ""}, target({"z": "other"}), None),
    ("both absent z", {"z": _UNDEFINED}, {"id": "target", "type": "custom", "x": 0}, None),
    ("both empty z", {"z": ""}, target({"z": ""}), None),
    ("runtime y only", {}, {"id": "target", "type": "custom", "y": 0, "z": "flow"}, None),
    ("runtime wires only", {}, {"id": "target", "type": "custom", "wires": [], "z": "flow"}, None),
    ("config z and outputs do not imply runtime", {}, {"id": "target", "type": "custom", "z": "other", "outputs": 2}, "non-runtime-target"),
]


@pytest.mark.parametrize(("name", "fields", "actual", "reason"), CASES, ids=[name for name, _, _, _ in CASES])
def test_ordinary_actual_wire_inventory_unchanged_edges_and_source_lifecycle(
    name: str, fields: dict[str, Any], actual: dict[str, Any] | None, reason: str | None
) -> None:
    node = source(fields)
    base, after = [*TABS, node, target()], [*TABS, node, *([actual] if actual else [])]
    saved = copy.deepcopy([base, after])
    expected = [issue(reason), issue(reason, 1)] if reason else []
    checked = copy.deepcopy(after)
    assert check_integrity(checked) == expected, "same-port duplicates collapse; separate ports keep separate issues"
    assert find(checked, "source")["wires"] == ([[], []] if reason else node["wires"])
    result = merge_flows(base, after, base)
    assert result["integrity"] == expected
    assert result["clean"] is (not reason)
    assert result["conflicts"] == []
    assert wire_changes(diff_flows(base, after)) is None, "unchanged IDs/ports do not synthesize deltas"
    for k in ("ours", "theirs"):
        assert wire_changes(result["summary"][k]) is None
    assert find(result["flows"], "source")["wires"] == ([[], []] if reason else [["target"], ["target"]])
    if reason:
        removed_ports = entry(result["summary"]["merged"])["wireChanges"]
        assert [p["port"] for p in removed_ports] == [0, 1]
        # Removed edges describe the valid base, whereas the finding describes actual target.
        for p in removed_ports:
            assert [[e["valid"], e["reason"]] for e in p["removed"]] == [[True, None]]

    def without(flow: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [n for n in flow if n["id"] != "source"]

    added = merge_flows(without(base), after, without(base))
    assert added["integrity"] == expected
    assert [p["added"][0]["reason"] for p in entry(added["summary"]["ours"])["wireChanges"]] == [reason, reason]
    removed = merge_flows(base, without(after), base)
    assert removed["integrity"] == []
    assert not any(n["id"] == "source" for n in removed["flows"])
    assert [base, after] == saved, "library calls never mutate inputs"


def test_actual_source_z_change_supplies_reason_without_a_branch_wire_delta() -> None:
    base, after = [*TABS, source(), target()], [*TABS, source({"z": "other"}), target()]
    result = merge_flows(base, after, base)
    assert result["integrity"] == [issue("wrong-flow"), issue("wrong-flow", 1)]
    assert "wireChanges" not in entry(result["summary"]["ours"])
    assert find(result["flows"], "source")["wires"] == [[], []]


def test_first_discovery_order_mixed_findings_and_pruning_keep_their_existing_contracts() -> None:
    flow = [
        source({"z": "gone", "type": "subflow:gone", "g": "gone", "outputs": 0, "wires": [["b", "a", "b"], ["b"]], "server": "cfg"}),
        {"id": "sf", "type": "subflow", "in": [{"wires": [{"id": "gone"}, {"id": "gone"}]}]},
    ]
    assert check_integrity(flow, [flow], ["subflow:gone.server"]) == [
        {"kind": "missing-tab", "nodeId": "source", "property": "/z", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "missing-subflow", "nodeId": "source", "property": "/type", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "missing-group", "nodeId": "source", "property": "/g", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "output-port-count", "nodeId": "source", "property": "/wires", "target": 0, "action": "review"},
        issue("missing-target", 0, "b"),
        issue("missing-target", 0, "a"),
        issue("missing-target", 1, "b"),
        {"kind": "missing-config", "nodeId": "source", "property": "/server", "target": "cfg", "action": "review", "reason": "missing-target"},
        {"kind": "dangling-subflow-wire", "nodeId": "sf", "property": "/in/0/wires", "target": "gone", "action": "pruned", "reason": "missing-target"},
    ]
    assert flow[0]["wires"] == [[], []]
    assert flow[0]["server"] == "cfg"
    assert flow[1]["in"][0]["wires"] == []


@pytest.mark.parametrize("repair", ["theirs", "custom"])
@pytest.mark.parametrize("scope", ["target", "wire"])
def test_cli_conflict_actual_reason_refuse_remaining_issue_and_repair(run_cli, sandbox: Path, scope: str, repair: str) -> None:
    node = source({"wires": [["target"]]})
    versions = (
        [[*TABS, node], [*TABS, node, {"id": "target", "type": "tab", "z": "other"}], [*TABS, node, target()]]
        if scope == "target"
        else [[*TABS, source({"wires": [[]]})], [*TABS, node], [*TABS, source({"wires": []})]]
    )
    files = []
    for i, v in enumerate(versions):
        p = sandbox / f"{i}.json"
        p.write_text(json.dumps(v, separators=(",", ":")), encoding="utf-8")
        files.append(p)
    data = [f.read_bytes() for f in files]
    out = sandbox / "out.json"
    report, manifest = Path(f"{out}.conflicts.json"), sandbox / "decisions.json"
    merged = run_cli(["merge", *files, "-o", out, "--manifest", manifest])
    assert merged.returncode == 1, merged.stderr
    r = json.loads(report.read_bytes())
    assert r["integrity"] == [issue("non-runtime-target" if scope == "target" else "missing-target")]
    m = json.loads(manifest.read_bytes())
    assert len(m["conflicts"]) == 1
    assert m["conflicts"][0]["kind"] == ("add/add" if scope == "target" else "wire-port-count")
    conflict_id = m["conflicts"][0]["id"]
    if scope == "target":
        for k in ("ours", "theirs"):
            assert wire_changes(r["summary"][k]) is None
    refused: list[dict[str, Any]] = [{"choice": "ours"}]
    if scope == "target":
        refused += [{"choice": "custom", "present": False}, {"choice": "custom", "present": True, "value": target({"z": "other"})}]
    for decision in refused:
        m["conflicts"][0]["decision"] = decision
        manifest.write_text(json.dumps(m), encoding="utf-8")
        protected = [f.read_bytes() for f in (out, report)]
        failed = run_cli(["resolve", *files, "-o", out, "--manifest", manifest])
        assert failed.returncode == 2, failed.stderr
        assert re.search("remaining integrity", failed.stderr)
        assert [f.read_bytes() for f in (out, report)] == protected
    m["conflicts"][0]["decision"] = (
        {"choice": "theirs"}
        if repair == "theirs"
        else {"choice": "custom", "present": True, "value": target({"name": "Reviewed"}) if scope == "target" else []}
    )
    manifest.write_text(json.dumps(m), encoding="utf-8")
    resolved = run_cli(["resolve", *files, "-o", out, "--manifest", manifest])
    assert resolved.returncode == 0, resolved.stderr
    final = json.loads(report.read_bytes())
    assert final["integrity"] == []
    assert final["clean"] is True
    assert final["conflicts"][0]["id"] == conflict_id
    assert final["conflicts"][0]["resolved"] is True
    assert find(json.loads(out.read_bytes()), "source")["wires"] == ([["target"]] if scope == "target" else [])
    assert [f.read_bytes() for f in files] == data
