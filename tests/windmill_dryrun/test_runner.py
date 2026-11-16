"""Port of test/runner.test.js."""

from __future__ import annotations

import re
from typing import Any

import pytest

from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.runner import run_flow

pytestmark = pytest.mark.needs_node


def jsx(expr: str) -> dict[str, Any]:
    return {"type": "javascript", "expr": expr}


def stat(value: Any) -> dict[str, Any]:
    return {"type": "static", "value": value}


def flow(modules: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "synthetic test", "value": {"modules": modules, **(extra or {})}}


def identity(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "identity"}, **(extra or {})}


def script(module_id: str, transforms: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": module_id,
        "value": {"type": "script", "path": f"f/demo/{module_id}", "input_transforms": transforms or {}},
        **(extra or {}),
    }


def loop(module_id: str, modules: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "whileloopflow", "modules": modules, "skip_failures": False}, **(extra or {})}


def run(f: Any, input: Any = None, mocks: Any = None, options: Any = None) -> dict[str, Any]:
    return run_flow(f, {} if input is None else input, {} if mocks is None else mocks, {} if options is None else options)


def ok(report: dict[str, Any]) -> dict[str, Any]:
    assert report["status"] == "ok", report.get("error")
    return report


def error(report: dict[str, Any], module_id: str, pattern: str) -> dict[str, Any]:
    assert report["status"] == "error"
    assert report["error"]["step_id"] == module_id
    assert re.search(pattern, report["error"]["message"]), report["error"]["message"]
    return report


def test_sequence_resolves_static_js_previous_result_and_hyphenated_ids() -> None:
    r = ok(run(flow([
        script("fetch-order", {"id": jsx("flow_input.id"), "prefix": stat("order")}),
        script("next", {"amount": jsx('results["fetch-order"].total * 2'), "previous": jsx("previous_result.total")}),
    ]), {"id": 7}, {"fetch-order": {"result": {"total": 12}}, "next": {"echo_args": True}}))
    assert r["result"] == {"amount": 24, "previous": 12}
    assert r["trace"][0]["args"] == {"id": 7, "prefix": "order"}


def test_identity_passes_previous_result_flow_identity_returns_full_flow_input() -> None:
    r = ok(run(flow([script("a"), identity("pass"), {"id": "root", "value": {"type": "identity", "flow": True}}]),
               {"n": 3}, {"a": {"result": 42}}))
    assert r["results"]["pass"] == 42
    assert r["result"] == {"n": 3}


def test_branchone_first_true_wins_predicates_short_circuit_default_executes() -> None:
    f = flow([{"id": "choice", "value": {"type": "branchone", "branches": [
        {"expr": "flow_input.choose", "modules": [script("a")]},
        {"expr": "results.typo", "modules": [script("b")]},
    ], "default": [identity("fallback")]}}])
    r = ok(run(f, {"choose": True}, {"a": {"result": "first"}}))
    assert r["result"] == "first"
    assert r["executed"] == ["choice", "a"]
    assert r["coverage"]["branches"]["executed"] == ["choice:0"]
    f["value"]["modules"][0]["value"]["branches"][1]["expr"] = "false"
    fallback = ok(run(f, {"choose": False}))
    assert fallback["executed"] == ["choice", "fallback"]
    assert fallback["coverage"]["branches"]["executed"] == ["choice:default"]


def test_branchall_returns_ordered_results_and_isolates_sibling_scopes() -> None:
    r = ok(run(flow([{"id": "all", "value": {"type": "branchall", "parallel": False, "branches": [
        {"modules": [script("a")]}, {"modules": [script("b", {"seen": jsx('results.a ?? "isolated"')})]},
    ]}}]), {}, {"a": {"result": "A"}, "b": {"echo_args": True}}))
    assert r["result"] == ["A", {"seen": "isolated"}]
    assert len(r["coverage"]["branches"]["executed"]) == 2


