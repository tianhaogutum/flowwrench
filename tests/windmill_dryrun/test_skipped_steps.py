"""Port of test/skipped-steps.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, clone, cli, js, read_json, read_text, sha

# Contract: exact ordered module_id/attempt/path projection of status=skipped.
pytestmark = pytest.mark.needs_node

RETRY = {"constant": {"attempts": 1, "seconds": 0}}
BAD = {"error": {"name": "Temporary", "message": "retry"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Ordered skipped steps", "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def choose(module_id: str, expr: str = "true", modules: list[Any] | None = None, fallback: list[Any] | None = None,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "branchone", "branches": [{"expr": expr, "modules": modules or []}], "default": fallback or []},
            **(extra or {})}


def each(modules: list[Any]) -> dict[str, Any]:
    return {"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["left", "right"]},
                                    "skip_failures": False, "modules": modules}}


def identity(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "identity"}, **(extra or {})}


def skip(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return identity(module_id, {"skip_if": {"expr": "true"}, **(extra or {})})


def skipped(module_id: str, path: list[Any] | None = None, attempt: int = 1) -> dict[str, Any]:
    return {"module_id": module_id, "attempt": attempt, "path": [module_id] if path is None else path}


def boundary() -> dict[str, Any]:
    return {
        "flow": flow([skip("skip"), identity("finish", {"stop_after_if": {"expr": "true"}})]),
        "expect": {"result": {}, "results": {"finish": {}}, "args": {"skip": [], "finish": []}, "path": ["finish"],
                   "stopped": True, "mock_errors": [], "mock_calls": [], "attempt_errors": [], "retry_delays": [],
                   "simulated_delay_seconds": 0, "attempt_results": [{"module_id": "finish", "attempt": 1, "result": {}}],
                   "branch_choices": [], "skipped_steps": [skipped("skip")]},
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def check(files: Files, value: dict[str, Any]) -> dict[str, Any]:
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1, js(suite["cases"][0]["failures"])
    assert suite["cases"][0]["report"] == run_flow(value["flow"], value.get("input", {}), value.get("mocks", {}), value.get("options"))
    return suite["cases"][0]["report"]


def test_skipped_steps_asserts_confirmed_explicit_skip_versus_unreachable_step_after_stop(files: Files) -> None:
    value = boundary()
    original = check(files, value)
    changed = clone(value)
    changed["flow"]["value"]["modules"].reverse()
    suite = run_cases([files.case_file(changed)])
    assert suite["cases"][0]["failures"] == [f"skipped_steps: expected {js(value['expect']['skipped_steps'])}, got []"]
    del changed["expect"]["skipped_steps"]
    report = check(files, changed)  # Every previous assertion still passes.
    assert report["coverage"] == original["coverage"]
    assert report == {**original, "trace": [e for e in original["trace"] if e["status"] != "skipped"]}


def test_skipped_steps_compares_entire_records_and_lists_exactly_with_unordered_object_keys(files: Files) -> None:
    value = {"flow": flow([skip("a"), skip("b")]), "expect": {"skipped_steps": [skipped("a"), skipped("b")]}}
    expected = value["expect"]["skipped_steps"]
    check(files, {**value, "expect": {"skipped_steps": [{"path": e["path"], "attempt": e["attempt"], "module_id": e["module_id"]} for e in expected]}})
    variants: list[Any] = [[], [expected[0]], [*expected, expected[1]], list(reversed(expected)), [{}, expected[1]]]
    for field in ["module_id", "attempt", "path"]:
        for v in [None, False, "wrong", 0, 1.5, {}, []]:
            variants.append([{**expected[0], field: v}, expected[1]])
        missing = {key: item for key, item in expected[0].items() if key != field}
        variants.append([missing, expected[1]])
    variants.extend([[{**expected[0], "attempt": "1"}, expected[1]], [{**expected[0], "extra": None}, expected[1]],
                     [{**expected[0], "path": ["a", "extra"]}, expected[1]], [{**expected[0], "path": [1]}, expected[1]]])
    suite = run_cases([files.case_file({**value, "expect": {"skipped_steps": skipped_steps}}) for skipped_steps in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"skipped_steps: expected {js(variants[i])}, got {js(expected)}"]
        assert c["report"] == suite["cases"][0]["report"]


def test_skipped_steps_preserves_complete_nested_paths_trace_order_loop_resets_and_retried_containers(files: Files) -> None:
    outer = choose("outer", "true", [skip("leaf"), script("send")], [], {"retry": RETRY})
    all_branches = {"id": "all", "value": {"type": "branchall", "branches": [{"modules": [outer]}, {"modules": [skip("other")]}]}}
    expected = [item for i in [0, 1] for item in [
        skipped("leaf", ["each", f"iter:{i}", "all", "branch:0", "outer", "branch:0", "leaf"]),
        skipped("leaf", ["each", f"iter:{i}", "all", "branch:0", "outer", "branch:0", "leaf"]),
        skipped("other", ["each", f"iter:{i}", "all", "branch:1", "other"]),
    ]]
    report = check(files, {"flow": flow([each([all_branches])]), "mocks": {"send": {"sequence": [BAD, {"result": 1}, BAD, {"result": 2}]}},
                           "expect": {"skipped_steps": expected,
                                      "mock_usage": {"send": {"expected": 4, "calls": 4, "consumed": 4, "remaining": 0, "extra": 0}}}})
    assert [e["attempt"] for e in report["trace"] if e["module_id"] == "outer"] == [1, 2, 1, 2]
    while_loop = {"id": "while", "value": {"type": "whileloopflow", "skip_failures": False, "modules": [skip("inner")]},
                  "stop_after_if": {"expr": "flow_input.iter.index === 1"}}
    check(files, {"flow": flow([while_loop]), "expect": {"skipped_steps": [skipped("inner", ["while", f"iter:{i}", "inner"]) for i in [0, 1]]}})


def test_skipped_steps_includes_skipped_containers_and_handlers_without_inventing_child_or_mock_events(files: Files) -> None:
    container = choose("container", "true", [script("child", {"skip_if": {"expr": "true"}})], [], {"skip_if": {"expr": "true"}})
    handler = script("failure", {"skip_if": {"expr": "true"}, "retry": RETRY})
    report = check(files, {"flow": flow([container, script("bad")], handler),
                           "mocks": {"bad": BAD, "child": {"sequence": [{"result": 1}]}, "failure": {"sequence": [{"result": 2}]}},
                           "expect": {"error": {"name": "Temporary", "step_id": "bad"},
                                      "skipped_steps": [skipped("container"), skipped("failure", ["failure_module", "failure"])],
                                      "mock_usage": {"child": {"expected": 1, "calls": 0, "consumed": 0, "remaining": 1, "extra": 0},
                                                     "failure": {"expected": 1, "calls": 0, "consumed": 0, "remaining": 1, "extra": 0}}}})
    assert not any(e["module_id"] == "child" for e in report["trace"])
    for e in [e for e in report["trace"] if e["status"] == "skipped"]:
        assert "mock" not in e
        assert "result" not in e
    # Failure-handler predicate errors are errors, not skips; the primary error remains.
    check(files, {"flow": flow([script("bad")], script("failure", {"skip_if": {"expr": "1"}})), "mocks": {"bad": BAD},
                  "expect": {"error": {"name": "Temporary"}, "failure_error": {"name": "ExpressionError"}, "skipped_steps": []}})


def test_skipped_steps_empty_excludes_unreachable_unselected_zero_iteration_and_predicate_error_events(files: Files) -> None:
    unselected = choose("choice", "false", [skip("unselected")])
    empty = each([skip("zero")])
    empty["value"]["iterator"]["value"] = []
    value = {"flow": flow([unselected, empty, identity("finish", {"stop_after_if": {"expr": "true", "skip_if_stopped": True}}), skip("unreachable")],
                          script("failure", {"skip_if": {"expr": "true"}})), "expect": {"skipped_steps": []}}
    report = check(files, value)
    assert report["skipped"] is True  # skipped_after is distinct from status=skipped.
    assert not any(e["module_id"] in ["unselected", "zero", "unreachable", "failure"] for e in report["trace"])
    check(files, {"flow": flow([]), "expect": {"skipped_steps": []}})
    check(files, {"flow": flow([skip("falseSkip", {"skip_if": {"expr": "false"}})]), "expect": {"skipped_steps": []}})
    check(files, {"flow": flow([skip("predicate", {"skip_if": {"expr": "1"}, "continue_on_error": True}), skip("after")]),
                  "expect": {"skipped_steps": [skipped("after")]}})
    failed = {"flow": flow([skip("predicate", {"skip_if": {"expr": "1"}, "retry": RETRY}), skip("after")]), "expect": {"skipped_steps": []}}
    assert run_cases([files.case_file(failed)])["cases"][0]["failures"] == ['status: expected "ok", got "error"']
    check(files, {**failed, "expect": {"error": {"name": "ExpressionError"}, "skipped_steps": []}})
    check(files, {"flow": flow([skip("first"), skip("budget")]), "options": {"maxSteps": 1},
                  "expect": {"error": {"name": "BudgetExceeded"}, "skipped_steps": [skipped("first")]}})


def test_cli_skipped_steps_rejects_only_malformed_array_object_shapes(files: Files, tmp_path: Path) -> None:
    value = boundary()
    for skipped_steps in [None, {}, 1, False, "branches"]:
        result = cli("test", files.case_file({**value, "expect": {"skipped_steps": skipped_steps}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.skipped_steps: expected array", result.stderr)
        assert result.stdout == ""
    for v in [None, [], 1, False, "branch"]:
        result = cli("test", files.case_file({**value, "expect": {"skipped_steps": [skipped("skip"), v]}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.skipped_steps\[1\]: expected object", result.stderr)
        assert result.stdout == ""


def test_cli_skipped_steps_full_list_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = boundary()
    wrong = clone(value)
    wrong["expect"]["skipped_steps"][0]["path"] = ["wrong <&雪>"]
    cases = [value, wrong, {"flow": flow([]), "expect": {"skipped_steps": []}}, {**value, "expect": {"skipped_steps": [{}]}}]
    paths = [files.case_file({**c, "name": f"skips <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 2
    failure = f"skipped_steps: expected {js(wrong['expect']['skipped_steps'])}, got {js(value['expect']['skipped_steps'])}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    escaped = failure.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    assert escaped in junit
    assert "skips &lt;&amp;雪&gt;" in junit
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
    for skipped_steps in [{}, [None], [[]], [False]]:
        saved["cases"][0]["snapshot"]["expect"]["skipped_steps"] = skipped_steps
        saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
        invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
        assert invalid.status == 2, invalid.stderr
        assert re.search(r"expect\.skipped_steps(?:\[0\])?: expected (?:array|object)", invalid.stderr)
