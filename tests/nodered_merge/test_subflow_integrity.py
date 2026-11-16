"""Port of test/subflow-integrity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, diff_flows, merge_flows

KINDS = ("in", "out", "status")
OTHER = {"id": "other", "type": "subflow"}


def target(fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "target", "type": "custom", "z": "sub", "x": 0, "outputs": 2, "wires": [[], []], **(fields or {})}


def definition() -> dict[str, Any]:
    return {"id": "sub", "type": "subflow", "in": [{"wires": []}, {"wires": []}], "out": [{"wires": []}], "status": {"wires": []}}


def sub(flow: list[dict[str, Any]]) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == "sub")


def port(node: dict[str, Any], kind: str, index: int = 0) -> dict[str, Any]:
    return node["status"] if kind == "status" else node[kind][index]


def entry(d: dict[str, Any]) -> dict[str, Any] | None:
    return next((n for n in [*d["changed"], *d["added"], *d["removed"]] if n["id"] == "sub"), None)


def subflow_changes(d: dict[str, Any]) -> Any:
    """``entry(d)?.subflowWireChanges`` (``None`` for undefined)."""
    return (entry(d) or {}).get("subflowWireChanges")


def issue(kind: str, reason: str, target_id: str = "target", index: int = 0) -> dict[str, Any]:
    return {
        "kind": "dangling-subflow-wire", "nodeId": "sub", "property": f"/{kind}/{index}/wires",
        "target": target_id, "action": "pruned", "reason": reason,
    }


CASES: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("missing", None, "missing-target"),
    *[
        case
        for type_ in ("tab", "subflow", "group", "config")
        for case in (
            (f"{type_} local", {"id": "target", "type": type_, "z": "sub"}, "non-runtime-target"),
            (f"{type_} foreign priority", {"id": "target", "type": type_, "z": "other"}, "wrong-flow"),
        )
    ],
    ("runtime foreign before port bound", target({"z": "other", "outputs": 0, "wires": []}), "wrong-flow"),
    ("runtime absent z", {"id": "target", "type": "custom", "x": 0}, "wrong-flow"),
    ("runtime empty z", target({"z": ""}), "wrong-flow"),
    ("same flow", target(), None),
    ("config outputs do not imply runtime", {"id": "target", "type": "custom", "z": "sub", "outputs": 2}, "non-runtime-target"),
]


@pytest.mark.parametrize(("name", "actual", "reason"), CASES, ids=[name for name, _, _ in CASES])
def test_subflow_actual_inventory_unchanged_in_out_status(name: str, actual: dict[str, Any] | None, reason: str | None) -> None:
    node = definition()
    for kind in KINDS:
        port(node, kind)["wires"] = [{"id": "target", "port": 1}, {"id": "target", "port": 1}]
    base, after = [node, target(), OTHER], [node, *([actual] if actual else []), OTHER]
    saved = copy.deepcopy([base, after])
    expected = [issue(k, reason) for k in KINDS] if reason else []
    checked = copy.deepcopy(after)
    assert check_integrity(checked) == expected
    for kind in KINDS:
        assert port(sub(checked), kind)["wires"] == ([] if reason else port(node, kind)["wires"])
    result = merge_flows(base, after, base)
    assert result["integrity"] == expected
    assert result["conflicts"] == []
    assert result["clean"] is (not reason)
    assert subflow_changes(diff_flows(base, after)) is None
    for k in ("ours", "theirs"):
        assert subflow_changes(result["summary"][k]) is None
    if reason:
        for group in entry(result["summary"]["merged"])["subflowWireChanges"]:
            assert group["added"] == []
            assert [[e["valid"], e["reason"]] for e in group["removed"]] == [[True, None]]

    def without(flow: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [n for n in flow if n["id"] not in ("sub", "target")]

    added = merge_flows(without(base), after, without(base))
    assert added["integrity"] == expected
    removed = merge_flows(base, without(after), base)
    assert removed["integrity"] == []
    assert [base, after] == saved


COUNTS: list[tuple[str, dict[str, Any], int, str | None]] = [
    ("unknown", {"x": 0}, 999, None),
    ("noninteger unknown", {"x": 0, "outputs": "opaque"}, 999, None),
    ("fallback accepted", {"wires": [[], []]}, 1, None),
    ("fallback exceeded", {"wires": [[]]}, 1, "output-port-out-of-range"),
    ("noninteger fallback", {"outputs": 1.5, "wires": [[]]}, 1, "output-port-out-of-range"),
    ("integer overrides fallback", {"outputs": 2, "wires": [[]]}, 1, None),
    ("zero overrides fallback", {"outputs": 0, "wires": [[]]}, 0, "output-port-out-of-range"),
    ("negative integer", {"outputs": -1, "x": 0}, 0, "output-port-out-of-range"),
    ("empty fallback", {"wires": []}, 0, "output-port-out-of-range"),
]


@pytest.mark.parametrize(("name", "fields", "endpoint", "reason"), COUNTS, ids=[name for name, _, _, _ in COUNTS])
def test_actual_subflow_count_input_ignores_outputs(name: str, fields: dict[str, Any], endpoint: int, reason: str | None) -> None:
    node = definition()
    for kind in KINDS:
        port(node, kind)["wires"] = [{"id": "target", "port": endpoint}]
    base = [node, target({"outputs": 1000})]
    after = [node, {"id": "target", "type": "custom", "z": "sub", **fields}]
    result = merge_flows(base, after, base)
    assert result["integrity"] == [
        *([issue(k, reason) for k in ("out", "status")] if reason else []),
        *([{"kind": "output-port-count", "nodeId": "target", "property": "/wires", "target": 0, "action": "review"}]
          if fields.get("outputs") == 0 and "outputs" in fields else []),
    ]
    assert port(sub(result["flows"]), "in")["wires"] == [{"id": "target", "port": endpoint}]
    for kind in ("out", "status"):
        assert port(sub(result["flows"]), kind)["wires"] == ([] if reason else port(node, kind)["wires"])
    assert subflow_changes(result["summary"]["ours"]) is None


_ABSENT = object()
SELF_INPUTS: list[tuple[str, Any]] = [("two", [{"wires": []}, {"wires": []}]), ("zero", []), ("absent", _ABSENT)]


@pytest.mark.parametrize(("name", "inputs"), SELF_INPUTS, ids=[name for name, _ in SELF_INPUTS])
def test_self_inventory_inputs_invalid_input_and_output_status_bounds(name: str, inputs: Any) -> None:
    node = definition()
    if inputs is _ABSENT:
        del node["in"]
    else:
        node["in"] = copy.deepcopy(inputs)
    has_inputs = inputs is not _ABSENT and len(inputs) > 0
    if has_inputs:
        node["in"][0]["wires"] = [{"id": "sub"}, {"id": "sub", "port": 99}]
    for kind in ("out", "status"):
        port(node, kind)["wires"] = [{"id": "sub", "port": p} for p in (0, 1, 2, 2)]
    original = copy.deepcopy(node)
    result = merge_flows([node], [node], [node])
    assert result["integrity"] == [
        *([issue("in", "self-input-reference", "sub")] if has_inputs else []),
        *[issue(k, "input-port-out-of-range", "sub") for k in ("out", "status")],
    ]
    for kind in ("out", "status"):
        assert port(sub(result["flows"]), kind)["wires"] == (
            [{"id": "sub", "port": 0}, {"id": "sub", "port": 1}] if has_inputs else []
        )
    assert node == original
    assert result["summary"]["ours"]["changed"] == []


def test_unchanged_self_pass_through_becomes_invalid_when_actual_input_count_shrinks() -> None:
    node = definition()
    for kind in ("out", "status"):
        port(node, kind)["wires"] = [{"id": "sub", "port": 1}]
    after = copy.deepcopy(node)
    after["in"].pop()
    r = merge_flows([node], [after], [node])
    assert r["integrity"] == [issue(k, "input-port-out-of-range", "sub") for k in ("out", "status")]
    assert "subflowWireChanges" not in entry(r["summary"]["ours"])


def test_first_rejected_endpoint_five_field_dedup_and_enclosing_port_order_coexist_with_other_findings() -> None:
    node = definition()
    node["in"][0]["wires"] = [{"id": "sub"}, {"id": "gone"}, {"id": "sub", "port": 9}]
    node["out"][0]["wires"] = [
        {"id": "b", "port": 0}, {"id": "a", "port": 2}, {"id": "b", "port": 2},
        {"id": "a", "port": 3}, {"id": "b", "port": 3}, {"id": "gone", "port": 0},
    ]
    node["out"].append({"wires": [{"id": "b", "port": 2}]})
    node["status"]["wires"] = [{"id": "b", "port": 2}, {"id": "gone", "port": 9}]
    flow = [
        node,
        target({"id": "a", "outputs": 1, "wires": [[]]}),
        target({"id": "b", "outputs": 1, "wires": [[]]}),
        {"id": "ordinary", "type": "custom", "x": 0, "wires": [["gone"]], "server": "gone"},
    ]
    assert check_integrity(flow, [flow], ["custom.server"]) == [
        issue("in", "self-input-reference", "sub"),
        issue("in", "missing-target", "gone"),
        issue("out", "output-port-out-of-range", "a"),
        issue("out", "output-port-out-of-range", "b"),
        issue("out", "missing-target", "gone"),
        issue("out", "output-port-out-of-range", "b", 1),
        issue("status", "output-port-out-of-range", "b"),
        issue("status", "missing-target", "gone"),
        {"kind": "dangling-wire", "nodeId": "ordinary", "property": "/wires/0", "target": "gone", "action": "pruned", "reason": "missing-target"},
        {"kind": "missing-config", "nodeId": "ordinary", "property": "/server", "target": "gone", "action": "review", "reason": "missing-target"},
    ]
    assert node["out"][0]["wires"] == [{"id": "b", "port": 0}]
    assert node["out"][1]["wires"] == []
    assert node["status"]["wires"] == []
    assert flow[-1]["wires"] == [[]]
    assert flow[-1]["server"] == "gone"


@pytest.mark.parametrize("repair", ["theirs", "custom"])
@pytest.mark.parametrize("scope", ["target", "wire"])
def test_cli_subflow_actual_reason_no_write_refusal_and_repair(run_cli, sandbox: Path, scope: str, repair: str) -> None:
    node = definition()
    node["out"][0]["wires"] = [{"id": "target", "port": 1}]
    empty, third = definition(), definition()
    third["out"] = []
    versions = (
        [[node], [node, {"id": "target", "type": "tab", "z": "sub"}], [node, target()]]
        if scope == "target"
        else [[empty], [node], [third]]
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
    assert r["integrity"] == [issue("out", "non-runtime-target" if scope == "target" else "missing-target")]
    assert len(m["conflicts"]) == 1
    assert m["conflicts"][0]["kind"] == ("add/add" if scope == "target" else "property")
    conflict_id = m["conflicts"][0]["id"]
    if scope == "target":
        for k in ("ours", "theirs"):
            assert subflow_changes(r["summary"][k]) is None
    refused: list[dict[str, Any]] = [{"choice": "ours"}]
    if scope == "target":
        refused += [
            {"choice": "custom", "present": False},
            {"choice": "custom", "present": True, "value": target({"z": "other"})},
            {"choice": "custom", "present": True, "value": target({"outputs": 1})},
        ]
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
    assert sub(json.loads(out.read_bytes()))["out"] == (node["out"] if scope == "target" else [])
    assert [f.read_bytes() for f in files] == data
