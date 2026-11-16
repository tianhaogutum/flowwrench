"""Port of test/mock-consumption.test.js."""

from __future__ import annotations

import re
from typing import Any

import pytest

from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.jsvalues import UNDEFINED
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import clone

pytestmark = pytest.mark.needs_node


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": f"f/demo/{module_id}", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "synthetic mock accounting", "value": {"modules": modules, **(extra or {})}}


def each(modules: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "each", "value": {
        "type": "forloopflow", "iterator": {"type": "static", "value": [0, 1]}, "skip_failures": False, "modules": modules, **(extra or {}),
    }}


def usage(expected: int, calls: int) -> dict[str, int]:
    return {"expected": expected, "calls": calls, "consumed": min(expected, calls),
            "remaining": max(0, expected - calls), "extra": max(0, calls - expected)}


def test_sequence_accounting_follows_calls_across_retries_and_loop_iterations_and_resets_per_run() -> None:
    f = flow([each([script("send", {"retry": {"constant": {"attempts": 1, "seconds": 0}}})])])
    mocks = {"send": {"sequence": [{"error": {"message": "retry"}}, {"result": 1}, {"result": 2}]}}
    original = clone(mocks)
    for _ in range(2):
        r = run_flow(f, {}, mocks, {"requireAllMocksUsed": True})
        assert r["status"] == "ok"
        assert r["result"] == [1, 2]
        assert r["mock_usage"] == {"send": usage(3, 3)}
        events = [e for e in r["trace"] if e["module_id"] == "send"]
        assert [e["mock"] for e in events] == [{"source": "sequence", "call": call} for call in [1, 2, 3]]
        assert [e["attempt"] for e in events] == [1, 2, 1]
        assert [e["path"][-2] for e in events] == ["iter:0", "iter:0", "iter:1"]
        assert "mock_errors" not in r
    assert mocks == original


def test_unexpected_extra_calls_fail_the_run_despite_continue_skipped_failures_and_retries() -> None:
    f = flow([each([script("send", {"continue_on_error": True, "retry": {"constant": {"attempts": 1, "seconds": 0}}})], {"skip_failures": True})])
    r = run_flow(f, {}, {"send": {"sequence": [{"result": "first"}]}})
    assert r["status"] == "error"
    assert r["error"]["name"] == "MockExhausted"
    assert r["error"]["step_id"] == "send"
    assert re.search("call 2", r["error"]["message"])
    assert r["result"] == {"error": r["error"]}
    assert r["mock_usage"] == {"send": usage(1, 3)}
    assert [e["message"] for e in r["mock_errors"]] == [
        "Mock sequence exhausted for 'send' at call 2", "Mock sequence exhausted for 'send' at call 3",
    ]
    assert r["trace"][-1]["continued"] is True
    assert [e["mock"]["call"] for e in r["trace"] if e.get("mock")] == [1, 2, 3]


def test_unused_sequence_outcomes_are_reported_by_default_and_fail_only_with_the_explicit_gate() -> None:
    f = flow([script("send")])
    mocks = {"send": {"sequence": [{"result": 7}, {"result": 8}]}}
    normal = run_flow(f, {}, mocks)
    assert normal["status"] == "ok"
    assert normal["result"] == 7
    assert normal["mock_usage"] == {"send": usage(2, 1)}
    strict = run_flow(f, {}, mocks, {"requireAllMocksUsed": True})
    assert strict["status"] == "error"
    assert strict["error"] == {"name": "MockUnused", "step_id": "send",
                               "message": "Mock sequence for 'send' has 1 unused response(s): consumed 1 of 2"}
    assert strict["mock_errors"] == [strict["error"]]
    assert strict["results"]["send"] == 7


def test_loop_and_branch_failure_recovery_cannot_hide_exhaustion_handler_errors_remain_secondary() -> None:
    skipped_loop = each([script("send")], {"skip_failures": True})
    skipped_branch = {"id": "all", "value": {"type": "branchall", "branches": [
        {"skip_failure": True, "modules": [each([script("send")])]},
    ]}}
    for container in [skipped_loop, skipped_branch]:
        r = run_flow(flow([container]), {}, {"send": {"sequence": [{"result": 1}]}})
        assert r["status"] == "error"
        assert r["error"]["name"] == "MockExhausted"
        assert r["mock_usage"] == {"send": usage(1, 2)}
    r = run_flow(flow([script("bad")], {"failure_module": script("failure", {
        "retry": {"constant": {"attempts": 1, "seconds": 0}},
    })}), {}, {"bad": {"error": {"name": "Original", "message": "bad"}},
               "failure": {"sequence": [{"error": {"message": "handler"}}]}})
    assert r["error"]["name"] == "Original"
    assert r["failure_error"]["name"] == "MockExhausted"
    assert r["mock_errors"] == [r["failure_error"]]
    assert r["mock_usage"] == {"failure": usage(1, 2)}


