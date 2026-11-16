"""Port of test/config-validity.test.js."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import InputError, diff_flows, merge_flows, summarize_merge

OPTIONS = {"config_refs": ["custom.kind.server"]}


def node(fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "source", "type": "custom.kind", "x": 0, "wires": [], **(fields or {})}


def config(fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "target", "type": "config", "name": "Config", **(fields or {})}


def entry(diff: dict[str, Any]) -> dict[str, Any] | None:
    return next((n for n in [*diff["changed"], *diff["added"], *diff["removed"]] if n["id"] == "source"), None)


def ref(prop: str, reason: str | None = None, name: str = "Config [target]", ref_id: str = "target") -> dict[str, Any]:
    return {"property": prop, "id": ref_id, "name": name, "valid": reason is None, "reason": reason}


CASES: list[tuple[str, dict[str, Any] | None, str | None]] = [
    ("absent", None, "missing-target"),
    *[(f"runtime {key}", config({key: [] if key == "wires" else 0}), "non-config-target") for key in ("x", "y", "wires")],
    *[
        case
        for type_ in ("tab", "subflow", "group")
        for case in (
            (type_, config({"type": type_}), "non-config-target"),
            (f"{type_} with x", config({"type": type_, "x": 0}), "non-config-target"),
        )
    ],
    ("config", config(), None),
    ("config with z/outputs/layout-like fields", config({"z": "", "outputs": 0, "w": 10, "h": 20}), None),
    ("unknown type uses existing heuristic", config({"type": "unknown"}), None),
]


@pytest.mark.parametrize(("name", "target", "reason"), CASES, ids=[name for name, _, _ in CASES])
def test_config_reasons_both_endpoint_versions_and_added_removed_sources(name: str, target: dict[str, Any] | None, reason: str | None) -> None:
    empty = [target] if target else []
    full = [*empty, node({"server": "target"})]
    expected = ref("/server", reason, "Config [target]" if target else "(missing) [target]")
    before = [*empty, node({"server": ""})]
    original = copy.deepcopy([empty, full, before])
    assert entry(diff_flows(before, full, **OPTIONS))["configReferences"] == {"removed": [], "added": [expected]}
    assert entry(diff_flows(full, before, **OPTIONS))["configReferences"] == {"removed": [expected], "added": []}
    assert entry(diff_flows(empty, full, **OPTIONS))["configReferences"] == {"removed": [], "added": [expected]}
    assert entry(diff_flows(full, empty, **OPTIONS))["configReferences"] == {"removed": [expected], "added": []}
    merged = merge_flows(before, full, before, **OPTIONS)
    assert entry(merged["summary"]["ours"])["configReferences"]["added"] == [expected]
    assert entry(merged["summary"]["merged"])["configReferences"]["added"] == [expected]
    assert merged["clean"] is (reason is None)
    assert merged["integrity"] == ([] if reason is None else [
        {"kind": "missing-config", "nodeId": "source", "property": "/server", "target": "target", "action": "review", "reason": reason}
    ])
    source = next(n for n in merged["flows"] if n["id"] == "source")
    assert source["server"] == "target", "config problems are retained for review"
    assert [empty, full, before] == original


def test_historical_inference_uses_endpoint_maps_and_current_non_config_names_before_historical_names() -> None:
    for _, target, reason in CASES:
        before = [config({"name": "Historical"}), node({"meta": {"a/b~c": ["target", "target"]}})]
        after = [*([{**target, "name": "Current"}] if target else []), node({"servers": ["target"]})]
        d = entry(diff_flows(before, after))
        assert d["configReferences"] == {
            "removed": [ref("/meta/a~1b~0c/0", None, "Historical [target]"), ref("/meta/a~1b~0c/1", None, "Historical [target]")],
            "added": [ref("/servers/0", reason, "Current [target]" if target else "Historical [target]")],
        }
        reverse = entry(diff_flows(after, before))
        assert reverse["configReferences"] == {"removed": d["configReferences"]["added"], "added": d["configReferences"]["removed"]}
        assert next(p for p in d["properties"] if p["property"] == "/meta")["before"]["value"] == before[1]["meta"]
        assert next(p for p in d["properties"] if p["property"] == "/servers")["after"]["value"] == ["target"]
        # Inferred references on added/removed source nodes also see cross-version history.
        no_source = [n for n in before if n["id"] != "source"]
        assert entry(diff_flows(no_source, after))["configReferences"]["added"] == d["configReferences"]["added"]
        assert entry(diff_flows(after, no_source))["configReferences"]["removed"] == d["configReferences"]["added"]


def test_history_is_ordered_base_ours_theirs_result_missing_names_do_not_imply_present_configs() -> None:
    base = [node({"server": ""})]
    ours = [node({"server": "target"}), config({"name": "Ours"})]
    theirs = [node({"server": "target"}), config({"name": "Theirs"})]
    merged = [node({"server": "target"})]
    s = summarize_merge(base, ours, theirs, merged, **OPTIONS)
    assert entry(s["ours"])["configReferences"]["added"] == [ref("/server", None, "Ours [target]")]
    assert entry(s["theirs"])["configReferences"]["added"] == [ref("/server", None, "Theirs [target]")]
    assert entry(s["merged"])["configReferences"]["added"] == [ref("/server", "missing-target", "Ours [target]")]
    historical_base = [config({"name": "Base"}), node({"server": "target"})]
    removal = summarize_merge(historical_base, [node({"server": ""})], theirs, merged, **OPTIONS)
    assert entry(removal["ours"])["configReferences"]["removed"] == [ref("/server", None, "Base [target]")]
    # Current label/type fallback wins too, even if historical config had a name.
    for target, name in (({"id": "target", "type": "tab", "label": "Label"}, "Label"), ({"id": "target", "type": "tab"}, "tab")):
        assert entry(diff_flows(historical_base, [target, node({"other": "target"})]))["configReferences"]["added"] == [
            ref("/other", "non-config-target", f"{name} [target]")
        ]


def test_unchanged_pointer_id_is_not_a_rewire_unknown_references_stay_opaque_and_raw_data_stays_visible() -> None:
    before = [config(), node({"server": "target", "meta": {"x": 1}, "name": "target", "func": "target", "label": "target"})]
    for target in (None, config({"name": "Rename"}), config({"type": "tab"})):
        after = [
            *([target] if target else []),
            node({"server": "target", "meta": {"x": 2}, "name": "target", "func": "target", "label": "target", "x": 200}),
        ]
        d = entry(diff_flows(before, after, **OPTIONS, ignore_layout=True))
        assert "configReferences" not in d
        assert d["properties"] == [
            {"property": "/meta/x", "before": {"present": True, "value": 1}, "after": {"present": True, "value": 2}, "layout": False}
        ]
    before_unknown = [node({"server": "", "name": "target", "func": "target", "label": "target"})]
    after_unknown = [node({"server": "target", "name": "other", "func": "other", "label": "other"})]
    assert "configReferences" not in entry(diff_flows(before_unknown, after_unknown))
    assert entry(diff_flows(before_unknown, after_unknown, **OPTIONS))["configReferences"]["added"] == [
        ref("/server", "missing-target", "(missing) [target]")
    ]
    assert "configReferences" not in entry(diff_flows([node()], [node({"server": ""})], **OPTIONS))


def test_explicit_arrays_retain_index_identity_duplicate_raw_values_and_escaped_property_paths() -> None:
    before = [node({"a/b~c": ["target", "never"]}), config()]
    after = [node({"a/b~c": ["never", "target", "target"]}), config({"type": "group", "name": "Group"})]
    d = entry(diff_flows(before, after, config_refs=["custom.kind.a/b~c"]))
    assert d["configReferences"] == {
        "removed": [ref("/a~1b~0c/0"), ref("/a~1b~0c/1", "missing-target", "(missing) [never]", "never")],
        "added": [
            ref("/a~1b~0c/1", "non-config-target", "Group [target]"),
            ref("/a~1b~0c/2", "non-config-target", "Group [target]"),
            ref("/a~1b~0c/0", "missing-target", "(missing) [never]", "never"),
        ],
    }
    assert next(p for p in d["properties"] if p["property"] == "/a~1b~0c")["after"]["value"] == ["never", "target", "target"]


def test_cli_config_reasons_distinguish_proposals_final_graph_and_reviewed_decisions_diff_is_read_only(run_cli, sandbox: Path) -> None:
    def write(name: str, value: Any) -> Path:
        p = sandbox / f"{name}.json"
        p.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        return p

    base = [node({"server": "", "broker": "", "type": "mqtt in"})]
    ours = [node({"server": "never", "broker": "target", "type": "mqtt in"}), config({"type": "tab", "name": "Tab"})]
    theirs = [node({"server": "never", "broker": "target", "type": "mqtt in"}), config({"name": "History"})]
    files = [write(str(i), flow) for i, flow in enumerate([base, ours, theirs])]
    out = sandbox / "merged.json"
    report, manifest = Path(f"{out}.conflicts.json"), sandbox / "decisions.json"
    rules = ["--config-ref", "mqtt in.server"]
    inputs = [f.read_bytes() for f in files]
    out.write_text("KEEP", encoding="utf-8")
    report.write_text("REPORT", encoding="utf-8")
    text = run_cli(["diff", files[0], files[1], *rules])
    assert text.returncode == 0, text.stderr
    assert "config /server + (missing) [never] (invalid: missing-target)\n" in text.stdout
    assert "config /broker + Tab [target] (invalid: non-config-target)\n" in text.stdout
    as_json = run_cli(["diff", files[0], files[1], *rules, "--format", "json"])
    assert as_json.returncode == 0, as_json.stderr
    assert out.read_text(encoding="utf-8") == "KEEP"
    assert report.read_text(encoding="utf-8") == "REPORT"
    merge = run_cli(["merge", *files, *rules, "-o", out, "--manifest", manifest])
    assert merge.returncode == 1, merge.stderr
    r = json.loads(report.read_bytes())
    assert r["summary"]["ours"] == json.loads(as_json.stdout)
    assert len(r["integrity"]) == 2
    assert all(i["kind"] == "missing-config" and i["action"] == "review" for i in r["integrity"])
    assert next(e for e in entry(r["summary"]["theirs"])["configReferences"]["added"] if e["property"] == "/broker")["reason"] is None
    assert next(e for e in entry(r["summary"]["merged"])["configReferences"]["added"] if e["property"] == "/broker")["reason"] == "non-config-target"
    assert next(n for n in json.loads(out.read_bytes()) if n["id"] == "source")["server"] == "never"
    # Resolve still refuses retained issues before writes; summaries do not create decisions.
    m = json.loads(manifest.read_bytes())
    for c in m["conflicts"]:
        c["decision"] = {"choice": "theirs"}
    manifest.write_text(json.dumps(m), encoding="utf-8")
    protected = [f.read_bytes() for f in (out, report)]
    assert run_cli(["resolve", *files, "-o", out, "--manifest", manifest]).returncode == 2
    assert [f.read_bytes() for f in (out, report)] == protected
    assert [f.read_bytes() for f in files] == inputs


MALFORMED = [None, False, 3, {}, [["target"]], ["target", None]]


@pytest.mark.parametrize("value", MALFORMED, ids=[json.dumps(v, separators=(",", ":")) for v in MALFORMED])
def test_malformed_declared_config_field_rejects_even_unchanged_nodes_without_replacing_outputs(run_cli, sandbox: Path, value: Any) -> None:
    flow = [node({"server": value})]
    with pytest.raises(InputError):
        diff_flows(flow, flow, **OPTIONS)
    p, out = sandbox / "bad.json", sandbox / "out.json"
    report = Path(f"{out}.conflicts.json")
    p.write_text(json.dumps(flow, separators=(",", ":")), encoding="utf-8")
    out.write_text("KEEP", encoding="utf-8")
    report.write_text("REPORT", encoding="utf-8")
    d = run_cli(["diff", p, p, "--format", "json", "--config-ref", "custom.kind.server"])
    assert d.returncode == 2, d.stderr
    assert d.stdout == ""
    assert run_cli(["merge", p, p, p, "-o", out, "--config-ref", "custom.kind.server"]).returncode == 2
    assert out.read_text(encoding="utf-8") == "KEEP"
    assert report.read_text(encoding="utf-8") == "REPORT"
