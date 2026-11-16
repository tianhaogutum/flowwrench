"""Port of test/config-integrity.test.js."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import check_integrity, merge_flows


def source(props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "source", "type": "custom", "x": 0, "wires": [], **(props or {})}


def config(props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "target", "type": "config", "name": "History", **(props or {})}


def issue(prop: str, target: str, reason: str, node_id: str = "source") -> dict[str, Any]:
    return {"kind": "missing-config", "nodeId": node_id, "property": prop, "target": target, "action": "review", "reason": reason}


TARGETS: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("absent", None, "missing-target"),
    *[(f"runtime {k}", config({k: [] if k == "wires" else 0}), "non-config-target") for k in ("x", "y", "wires")],
    *[(type_, config({"type": type_}), "non-config-target") for type_ in ("tab", "subflow", "group")],
    ("accepted config", config(), None),
    ("accepted unknown type", config({"type": "unknown", "z": "", "outputs": 0}), None),
]
TARGET_IDS = [name for name, _, _ in TARGETS]


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    return next((n for n in flow if n["id"] == node_id), None)


@pytest.mark.parametrize(("name", "target", "reason"), TARGETS, ids=TARGET_IDS)
def test_actual_config_inventory_unchanged_inferred_and_explicit_refs(name: str, target: dict[str, Any] | None, reason: str | None) -> None:
    node = source({"server": "target", "meta": {"a/b~c": ["target", "target"]}})
    base, after = [config(), node], [*([target] if target else []), node]
    originals = copy.deepcopy([base, after])
    expected = [] if reason is None else [
        issue("/meta/a~1b~0c/0", "target", reason),
        issue("/meta/a~1b~0c/1", "target", reason),
        issue("/server", "target", reason),
    ]
    for options in ({}, {"config_refs": ["custom.server", "custom.server"]}):
        result = merge_flows(base, after, base, **options)
        assert result["integrity"] == expected, "inferred/explicit overlap keeps one issue per pointer, in existing discovery order"
        assert result["clean"] is (reason is None)
        assert result["conflicts"] == []
        assert find(result["flows"], "source") == node
        for summary in (result["summary"][k] for k in ("ours", "theirs", "merged")):
            assert find([*summary["changed"], *summary["added"], *summary["removed"]], "source") is None, (
                "unchanged pointer/IDs never synthesize proposal deltas"
            )
    # Direct checks use actual map, not the valid historical target. Input order is preserved.
    actual = [node, *([target] if target else [])]
    direct = check_integrity(actual, [base], ["custom.server"])
    assert direct == ([] if reason is None else [expected[2], expected[0], expected[1]])
    assert [base, after] == originals


def test_explicit_never_present_fields_escaped_arrays_repeated_refs_and_empty_scalar_retain_identity_order() -> None:
    node = source({"a/b~c": ["never", "never", ""], "server": "never", "empty": "", "opaque": "unknown"})
    flow = [node]
    assert check_integrity(copy.deepcopy(flow), [flow], ["custom.server", "custom.a/b~c", "custom.empty", "custom.server"]) == [
        issue("/a~1b~0c/0", "never", "missing-target"),
        issue("/a~1b~0c/1", "never", "missing-target"),
        issue("/a~1b~0c/2", "", "missing-target"),
        issue("/server", "never", "missing-target"),
    ]
    assert check_integrity(copy.deepcopy(flow)) == [], "unknown never-present fields stay opaque without explicit rules"


@pytest.mark.parametrize(("name", "target", "reason"), TARGETS, ids=TARGET_IDS)
def test_config_inventory_follows_added_removed_source_lifecycle(name: str, target: dict[str, Any] | None, reason: str | None) -> None:
    node = source({"server": "target"})
    base, after = [config()], [*([target] if target else []), node]
    added = merge_flows(base, after, base, config_refs=["custom.server"])
    assert added["integrity"] == ([] if reason is None else [issue("/server", "target", reason)])
    assert find(added["flows"], "source") == node
    removed = merge_flows([*base, node], [target] if target else [], [*base, node], config_refs=["custom.server"])
    assert removed["integrity"] == []
    assert not any(n["id"] == "source" for n in removed["flows"])


def test_mixed_issue_order_and_ordinary_subflow_wire_pruning_remain_unchanged() -> None:
    flow = [
        source({"z": "gone", "type": "subflow:gone", "g": "gone", "outputs": 0, "wires": [["gone", "gone"]], "server": "never"}),
        {"id": "sf", "type": "subflow", "in": [{"wires": [{"id": "gone"}, {"id": "gone"}]}]},
    ]
    issues = check_integrity(flow, [flow], ["subflow:gone.server"])
    assert issues == [
        {"kind": "missing-tab", "nodeId": "source", "property": "/z", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "missing-subflow", "nodeId": "source", "property": "/type", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "missing-group", "nodeId": "source", "property": "/g", "target": "gone", "action": "review", "reason": "missing-target"},
        {"kind": "output-port-count", "nodeId": "source", "property": "/wires", "target": 0, "action": "review"},
        {"kind": "dangling-wire", "nodeId": "source", "property": "/wires/0", "target": "gone", "action": "pruned", "reason": "missing-target"},
        issue("/server", "never", "missing-target"),
        {"kind": "dangling-subflow-wire", "nodeId": "sf", "property": "/in/0/wires", "target": "gone", "action": "pruned", "reason": "missing-target"},
    ]
    assert flow[0]["wires"] == [[]]
    assert flow[0]["server"] == "never"
    assert flow[1]["in"][0]["wires"] == []


@pytest.mark.parametrize("repair", ["theirs", "custom"])
def test_cli_inventory_uses_actual_chosen_target_resolution_repairs_it_invalid_choices_preserve_files(run_cli, sandbox: Path, repair: str) -> None:
    node = source({"server": "target"})
    versions = [[node], [node, config({"type": "tab"})], [node, config()]]
    files = []
    for i, value in enumerate(versions):
        p = sandbox / f"{i}.json"
        p.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        files.append(p)
    data = [f.read_bytes() for f in files]
    out = sandbox / "out.json"
    report, manifest = Path(f"{out}.conflicts.json"), sandbox / "decisions.json"
    merged = run_cli(["merge", *files, "-o", out, "--manifest", manifest, "--config-ref", "custom.server"])
    assert merged.returncode == 1, merged.stderr
    r = json.loads(report.read_bytes())
    assert r["integrity"] == [issue("/server", "target", "non-config-target")]
    assert find(json.loads(out.read_bytes()), "source")["server"] == "target"
    for d in (r["summary"][k] for k in ("ours", "theirs", "merged")):
        assert not any(n["id"] == "source" for n in d["changed"])
    m = json.loads(manifest.read_bytes())
    assert len(m["conflicts"]) == 1
    conflict_id = m["conflicts"][0]["id"]
    for decision in ({"choice": "ours"}, {"choice": "custom", "present": False}):
        m["conflicts"][0]["decision"] = decision
        manifest.write_text(json.dumps(m), encoding="utf-8")
        protected = [f.read_bytes() for f in (out, report)]
        failed = run_cli(["resolve", *files, "-o", out, "--manifest", manifest])
        assert failed.returncode == 2, failed.stderr
        assert re.search("remaining integrity", failed.stderr)
        assert [f.read_bytes() for f in (out, report)] == protected
    m["conflicts"][0]["decision"] = (
        {"choice": "theirs"} if repair == "theirs" else {"choice": "custom", "present": True, "value": config({"name": "Reviewed"})}
    )
    manifest.write_text(json.dumps(m), encoding="utf-8")
    resolved = run_cli(["resolve", *files, "-o", out, "--manifest", manifest])
    assert resolved.returncode == 0, resolved.stderr
    final = json.loads(report.read_bytes())
    assert final["integrity"] == []
    assert final["clean"] is True
    assert final["conflicts"][0]["id"] == conflict_id
    assert final["conflicts"][0]["resolved"] is True
    assert find(json.loads(out.read_bytes()), "source")["server"] == "target"
    assert [f.read_bytes() for f in files] == data