def test_branchall_propagates_failure_unless_skip_failure_explicitly_set() -> None:
    f = flow([{"id": "all", "value": {"type": "branchall", "branches": [
        {"skip_failure": True, "modules": [script("bad")]}, {"modules": [script("good")]},
    ]}}])
    mocks = {"bad": {"error": {"message": "fail"}}, "good": {"result": 5}}
    assert ok(run(f, {}, mocks))["result"] == [None, 5]
    f["value"]["modules"][0]["value"]["branches"][0]["skip_failure"] = False
    error(run(f, {}, mocks), "bad", "fail")


def test_forloop_maps_iter_values_index_and_collects_last_body_results() -> None:
    r = ok(run(flow([{"id": "each", "value": {"type": "forloopflow", "iterator": jsx("flow_input.items"), "skip_failures": False,
                                               "modules": [script("item", {"item": jsx("flow_input.iter.value"), "index": jsx("flow_input.iter.index")})]}},
                     identity("after")]),
               {"items": ["a", "b"]}, {"item": {"echo_args": True}}))
    assert r["result"] == [{"item": "a", "index": 0}, {"item": "b", "index": 1}]
    assert r["executed"] == ["each", "item", "item", "after"]


def test_forloop_rejects_non_array_iterator_and_oversized_array() -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": stat("abc"), "skip_failures": False, "modules": []}}])
    error(run(f), "each", "array")
    f["value"]["modules"][0]["value"]["iterator"] = stat([1, 2, 3])
    error(run(f, {}, {}, {"maxIterations": 2}), "each", "max-iterations")
    f["value"]["modules"][0]["value"]["iterator"] = stat(None)
    error(run(f), "each", "array")


def test_forloop_skip_failures_returns_null_and_mock_sequences_consume_one_per_invocation() -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": stat([0, 1]), "skip_failures": True, "modules": [script("item")]}}])
    r = ok(run(f, {}, {"item": {"sequence": [{"error": {"message": "first fails"}}, {"result": 2}]}}))
    assert r["result"] == [None, 2]


def test_nested_loops_restore_the_parent_iter_and_isolate_forloop_iteration_results() -> None:
    f = flow([{"id": "outer", "value": {"type": "forloopflow", "skip_failures": False, "iterator": stat(["a", "b"]), "modules": [
        {"id": "inner", "value": {"type": "forloopflow", "skip_failures": False, "iterator": stat([1]), "modules": [identity("noop")]}},
        script("mapped", {"value": jsx("flow_input.iter.value"), "old": jsx("results.mapped ?? null")}),
    ]}}])
    r = ok(run(f, {}, {"mapped": {"echo_args": True}}))
    assert r["result"] == [{"value": "a", "old": None}, {"value": "b", "old": None}]


def test_whileloop_stops_on_inner_step_carries_prior_body_result_then_resumes_outer_flow() -> None:
    r = ok(run(flow([
        loop("repeat", [script("counter", {
            "n": jsx("(results.counter?.n ?? 0) + 1"), "index": jsx("flow_input.iter.index"), "value": jsx("flow_input.iter.value"),
        }, {"stop_after_if": {"expr": "result.n === 3"}})]),
        identity("after"),
    ]), {}, {"counter": {"echo_args": True}}))
    assert [x["n"] for x in r["result"]] == [1, 2, 3]
    assert [x["value"] for x in r["result"]] == [0, 1, 2]
    assert r["executed"][-1] == "after"


def test_whileloop_stop_after_if_evaluates_per_iteration_and_max_iterations_fails_precisely() -> None:
    f = flow([loop("repeat", [script("count", {"n": jsx("flow_input.iter.index")})], {"stop_after_if": {"expr": "result.n === 1"}}), identity("after")])
    assert len(ok(run(f, {}, {"count": {"echo_args": True}}))["result"]) == 2
    f["value"]["modules"][0]["stop_after_if"]["expr"] = "false"
    f["value"]["modules"][0]["value"]["max_iterations"] = 2
    r = error(run(f, {}, {"count": {"echo_args": True}}, {"maxIterations": 5}), "repeat", "max-iterations 2")
    assert len([e for e in r["trace"] if e["module_id"] == "count"]) == 2
    assert "after" not in r["executed"]


