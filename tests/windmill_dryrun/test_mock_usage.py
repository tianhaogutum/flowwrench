"""Port of test/mock-usage.test.js."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases

from .helpers import cli, fixture, js, read_json, read_text, sha, write_json

# Contract: the mapping selects modules; each selected record compares in full.
# Shape/id/counter-type errors exit 2; missing/extra record fields fail exit 1.
pytestmark = pytest.mark.needs_node


def usage(expected: int, calls: int) -> dict[str, int]:
    return {"expected": expected, "calls": calls, "consumed": min(expected, calls),
            "remaining": max(0, expected - calls), "extra": max(0, calls - expected)}


def script(module_id: str) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-run", "input_transforms": {}}}


class Writer:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.sequence = 0

    def case(self, mock_usage: Any, overrides: dict[str, Any] | None = None) -> str:
        self.sequence += 1
        return write_json(self.tmp / f"case-{self.sequence}.json", {
            "name": f"usage <{self.sequence}>", "flow": fixture("mock-consumption/flow.yaml"),
            "mocks": fixture("mock-consumption/mocks.yaml"), "input": {"items": ["alice", "bob"]},
            "expect": {"mock_usage": mock_usage}, **(overrides or {}),
        })


@pytest.fixture
def files(tmp_path: Path) -> Writer:
    return Writer(tmp_path)


def test_exact_per_module_mock_usage_assertions_accept_sequence_counters(files: Writer) -> None:
    suite = run_cases([files.case({"send": usage(2, 2)})])
    assert suite["passed"] == 1
    assert suite["cases"][0]["report"]["mock_usage"] == {"send": usage(2, 2)}
    assert run_cases([fixture("mock-consumption/cases/exact.yaml")])["passed"] == 1


def test_mock_usage_asserts_unused_and_exhausted_counts_without_changing_error_behavior(files: Writer) -> None:
    unused = files.case({"send": usage(2, 1)}, {"input": {"items": ["alice"]}})
    gated = files.case(None, {"input": {"items": ["alice"]}, "options": {"requireAllMocksUsed": True},
                              "expect": {"error": {"name": "MockUnused"}, "mock_usage": {"send": usage(2, 1)}}})
    extra = files.case(None, {"input": {"items": ["alice", "bob", "extra"]},
                              "expect": {"error": {"name": "MockExhausted"}, "mock_usage": {"send": usage(2, 3)}}})
    suite = run_cases([unused, gated, extra])
    assert suite["passed"] == 3
    assert suite["cases"][0]["report"]["status"] == "ok"
    for c in suite["cases"][1:]:
        assert c["report"]["mock_errors"] == [c["report"]["error"]]
    no_opt_in = run_cases([files.case({"send": usage(2, 3)}, {"input": {"items": ["alice", "bob", "extra"]}})])
    assert no_opt_in["failed"] == 1
    assert no_opt_in["cases"][0]["failures"][0] == 'status: expected "ok", got "error"'


def test_mock_usage_compares_complete_records_ignores_key_order_and_leaves_other_modules_unconstrained(files: Writer, tmp_path: Path) -> None:
    flow = str(tmp_path / "two-modules.json")
    write_json(flow, {"summary": "two sequences", "value": {"modules": [script("send"), script("other")]}})
    overrides = {"flow": flow, "mocks": {"send": {"sequence": [{"result": 1}]}, "other": {"sequence": [{"result": 2}, {"result": 3}]}}}
    reordered = {"extra": 0, "remaining": 0, "consumed": 1, "calls": 1, "expected": 1}
    assert run_cases([files.case({"send": reordered}, overrides)])["passed"] == 1
    variants: list[dict[str, Any]] = [{**usage(1, 1), field: 2} for field in ["expected", "calls", "consumed", "remaining", "extra"]]
    for field in usage(1, 1):
        variants.append({key: value for key, value in usage(1, 1).items() if key != field})
    variants.extend([{}, {**usage(1, 1), "unexpected": None}])
    suite = run_cases([files.case({"send": record}, overrides) for record in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"mock_usage.send: expected {js(variants[i])}, got {js(usage(1, 1))}"]
        assert c["report"]["mock_usage"] == {"send": usage(1, 1), "other": usage(2, 1)}
    # Known module without an external sequence has no usage record, not zeros.
    missing = run_cases([files.case({"send": usage(0, 0)}, {"mocks": {"send": {"result": 1}}})])
    assert missing["cases"][0]["failures"] == [f"mock_usage.send: expected {js(usage(0, 0))}, got missing"]


def test_mock_usage_includes_retry_and_failure_handler_consumption_preserving_a_primary_error(files: Writer, tmp_path: Path) -> None:
    flow = str(tmp_path / "handler.json")
    write_json(flow, {"summary": "handler counters", "value": {
        "modules": [{**script("send"), "retry": {"constant": {"attempts": 1, "seconds": 0}}}, script("never")], "failure_module": script("failure"),
    }})
    mocks = {"send": {"sequence": [{"error": {"name": "Temporary", "message": "retry"}}, {"error": {"name": "Original", "message": "bad"}}]},
             "never": {"sequence": [{"result": 2}]}, "failure": {"sequence": [{"result": None}]}}
    error = {"name": "Original", "message": "bad", "step_id": "send"}
    mock_errors = [{"name": "MockUnused", "message": "Mock sequence for 'never' has 1 unused response(s): consumed 0 of 1", "step_id": "never"}]
    expected = {"send": usage(2, 2), "never": usage(1, 0), "failure": usage(1, 1)}
    good = {"error": error, "mock_errors": mock_errors, "mock_usage": expected, "failure_result": None}
    suite = run_cases([files.case(None, {"flow": flow, "mocks": mocks, "options": {"requireAllMocksUsed": True}, "expect": good}),
                       files.case(None, {"flow": flow, "mocks": mocks, "options": {"requireAllMocksUsed": True},
                                         "expect": {**good, "mock_usage": {"failure": usage(1, 0)}}})])
    assert suite["passed"] == 1
    assert suite["failed"] == 1
    assert suite["cases"][0]["report"]["error"] == error
    assert suite["cases"][1]["report"] == suite["cases"][0]["report"]
    assert suite["cases"][1]["failures"] == [f"mock_usage.failure: expected {js(usage(1, 0))}, got {js(usage(1, 1))}"]


def test_cli_mock_usage_diffs_survive_json_escaped_junit_and_source_free_replay(files: Writer, tmp_path: Path) -> None:
    source = tmp_path / "standalone"
    # All original files are copied into one removable directory.
    source.mkdir()
    for name in ["flow.yaml", "mocks.yaml"]:
        shutil.copyfile(fixture(f"mock-consumption/{name}"), source / name)
    overrides = {"flow": str(source / "flow.yaml"), "mocks": str(source / "mocks.yaml")}
    paths = [files.case({"send": usage(2, 2)}, overrides), files.case({"send": usage(2, 1)}, overrides)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stderr + result.stdout
    suite, saved = read_json(report), read_json(bundle)
    assert suite["passed"] == 1
    assert suite["failed"] == 1
    failure = f"mock_usage.send: expected {js(usage(2, 1))}, got {js(usage(2, 2))}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    junit = read_text(xml)
    assert junit == saved["junit_xml"]
    assert "usage &lt;" in junit
    assert failure.replace('"', "&quot;") in junit
    shutil.rmtree(source)
    for path in paths:
        os.remove(path)
    for case_id in [1, 2]:
        replay_report = tmp_path / f"replay-{case_id}.json"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", replay_report, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == case_id - 1, replay.stderr + replay.stdout
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(replay_report)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
    saved["cases"][0]["snapshot"]["expect"]["mock_usage"]["send"]["calls"] = -1
    saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
    malformed = tmp_path / "malformed.json"
    write_json(malformed, saved)
    replay = cli("replay", malformed, "--case", "1", cwd=tmp_path)
    assert replay.status == 2, replay.stderr
    assert re.search(r"mock_usage\.send\.calls: expected nonnegative safe integer", replay.stderr)


def test_cli_malformed_mock_usage_shapes_ids_and_counter_types_are_invalid_input(files: Writer, tmp_path: Path) -> None:
    invalid: list[tuple[Any, str]] = [
        *[(value, r"expect\.mock_usage: expected object") for value in [None, [], 1, False, "usage"]],
        ({}, r"expect\.mock_usage: at least one module assertion required"),
        ({"absent": usage(0, 0)}, r"expect\.mock_usage: unknown module absent"),
        *[({"send": value}, r"mock_usage\.send: expected object") for value in [None, [], 1, False, "usage"]],
        *[({"send": {**usage(2, 2), field: value}}, rf"mock_usage\.send\.{field}: expected nonnegative safe integer")
          for field in usage(0, 0) for value in [-1, 0.5, "2", None, True, 2**53]],
    ]
    for value, pattern in invalid:
        result = cli("test", files.case(value), cwd=tmp_path)
        assert result.status == 2, result.stderr + result.stdout
        assert re.search(pattern, result.stderr)
        assert result.stdout == ""
