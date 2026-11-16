"""Port of test/mock-errors.test.js."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases

from .helpers import clone, cli, fixture, js, read_json, read_text, sha, write_json, xml_escape

pytestmark = pytest.mark.needs_node


def _errors() -> list[dict[str, Any]]:
    errors = []
    for i, recipient in enumerate(["alice", "bob"]):
        actual = ["bob", "alice"][i]
        errors.append({"name": "MockArgsMismatch",
                       "message": f'Mock arguments mismatch for \'send\' at call {i + 1}: expected {{"recipient":"{recipient}"}}, got {{"recipient":"{actual}"}}',
                       "step_id": "send", "call": i + 1, "expected_args": {"recipient": recipient}, "actual_args": {"recipient": actual}})
    return errors


ERRORS = _errors()


class Writer:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.sequence = 0

    def case(self, expected: Any, overrides: dict[str, Any] | None = None) -> str:
        self.sequence += 1
        return write_json(self.tmp / f"case-{self.sequence}.json", {
            "name": f"collected errors <{self.sequence}>", "flow": fixture("mock-arguments/flow.yaml"),
            "mocks": fixture("mock-arguments/mocks.yaml"), "input": {"items": ["bob", "alice"]},
            "options": {"requireAllMocksUsed": True}, "expect": expected, **(overrides or {}),
        })


@pytest.fixture
def files(tmp_path: Path) -> Writer:
    return Writer(tmp_path)


def changed_lists() -> list[Any]:
    def change_second(mutate: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
        errors = clone(ERRORS)
        mutate(errors[1])
        return errors

    def set_call(e: dict[str, Any]) -> None:
        e["call"] = 3

    def set_expected(e: dict[str, Any]) -> None:
        e["expected_args"]["recipient"] = "alice"

    def set_actual(e: dict[str, Any]) -> None:
        e["actual_args"]["recipient"] = "bob"

    def drop_message(e: dict[str, Any]) -> None:
        del e["message"]

    def add_extra(e: dict[str, Any]) -> None:
        e["extra"] = None

    def empty_actual(e: dict[str, Any]) -> None:
        e["actual_args"] = {}

    def string_call(e: dict[str, Any]) -> None:
        e["call"] = "2"

    return [
        change_second(set_call), change_second(set_expected), change_second(set_actual),
        [ERRORS[1], ERRORS[0]], [ERRORS[0]], [*ERRORS, ERRORS[1]], [],
        change_second(drop_message), change_second(add_extra), change_second(empty_actual), change_second(string_call),
    ]


def test_exact_ordered_mock_errors_asserts_both_recovered_same_count_mismatches(files: Writer) -> None:
    suite = run_cases([files.case({"status": "error", "error": {"name": "MockArgsMismatch", "call": 1}, "mock_errors": ERRORS})])
    assert suite["passed"] == 1
    report = suite["cases"][0]["report"]
    assert report["mock_errors"] == ERRORS
    assert report["error"] == ERRORS[0]
    assert report["mock_usage"]["send"] == {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}
    shipped = run_cases([fixture("mock-arguments/cases/all-errors.yaml")])
    assert shipped["passed"] == 1
    assert shipped["cases"][0]["report"]["mock_errors"] == ERRORS


def test_mock_errors_compares_complete_ordered_objects_and_leaves_primary_error_assertions_unchanged(files: Writer) -> None:
    variants = changed_lists()
    suite = run_cases([files.case({"error": {"call": 1}, "mock_errors": mock_errors}) for mock_errors in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"mock_errors: expected {js(variants[i])}, got {js(ERRORS)}"]
        assert c["report"]["error"] == ERRORS[0]
        assert c["report"]["mock_errors"] == ERRORS
    legacy = run_cases([files.case({"error": {"name": "MockArgsMismatch", "call": 1}}),
                        files.case({"error": {"call": 2}, "mock_errors": ERRORS})])
    assert legacy["passed"] == 1
    assert legacy["cases"][1]["failures"] == ["error.call: expected 2, got 1"]


def test_empty_mock_errors_normalizes_absence_without_opting_into_runtime_errors(files: Writer, tmp_path: Path) -> None:
    ok = files.case({"mock_errors": []}, {"input": {"items": ["alice", "bob"]}})
    scripted = {"send": {"error": {"name": "Temporary", "message": "try later"}}}
    failed = files.case({"status": "error", "mock_errors": []}, {"mocks": scripted, "options": {}})
    default_status = files.case({"mock_errors": []}, {"mocks": scripted, "options": {}})
    # Use an unrecovered scripted failure so the legacy default status is tested.
    flow = str(tmp_path / "scripted-flow.json")
    write_json(flow, {"summary": "scripted", "value": {"modules": [{"id": "send", "value": {
        "type": "script", "path": "/never-load", "input_transforms": {},
    }}]}})
    for path in [failed, default_status]:
        c = read_json(path)
        c["flow"] = flow
        write_json(path, c)
    suite = run_cases([ok, failed, default_status, files.case({"mock_errors": ERRORS})])
    assert suite["passed"] == 2
    assert suite["failed"] == 2
    assert all("mock_errors" not in c["report"] for c in suite["cases"][:3])
    assert suite["cases"][2]["failures"] == ['status: expected "ok", got "error"']
    assert re.search(r'^status: expected "ok", got "error"$', suite["cases"][3]["failures"][0])


def test_mock_errors_uses_deep_json_equality_and_supports_exhaustion_unused_errors_alongside_a_primary_runtime_error(files: Writer, tmp_path: Path) -> None:
    flow = str(tmp_path / "nested-flow.json")
    payload = {"list": [1, None, False], "nested": {"value": "x"}}
    write_json(flow, {"summary": "nested", "value": {"modules": [{"id": "send", "value": {
        "type": "script", "path": "/never-load", "input_transforms": {"payload": {"type": "static", "value": payload}},
    }}]}})
    expected = {"name": "MockArgsMismatch", "message": f"Mock arguments mismatch for 'send' at call 1: expected {{}}, got {js({'payload': payload})}",
                "step_id": "send", "call": 1, "expected_args": {}, "actual_args": {"payload": {"nested": {"value": "x"}, "list": [1, None, False]}}}
    overrides = {"flow": flow, "mocks": {"send": {"sequence": [{"expect_args": {}, "result": None}]}}}
    assert run_cases([files.case({"error": {"call": 1}, "mock_errors": [expected]}, overrides)])["passed"] == 1
    for values in [[None, 1, False], [1, "null", False], [1, False]]:
        changed = clone(expected)
        changed["actual_args"]["payload"]["list"] = values
        assert run_cases([files.case({"status": "error", "mock_errors": [changed]}, overrides)])["failed"] == 1
    unused = {"name": "MockUnused", "message": "Mock sequence for 'send' has 1 unused response(s): consumed 0 of 1", "step_id": "send"}
    exhaustion = {"name": "MockExhausted", "message": "Mock sequence exhausted for 'send' at call 3", "step_id": "send"}
    suite = run_cases([
        files.case({"status": "error", "mock_errors": [exhaustion]}, {"input": {"items": ["alice", "bob", "extra"]}}),
        files.case({"error": {"name": "IteratorError"}, "mock_errors": [unused]},
                   {"input": {"items": "invalid"}, "mocks": {"send": {"sequence": [{"result": 1}]}}}),
    ])
    assert suite["passed"] == 2, js([c["failures"] for c in suite["cases"]])
    assert suite["cases"][1]["report"]["error"]["name"] == "IteratorError"


def test_cli_mock_errors_diffs_persist_in_json_escaped_junit_and_source_free_replay(files: Writer, tmp_path: Path) -> None:
    cases = [files.case({"status": "error", "mock_errors": ERRORS}),
             *[files.case({"status": "error", "error": {"call": 1}, "mock_errors": mock_errors}) for mock_errors in changed_lists()]]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "saved.json"
    result = cli("test", *cases, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stderr + result.stdout
    suite, saved = read_json(report), read_json(bundle)
    assert suite["passed"] == 1
    assert suite["failed"] == len(cases) - 1
    junit = read_text(xml)
    assert junit == saved["junit_xml"]
    assert "collected errors &lt;" in junit
    for c in suite["cases"][1:]:
        assert c["report"]["error"] == ERRORS[0]
        assert c["failures"][0] in result.stdout
        assert xml_escape(c["failures"][0]) in junit
    for path in cases:
        os.remove(path)
    # Remove every original source path in the saved metadata. Replay must only
    # use the captured snapshots, including the exact mock_errors expectation.
    for c in saved["cases"]:
        c["path"] = "/missing/case.json"
        c["flowPath"] = "/missing/flow.yaml"
        c["outcome"]["path"] = c["path"]
        c["hashes"]["outcome"] = sha(c["outcome"])
    write_json(bundle, saved)
    for case_id in [1, 2, 5]:
        replay_report = tmp_path / f"replay-{case_id}.json"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", replay_report, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (0 if case_id == 1 else 1), replay.stderr + replay.stdout
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(replay_report)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
    # A correctly rehashed malformed expectation must still fail shape validation.
    saved["cases"][0]["snapshot"]["expect"]["mock_errors"] = [None]
    saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
    invalid = tmp_path / "malformed-bundle.json"
    write_json(invalid, saved)
    malformed = cli("replay", invalid, "--case", "1", cwd=tmp_path)
    assert malformed.status == 2, malformed.stderr
    assert re.search(r"expect\.mock_errors\[0\]: expected object", malformed.stderr)


def test_cli_rejects_non_array_mock_errors_and_non_object_elements_as_invalid_input(files: Writer, tmp_path: Path) -> None:
    for value in [None, {}, "error", 1, False]:
        result = cli("test", files.case({"status": "error", "mock_errors": value}), cwd=tmp_path)
        assert result.status == 2, result.stderr
        assert re.search(r"expect\.mock_errors: expected array", result.stderr)
        assert result.stdout == ""
    for value in [None, [], "error", 1, False]:
        result = cli("test", files.case({"status": "error", "mock_errors": [ERRORS[0], value]}), cwd=tmp_path)
        assert result.status == 2, result.stderr
        assert re.search(r"expect\.mock_errors\[1\]: expected object", result.stderr)
        assert result.stdout == ""