def test_skip_if_does_not_consume_a_mock_or_overwrite_previous_result() -> None:
    r = ok(run(flow([script("a"), script("skip", {}, {"skip_if": {"expr": "results.a === 9"}}), identity("after")]), {}, {"a": {"result": 9}}))
    assert r["result"] == 9
    assert r["executed"] == ["a", "after"]
    assert r["trace"][1]["status"] == "skipped"
    assert "skip" in r["coverage"]["modules"]["missed"]


def test_stop_after_if_ends_root_sequence_and_can_inspect_current_result_id() -> None:
    r = ok(run(flow([script("a", {}, {"stop_after_if": {"expr": "results.a === 9", "skip_if_stopped": True}}), script("never")]),
               {}, {"a": {"result": 9}}))
    assert r["stopped"] is True
    assert r["executed"] == ["a"]
    assert r["result"] == 9


def test_stop_after_if_on_an_inner_step_breaks_nearest_forloop_stop_after_all_iters_if_ends_root() -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": stat([1, 2, 3]), "skip_failures": False,
                                        "modules": [script("item", {"n": jsx("flow_input.iter.value")}, {"stop_after_if": {"expr": "result.n === 2"}}), script("tail")]},
               "stop_after_all_iters_if": {"expr": "result.length === 2"}}, script("after")])
    r = ok(run(f, {}, {"item": {"echo_args": True}, "tail": {"result": "tail"}}))
    assert r["result"] == ["tail", {"n": 2}]
    assert r["stopped"] is True
    assert "after" not in r["executed"]


def test_stop_with_error_and_failure_module_get_structured_failure_details() -> None:
    f = flow([script("work", {}, {"stop_after_if": {"expr": "true", "error_message": "rejected"}}), script("never")], {
        "failure_module": script("failure", {"step": jsx("error.step_id"), "message": jsx("flow_input.error.message"),
                                             "prior": jsx("previous_result.error.name")}),
    })
    r = error(run(f, {}, {"work": {"result": 5}, "failure": {"echo_args": True}}), "work", "rejected")
    assert r["failure_result"] == {"step": "work", "message": "rejected", "prior": "StopAfterError"}
    assert r["executed"] == ["work", "failure"]


def test_failure_handler_failure_preserves_original_failure() -> None:
    r = run(flow([script("a")], {"failure_module": script("failure")}), {}, {
        "a": {"error": {"message": "original"}}, "failure": {"error": {"message": "handler"}},
    })
    assert r["error"]["message"] == "original"
    assert r["failure_error"]["message"] == "handler"


def test_continue_on_error_publishes_structured_error_and_resumes_sequence() -> None:
    r = ok(run(flow([script("bad", {}, {"continue_on_error": True}), script("after", {"error": jsx("results.bad.error.message")})]), {}, {
        "bad": {"error": {"message": "failed"}}, "after": {"echo_args": True},
    }))
    assert r["result"] == {"error": "failed"}
    assert r["trace"][0]["continued"] is True


def test_retry_consumes_error_result_sequence_records_delays_without_sleeping() -> None:
    r = ok(run(flow([script("a", {}, {"retry": {
        "constant": {"attempts": 1, "seconds": 60}, "exponential": {"attempts": 1, "multiplier": 2, "seconds": 3},
        "retry_if": {"expr": 'error.name === "Temporary"'},
    }})]), {}, {
        "a": {"sequence": [{"error": {"name": "Temporary", "message": "retry 1"}}, {"error": {"name": "Temporary", "message": "retry 2"}}, {"result": 7}]},
    }))
    assert r["result"] == 7
    assert r["simulated_delay_seconds"] == 78
    assert [e["attempt"] for e in r["trace"]] == [1, 2, 3]
    assert [e["retry_delay_seconds"] for e in r["trace"][:2]] == [60, 18]


