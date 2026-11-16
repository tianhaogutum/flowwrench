"""Port of test/attempt-errors.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, clone, cli, js, read_json, read_text, sha

# Contract: all trace events owning error, in trace order, with full error values.
# Only array-of-object shape is validated; record contents are assertion values.
pytestmark = pytest.mark.needs_node

RETRY = {"constant": {"attempts": 1, "seconds": 0}}
BAD = {"error": {"name": "Temporary", "message": "retry A"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Ordered attempt error assertions",
            "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def error(step_id: str, message: str = "retry A", name: str = "Temporary") -> dict[str, Any]:
    return {"name": name, "message": message, "step_id": step_id}


def record(module_id: str, attempt: int, error_value: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"module_id": module_id, "attempt": attempt, "error": error(module_id) if error_value is None else error_value}


def boundary() -> dict[str, Any]:
    return {
        "flow": flow([script("send", {"retry": clone(RETRY)})]),
        "mocks": {"send": {"sequence": [BAD, {"result": "recovered"}]}},
        "expect": {"result": "recovered", "simulated_delay_seconds": 0,
                   "retry_delays": [{"module_id": "send", "attempt": 1, "retry_delay_seconds": 0}], "mock_errors": [],
                   "mock_calls": [{"module_id": "send", "source": "sequence", "call": call} for call in [1, 2]],
                   "mock_usage": {"send": {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}},
                   "attempt_errors": [record("send", 1)]},
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def check(files: Files, value: dict[str, Any]) -> dict[str, Any]:
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1, js(suite["cases"][0]["failures"])
    assert suite["cases"][0]["report"] == run_flow(value["flow"], value.get("input", {}), value["mocks"], value.get("options"))
    return suite["cases"][0]["report"]


def test_attempt_errors_asserts_the_confirmed_recovered_retry_error_and_catches_a_changed_message(files: Files) -> None:
    value = boundary()
    check(files, value)
    changed = clone(value)
    changed["mocks"]["send"]["sequence"][0]["error"]["message"] = "retry B"
    suite = run_cases([files.case_file(changed)])
    assert suite["failed"] == 1
    assert suite["cases"][0]["failures"] == [
        f"attempt_errors: expected {js(value['expect']['attempt_errors'])}, got {js([record('send', 1, error('send', 'retry B'))])}"
    ]
    del changed["expect"]["attempt_errors"]
    check(files, changed)  # Every existing expectation still passes for retry B.


def test_attempt_errors_compares_complete_ordered_lists_and_record_error_fields_and_types(files: Files) -> None:
    value = boundary()
    value["flow"]["value"]["modules"][0]["retry"]["constant"]["attempts"] = 2
    value["mocks"]["send"]["sequence"].insert(1, {"error": {"name": "Again", "message": "retry B"}})
    expected = [record("send", 1), record("send", 2, error("send", "retry B", "Again"))]
    value["expect"] = {"result": "recovered", "attempt_errors": expected}
    check(files, {**value, "expect": {**value["expect"], "attempt_errors": [
        {"error": {"step_id": e["error"]["step_id"], "message": e["error"]["message"], "name": e["error"]["name"]},
         "attempt": e["attempt"], "module_id": e["module_id"]} for e in expected
    ]}})
    variants: list[Any] = [[], [expected[0]], [*expected, expected[1]], list(reversed(expected)), [{}, expected[1]]]
    for field in ["module_id", "attempt", "error"]:
        for v in [None, False, "wrong", 0, 1.5, {}, []]:
            variants.append([{**expected[0], field: v}, expected[1]])
        missing = {key: item for key, item in expected[0].items() if key != field}
        variants.append([missing, expected[1]])
    variants.extend([[{**expected[0], "attempt": "1"}, expected[1]], [{**expected[0], "extra": None}, expected[1]]])
    for field in ["name", "message", "step_id"]:
        for v in [None, False, 1, {}, [], "wrong"]:
            variants.append([record("send", 1, {**expected[0]["error"], field: v}), expected[1]])
        missing = {key: item for key, item in expected[0]["error"].items() if key != field}
        variants.append([record("send", 1, missing), expected[1]])
    variants.append([record("send", 1, {**expected[0]["error"], "extra": None}), expected[1]])
    suite = run_cases([files.case_file({**value, "expect": {**value["expect"], "attempt_errors": attempt_errors}})
                       for attempt_errors in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"attempt_errors: expected {js(variants[i])}, got {js(expected)}"]
        assert c["report"] == suite["cases"][0]["report"]


def test_attempt_errors_covers_loop_attempt_resets_exhausted_primary_and_recovered_exhausted_handlers(files: Files) -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["a", "b"]},
                                       "skip_failures": False, "modules": [script("send", {"retry": RETRY})]}},
              script("bad", {"retry": RETRY})], script("failure", {"retry": RETRY}))
    primary = [record("send", 1), record("send", 1), record("bad", 1), record("bad", 2), record("failure", 1)]
    for exhausted in [False, True]:
        exhaustion = error("failure", "Mock sequence exhausted for 'failure' at call 2", "MockExhausted")
        mocks = {"send": {"sequence": [BAD, {"result": "a"}, BAD, {"result": "b"}]}, "bad": BAD,
                 "failure": {"sequence": [BAD] if exhausted else [BAD, {"result": "handled"}]}}
        report = check(files, {"flow": f, "mocks": mocks, "expect": {
            "error": {"name": "Temporary", "step_id": "bad"},
            "attempt_errors": [*primary, record("failure", 2, exhaustion)] if exhausted else primary,
            "mock_errors": [exhaustion] if exhausted else [], "simulated_delay_seconds": 0,
            "retry_delays": [{"module_id": module_id, "attempt": 1, "retry_delay_seconds": 0} for module_id in ["send", "send", "bad", "failure"]],
            **({"failure_error": exhaustion} if exhausted else {"failure_result": "handled"}),
        }})
        assert [[e["attempt"], e["mock"]["call"]] for e in report["trace"] if e["module_id"] == "send"] == [[1, 1], [2, 2], [1, 3], [2, 4]]
        assert report["mock_usage"]["failure"]["calls"] == 2


def test_attempt_errors_retains_full_structured_contract_errors_and_nested_error_results(files: Files) -> None:
    payload = {"tags": [1, None, False], "nested": {"x": "a"}}
    mismatch = {**error("send", f"Mock arguments mismatch for 'send' at call 1: expected {{}}, got {js({'payload': payload})}", "MockArgsMismatch"),
                "call": 1, "expected_args": {}, "actual_args": {"payload": payload}}
    f = flow([script("send", {"retry": RETRY, "value": {"type": "script", "path": "/never-execute",
                                                         "input_transforms": {"payload": {"type": "static", "value": payload}}}})])
    mocks = {"send": {"sequence": [{"expect_args": {}, "result": 0}, {"result": "recovered"}]}}
    check(files, {"flow": f, "mocks": mocks, "expect": {"error": mismatch, "result": {"error": mismatch}, "mock_errors": [mismatch],
                                                         "attempt_errors": [record("send", 1, mismatch)]}})
    changed = clone(mismatch)
    changed["actual_args"]["payload"]["tags"] = [1, "null", False]
    suite = run_cases([files.case_file({"flow": f, "mocks": mocks, "expect": {"status": "error", "attempt_errors": [record("send", 1, changed)]}})])
    assert suite["failed"] == 1
    assert re.search(r"^attempt_errors:", suite["cases"][0]["failures"][0])
    stop_error = {**error("send", "stopped <&雪>", "StopAfterError"), "result": payload}
    check(files, {"flow": flow([script("send", {"stop_after_if": {"expr": "true", "error_message": "stopped <&雪>", "error_include_result": True}})]),
                  "mocks": {"send": {"result": payload}}, "expect": {"status": "error", "attempt_errors": [record("send", 1, {
                      "result": {"nested": {"x": "a"}, "tags": [1, None, False]}, "step_id": "send",
                      "message": stop_error["message"], "name": stop_error["name"],
                  })]}})


def test_attempt_errors_includes_mapping_predicate_failures_and_retry_if_suppression_or_replacement(files: Files) -> None:
    predicate_error = error("send", "Predicate must return a boolean", "ExpressionError")
    mapping_error = error("send", "mapped failure", "ExpressionError")
    cases = [
        {"flow": flow([script("send", {"retry": RETRY, "value": {"type": "script", "path": "/never-execute", "input_transforms": {
            "x": {"type": "javascript", "expr": '(() => { throw new Error("mapped failure") })()'},
        }}})]), "mocks": {}, "expect": {"status": "error", "attempt_errors": [record("send", 1, mapping_error), record("send", 2, mapping_error)]}},
        {"flow": flow([script("send", {"skip_if": {"expr": "1"}})]), "mocks": {},
         "expect": {"status": "error", "attempt_errors": [record("send", 1, predicate_error)], "mock_calls": []}},
        {"flow": flow([script("send", {"retry": {**RETRY, "retry_if": {"expr": "false"}}})]), "mocks": {"send": BAD},
         "expect": {"status": "error", "attempt_errors": [record("send", 1)], "retry_delays": []}},
        {"flow": flow([script("send", {"retry": {**RETRY, "retry_if": {"expr": "1"}}})]), "mocks": {"send": BAD},
         "expect": {"error": predicate_error, "attempt_errors": [record("send", 1, predicate_error)], "retry_delays": []}},
        # The parent event precedes its child in trace, even though it catches later.
        {"flow": flow([{"id": "outer", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": [1]}, "skip_failures": False,
                                                  "modules": [script("send")]}}]),
         "mocks": {"send": BAD}, "expect": {"status": "error", "attempt_errors": [record("outer", 1, error("send")), record("send", 1)]}},
        {"flow": flow([script("send", {"continue_on_error": True})]), "mocks": {"send": BAD}, "expect": {"attempt_errors": [record("send", 1)]}},
    ]
    for value in cases:
        check(files, value)


def test_attempt_errors_empty_list_excludes_non_event_errors_and_preserves_the_default_expected_status(files: Files) -> None:
    unused = error("send", "Mock sequence for 'send' has 1 unused response(s): consumed 0 of 1", "MockUnused")
    cases = [
        {"flow": flow([]), "mocks": {}, "expect": {"attempt_errors": []}},
        {"flow": flow([script("send")], script("failure")), "mocks": {"send": {"result": None}, "failure": BAD}, "expect": {"attempt_errors": []}},
        {"flow": flow([script("send", {"skip_if": {"expr": "true"}})]), "mocks": {"send": {"sequence": [BAD]}}, "options": {"requireAllMocksUsed": True},
         "expect": {"error": unused, "mock_errors": [unused], "attempt_errors": []}},
        {"flow": flow([script("send"), script("later")]), "mocks": {"send": {"result": 1}}, "options": {"maxSteps": 1},
         "expect": {"error": {"name": "BudgetExceeded", "step_id": "later"}, "attempt_errors": []}},
    ]
    for value in cases:
        check(files, value)
    value = {"flow": flow([script("send")]), "mocks": {"send": BAD}, "expect": {"attempt_errors": [record("send", 1)]}}
    suite = run_cases([files.case_file(value)])
    assert suite["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    value["expect"] = {"status": "error", "attempt_errors": []}
    missing = run_cases([files.case_file(value)])
    assert missing["cases"][0]["failures"] == [f"attempt_errors: expected [], got {js([record('send', 1)])}"]


def test_cli_attempt_errors_rejects_only_malformed_array_object_shapes(files: Files, tmp_path: Path) -> None:
    value = boundary()
    for attempt_errors in [None, {}, 1, False, "errors"]:
        result = cli("test", files.case_file({**value, "expect": {"attempt_errors": attempt_errors}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.attempt_errors: expected array", result.stderr)
        assert result.stdout == ""
    for v in [None, [], 1, False, "error"]:
        result = cli("test", files.case_file({**value, "expect": {"attempt_errors": [record("send", 1), v]}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.attempt_errors\[1\]: expected object", result.stderr)
        assert result.stdout == ""


def test_cli_attempt_errors_full_list_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = boundary()
    value["mocks"]["send"]["sequence"][0] = {"error": {**BAD["error"], "message": "retry <&雪>"}}
    value["expect"]["attempt_errors"][0]["error"]["message"] = "retry <&雪>"
    cases = [value, {**value, "expect": {**value["expect"], "attempt_errors": [record("send", 1)]}},
             {"flow": flow([]), "mocks": {}, "expect": {"attempt_errors": []}}]
    paths = [files.case_file({**c, "name": f"errors <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 1
    failure = f"attempt_errors: expected {js(cases[1]['expect']['attempt_errors'])}, got {js(value['expect']['attempt_errors'])}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    escaped = failure.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    assert escaped in junit
    assert "errors &lt;&amp;雪&gt;" in junit
    assert junit == saved["junit_xml"]
    shutil.rmtree(source)
    for case_id in [1, 2, 3]:
        out, xml_out = tmp_path / f"replayed-{case_id}.json", tmp_path / f"replayed-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", out, "--junit", xml_out, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (1 if case_id == 2 else 0), replay.stdout + replay.stderr
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(out)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        if case_id == 2:
            assert escaped in read_text(xml_out)
    for attempt_errors in [{}, [None], [[]], [False]]:
        saved["cases"][0]["snapshot"]["expect"]["attempt_errors"] = attempt_errors
        saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
        invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
        assert invalid.status == 2, invalid.stderr
        assert re.search(r"expect\.attempt_errors(?:\[0\])?: expected (?:array|object)", invalid.stderr)
