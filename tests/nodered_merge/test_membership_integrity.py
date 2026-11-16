"""Port of test/membership-integrity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows
from flowwrench.nodered_merge.jsjson import stringify

SOURCE = {"id": "source", "type": "custom", "z": "target", "x": 0, "wires": []}
DEFINITION = {"id": "target", "type": "tab", "name": "Flow"}
SUPPORT = {"id": "valid", "type": "subflow"}


def issue(reason: str, node_id: str = "source", target: str = "target") -> dict[str, Any]:
    return {"kind": "missing-tab", "nodeId": node_id, "property": "/z", "target": target, "action": "review", "reason": reason}


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    return next((n for n in flow if n["id"] == node_id), None)


def without_source(flow: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in flow if n["id"] != "source"]


CASES: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("absent", None, "missing-target"),
    *[(type_, {"id": "target", "type": type_}, "non-flow-target") for type_ in ("group", "config")],
    *[
        (f"runtime {key}", {"id": "target", "type": "custom", key: [] if key == "wires" else 0}, "non-flow-target")
        for key in ("x", "y", "wires")
    ],
    ("another instance", {"id": "target", "type": "subflow:valid", "x": 0}, "non-flow-target"),
    ("tab", DEFINITION, None),
    ("subflow", {"id": "target", "type": "subflow"}, None),
    *[
        (f"{type_} with runtime fields", {"id": "target", "type": type_, "z": "valid", "x": 0, "wires": []}, None)
        for type_ in ("tab", "subflow")
    ],
]


@pytest.mark.parametrize(("name", "target", "reason"), CASES, ids=[name for name, _, _ in CASES])
def test_actual_membership_inventory_unchanged_z(name: str, target: dict[str, Any] | None, reason: str | None) -> None:
    base, after = [SOURCE, DEFINITION, SUPPORT], [SOURCE, *([target] if target else []), SUPPORT]
    saved = copy.deepcopy([base, after])
    expected = [issue(reason)] if reason else []
    checked = copy.deepcopy(after)
    assert check_integrity(checked, [base]) == expected, "historical flow does not rescue actual invalid target"
    assert checked == after, "members and z strings are retained"
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


_UNDEFINED = object()


@pytest.mark.parametrize("z", ["", _UNDEFINED, "TARGET", "source"], ids=["empty string", "undefined", "TARGET", "source"])
def test_exact_membership_lookup_and_guards(z: Any) -> None:
    node = dict(SOURCE)
    if z is _UNDEFINED:
        del node["z"]
    else:
        node["z"] = z
    flow = [node, DEFINITION]
    checked = copy.deepcopy(flow)
    expected = [] if z is _UNDEFINED or not z else [issue("non-flow-target" if z == "source" else "missing-target", "source", z)]
    assert check_integrity(checked) == expected
    assert checked == flow
    assert merge_flows(flow, flow, flow)["integrity"] == expected


@pytest.mark.parametrize("type_", ["tab", "subflow", "group", "config", "custom", "subflow:valid"])
def test_membership_checks_retain_source_kind(type_: str) -> None:
    node = {**SOURCE, "type": type_}
    flow = [node, SUPPORT]
    assert check_integrity(copy.deepcopy(flow)) == [issue("missing-target")]
    assert find(merge_flows(flow, flow, flow)["flows"], "source") == node


def test_five_field_identity_keeps_multiple_members_mixed_finding_order_and_pruning() -> None:
    flow = [
        {"id": "z-source", "type": "subflow:gone", "z": "gone", "g": "gone", "outputs": 0, "wires": [["gone", "gone"]], "server": ["gone", "gone"]},
        {"id": "a-source", "type": "custom", "z": "gone", "x": 0},
        {"id": "sub", "type": "subflow", "in": [{"wires": [{"id": "gone"}, {"id": "gone"}]}]},
    ]
    original = copy.deepcopy(flow)
    assert check_integrity(flow, [flow], ["subflow:gone.server", "subflow:gone.server"]) == [
        issue("missing-target", "z-source", "gone"),
        {"kind": "missing-subflow", "nodeId": "z-source", "property": "/type", "target": "gone", "action": "review", "reason": "missing-target"},
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
    keys = [stringify([i["kind"], i["nodeId"], i["property"], i["target"], i["action"]]) for i in check_integrity(copy.deepcopy(original))]
    assert len(set(keys)) == len(keys)
    original[0]["wires"] = [[]]
    original[2]["in"][0]["wires"] = []
    assert flow == original, "only existing invalid wire pruning changes graph"


def test_actual_preference_and_reviewed_choices_classify_the_selected_target() -> None:
    base, ours, theirs = [SOURCE], [SOURCE, {"id": "target", "type": "group"}], [SOURCE, DEFINITION]
    first = merge_flows(base, ours, theirs)
    assert first["integrity"] == [issue("non-flow-target")]
    assert merge_flows(base, ours, theirs, prefer="theirs")["integrity"] == []
    conflict_id = first["conflicts"][0]["id"]
    for choice, reason in (
        ({"choice": "ours"}, "non-flow-target"),
        ({"choice": "custom", "present": False}, "missing-target"),
        ({"choice": "theirs"}, None),
    ):
        r = merge_flows(base, ours, theirs, resolutions={conflict_id: choice})
        assert r["integrity"] == ([issue(reason)] if reason else [])
        assert r["clean"] is (not reason)
        assert r["conflicts"][0]["id"] == conflict_id
        assert r["conflicts"][0]["resolved"] is True


@pytest.mark.parametrize("repair", ["theirs", "custom"])
@pytest.mark.parametrize("scope", ["target", "z"])
def test_cli_membership_actual_reason_no_write_refusal_and_repair(run_cli, sandbox: Path, scope: str, repair: str) -> None:
    versions = (
        [[SOURCE], [SOURCE, {"id": "target", "type": "group"}], [SOURCE, DEFINITION]]
        if scope == "target"
        else [[{**SOURCE, "z": ""}], [SOURCE], [{**SOURCE, "z": "valid"}]]
    )
    for v in versions:
        v.append(SUPPORT)
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
    assert r["integrity"] == [issue("non-flow-target" if scope == "target" else "missing-target")]
    assert len(m["conflicts"]) == 1
    assert m["conflicts"][0]["kind"] == ("add/add" if scope == "target" else "property")
    conflict_id = m["conflicts"][0]["id"]
    refused = [{"choice": "ours"}] + (
        [{"choice": "custom", "present": False}, {"choice": "custom", "present": True, "value": {"id": "target", "type": "group"}}]
        if scope == "target"
        else [{"choice": "custom", "present": True, "value": "another-missing"}]
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
        else {"choice": "custom", "present": True, "value": {**DEFINITION, "name": "Reviewed"} if scope == "target" else "valid"}
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
    assert find(result, "source")["z"] == (SOURCE["z"] if scope == "target" else "valid")
    if scope == "target":
        assert find(result, "target")["type"] == "tab"
    assert [f.read_bytes() for f in files] == data
