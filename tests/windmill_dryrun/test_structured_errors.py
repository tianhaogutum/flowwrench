"""Port of test/structured-errors.test.js."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases

from .helpers import Files, cli, fixture, read_json, read_text, write_json

# Contract: supplied error fields compare exactly. call is a positive safe
# integer; argument details are objects. Absent details differ from JSON null.
pytestmark = pytest.mark.needs_node

DETAILS = {"name": "MockArgsMismatch", "step_id": "send", "call": 2,
           "expected_args": {"recipient": "bob"}, "actual_args": {"recipient": "alice"}}


class Cases(Files):
    def case(self, error: Any, overrides: dict[str, Any] | None = None) -> str:
        self.sequence += 1
        return write_json(self.base / f"case-{self.sequence}.json", {
            "flow": fixture("mock-arguments/flow.yaml"), "mocks": fixture("mock-arguments/mocks.yaml"),
            "input": {"items": ["alice", "alice"]}, "expect": {"error": error}, **(overrides or {}),
        })


@pytest.fixture
def files(tmp_path: Path) -> Cases:
    return Cases(tmp_path)


def test_case_assertions_accept_second_call_structured_error_details_and_legacy_strings(files: Cases) -> None:
    full = {**DETAILS, "message": "Mock arguments mismatch for 'send' at call 2: expected {\"recipient\":\"bob\"}, got {\"recipient\":\"alice\"}"}
    suite = run_cases([files.case(full), files.case({"name": full["name"], "message": full["message"], "step_id": "send"}),
                       files.case({"call": 2}), files.case({"expected_args": {"recipient": "bob"}}),
                       files.case({"actual_args": {"recipient": "alice"}})])
    assert suite["passed"] == 5
    assert suite["failed"] == 0
    assert suite["cases"][0]["report"]["error"] == full


def test_argument_detail_assertions_use_complete_deep_json_equality(files: Cases, tmp_path: Path) -> None:
    actual = {"payload": {"tags": ["one", "two"], "enabled": False, "count": 0, "nil": None}, "recipient": "alice"}
    guard = {"recipient": "bob", "payload": {"nil": None, "count": 0, "enabled": False, "tags": ["one", "two"]}}
    mocks = {"send": {"sequence": [{"expect_args": guard, "result": 1}]}}
    flow = str(tmp_path / "nested-flow.json")
    write_json(flow, {"summary": "Nested argument assertion", "value": {"modules": [{"id": "send", "value": {
        "type": "script", "path": "/never-execute",
        "input_transforms": {key: {"type": "static", "value": value} for key, value in actual.items()},
    }}]}})
    expected = {"call": 1, "expected_args": guard, "actual_args": {"recipient": "alice", "payload": {
        "nil": None, "count": 0, "enabled": False, "tags": ["one", "two"]}}}
    assert run_cases([files.case(expected, {"flow": flow, "mocks": mocks})])["passed"] == 1
    variants = [
        {**expected, "call": 2},
        {**expected, "expected_args": {**guard, "extra": None}},
        {**expected, "expected_args": {"recipient": "bob"}},
        {**expected, "actual_args": {**actual, "extra": None}},
        {**expected, "actual_args": {"recipient": "alice"}},
        {**expected, "actual_args": {**actual, "payload": {**actual["payload"], "tags": ["two", "one"]}}},
        {**expected, "actual_args": {**actual, "payload": {**actual["payload"], "count": "0"}}},
        {**expected, "actual_args": {**actual, "payload": {"tags": ["one", "two"], "enabled": False, "count": 0}}},
    ]
    suite = run_cases([files.case(error, {"flow": flow, "mocks": mocks}) for error in variants])
    assert suite["failed"] == len(variants)
    assert all(len(c["failures"]) == 1 and re.search(r"^error\.(call|expected_args|actual_args): expected .+, got .+", c["failures"][0])
               for c in suite["cases"])
    empty_flow = str(tmp_path / "empty-args-flow.json")
    write_json(empty_flow, {"summary": "Empty argument details", "value": {"modules": [{
        "id": "send", "value": {"type": "script", "path": "/never-execute", "input_transforms": {}},
    }]}})
    assert run_cases([files.case({"expected_args": guard, "actual_args": {}}, {"flow": empty_flow, "mocks": mocks})])["passed"] == 1
    assert run_cases([files.case({"expected_args": {}, "actual_args": actual}, {
        "flow": flow, "mocks": {"send": {"sequence": [{"expect_args": {}, "result": 1}]}},
    })])["passed"] == 1


def test_cli_structured_assertions_pass_and_retain_readable_field_differences_in_json_junit_and_replay(files: Cases, tmp_path: Path) -> None:
    good = files.case(DETAILS)
    ok = cli("test", good, cwd=tmp_path)
    assert ok.status == 0, ok.stderr + ok.stdout
    bad = files.case({**DETAILS, "call": 1, "expected_args": {"recipient": "alice"}, "actual_args": {"recipient": "bob"}})
    key_variants = [files.case(error) for error in [
        {**DETAILS, "expected_args": {"recipient": "bob", "extra": None}}, {**DETAILS, "expected_args": {}},
        {**DETAILS, "actual_args": {"recipient": "alice", "extra": None}}, {**DETAILS, "actual_args": {}},
    ]]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    failed = cli("test", bad, *key_variants, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert failed.status == 1, failed.stderr + failed.stdout
    failures = [
        "error.call: expected 1, got 2",
        'error.expected_args: expected {"recipient":"alice"}, got {"recipient":"bob"}',
        'error.actual_args: expected {"recipient":"bob"}, got {"recipient":"alice"}',
    ]
    suite = read_json(report)
    assert suite["failed"] == 5
    assert suite["cases"][0]["failures"] == failures
    assert all(len(c["failures"]) == 1 and re.search(r"^error\.(expected_args|actual_args):", c["failures"][0]) for c in suite["cases"][1:])
    assert suite["cases"][0]["report"]["error"]["call"] == 2
    for failure in failures:
        assert failure in failed.stdout
    junit = read_text(xml)
    assert re.search('failures="5"', junit)
    for failure in failures:
        assert failure.replace('"', "&quot;") in junit
    replay = cli("replay", bundle, "--case", "1", cwd=tmp_path)
    assert replay.status == 1, replay.stderr
    assert re.search("REPLAY case 1: reproduced", replay.stdout)
    for failure in failures:
        assert failure in replay.stdout
    pass_bundle = tmp_path / "passing-bundle.json"
    assert cli("test", good, "--bundle", pass_bundle, "--output-dir", tmp_path, cwd=tmp_path).status == 0
    replay_pass = cli("replay", pass_bundle, "--case", "1", cwd=tmp_path)
    assert replay_pass.status == 0, replay_pass.stderr
    assert re.search("REPLAY case 1: reproduced", replay_pass.stdout)


def test_cli_absent_error_details_fail_as_missing_including_empty_argument_objects(files: Cases, tmp_path: Path) -> None:
    # A normal scripted error contains no call or argument details.
    flow = str(tmp_path / "failure-flow.json")
    write_json(flow, {"summary": "Unrecovered scripted error", "value": {"modules": [{
        "id": "send", "value": {"type": "script", "path": "/never-execute", "input_transforms": {}},
    }]}})
    path = files.case({"name": "Temporary", "call": 1, "expected_args": {}, "actual_args": {"recipient": None}}, {
        "flow": flow, "mocks": {"send": {"error": {"name": "Temporary", "message": "try later"}}},
    })
    report = tmp_path / "missing.json"
    result = cli("test", path, "--report", report, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stderr
    failures = read_json(report)["cases"][0]["failures"]
    assert failures == ["error.call: expected 1, got missing", "error.expected_args: expected {}, got missing",
                        'error.actual_args: expected {"recipient":null}, got missing']
    for failure in failures:
        assert failure in result.stdout
    no_error = files.case({"call": 1, "expected_args": {}, "actual_args": {}}, {"input": {"items": ["alice", "bob"]}})
    absent = cli("test", no_error, cwd=tmp_path)
    assert absent.status == 1, absent.stderr
    assert re.search(r'status: expected "error", got "ok"', absent.stdout)
    assert re.search(r"error.actual_args: expected \{\}, got missing", absent.stdout)


def test_cli_malformed_structured_and_legacy_error_assertions_are_invalid_input(files: Cases, tmp_path: Path) -> None:
    invalid: list[tuple[Any, str]] = [
        *[(error, r"expect\.error") for error in [None, {}, [], "wrong"]],
        *[({"call": call}, r"error\.call: expected positive safe integer") for call in [0, -1, 1.5, "2", None, True, 2**53]],
        *[({field: value}, rf"error\.{field}: expected object")
          for field in ["expected_args", "actual_args"] for value in [None, [], "x", 1, False]],
        *[({field: 2}, rf"error\.{field}: expected string") for field in ["name", "message", "step_id"]],
        ({"typo": 2}, "unsupported field 'typo'"),
    ]
    for error, pattern in invalid:
        r = cli("test", files.case(error), cwd=tmp_path)
        assert r.status == 2, {"error": error, "stdout": r.stdout, "stderr": r.stderr}
        assert re.search(pattern, r.stderr)
        assert r.stdout == ""