def test_retry_predicate_can_deny_retry_and_sequences_fail_on_exhaustion() -> None:
    f = flow([script("a", {}, {"retry": {"constant": {"attempts": 2, "seconds": 0}, "retry_if": {"expr": "false"}}})])
    mocks = {"a": {"sequence": [{"error": {"message": "denied"}}]}}
    assert len(error(run(f, {}, mocks), "a", "denied")["trace"]) == 1
    f["value"]["modules"][0]["retry"]["retry_if"]["expr"] = "true"
    assert error(run(f, {}, mocks), "a", "exhausted")["error"]["name"] == "MockExhausted"


def test_unknown_result_references_are_hard_errors_including_computed_properties_and_membership() -> None:
    for expr in ["results.typo", "results[flow_input.key]", '"typo" in results']:
        r = error(run(flow([script("map", {"x": jsx(expr)})]), {"key": "typo"}, {"map": {"echo_args": True}}), "map", "Unknown result id: typo")
        assert r["error"]["name"] == "ExpressionError"


def test_expression_exceptions_and_nonboolean_predicates_are_explicit_module_errors() -> None:
    error(run(flow([script("map", {"x": jsx("flow_input.missing.value")})]), {}, {"map": {"echo_args": True}}), "map", "undefined")
    error(run(flow([identity("a", {"skip_if": {"expr": "1"}})])), "a", "boolean")


def test_vm_timeout_covers_infinite_js_hostile_serialization_and_microtasks() -> None:
    for source in ["(()=>{while(true){}})()", "({toJSON(){while(true){}}})", "(()=>{Promise.resolve().then(()=>{while(true){}});return 1;})()"]:
        r = error(run(flow([script("map", {"x": jsx(source)})]), {}, {"map": {"echo_args": True}}, {"timeoutMs": 15}), "map", "timed out|budget")
        assert r["error"]["name"] == "ExpressionError"


def test_vm_cannot_see_host_process_require_environment_or_dynamically_create_code() -> None:
    for source in ["process.env.HOME", 'require("node:fs").readFileSync("/etc/passwd")', '({}).constructor.constructor("return process")()',
                   'globalThis.constructor.constructor("return process")()', 'import("node:fs")']:
        error(run(flow([script("map", {"x": jsx(source)})]), {}, {"map": {"echo_args": True}}), "map", "not defined|Code generation|Async|import|callback")
    r = ok(run(flow([script("map", {"x": jsx("[typeof fetch, typeof WebSocket, typeof process, typeof require]")})]), {}, {"map": {"echo_args": True}}))
    assert r["result"]["x"] == ["undefined", "undefined", "undefined", "undefined"]


def test_expressions_must_return_finite_json_no_silent_undefined_nan_function_conversion() -> None:
    for source in ["undefined", "NaN", "({x:undefined})", "(()=>{})", "Promise.resolve(1)", "1n"]:
        error(run(flow([script("map", {"x": jsx(source)})]), {}, {"map": {"echo_args": True}}), "map", "JSON|Async|BigInt")


def test_missing_mocks_fail_null_and_echo_args_policies_and_inline_mock_are_explicit() -> None:
    f = flow([script("a", {"x": stat(1)})])
    error(run(f), "a", "Missing mock")
    assert ok(run(f, {}, {}, {"unmocked": "null"}))["result"] is None
    assert ok(run(f, {}, {}, {"unmocked": "echo-args"}))["result"] == {"x": 1}
    f["value"]["modules"][0]["mock"] = {"enabled": True, "preview_only": True, "return_value": "inline"}
    assert ok(run(f))["result"] == "inline"
    assert ok(run(f, {}, {"a": {"result": "external"}}))["result"] == "external"


def test_rawscript_content_and_referenced_paths_are_never_executed_or_loaded() -> None:
    r = ok(run(flow([
        {"id": "raw", "value": {"type": "rawscript", "language": "bash", "content": "touch /THIS_MUST_NEVER_RUN; exit 99", "input_transforms": {}}},
        {"id": "sub", "value": {"type": "flow", "path": "/etc/passwd", "input_transforms": {}}},
    ]), {}, {"raw": {"result": 1}, "sub": {"result": 2}}))
    assert r["result"] == 2