def test_skips_and_failed_mappings_consume_no_responses_unselected_sequences_are_checked() -> None:
    f = flow([
        script("skip", {"skip_if": {"expr": "true"}}),
        {**script("bad", {"continue_on_error": True}), "value": {
            **script("bad")["value"], "input_transforms": {"x": {"type": "javascript", "expr": "flow_input.missing.value"}},
        }},
        {"id": "branch", "value": {"type": "branchone", "branches": [{"expr": "false", "modules": [script("never")]}], "default": []}},
    ])
    r = run_flow(f, {}, {module_id: {"sequence": [{"result": 1}]} for module_id in ["skip", "bad", "never"]}, {"requireAllMocksUsed": True})
    assert r["status"] == "error"
    assert r["mock_usage"] == {module_id: usage(1, 0) for module_id in ["skip", "bad", "never"]}
    assert [e["step_id"] for e in r["mock_errors"]] == ["skip", "bad", "never"]
    assert all("mock" not in e for e in r["trace"])


def test_final_accounting_includes_the_failure_handler_and_preserves_the_original_error() -> None:
    f = flow([script("bad"), script("never")], {"failure_module": script("failure")})
    r = run_flow(f, {}, {
        "bad": {"sequence": [{"error": {"name": "Original", "message": "bad"}}]},
        "never": {"sequence": [{"result": 1}]},
        "failure": {"sequence": [{"result": None}]},
    }, {"requireAllMocksUsed": True})
    assert r["error"]["name"] == "Original"
    assert r["failure_result"] is None
    assert r["mock_usage"] == {"bad": usage(1, 1), "never": usage(1, 0), "failure": usage(1, 1)}
    assert [e["step_id"] for e in r["mock_errors"]] == ["never"]
    assert r["trace"][-1]["mock"] == {"source": "sequence", "call": 1}


def test_unused_checks_do_not_invoke_failure_handlers_or_mask_budget_failures() -> None:
    f = flow([script("send")], {"failure_module": script("failure")})
    mocks = {"send": {"sequence": [{"result": 1}, {"result": 2}]}, "failure": {"sequence": [{"result": 3}]}}
    r = run_flow(f, {}, mocks, {"requireAllMocksUsed": True})
    assert r["error"]["name"] == "MockUnused"
    assert r["executed"] == ["send"]
    assert r["mock_usage"]["failure"]["calls"] == 0
    budget = run_flow(flow([each([script("send")])], {"failure_module": script("failure")}), {}, mocks,
                      {"maxSteps": 1, "requireAllMocksUsed": True})
    assert budget["error"]["name"] == "BudgetExceeded"
    assert budget["mock_usage"] == {"send": usage(2, 0), "failure": usage(1, 0)}


def test_every_actual_mock_lookup_is_marked_including_repeating_inline_fallback_and_missing_mocks() -> None:
    repeated = run_flow(flow([each([script("send")])]), {}, {"send": {"result": 1}}, {"requireAllMocksUsed": True})
    assert repeated["status"] == "ok"
    assert repeated["mock_usage"] == {}
    assert [e["mock"] for e in repeated["trace"] if e.get("mock")] == [{"source": "external", "call": call} for call in [1, 2]]
    inline_flow = flow([script("send", {"mock": {"enabled": True, "return_value": 3}})])
    assert run_flow(inline_flow)["trace"][0]["mock"] == {"source": "inline", "call": 1}
    assert run_flow(inline_flow, {}, {"send": {"result": 4}})["trace"][0]["mock"] == {"source": "external", "call": 1}
    for policy in ["null", "echo-args"]:
        assert run_flow(flow([script("send")]), {}, {}, {"unmocked": policy})["trace"][0]["mock"] == {"source": "fallback", "call": 1}
    assert run_flow(flow([script("send")]))["trace"][0]["mock"] == {"source": "missing", "call": 1}


def test_require_all_mocks_used_accepts_booleans_only() -> None:
    # UNDEFINED stands in for JavaScript's explicit `undefined` option value.
    for value in [None, "true", 1, UNDEFINED]:
        with pytest.raises(FormatError, match="requireAllMocksUsed.*boolean"):
            run_flow(flow([]), {}, {}, {"requireAllMocksUsed": value})
    assert run_flow(flow([]), {}, {}, {"requireAllMocksUsed": False})["status"] == "ok"
