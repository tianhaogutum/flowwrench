"""Port of test/instance-integrity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows

SOURCE = {"id": "source", "type": "subflow:target", "x": 0, "wires": []}
DEFINITION = {"id": "target", "type": "subflow", "name": "Definition"}
SUPPORT = {"id": "valid", "type": "subflow"}


def issue(reason: str, node_id: str = "source", target: str = "target") -> dict[str, Any]:
    return {"kind": "missing-subflow", "nodeId": node_id, "property": "/type", "target": target, "action": "review", "reason": reason}


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    return next((n for n in flow if n["id"] == node_id), None)


def without_source(flow: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in flow if n["id"] != "source"]


CASES: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("absent", None, "missing-target"),
    *[(type_, {"id": "target", "type": type_}, "non-subflow-target") for type_ in ("tab", "group", "config")],
    *[
        (f"runtime {key}", {"id": "target", "type": "custom", key: [] if key == "wires" else 0}, "non-subflow-target")
        for key in ("x", "y", "wires")
    ],
    ("another instance", {"id": "target", "type": "subflow:valid", "x": 0}, "non-subflow-target"),
    ("definition", DEFINITION, None),
]


@pytest.mark.parametrize(("name", "target", "reason"), CASES, ids=[name for name, _, _ in CASES])
def test_actual_instance_inventory_unchanged_type(name: str, target: dict[str, Any] | None, reason: str | None) -> None:
    base, after = [SOURCE, DEFINITION, SUPPORT], [SOURCE, *([target] if target else []), SUPPORT]
    saved = copy.deepcopy([base, after])
    expected = [issue(reason)] if reason else []
    checked = copy.deepcopy(after)
    assert check_integrity(checked, [base]) == expected, "historical definition does not rescue actual invalid target"
    assert checked == after, "instances and type strings are retained"
    for prefer in ("ours", "theirs"):
        result = merge_flows(base, after, base, prefer=prefer)
        assert result["integrity"] == expected
        assert result["conflicts"] == []
        assert result["clean"] is (not reason)
        assert find(result["flows"], "source") == SOURCE
        for summary in (diff_flows(base, after), result["summary"]["ours"], result["summary"]["theirs"], result["summary"]["merged"]):
            assert not any(n["id"] == "source" for n in [*summary["changed"], *summary["added"], *summary["removed"]])
    added = merge_flows(without_source(base), after, without_source(base))
    assert added["integrity"] == expected
    assert find(added["flows"], "source") == SOURCE
    removed = merge_flows(base, without_source(after), base)
    assert removed["integrity"] == []
    assert not any(n["id"] == "source" for n in removed["flows"])
    assert [base, after] == saved


LOOKUPS: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("subflow:", None, "missing-target"),
    ("subflow:TARGET", DEFINITION, "missing-target"),
    ("subflow:source", None, "non-subflow-target"),
    ("subflow:target", {**DEFINITION, "z": "valid", "x": 0, "wires": []}, None),
]


@pytest.mark.parametrize(
    ("type_", "target", "reason"), LOOKUPS, ids=[f"{t}, {(g or {}).get('type') or 'absent'}" for t, g, _ in LOOKUPS]
)
def test_exact_definition_lookup(type_: str, target: dict[str, Any] | None, reason: str | None) -> None:
    node = {**SOURCE, "type": type_}
    flow = [node, *([target] if target else []), SUPPORT]
    checked = copy.deepcopy(flow)
    assert check_integrity(checked) == ([issue(reason, "source", type_[8:])] if reason else [])
    assert checked == flow


def test_five_field_identity_keeps_multiple_instances_and_existing_mixed_finding_order_and_pruning() -> None:
    flow = [
        {"id": "z-source", "type": "subflow:gone", "z": "gone", "g": "gone", "outputs": 0, "wires": [["gone", "gone"]], "server": ["gone", "gone"]},
        {"id": "a-source", "type": "subflow:gone", "x": 0},
        {"id": "sub", "type": "subflow", "in": [{"wires": [{"id": "gone"}, {"id": "gone"}]}]},
    ]
    original = copy.deepcopy(flow)
    assert check_integrity(flow, [flow], ["subflow:gone.server", "subflow:gone.server"]) == [
        {"kind": "missing-tab", "nodeId": "z-source", "property": "/z", "target": "gone", "action": "review", "reason": "missing-target"},
        issue("missing-target", "z-source", "gone"),
        {"kind": "missing-group", "nodeId": "z-source", "property": "/g", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "output-port-count", "nodeId": "z-source", "property": "/wires", "target": 0, "action": "review"},
        {"kind": "dangling-wire", "nodeId": "z-source", "property": "/wires/0", "target": "gone", "action": "pruned", "reason": "missing-target"},
        *[
            {"kind": "missing-config", "nodeId": "z-source", "property": f"/server/{i}", "target": "gone", "action": "review", "reason": "missing-target"}
            for i in (0, 1)
        ],
        issue("missing-target", "a-source", "gone"),
        {"kind": "dangling-subflow-wire", "nodeId": "sub", "property": "/in/0/wires", "target": "gone", "action": "pruned", "reason": "missing-target"},
    ]
    original[0]["wires"] = [[]]
    original[2]["in"][0]["wires"] = []
    assert flow == original, "only existing invalid wire pruning changes graph"


def test_actual_preference_and_reviewed_choices_classify_the_selected_target() -> None:
    base, ours, theirs = [SOURCE], [SOURCE, {"id": "target", "type": "tab"}], [SOURCE, DEFINITION]
    first = merge_flows(base, ours, theirs)
    assert first["integrity"] == [issue("non-subflow-target")]
    assert merge_flows(base, ours, theirs, prefer="theirs")["integrity"] == []
    conflict_id = first["conflicts"][0]["id"]
    for choice, reason in (
        ({"choice": "ours"}, "non-subflow-target"),
        ({"choice": "custom", "present": False}, "missing-target"),
        ({"choice": "theirs"}, None),
    ):
        r = merge_flows(base, ours, theirs, resolutions={conflict_id: choice})
        assert r["integrity"] == ([issue(reason)] if reason else [])
        assert r["clean"] is (not reason)
        assert r["conflicts"][0]["id"] == conflict_id
        assert r["conflicts"][0]["resolved"] is True


@pytest.mark.parametrize("repair", ["theirs", "custom"])
@pytest.mark.parametrize("scope", ["target", "type"])
def test_cli_instance_actual_reason_no_write_refusal_and_repair(run_cli, sandbox: Path, scope: str, repair: str) -> None:
    versions = (
        [[SOURCE], [SOURCE, {"id": "target", "type": "tab"}], [SOURCE, DEFINITION]]
        if scope == "target"
        else [[{**SOURCE, "type": "custom"}], [SOURCE], [{**SOURCE, "type": "debug"}]]
    )
    files = []
    for i, v in enumerate(versions):
        p = sandbox / f"{i}.json"
        p.write_text(" \n" + json.dumps(v, indent=2) + "\n", encoding="utf-8")
        files.append(p)
    data = [f.read_bytes() for f in files]
    out = sandbox / "out.json"
    report, manifest = Path(f"{out}.conflicts.json"), sandbox / "decisions.json"
    merged = run_cli(["merge", *files, "-o", out, "--manifest", manifest])
    assert merged.returncode == 1, merged.stderr
    r, m = json.loads(report.read_bytes()), json.loads(manifest.read_bytes())
    assert r["integrity"] == [issue("non-subflow-target" if scope == "target" else "missing-target")]
    assert len(m["conflicts"]) == 1
    assert m["conflicts"][0]["kind"] == ("add/add" if scope == "target" else "property")
    conflict_id = m["conflicts"][0]["id"]
    refused = [{"choice": "ours"}] + (
        [{"choice": "custom", "present": False}, {"choice": "custom", "present": True, "value": {"id": "target", "type": "group"}}]
        if scope == "target"
        else [{"choice": "custom", "present": True, "value": "subflow:"}]
    )
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
        else {"choice": "custom", "present": True, "value": {**DEFINITION, "name": "Reviewed"} if scope == "target" else "custom-reviewed"}
    )
    manifest.write_text(json.dumps(m), encoding="utf-8")
    resolved = run_cli(["resolve", *files, "-o", out, "--manifest", manifest])
    assert resolved.returncode == 0, resolved.stderr
    final = json.loads(report.read_bytes())
    assert final["integrity"] == []
    assert final["clean"] is True
    assert final["conflicts"][0]["id"] == conflict_id
    assert final["conflicts"][0]["resolved"] is True
    result = json.loads(out.read_bytes())
    expected_type = SOURCE["type"] if scope == "target" else ("debug" if repair == "theirs" else "custom-reviewed")
    assert find(result, "source")["type"] == expected_type
    if scope == "target":
        assert find(result, "target")["type"] == "subflow"
    assert [f.read_bytes() for f in files] == data
