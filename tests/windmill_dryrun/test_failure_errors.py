"""Port of test/failure-errors.test.js."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, cli, js, read_json, read_text, sha

# Contract: failure_error has the same nonempty structured shape as error,
# compares only supplied fields on the secondary error, and never opts into status:error.
pytestmark = pytest.mark.needs_node

ORIGINAL = {"name": "Original", "message": "bad", "step_id": "bad"}
BAD = {"error": {"name": "Original", "message": "bad"}}
PRIMARY = {"error": {"name": "Original"}}
ACTUAL_ARGS = {"recipient": "alice", "payload": {"tags": ["one", "two"], "enabled": False, "count": 0, "nil": None}}
EXPECTED_ARGS = {"payload": {"nil": None, "count": 0, "enabled": False, "tags": ["one", "two"]}, "recipient": "bob"}
MISMATCH = {"name": "MockArgsMismatch",
            "message": f"Mock arguments mismatch for 'failure' at call 2: expected {js(EXPECTED_ARGS)}, got {js(ACTUAL_ARGS)}",
            "step_id": "failure", "call": 2, "expected_args": EXPECTED_ARGS, "actual_args": ACTUAL_ARGS}
_NO_HANDLER: Any = object()


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(handler: Any = _NO_HANDLER, modules: list[Any] | None = None) -> dict[str, Any]:
    handler = script("failure") if handler is _NO_HANDLER else handler
    return {"summary": "Separate handler errors", "value": {
        "modules": [script("bad")] if modules is None else modules, **({"failure_module": handler} if handler else {}),
    }}


class Cases(Files):
    def make_case(self, failure_error: Any, overrides: dict[str, Any] | None = None) -> str:
        return self.write({
            "flow": self.write(flow()), "mocks": {"bad": BAD, "failure": {"error": {"name": "HandlerError", "message": "handler <&雪>"}}},
            "expect": {**PRIMARY, "failure_error": failure_error}, **(overrides or {}),
        })

    def guarded(self) -> dict[str, Any]:
        handler = script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 0}}})
        handler["value"]["input_transforms"] = {key: {"type": "static", "value": value} for key, value in ACTUAL_ARGS.items()}
        return {"flow": self.write(flow(handler)), "mocks": {"bad": BAD, "failure": {"sequence": [
            {"error": {"message": "retry"}}, {"expect_args": EXPECTED_ARGS, "error": {"message": "must not run"}},
        ]}}}


@pytest.fixture
def files(tmp_path: Path) -> Cases:
    return Cases(tmp_path)


def test_failure_error_asserts_handler_retry_exhaustion_independently_of_primary_error(files: Cases) -> None:
    f = flow(script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 0}}}))
    mocks = {"bad": BAD, "failure": {"sequence": [{"error": {"message": "handler retry"}}]}}
    secondary = {"name": "MockExhausted", "message": "Mock sequence exhausted for 'failure' at call 2", "step_id": "failure"}
    suite = run_cases([files.make_case(secondary, {"flow": files.write(f), "mocks": mocks})])
    assert suite["passed"] == 1
    report = suite["cases"][0]["report"]
    assert report["error"] == ORIGINAL
    assert report["failure_error"] == secondary
    assert report["mock_errors"] == [secondary]
    assert report["mock_usage"]["failure"] == {"expected": 1, "calls": 2, "consumed": 1, "remaining": 0, "extra": 1}
    assert [e["mock"] for e in report["trace"]] == [{"source": "external", "call": 1}, {"source": "sequence", "call": 1}, {"source": "sequence", "call": 2}]
    assert report == run_flow(f, {}, mocks)


def test_failure_error_supports_exact_structured_argument_fields_and_every_single_field_assertion(files: Cases) -> None:
    overrides = files.guarded()
    suite = run_cases([files.make_case(MISMATCH, overrides), *[files.make_case({key: value}, overrides) for key, value in MISMATCH.items()]])
    assert suite["passed"] == 7
    assert suite["cases"][0]["report"]["error"] == ORIGINAL
    assert suite["cases"][0]["report"]["failure_error"] == MISMATCH
    assert suite["cases"][0]["report"]["mock_errors"] == [MISMATCH]
    assert suite["cases"][0]["report"] == run_flow(read_json(overrides["flow"]), {}, overrides["mocks"])
    variants: list[dict[str, Any]] = [
        {"name": "Original"}, {"message": "bad"}, {"step_id": "bad"}, {"call": 1},
        {"expected_args": {}}, {"actual_args": {}}, {"expected_args": {**EXPECTED_ARGS, "extra": None}},
        {"actual_args": {"recipient": "alice"}},
        {"actual_args": {**ACTUAL_ARGS, "payload": {**ACTUAL_ARGS["payload"], "tags": ["two", "one"]}}},
        {"actual_args": {**ACTUAL_ARGS, "payload": {**ACTUAL_ARGS["payload"], "count": "0"}}},
        {"actual_args": {**ACTUAL_ARGS, "payload": {"tags": ["one", "two"], "enabled": False, "count": 0}}},
    ]
    failed = run_cases([files.make_case(value, overrides) for value in variants])
    assert failed["failed"] == len(variants)
    for i, c in enumerate(failed["cases"]):
        field, value = next(iter(variants[i].items()))
        assert c["failures"] == [f"failure_error.{field}: expected {js(value)}, got {js(MISMATCH[field])}"]
        assert c["report"] == suite["cases"][0]["report"]
    # Empty argument objects are valid values, distinct from absent details.
    f = files.write(flow())
    for guard, actual in [({}, {"recipient": "alice"}), ({"recipient": "bob"}, {})]:
        value = read_json(f)
        value["value"]["failure_module"]["value"]["input_transforms"] = {key: {"type": "static", "value": item} for key, item in actual.items()}
        c = files.make_case({"expected_args": guard, "actual_args": actual}, {
            "flow": files.write(value), "mocks": {"bad": BAD, "failure": {"sequence": [{"expect_args": guard, "result": None}]}},
        })
        assert run_cases([c])["passed"] == 1


def test_failure_error_scripted_and_absent_details_fail_per_field_missing_successful_handlers_stay_absent(files: Cases) -> None:
    full = {"name": "HandlerError", "message": "handler <&雪>", "step_id": "failure"}
    good = run_cases([files.make_case(full)])
    assert good["passed"] == 1
    assert good["cases"][0]["report"]["failure_error"] == full
    assert good["cases"][0]["report"]["error"] == ORIGINAL
    assert "mock_errors" not in good["cases"][0]["report"]
    fields = {"call": 1, "expected_args": {}, "actual_args": {"recipient": None}}
    missing_details = run_cases([files.make_case(fields)])
    assert missing_details["cases"][0]["failures"] == [f"failure_error.{key}: expected {js(value)}, got missing" for key, value in fields.items()]
    scenarios = [
        {"flow": files.write(flow(None)), "mocks": {"bad": BAD}, "expect": {**PRIMARY, "failure_error": full}},
        {"mocks": {"bad": BAD, "failure": {"result": None}}, "expect": {**PRIMARY, "failure_error": full, "failure_result": None}},
        {"flow": files.write(flow(script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 0}}}))),
         "mocks": {"bad": BAD, "failure": {"sequence": [{"error": {"message": "retry"}}, {"result": None}]}},
         "expect": {**PRIMARY, "failure_error": full, "failure_result": None}},
        {"mocks": {"bad": {"result": 1}, "failure": {"result": 2}}, "expect": {"failure_error": full}},
    ]
    for overrides in scenarios:
        suite = run_cases([files.make_case(full, overrides)])
        assert suite["failed"] == 1
        assert suite["cases"][0]["failures"] == [f"failure_error.{key}: expected {js(value)}, got missing" for key, value in full.items()]
        assert "failure_error" not in suite["cases"][0]["report"]


def test_failure_error_leaves_the_existing_default_expected_status_unchanged(files: Cases) -> None:
    value = {"name": "HandlerError"}
    c = read_json(files.make_case(value))
    suite = run_cases([
        files.write({**c, "expect": {"failure_error": value}}),
        files.write({**c, "expect": {"status": "error", "failure_error": value}}),
        files.write({**c, "expect": {**PRIMARY, "failure_error": value}}),
        files.write({**c, "expect": {"error": {"name": "HandlerError"}, "failure_error": value}}),
    ])
    assert suite["passed"] == 2
    assert suite["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    assert suite["cases"][3]["failures"] == ['error.name: expected "HandlerError", got "Original"']
    for result in suite["cases"]:
        assert result["report"] == suite["cases"][0]["report"]


def test_cli_failure_error_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Cases, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    structured = files.guarded()
    inputs = [read_json(files.make_case({"name": "HandlerError", "message": "handler <&雪>", "step_id": "failure"})),
              read_json(files.make_case(MISMATCH, structured)),
              read_json(files.make_case({"name": "Original", "call": 1, "expected_args": {}}, structured)),
              read_json(files.make_case({"name": "HandlerError"}, {"mocks": {"bad": BAD, "failure": {"result": None}}}))]
    original_flows = {value["flow"] for value in inputs}
    paths = []
    for i, value in enumerate(inputs):
        value["flow"] = files.write(read_json(value["flow"]), source)
        value["name"] = f"handler <&雪> {i}"
        paths.append(files.write(value, source))
    for path in original_flows:
        os.remove(path)
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stderr + result.stdout
    suite, saved = read_json(report), read_json(bundle)
    assert suite["passed"] == 2
    assert suite["failed"] == 2
    assert suite["cases"][2]["failures"] == [
        'failure_error.name: expected "Original", got "MockArgsMismatch"',
        "failure_error.call: expected 1, got 2",
        f"failure_error.expected_args: expected {{}}, got {js(EXPECTED_ARGS)}",
    ]
    assert suite["cases"][3]["failures"] == ['failure_error.name: expected "HandlerError", got missing']
    junit = read_text(xml)
    assert junit == saved["junit_xml"]
    assert "handler &lt;&amp;雪&gt;" in junit
    for c in suite["cases"]:
        for failure in c["failures"]:
            assert failure in result.stdout
            assert failure.replace('"', "&quot;") in junit
    shutil.rmtree(source)
    for case_id in [1, 2, 3, 4]:
        out, junit_out = tmp_path / f"replay-{case_id}.json", tmp_path / f"replay-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", out, "--junit", junit_out, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (1 if case_id > 2 else 0), replay.stderr + replay.stdout
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(out)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        for failure in suite["cases"][case_id - 1]["failures"]:
            assert failure.replace('"', "&quot;") in read_text(junit_out)
    saved["cases"][0]["snapshot"]["expect"]["failure_error"] = {"call": 0}
    saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
    invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
    assert invalid.status == 2, invalid.stderr
    assert re.search(r"failure_error\.call: expected positive safe integer", invalid.stderr)


def test_cli_malformed_failure_error_assertions_are_invalid_input(files: Cases, tmp_path: Path) -> None:
    invalid: list[tuple[Any, str]] = [
        *[(value, r"expect\.failure_error: expected object") for value in [None, [], "wrong", 1, False]],
        ({}, r"failure_error: at least one assertion required"),
        *[({"call": call}, r"failure_error\.call: expected positive safe integer") for call in [0, -1, 1.5, "2", None, True, 2**53]],
        *[({field: value}, rf"failure_error\.{field}: expected object")
          for field in ["expected_args", "actual_args"] for value in [None, [], "x", 1, False]],
        *[({field: 2}, rf"failure_error\.{field}: expected string") for field in ["name", "message", "step_id"]],
        ({"typo": 2}, "unsupported field 'typo'"),
    ]
    for value, pattern in invalid:
        result = cli("test", files.make_case(value), cwd=tmp_path)
        assert result.status == 2, {"value": value, "stdout": result.stdout, "stderr": result.stderr}
        assert re.search(pattern, result.stderr)
        assert result.stdout == ""
