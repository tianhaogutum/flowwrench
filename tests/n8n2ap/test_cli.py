"""Port of ``test/cli.test.js``."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from flowwrench.n8n2ap import LIMITS, convert

from .helpers import edge, fixture, mapping_path, node, run_cli, start, steps, workflow


def write_input(directory: Path, value: Any = None) -> Path:
    path = directory / "input.json"
    path.write_text(json.dumps(fixture("webhook-http-if") if value is None else value), encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_writes_schema_valid_json_and_markdown_report_permissions_are_private(tmp_path: Path) -> None:
    src, out, report = write_input(tmp_path), tmp_path / "out/flow.json", tmp_path / "out/report.md"
    res = run_cli("convert", src, "-o", out, "--report", report, "--strict")
    assert res.status == 0, res.stderr
    assert re.search(r"4/4 nodes", res.stdout)
    assert read_json(out) == convert(fixture("webhook-http-if"))["template"]
    assert re.search(r"Coverage: \*\*100%\*\*", report.read_text(encoding="utf-8"))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_cli_report_defaults_to_json_and_strict_writes_partial_artifacts_then_exits_2(tmp_path: Path) -> None:
    src, out = write_input(tmp_path, fixture("manual-unsupported")), tmp_path / "flow.json"
    res = run_cli("convert", src, "-o", out, "--strict")
    assert res.status == 2, res.stderr
    assert out.exists()
    report = read_json(Path(str(out) + ".report.json"))
    assert report["complete"] is False
    assert report["counts"]["unsupported"] == 1
    assert run_cli("convert", src, "-o", tmp_path / "non-strict.json").status == 0


def test_dry_run_writes_no_directories_or_files_even_with_explicit_destinations_and_force(tmp_path: Path) -> None:
    src = write_input(tmp_path)
    before = sorted(os.listdir(tmp_path))
    res = run_cli("convert", src, "-o", tmp_path / "missing/flow.json", "--report", tmp_path / "missing/report.md",
                  "--dry-run", "--force")
    assert res.status == 0, res.stderr
    assert json.loads(res.stdout)["coveragePercent"] == 100
    assert sorted(os.listdir(tmp_path)) == before
    assert run_cli("convert", write_input(tmp_path, fixture("manual-unsupported")), "--dry-run", "--strict").status == 2


def test_invalid_json_errors_withhold_source_excerpts_wrong_workflow_shape_fails(tmp_path: Path) -> None:
    src = tmp_path / "bad.json"
    src.write_text('{"password":"DONT_ECHO_THIS",', encoding="utf-8")
    res = run_cli("convert", src, "--dry-run")
    assert res.status == 1
    assert re.search(r"Invalid JSON", res.stderr)
    assert "DONT_ECHO_THIS" not in res.stderr
    src.write_text("{}", encoding="utf-8")
    res = run_cli("convert", src, "--dry-run")
    assert res.status == 1
    assert re.search(r"n8n workflow", res.stderr)


def test_cycle_and_multi_input_merge_cli_exit_codes(tmp_path: Path) -> None:
    w = workflow(start(), node("A", "noOp"))
    w["connections"]["A"] = {"main": [[edge("Start")]]}
    assert re.search(r"Cycle", run_cli("convert", write_input(tmp_path, w), "--dry-run").stderr)
    merge = workflow(start(), node("Merge", "merge", {"mode": "append"}, 3))
    res = run_cli("convert", write_input(tmp_path, merge), "--dry-run", "--strict")
    assert res.status == 2
    assert json.loads(res.stdout)["counts"]["unsupported"] == 1


def test_existing_files_input_paths_and_hardlinks_are_protected_force_replaces_only_outputs(tmp_path: Path) -> None:
    src, out = write_input(tmp_path), tmp_path / "flow.json"
    out.write_text("KEEP", encoding="utf-8")
    res = run_cli("convert", src, "-o", out)
    assert res.status == 1
    assert out.read_text(encoding="utf-8") == "KEEP"
    res = run_cli("convert", src, "-o", out, "--force")
    assert res.status == 0, res.stderr
    assert read_json(out)["flows"]
    before = src.read_text(encoding="utf-8")
    assert run_cli("convert", src, "-o", src, "--force").status == 1
    assert src.read_text(encoding="utf-8") == before
    hard = tmp_path / "hard.json"
    os.link(src, hard)
    assert run_cli("convert", src, "-o", hard, "--force").status == 1
    assert src.read_text(encoding="utf-8") == before


def test_symlink_files_and_symlink_parents_cannot_redirect_writes(tmp_path: Path) -> None:
    src, outside = write_input(tmp_path), tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("KEEP", encoding="utf-8")
    symbolic = tmp_path / "link.json"
    os.symlink(outside / "target", symbolic)
    assert run_cli("convert", src, "-o", symbolic, "--force").status == 1
    assert (outside / "target").read_text(encoding="utf-8") == "KEEP"
    parent_link = tmp_path / "linked"
    os.symlink(outside, parent_link, target_is_directory=True)
    assert run_cli("convert", src, "-o", parent_link / "new.json").status == 1
    assert not (outside / "new.json").exists()


def test_output_root_contains_both_outputs_and_catches_dotdot_traversal(tmp_path: Path) -> None:
    src, root = write_input(tmp_path), tmp_path / "allowed"
    assert run_cli("convert", src, "-o", f"{root}/../escape.json", "--output-root", root).status == 1
    assert not (tmp_path / "escape.json").exists()
    assert run_cli("convert", src, "-o", root / "flow.json", "--report", tmp_path / "report.json",
                   "--output-root", root).status == 1
    assert not root.exists()
    assert run_cli("convert", src, "-o", root / "flow.json", "--output-root", root).status == 0


def test_input_size_bound_non_regular_input_same_destinations_and_directory_output_fail(tmp_path: Path) -> None:
    src, huge = write_input(tmp_path), tmp_path / "huge.json"
    huge.write_text(" " * (LIMITS.bytes + 1), encoding="utf-8")
    assert re.search(r"5 MiB", run_cli("convert", huge, "--dry-run").stderr)
    assert re.search(r"regular JSON file", run_cli("convert", tmp_path, "--dry-run").stderr)
    out = tmp_path / "out"
    assert run_cli("convert", src, "-o", out, "--report", f"{out}.json", "--output-root", out).status == 1
    assert run_cli("convert", src, "-o", tmp_path / "a.json", "--report", tmp_path / "a.json").status == 1
    assert run_cli("convert", src, "-o", tmp_path).status == 1


def test_all_outputs_are_preflighted_before_writing_any_artifact(tmp_path: Path) -> None:
    src, out, report = write_input(tmp_path), tmp_path / "flow.json", tmp_path / "report.md"
    report.write_text("KEEP", encoding="utf-8")
    res = run_cli("convert", src, "-o", out, "--report", report)
    assert res.status == 1
    assert not out.exists()
    assert report.read_text(encoding="utf-8") == "KEEP"


def test_help_version_missing_files_unknown_duplicate_options_and_report_formats(tmp_path: Path) -> None:
    src = write_input(tmp_path)
    assert run_cli("--help").status == 0
    assert re.search(r"0.1.0", run_cli("--version").stdout)
    for args in [[], ["convert"], ["run", src], ["convert", src], ["convert", src, "--wat"],
                 ["convert", src, "--dry-run", "--strict", "--strict"], ["convert", src, "--dry-run", "--trigger"],
                 ["convert", src, "--dry-run", "--report", "report.txt"],
                 ["convert", tmp_path / "missing.json", "--dry-run"]]:
        assert run_cli(*args, cwd=tmp_path).status == 1, [str(a) for a in args]


def test_connection_map_cli_writes_unresolved_basic_http_and_provenance_strict_exits_2(tmp_path: Path) -> None:
    src, map_file, out = write_input(tmp_path, fixture("http-basic")), tmp_path / "connections.json", tmp_path / "flow.json"
    map_file.write_bytes(mapping_path("http-basic").read_bytes())
    res = run_cli("convert", src, "--connection-map", map_file, "-o", out, "--strict")
    assert res.status == 2, res.stderr
    assert re.search(r"1 unresolved; template remains invalid", res.stdout)
    flow, report = read_json(out), read_json(Path(str(out) + ".report.json"))
    assert flow["flows"][0]["valid"] is False
    assert report["complete"] is False
    assert report["connectionPlaceholders"][0]["source"]["node"] == "Fetch"
    assert re.search(r"connection_crm_basic.output.username", json.dumps(flow))
    dry = run_cli("convert", src, "--connection-map", map_file, "--dry-run", "--strict")
    assert dry.status == 2, dry.stderr
    assert json.loads(dry.stdout) == report
    assert run_cli("convert", src, "--connection-map", map_file, "-o", tmp_path / "non-strict.json").status == 0


def test_shared_connection_cli_writes_one_guard_with_per_node_provenance_strict_exits_2(tmp_path: Path) -> None:
    src = write_input(tmp_path, fixture("http-shared"))
    map_file, out, md = tmp_path / "map.json", tmp_path / "flow.json", tmp_path / "report.md"
    map_file.write_bytes(mapping_path("http-shared").read_bytes())
    res = run_cli("convert", src, "--connection-map", map_file, "-o", out, "--report", md, "--strict")
    assert res.status == 2, res.stderr
    assert re.search(r"1 unresolved; template remains invalid", res.stdout)
    flow, markdown = read_json(out), md.read_text(encoding="utf-8")
    assert len([s for s in steps(flow) if s["name"] == "connection_crm_basic"]) == 1
    assert len([s for s in steps(flow) if s["settings"].get("actionName") == "send_request"]) == 2
    assert flow["flows"][0]["valid"] is False
    for name in ["Fetch", "Fetch again"]:
        assert f"| {name} | httpBasicAuth | Example CRM | crm_basic | connection_crm_basic |" in markdown
    dry = run_cli("convert", src, "--connection-map", map_file, "--dry-run", "--strict")
    assert dry.status == 2, dry.stderr
    report = json.loads(dry.stdout)
    assert len(report["connectionPlaceholders"]) == 2
    assert report["complete"] is False
    assert report["coveragePercent"] == 100
    everything = json.dumps({"flow": flow, "report": report, "markdown": markdown, "stdout": res.stdout,
                             "stderr": res.stderr})
    assert "SYNTHETIC_SHARED_CREDENTIAL_ID" not in everything


def test_conflicting_same_name_credential_ids_fail_before_outputs_without_id_disclosure(tmp_path: Path) -> None:
    source, map_file, out = fixture("http-shared"), tmp_path / "map.json", tmp_path / "missing/flow.json"
    source["nodes"][2]["credentials"]["httpBasicAuth"]["id"] = "PRIVATE_CONFLICT_ID"
    src = write_input(tmp_path, source)
    connection_map = read_json(mapping_path("http-shared"))
    for separate in [False, True]:
        if separate:
            connection_map["bindings"][1]["placeholder"] = "other"
        map_file.write_text(json.dumps(connection_map), encoding="utf-8")
        res = run_cli("convert", src, "--connection-map", map_file, "-o", out, "--report",
                      tmp_path / "missing-report/report.md", "--force")
        assert res.status == 1
        assert re.search(r"Conflicting source credential IDs", res.stderr)
        assert not re.search(r"SYNTHETIC_SHARED_CREDENTIAL_ID|PRIVATE_CONFLICT_ID", res.stdout + res.stderr)
        assert not (tmp_path / "missing").exists()
        assert not (tmp_path / "missing-report").exists()


def test_bad_or_missing_connection_bindings_fail_before_creating_any_output_directory(tmp_path: Path) -> None:
    src, map_file, out = write_input(tmp_path, fixture("http-basic")), tmp_path / "map.json", tmp_path / "missing/flow.json"
    literal = {"formatVersion": 1, "bindings": [{"source": {"node": "Fetch", "type": "httpBasicAuth", "name": "Example CRM"},
                                                 "placeholder": "crm", "password": "DONT_ECHO_THIS"}]}
    for value in ['{"formatVersion":1,"bindings":[]}', '{"password":"DONT_ECHO_THIS",', json.dumps(literal)]:
        map_file.write_text(value, encoding="utf-8")
        res = run_cli("convert", src, "--connection-map", map_file, "-o", out, "--force")
        assert res.status == 1
        assert "DONT_ECHO_THIS" not in res.stderr
        assert not (tmp_path / "missing").exists()
    assert run_cli("convert", src, "--connection-map", tmp_path / "absent", "--dry-run").status == 1
    map_file.write_text(" " * (LIMITS.bytes + 1), encoding="utf-8")
    assert re.search(r"5 MiB", run_cli("convert", src, "--connection-map", map_file, "--dry-run").stderr)
    assert run_cli("convert", src, "--connection-map", tmp_path, "--dry-run").status == 1


def test_connection_mapping_input_and_its_hardlinks_cannot_be_overwritten_by_either_artifact(tmp_path: Path) -> None:
    src, map_file = write_input(tmp_path, fixture("http-basic")), tmp_path / "map.json"
    map_file.write_bytes(mapping_path("http-basic").read_bytes())
    before, hard = map_file.read_text(encoding="utf-8"), tmp_path / "hard.json"
    os.link(map_file, hard)
    for target in [map_file, hard]:
        for is_report in [False, True]:
            out = tmp_path / "new.json"
            argv: list[Any] = ["convert", src, "--connection-map", map_file, "-o", out if is_report else target, "--force"]
            if is_report:
                argv += ["--report", target]
            res = run_cli(*argv)
            assert res.status == 1, res.stderr
            assert map_file.read_text(encoding="utf-8") == before
            assert not out.exists()


# --------------------------------------------------------------------------- Python-only additions


def test_program_name_in_errors_and_help(tmp_path: Path) -> None:
    res = run_cli("convert", tmp_path / "missing.json", "--dry-run", cwd=tmp_path)
    assert res.status == 1
    assert res.stdout == ""
    assert res.stderr.startswith("flowwrench n8n2ap: ENOENT: no such file or directory, open ")
    help_text = run_cli("-h").stdout
    assert help_text.startswith("flowwrench n8n2ap 0.1.0 — offline n8n → Activepieces converter\n")
    assert help_text.endswith("strict review needed.\n\n")


@pytest.mark.parametrize("name", ["webhook-http-if", "schedule-set", "manual-unsupported"])
def test_cli_artifacts_match_golden_bytes(tmp_path: Path, name: str) -> None:
    from .helpers import golden_text

    src, out = write_input(tmp_path, fixture(name)), tmp_path / "flow.json"
    assert run_cli("convert", src, "-o", out).status == 0
    expected = json.loads(golden_text(name))
    from flowwrench.n8n2ap.jscompat import stringify

    assert out.read_text(encoding="utf-8") == stringify(expected["template"], 2) + "\n"
    assert Path(str(out) + ".report.json").read_text(encoding="utf-8") == stringify(expected["report"], 2) + "\n"
