"""Port of test/bundle.test.js."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .helpers import clone, cli, fixture, read_json, read_text, sha, write_json

pytestmark = pytest.mark.needs_node


def capture_fixture(tmp: Path, name: str) -> dict[str, Any]:
    path = tmp / f"{name}.json"
    failure = fixture("diagnostics/failure.yaml")
    result = cli("test", failure, failure, "--bundle", path, cwd=tmp)
    assert result.status == 1, result.stderr
    return read_json(path)


def test_bundle_captures_resolved_cases_and_replays_just_one_failure_after_all_source_files_are_deleted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ["flow.yaml", "input.json", "mocks.yaml", "failure.yaml"]:
        shutil.copyfile(fixture(f"diagnostics/{name}"), source / name)
    passing = write_json(source / "pass.json", {"name": "default passes", "flow": "flow.yaml", "input": {"route": "other"},
                                                "expect": {"result": {"route": "other"}}})
    bundle, report = tmp_path / "saved.json", tmp_path / "saved-report.json"
    result = cli("test", passing, source / "failure.yaml", "--bundle", bundle, "--report", report, cwd=tmp_path)
    assert result.status == 1, result.stderr
    assert re.search("2 cases, 1 passed, 1 failed", result.stdout)
    saved = read_json(bundle)
    failed = saved["cases"][1]
    assert saved["format"] == "openflow-dryrun-bundle"
    assert saved["version"] == 1
    assert failed["id"] == 2
    assert failed["snapshot"]["input"] == {"route": "paid"}
    for name in ["flow", "input", "mocks", "expect", "options"]:
        assert failed["hashes"][name] == sha(failed["snapshot"][name])
    assert failed["hashes"]["outcome"] == sha(failed["outcome"])
    assert failed["selected_branches"] == ["choose:0"]
    assert [[c["module_id"], c["call"], c["attempt"], c["source"]] for c in failed["mock_calls"]] == [
        ["work", 1, 1, "sequence"], ["work", 2, 2, "sequence"],
    ]
    assert [failed["outcome"]["report"]["trace"][c["trace_index"]]["args"] for c in failed["mock_calls"]] == [{"route": "paid"}, {"route": "paid"}]
    assert failed["junit"]["href"] == "saved.json.junit.xml"
    assert failed["junit"]["testcase_index"] == 1
    assert failed["junit"]["name"] == "paid retry returns wrong result"
    assert read_text(tmp_path / failed["junit"]["href"]) == saved["junit_xml"]
    assert re.search(r'tests="2" failures="1"', saved["junit_xml"])
    assert os.stat(bundle).st_mode & 0o777 == 0o600
    shutil.rmtree(source)
    moved = tmp_path / "moved"
    moved.mkdir()
    standalone = moved / "only-bundle.json"
    shutil.copyfile(bundle, standalone)
    replay_report, xml = moved / "replay.json", moved / "replay.xml"
    replay = cli("replay", standalone, "--case", "2", "--report", replay_report, "--junit", xml, "--output-dir", moved, cwd=tmp_path)
    assert replay.status == 1, replay.stderr
    assert re.search("1 cases, 0 passed, 1 failed", replay.stdout)
    actual = read_json(replay_report)
    assert actual["replay"]["reproduced"] is True
    assert actual["replay"]["case"] == 2
    assert actual["cases"][0] == failed["outcome"]
    assert read_json(report)["cases"][1] == failed["outcome"]
    assert re.search("expected 99, got 7", read_text(xml))
    success = cli("replay", standalone, "--case=1", cwd=tmp_path)
    assert success.status == 0, success.stderr
    assert re.search("REPLAY case 1: reproduced", success.stdout)


def test_runtime_failure_bundles_preserve_effective_cli_overrides_and_link_an_explicit_junit_path(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    source.mkdir()
    flow = {"summary": "offline", "value": {"modules": [{"id": "work", "value": {"type": "script", "path": "/never-load", "input_transforms": {}}}]}}
    write_json(source / "flow.json", flow)
    path = write_json(source / "case.json", {"flow": "flow.json", "mocks": {"work": {"sequence": [{"result": None}, {"result": 2}]}},
                                             "options": {"requireAllMocksUsed": False}, "expect": {"result": None}})
    bundle, xml = source / "bundle.json", tmp_path / "explicit.xml"
    result = cli("test", path, "--require-all-mocks-used=true", "--bundle", bundle, "--junit", xml, cwd=tmp_path)
    assert result.status == 1, result.stderr
    saved = read_json(bundle)
    assert saved["cases"][0]["snapshot"]["options"]["requireAllMocksUsed"] is True
    assert saved["cases"][0]["snapshot"]["options"]["maxSteps"] == 1000
    assert saved["cases"][0]["junit"]["href"] == "../explicit.xml"
    assert saved["cases"][0]["outcome"]["report"]["error"]["name"] == "MockUnused"
    os.remove(path)
    os.remove(source / "flow.json")
    report = source / "replayed.json"
    replay = cli("replay", bundle, "--case", "1", "--report", report, cwd=tmp_path)
    assert replay.status == 1, replay.stderr
    assert read_json(report)["replay"]["reproduced"] is True
    assert read_json(report)["cases"][0]["report"]["error"]["name"] == "MockUnused"


def test_replay_rejects_corrupt_hashes_unsupported_versions_selectors_and_execution_overrides(tmp_path: Path) -> None:
    original = capture_fixture(tmp_path, "corruption-baseline")

    def set_input_route(b: dict[str, Any]) -> None:
        b["cases"][1]["snapshot"]["input"]["route"] = "other"

    def set_expect(b: dict[str, Any]) -> None:
        b["cases"][1]["snapshot"]["expect"]["result"] = 7

    def set_outcome(b: dict[str, Any]) -> None:
        b["cases"][1]["outcome"]["report"]["result"] = 9

    def set_version(b: dict[str, Any]) -> None:
        b["version"] = 2

    def set_tool(b: dict[str, Any]) -> None:
        b["tool"]["version"] = "99.0.0"

    def set_max_steps(b: dict[str, Any]) -> None:
        b["cases"][1]["snapshot"]["options"]["maxSteps"] = 10001
        b["cases"][1]["hashes"]["options"] = sha(b["cases"][1]["snapshot"]["options"])

    def set_proto(b: dict[str, Any]) -> None:
        b["cases"][1]["snapshot"]["input"] = json.loads('{"__proto__":{"polluted":true}}')
        b["cases"][1]["hashes"]["input"] = sha(b["cases"][1]["snapshot"]["input"])

    corruptions: list[tuple[Callable[[dict[str, Any]], None], str]] = [
        (set_input_route, "hash mismatch.*input"), (set_expect, "hash mismatch.*expect"),
        (set_outcome, "hash mismatch.*outcome"), (set_version, "version"), (set_tool, "tool version"),
        (set_max_steps, "maxSteps.*integer"), (set_proto, "forbidden key"),
    ]
    for i, (mutate, pattern) in enumerate(corruptions):
        value = clone(original)
        mutate(value)
        path = write_json(tmp_path / f"corrupt-{i}.json", value)
        result = cli("replay", path, "--case", "2", cwd=tmp_path)
        assert result.status == 2, result.stderr
        assert re.search(pattern, result.stderr)
        assert result.stdout == ""
    for args in [[], ["--case", "0"], ["--case", "3"], ["--case", "1.5"], ["--case", "1", "--max-steps", "1"], ["--case", "1", "--input", "missing"]]:
        result = cli("replay", tmp_path / "corruption-baseline.json", *args, cwd=tmp_path)
        assert result.status == 2, result.stderr


def test_replay_detects_execution_drift_instead_of_claiming_reproduction_on_a_changed_saved_outcome(tmp_path: Path) -> None:
    value = capture_fixture(tmp_path, "drift-baseline")
    value["cases"][1]["outcome"]["report"]["result"] = 9
    value["cases"][1]["hashes"]["outcome"] = sha(value["cases"][1]["outcome"])
    path = write_json(tmp_path / "drift.json", value)
    report = tmp_path / "drift-report.json"
    result = cli("replay", path, "--case", "2", "--report", report, cwd=tmp_path)
    assert result.status == 1, result.stderr
    assert read_json(report)["replay"]["reproduced"] is False
    assert re.search("ReplayMismatch", result.stdout)


def test_bundles_larger_than_normal_inputs_replay_but_oversized_bundles_and_output_overwrite_reject(tmp_path: Path) -> None:
    source = tmp_path / "large"
    source.mkdir()
    write_json(source / "flow.json", {"summary": "large", "value": {"modules": [{"id": "echo", "value": {"type": "identity"}}]}})
    path = write_json(source / "case.json", {"flow": "flow.json", "input": {"text": "x" * 600000}, "expect": {"status": "ok"}})
    bundle = source / "large.json"
    assert cli("test", path, "--bundle", bundle, cwd=tmp_path).status == 0
    assert os.stat(bundle).st_size > 1024 * 1024
    assert cli("replay", bundle, "--case", "1", cwd=tmp_path).status == 0
    before = read_text(bundle)
    assert cli("test", path, "--bundle", bundle, cwd=tmp_path).status == 2
    assert read_text(bundle) == before
    outside = tmp_path / "outside.json"
    assert cli("test", path, "--bundle", outside, "--output-dir", source, cwd=tmp_path).status == 2
    assert not outside.exists()
    oversized = source / "oversized.json"
    oversized.write_text("{}", encoding="utf-8")
    os.truncate(oversized, 64 * 1024 * 1024 + 1)
    result = cli("replay", oversized, "--case", "1", cwd=tmp_path)
    assert result.status == 2
    assert re.search("64 MiB", result.stderr)


def test_nested_loop_diagnostics_can_wrap_deep_valid_mock_values_without_relaxing_source_depth_limits(tmp_path: Path) -> None:
    source = tmp_path / "deep"
    source.mkdir()
    module: dict[str, Any] = {"id": "leaf", "value": {"type": "script", "path": "f/never-loaded", "input_transforms": {}}}
    for i in range(18):
        module = {"id": f"loop{i}", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": [0]},
                                              "skip_failures": False, "modules": [module]}}
    write_json(source / "flow.json", {"summary": "deep valid flow", "value": {"modules": [module]}})
    value: Any = None
    for _ in range(60):
        value = {"item": value}
    write_json(source / "mocks.json", {"leaf": {"result": value}})
    path = write_json(source / "case.json", {"flow": "flow.json", "mocks": "mocks.json", "expect": {"status": "ok"}})
    bundle = source / "deep.json"
    capture = cli("test", path, "--bundle", bundle, cwd=tmp_path)
    assert capture.status == 0, capture.stderr
    for name in [path, source / "flow.json", source / "mocks.json"]:
        os.remove(name)
    replay = cli("replay", bundle, "--case", "1", cwd=tmp_path)
    assert replay.status == 0, replay.stderr
    assert re.search("reproduced", replay.stdout)
