"""Port of test/attempt-results.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, clone, cli, js, read_json, read_text, sha

# Contract: every trace event owning result, in trace order, with complete values.
# Shape validation accepts only arrays of objects; contents are assertion values.
pytestmark = pytest.mark.needs_node

RETRY = {"constant": {"attempts": 1, "seconds": 0}}
BAD = {"error": {"name": "Temporary", "message": "retry"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Ordered attempt result assertions",
            "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def each(modules: list[Any]) -> dict[str, Any]:
    return {"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["a", "b"]},
                                    "skip_failures": False, "modules": modules}}


def record(module_id: str, result: Any, attempt: int = 1) -> dict[str, Any]:
    return {"module_id": module_id, "attempt": attempt, "result": result}


def boundary() -> dict[str, Any]:
    return {
        "flow": flow([each([script("send"), script("mask")])]),
        "mocks": {"send": {"sequence": [{"result": "first"}, {"result": "last"}]}, "mask": {"result": "same"}},
        "expect": {
            "result": ["same", "same"], "results": {"each": ["same", "same"], "send": "last", "mask": "same"},
            "args": {"send": [{}, {}], "mask": [{}, {}]}, "path": ["each", "send", "mask", "send", "mask"],
            "mock_errors": [], "attempt_errors": [], "simulated_delay_seconds": 0, "retry_delays": [],
            "mock_calls": [record_call for call in [1, 2] for record_call in [
                {"module_id": "send", "source": "sequence", "call": call}, {"module_id": "mask", "source": "external", "call": call},
            ]],
            "mock_usage": {"send": {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}},
            "attempt_results": [record("each", ["same", "same"]), record("send", "first"), record("mask", "same"),
                                record("send", "last"), record("mask", "same")],
        },
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def check(files: Files, value: dict[str, Any]) -> dict[str, Any]:
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1, js(suite["cases"][0]["failures"])
    assert suite["cases"][0]["report"] == run_flow(value["flow"], value.get("input", {}), value["mocks"], value.get("options"))
    return suite["cases"][0]["report"]


def test_attempt_results_asserts_the_confirmed_intermediate_loop_result_and_catches_a_masked_change(files: Files) -> None:
    value = boundary()
    check(files, value)
    changed = clone(value)
    changed["mocks"]["send"]["sequence"][0]["result"] = "changed first"
    actual = clone(value["expect"]["attempt_results"])
    actual[1]["result"] = "changed first"
    suite = run_cases([files.case_file(changed)])
    assert suite["failed"] == 1
    assert suite["cases"][0]["failures"] == [f"attempt_results: expected {js(value['expect']['attempt_results'])}, got {js(actual)}"]
    del changed["expect"]["attempt_results"]
    check(files, changed)  # All existing assertions still pass; last results hide the change.


def test_attempt_results_compares_complete_ordered_lists_record_fields_result_fields_and_json_types(files: Files) -> None:
    payload = {"tags": [1, None, False], "nested": {"x": "a", "y": []}}
    value = {"flow": flow([script("send"), script("mask")]), "mocks": {"send": {"result": payload}, "mask": {"result": 0}}}
    expected = [record("send", payload), record("mask", 0)]
    check(files, {**value, "expect": {"attempt_results": [
        {"result": {"nested": {"y": [], "x": "a"}, "tags": [1, None, False]}, "attempt": 1, "module_id": "send"}, expected[1],
    ]}})
    variants: list[Any] = [[], [expected[0]], [*expected, expected[1]], list(reversed(expected)), [{}, expected[1]]]
    for field in ["module_id", "attempt", "result"]:
        for v in [None, False, "wrong", 0, 1.5, {}, []]:
            variants.append([{**expected[0], field: v}, expected[1]])
        missing = {key: item for key, item in expected[0].items() if key != field}
        variants.append([missing, expected[1]])
    variants.extend([[{**expected[0], "attempt": "1"}, expected[1]], [{**expected[0], "extra": None}, expected[1]]])
    for result in [{**payload, "extra": None}, {"tags": payload["tags"]}, {**payload, "tags": [1, "null", False]},
                   {**payload, "tags": [False, None, 1]}, {**payload, "tags": [1, None]}, {**payload, "nested": {"x": "a", "y": [], "z": 0}}]:
        variants.append([record("send", result), expected[1]])
    suite = run_cases([files.case_file({**value, "expect": {"attempt_results": attempt_results}}) for attempt_results in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"attempt_results: expected {js(variants[i])}, got {js(expected)}"]
        assert c["report"] == suite["cases"][0]["report"]


def test_attempt_results_covers_retry_attempt_resets_and_primary_and_handler_results(files: Files) -> None:
    f = flow([each([script("send", {"retry": RETRY})]), script("bad")], script("failure", {"retry": RETRY}))
    primary = [record("each", ["a", "b"]), record("send", "a", 2), record("send", "b", 2)]
    for exhausted in [False, True]:
        mocks = {"send": {"sequence": [BAD, {"result": "a"}, BAD, {"result": "b"}]}, "bad": BAD,
                 "failure": {"sequence": [BAD] if exhausted else [BAD, {"result": None}]}}
        report = check(files, {"flow": f, "mocks": mocks, "expect": {
            "error": {"name": "Temporary", "step_id": "bad"},
            "results": {"each": ["a", "b"], "send": "b"},
            "attempt_results": primary if exhausted else [*primary, record("failure", None, 2)],
            "simulated_delay_seconds": 0,
            "retry_delays": [{"module_id": module_id, "attempt": 1, "retry_delay_seconds": 0} for module_id in ["send", "send", "failure"]],
            **({"failure_error": {"name": "MockExhausted"}} if exhausted else {"failure_result": None}),
        }})
        assert [[e["attempt"], e["mock"]["call"]] for e in report["trace"] if e["module_id"] == "send"] == [[1, 1], [2, 2], [1, 3], [2, 4]]
        assert report["mock_usage"]["failure"]["calls"] == 2


def test_attempt_results_includes_errored_events_retaining_a_result_even_before_a_retry(files: Files) -> None:
    stop_error = {"name": "StopAfterError", "message": "stop <&雪>", "step_id": "send"}
    stop = {"stop_after_if": {"expr": 'result === "first"', "error_message": stop_error["message"]}}
    check(files, {"flow": flow([script("send", {**stop, "retry": RETRY})]), "mocks": {"send": {"sequence": [{"result": "first"}, {"result": "last"}]}},
                  "expect": {"result": "last", "results": {"send": "last"}, "attempt_results": [record("send", "first"), record("send", "last", 2)],
                             "attempt_errors": [{"module_id": "send", "attempt": 1, "error": stop_error}],
                             "retry_delays": [{"module_id": "send", "attempt": 1, "retry_delay_seconds": 0}]}})
    for error_include_result in [False, True]:
        check(files, {"flow": flow([script("send", {"stop_after_if": {"expr": "true", "error_message": stop_error["message"],
                                                                      "error_include_result": error_include_result}})], script("failure")),
                      "mocks": {"send": {"result": False}, "failure": {"result": 0}}, "expect": {
                          "error": stop_error, "failure_result": 0,
                          "attempt_results": [record("send", False), record("failure", 0)],
                          "attempt_errors": [{"module_id": "send", "attempt": 1,
                                              "error": {**stop_error, **({"result": False} if error_include_result else {})}}]}})
    check(files, {"flow": flow([script("send", {"stop_after_if": {"expr": "1"}})]), "mocks": {"send": {"result": None}},
                  "expect": {"error": {"name": "ExpressionError"}, "attempt_results": [record("send", None)]}})


def test_attempt_results_preserves_null_false_zero_empty_and_nested_values_in_container_trace_order(files: Files) -> None:
    values: list[Any] = [None, False, 0, "", [], {}, {"a": [None, False, 0, "", [], {}]}]
    children = [script(f"s{i}") for i in range(len(values))]
    nested = {"id": "choose", "value": {"type": "branchone", "branches": [{"expr": "true", "modules": children}], "default": []}}
    report = check(files, {"flow": flow([{"id": "all", "value": {"type": "branchall", "branches": [{"modules": [nested]}]}}]),
                           "mocks": {f"s{i}": {"result": result} for i, result in enumerate(values)}, "expect": {
                               "attempt_results": [record("all", [values[-1]]), record("choose", values[-1]),
                                                   *[record(f"s{i}", v) for i, v in enumerate(values)]]}})
    assert [e["status"] for e in report["trace"]] == ["success"] * 9


def test_attempt_results_empty_list_excludes_skips_and_errors_without_result_and_preserves_default_status(files: Files) -> None:
    for value in [
        {"flow": flow([]), "mocks": {}, "expect": {"attempt_results": []}},
        {"flow": flow([script("send", {"skip_if": {"expr": "true"}})]), "mocks": {}, "expect": {"attempt_results": []}},
        {"flow": flow([script("send", {"continue_on_error": True})]), "mocks": {"send": BAD}, "expect": {"attempt_results": []}},
        {"flow": flow([script("send", {"value": {"type": "script", "path": "/never-execute", "input_transforms": {
            "x": {"type": "javascript", "expr": '(() => { throw Error("mapping"); })()'}}}})]),
         "mocks": {}, "expect": {"error": {"name": "ExpressionError"}, "attempt_results": []}},
    ]:
        check(files, value)
    value = {"flow": flow([script("send")]), "mocks": {"send": BAD}, "expect": {"attempt_results": []}}
    suite = run_cases([files.case_file(value)])
    assert suite["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    check(files, {**value, "expect": {"status": "error", "attempt_results": []}})
    wrong = run_cases([files.case_file({"flow": flow([script("send")]), "mocks": {"send": {"result": None}}, "expect": {"attempt_results": []}})])
    assert wrong["cases"][0]["failures"] == [f"attempt_results: expected [], got {js([record('send', None)])}"]


def test_cli_attempt_results_rejects_only_malformed_array_object_shapes(files: Files, tmp_path: Path) -> None:
    value = boundary()
    for attempt_results in [None, {}, 1, False, "results"]:
        result = cli("test", files.case_file({**value, "expect": {"attempt_results": attempt_results}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.attempt_results: expected array", result.stderr)
        assert result.stdout == ""
    for v in [None, [], 1, False, "result"]:
        result = cli("test", files.case_file({**value, "expect": {"attempt_results": [record("send", "first"), v]}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.attempt_results\[1\]: expected object", result.stderr)
        assert result.stdout == ""


def test_cli_attempt_results_full_list_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = boundary()
    value["mocks"]["send"]["sequence"][0]["result"] = "first <&雪>"
    value["expect"]["attempt_results"][1]["result"] = "first <&雪>"
    cases = [value, {**value, "expect": {**value["expect"], "attempt_results": boundary()["expect"]["attempt_results"]}},
             {"flow": flow([]), "mocks": {}, "expect": {"attempt_results": []}},
             {**value, "expect": {"attempt_results": [{}]}}]
    paths = [files.case_file({**c, "name": f"results <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 2
    failure = f"attempt_results: expected {js(cases[1]['expect']['attempt_results'])}, got {js(value['expect']['attempt_results'])}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    escaped = failure.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    assert escaped in junit
    assert "results &lt;&amp;雪&gt;" in junit
    assert junit == saved["junit_xml"]
    shutil.rmtree(source)
    for case_id in [1, 2, 3, 4]:
        out, xml_out = tmp_path / f"replayed-{case_id}.json", tmp_path / f"replayed-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", out, "--junit", xml_out, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (1 if case_id in (2, 4) else 0), replay.stdout + replay.stderr
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(out)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        if case_id == 2:
            assert escaped in read_text(xml_out)
    for attempt_results in [{}, [None], [[]], [False]]:
        saved["cases"][0]["snapshot"]["expect"]["attempt_results"] = attempt_results
        saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
        invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
        assert invalid.status == 2, invalid.stderr
        assert re.search(r"expect\.attempt_results(?:\[0\])?: expected (?:array|object)", invalid.stderr)