def test_execution_budget_fails_even_with_continue_on_error_and_skip_failures() -> None:
    r = error(run(flow([loop("repeat", [identity("body")], {"continue_on_error": True})]), {}, {}, {"maxSteps": 4}), "body", "max-steps")
    assert len(r["trace"]) == 4
    assert r["error"]["name"] == "BudgetExceeded"


def test_malformed_unsupported_behaviors_reject_before_execution() -> None:
    bad = [
        {}, {"modules": []}, flow([identity("same"), identity("same")]),
        flow([{"id": "AI", "value": {"type": "aiagent"}}]),
        {**flow([]), "value": {"modules": [], "skip_expr": "true"}},
        flow([script("a", {}, {"sleep": stat(1)})]),
        flow([identity("a", {"suspend": {}})]),
        flow([{"id": "loop", "value": {"type": "whileloopflow", "modules": [], "skip_failures": False, "iterator": jsx("true")}}]),
        flow([{"id": "loop", "value": {"type": "forloopflow", "modules": [], "skip_failures": False, "iterator": stat([]), "parallel": True}}]),
        flow([script("a", {"x": {"type": "ai"}})]),
        flow([identity("a", {"stop_after_all_iters_if": {"expr": "true"}})]),
        flow([script("a", {}, {"retry": {"exponential": {"attempts": 1, "multiplier": 1, "seconds": 2, "random_factor": 20}}})]),
    ]
    for f in bad:
        with pytest.raises(FormatError):
            run(f)
    with pytest.raises(FormatError, match="unknown module"):
        run(flow([script("a")]), {}, {"typo": {"result": 1}})
    with pytest.raises(FormatError, match="exactly one"):
        run(flow([script("a")]), {}, {"a": {"result": 1, "error": {"message": "x"}}})
    with pytest.raises(FormatError, match="only script"):
        run(flow([identity("a")]), {}, {"a": {"result": 1}})
    with pytest.raises(FormatError, match="input"):
        run(flow([]), [], {})
    with pytest.raises(FormatError, match="integer"):
        run(flow([]), {}, {}, {"timeoutMs": float("nan")})


def test_skip_if_stopped_is_visible_in_trace_and_root_report() -> None:
    r = ok(run(flow([identity("a", {"stop_after_if": {"expr": "true", "skip_if_stopped": True}}), identity("after")])))
    assert r["skipped"] is True
    assert r["trace"][0]["skipped_after"] is True
    assert r["executed"] == ["a"]


def test_error_include_result_preserves_stop_result_in_failure_error_and_trace() -> None:
    r = error(run(flow([script("a", {}, {"stop_after_if": {"expr": "true", "error_message": "", "error_include_result": True}})]),
                  {}, {"a": {"result": {"reason": "demo"}}}), "a", "Stopped")
    assert r["error"]["result"] == {"reason": "demo"}
    assert r["trace"][0]["result"] == {"reason": "demo"}


def test_malformed_optional_values_and_unknown_runner_options_reject_explicitly() -> None:
    for key in ["skip_if", "stop_after_if", "retry", "mock"]:
        with pytest.raises(FormatError):
            run(flow([identity("a", {key: None})]))
    with pytest.raises(FormatError):
        run(flow([], {"failure_module": None}))
    with pytest.raises(FormatError, match="unsupported field"):
        run(flow([]), {}, {}, {"typo": 1})


def test_retry_predicate_exceptions_are_traced_as_expression_errors() -> None:
    r = error(run(flow([script("a", {}, {"retry": {"constant": {"attempts": 1, "seconds": 0}, "retry_if": {"expr": "results.missing"}}})]),
                  {}, {"a": {"error": {"message": "original"}}}), "a", "Unknown result")
    assert r["trace"][0]["error"]["name"] == "ExpressionError"


def test_object_prototype_names_cannot_masquerade_as_mocks_or_executed_results() -> None:
    for module_id in ["toString", "hasOwnProperty", "valueOf"]:
        error(run(flow([script(module_id)])), module_id, "Missing mock")
        r = ok(run(flow([script(module_id)]), {}, {module_id: {"result": 123}}))
        assert r["result"] == 123
