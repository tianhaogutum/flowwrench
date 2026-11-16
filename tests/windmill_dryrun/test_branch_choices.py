"""Port of test/branch-choices.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, clone, cli, js, read_json, read_text, sha

# Contract: complete ordered module_id/attempt/branch projection of trace events
# owning branch. Shape validation is array-of-objects only; contents are exact.
pytestmark = pytest.mark.needs_node

RETRY = {"constant": {"attempts": 1, "seconds": 0}}
BAD = {"error": {"name": "Temporary", "message": "retry"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Ordered branch selections", "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def choose(module_id: str, expr: str = "true", modules: list[Any] | None = None, fallback: list[Any] | None = None,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "branchone", "branches": [{"expr": expr, "modules": modules or []}], "default": fallback or []},
            **(extra or {})}


def each(modules: list[Any]) -> dict[str, Any]:
    return {"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["left", "right"]},
                                    "skip_failures": False, "modules": modules}}


def record(module_id: str, branch: Any, attempt: int = 1) -> dict[str, Any]:
    return {"module_id": module_id, "attempt": attempt, "branch": branch}


def boundary() -> dict[str, Any]:
    return {
        "flow": flow([each([choose("choose", 'flow_input.iter.value === "left"')])]),
        "expect": {
            "result": [{}, {}], "results": {"each": [{}, {}], "choose": {}}, "args": {"each": [], "choose": []},
            "path": ["each", "choose", "choose"], "stopped": False, "mock_errors": [], "attempt_errors": [],
            "simulated_delay_seconds": 0, "retry_delays": [], "mock_calls": [],
            "attempt_results": [{"module_id": "each", "attempt": 1, "result": [{}, {}]},
                                *[{"module_id": "choose", "attempt": 1, "result": {}} for _ in [1, 2]]],
            "branch_choices": [record("choose", "0"), record("choose", "default")],
        },
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def check(files: Files, value: dict[str, Any]) -> dict[str, Any]:
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1, js(suite["cases"][0]["failures"])
    assert suite["cases"][0]["report"] == run_flow(value["flow"], value.get("input", {}), value.get("mocks", {}), value.get("options"))
    return suite["cases"][0]["report"]


def without_branch(event: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in event.items() if key != "branch"}


def test_branch_choices_asserts_confirmed_equal_result_loop_choices_and_catches_their_reversal(files: Files) -> None:
    value = boundary()
    original = check(files, value)
    changed = clone(value)
    changed["flow"]["value"]["modules"][0]["value"]["iterator"]["value"].reverse()
    actual = list(reversed(value["expect"]["branch_choices"]))
    suite = run_cases([files.case_file(changed)])
    assert suite["failed"] == 1
    assert suite["cases"][0]["failures"] == [f"branch_choices: expected {js(value['expect']['branch_choices'])}, got {js(actual)}"]
    del changed["expect"]["branch_choices"]
    changed_report = check(files, changed)  # All older assertions and coverage still pass.
    assert changed_report["coverage"] == original["coverage"]
    assert [without_branch(e) for e in changed_report["trace"]] == [without_branch(e) for e in original["trace"]]


def test_branch_choices_compares_complete_lists_fields_values_and_types_with_unordered_object_keys(files: Files) -> None:
    value = boundary()
    expected = value["expect"]["branch_choices"]
    check(files, {**value, "expect": {"branch_choices": [{"branch": e["branch"], "attempt": e["attempt"], "module_id": e["module_id"]} for e in expected]}})
    variants: list[Any] = [[], [expected[0]], [*expected, expected[1]], list(reversed(expected)), [{}, expected[1]]]
    for field in ["module_id", "attempt", "branch"]:
        for v in [None, False, "wrong", 0, 1.5, {}, []]:
            variants.append([{**expected[0], field: v}, expected[1]])
        missing = {key: item for key, item in expected[0].items() if key != field}
        variants.append([missing, expected[1]])
    variants.extend([[{**expected[0], "attempt": "1"}, expected[1]], [{**expected[0], "branch": 0}, expected[1]],
                     [{**expected[0], "branch": ["0"]}, expected[1]], [{**expected[0], "extra": None}, expected[1]]])
    suite = run_cases([files.case_file({**value, "expect": {"branch_choices": branch_choices}}) for branch_choices in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"branch_choices: expected {js(variants[i])}, got {js(expected)}"]
        assert c["report"] == suite["cases"][0]["report"]
    # Nonzero numeric indices are recorded strings too; first matching arm wins.
    multi = choose("multi", "false")
    multi["value"]["branches"].extend([{"expr": "true", "modules": []}, {"expr": "true", "modules": []}])
    check(files, {"flow": flow([multi]), "expect": {"branch_choices": [record("multi", "1")]}})


def test_branch_choices_includes_retried_containers_nested_selections_and_loop_resets_without_script_handler_events(files: Files) -> None:
    outer = choose("outer", "true", [choose("inner", "false", [], [script("send")])], [], {"retry": RETRY})
    all_branches = {"id": "all", "value": {"type": "branchall", "branches": [{"modules": [outer]}]}}
    handler = script("failure", {"retry": RETRY})
    choices = [item for _ in [1, 2] for item in [record("outer", "0"), record("inner", "default"), record("outer", "0", 2), record("inner", "default")]]
    report = check(files, {"flow": flow([each([all_branches]), script("bad")], handler),
                           "mocks": {"send": {"sequence": [BAD, {"result": "a"}, BAD, {"result": "b"}]}, "bad": BAD,
                                     "failure": {"sequence": [BAD, {"result": "recovered"}]}},
                           "expect": {"error": {"name": "Temporary", "step_id": "bad"}, "failure_result": "recovered",
                                      "branch_choices": choices,
                                      "mock_usage": {"send": {"expected": 4, "calls": 4, "consumed": 4, "remaining": 0, "extra": 0},
                                                     "failure": {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}}}})
    assert [[e["attempt"], e["status"]] for e in report["trace"] if e["module_id"] == "outer"] == [[1, "error"], [2, "success"], [1, "error"], [2, "success"]]
    assert [[e["attempt"], e["mock"]["call"]] for e in report["trace"] if e["module_id"] == "send"] == [[1, 1], [1, 2], [1, 3], [1, 4]]
    assert all("branch" not in e for e in report["trace"] if e["module_id"] == "all")
    assert "failure_module" in next(e for e in report["trace"] if e["module_id"] == "failure")["path"]
    assert [[e["attempt"], "branch" in e] for e in report["trace"] if e["module_id"] == "failure"] == [[1, False], [2, False]]


def test_branch_choices_retains_selections_on_child_stop_retry_if_and_failure_handler_errors(files: Files) -> None:
    primary = choose("primary", "true", [script("bad")])
    for handler_fails in [False, True]:
        report = check(files, {"flow": flow([primary], script("failure")),
                               "mocks": {"bad": BAD, "failure": BAD if handler_fails else {"result": None}},
                               "expect": {"error": {"name": "Temporary", "step_id": "bad"}, "branch_choices": [record("primary", "0")],
                                          **({"failure_error": {"name": "Temporary", "step_id": "failure"}} if handler_fails else {"failure_result": None})}})
        assert "result" not in report["trace"][0]
        assert report["trace"][0]["branch"] == "0"
        assert "branch" not in report["trace"][-1]
    # The supported OpenFlow schema restricts failure handlers to script/rawscript.
    # Branch assertions must not expand runtime/format support to meet a fixture.
    with pytest.raises(FormatError, match="failure_module must have id failure and type script/rawscript"):
        run_cases([files.case_file({"flow": flow([primary], choose("failure")), "mocks": {"bad": BAD}, "expect": {"branch_choices": []}})])
    stopped = choose("stop", "true", [], [], {"stop_after_if": {"expr": "true", "error_message": "stopped"}, "retry": RETRY})
    check(files, {"flow": flow([stopped]), "expect": {"error": {"name": "StopAfterError"},
                                                      "branch_choices": [record("stop", "0"), record("stop", "0", 2)],
                                                      "attempt_results": [{"module_id": "stop", "attempt": 1, "result": {}},
                                                                          {"module_id": "stop", "attempt": 2, "result": {}}]}})
    retry_error = {**primary, "retry": {**RETRY, "retry_if": {"expr": "1"}}}
    check(files, {"flow": flow([retry_error]), "mocks": {"bad": BAD}, "expect": {"error": {"name": "ExpressionError"},
                                                                                "branch_choices": [record("primary", "0")]}})


def test_branch_choices_empty_excludes_unselected_skipped_predicate_failed_events_and_keeps_default_status(files: Files) -> None:
    for value in [
        {"flow": flow([])},
        {"flow": flow([choose("skipped", "true", [], [], {"skip_if": {"expr": "true"}})])},
        {"flow": flow([{"id": "all", "value": {"type": "branchall", "branches": [{"modules": []}, {"modules": []}]}}])},
        {"flow": flow([choose("predicate", "1", [], [], {"continue_on_error": True})])},
        {"flow": flow([script("send")]), "mocks": {"send": {"result": None}}},
    ]:
        check(files, {**value, "expect": {"branch_choices": []}})
    value = {"flow": flow([choose("predicate", "1")]), "expect": {"branch_choices": []}}
    suite = run_cases([files.case_file(value)])
    assert suite["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    check(files, {**value, "expect": {"status": "error", "branch_choices": []}})
    selected_error = {"flow": flow([choose("selected", "true", [script("bad")])]), "mocks": {"bad": BAD},
                      "expect": {"branch_choices": [record("selected", "0")]}}
    assert run_cases([files.case_file(selected_error)])["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    check(files, {**selected_error, "expect": {"status": "error", "branch_choices": [record("selected", "0")]}})


def test_cli_branch_choices_rejects_only_malformed_array_object_shapes(files: Files, tmp_path: Path) -> None:
    value = boundary()
    for branch_choices in [None, {}, 1, False, "branches"]:
        result = cli("test", files.case_file({**value, "expect": {"branch_choices": branch_choices}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.branch_choices: expected array", result.stderr)
        assert result.stdout == ""
    for v in [None, [], 1, False, "branch"]:
        result = cli("test", files.case_file({**value, "expect": {"branch_choices": [record("choose", "0"), v]}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.branch_choices\[1\]: expected object", result.stderr)
        assert result.stdout == ""


def test_cli_branch_choices_full_list_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = boundary()
    wrong = clone(value)
    wrong["expect"]["branch_choices"][0]["branch"] = "wrong <&雪>"
    cases = [value, wrong, {"flow": flow([]), "expect": {"branch_choices": []}}, {**value, "expect": {"branch_choices": [{}]}}]
    paths = [files.case_file({**c, "name": f"branches <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 2
    failure = f"branch_choices: expected {js(wrong['expect']['branch_choices'])}, got {js(value['expect']['branch_choices'])}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    escaped = failure.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    assert escaped in junit
    assert "branches &lt;&amp;雪&gt;" in junit
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
    for branch_choices in [{}, [None], [[]], [False]]:
        saved["cases"][0]["snapshot"]["expect"]["branch_choices"] = branch_choices
        saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
        invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
        assert invalid.status == 2, invalid.stderr
        assert re.search(r"expect\.branch_choices(?:\[0\])?: expected (?:array|object)", invalid.stderr)
