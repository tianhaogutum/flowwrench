"""Port of ``test/converter.test.js``."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from flowwrench.n8n2ap import ConversionError, InputError, convert, report_markdown, validate_template
from flowwrench.n8n2ap.jscompat import stringify, parse_json

from .helpers import (
    JsError,
    clone,
    edge,
    fixture,
    golden_text,
    js,
    node,
    run_code,
    schema_valid,
    start,
    steps,
    workflow,
)


def find(items: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(item for item in items if predicate(item))


@pytest.mark.parametrize("name", ["webhook-http-if", "schedule-set", "manual-unsupported"])
def test_golden_e2e(name: str) -> None:
    result = convert(fixture(name))
    expected = json.loads(golden_text(name))
    assert result == expected
    assert schema_valid(result["template"])
    assert re.search(r"Coverage:", report_markdown(result["report"]))


def test_webhook_http_if_set_maps_response_and_branch_local_input() -> None:
    result = convert(fixture("webhook-http-if"))
    template, report = result["template"], result["report"]
    assert report["complete"] is True
    assert report["coveragePercent"] == 100
    trigger, all_steps = template["flows"][0]["trigger"], steps(template)
    http = find(all_steps, lambda s: s["type"] == "PIECE")
    router = find(all_steps, lambda s: s["type"] == "ROUTER")
    set_step = router["children"][0]
    assert trigger["settings"]["triggerName"] == "catch_webhook"
    assert http["settings"]["input"]["url"] == "https://api.example.test/orders/{{webhook_method_guard.output.body.id}}"
    assert http["settings"]["input"]["queryParams"]["region"] == "{{webhook_method_guard.output.query.region}}"
    assert http["settings"]["input"]["timeout"] == 5
    assert router["settings"]["branches"][0]["conditions"][0][0]["firstValue"] == "{{step_2.output.body.status}}"
    assert router["children"][1] is None
    assert set_step["settings"]["input"]["values"][1]["value"] == "{{step_2.output.body.amount}}"
    assert run_code(set_step, {"values": [{"name": "orderId", "type": "string", "value": "A1"},
                                          {"name": "amount", "type": "number", "value": 42}]}) == {"orderId": "A1", "amount": 42}
    method_guard = find(all_steps, lambda s: s["name"] == "webhook_method_guard")
    assert run_code(method_guard, {"data": {"method": "POST", "body": {}, "headers": {}, "queryParams": {"region": "eu"}},
                                   "method": "POST"}) == {"body": {}, "headers": {}, "query": {"region": "eu"}}
    response_guard = find(all_steps, lambda s: s["name"] == "response_guard_step_2")
    assert run_code(response_guard, {"data": {"status": "paid"}}) == {"status": "paid"}
    for data in [[], None, "text", 42]:
        with pytest.raises(JsError, match="single JSON object"):
            run_code(response_guard, {"data": data})
    with pytest.raises(JsError, match="unexpected"):
        run_code(method_guard, {"data": {"method": "GET"}, "method": "POST"})
    with pytest.raises(JsError, match="missing"):
        run_code(method_guard, {"data": {}, "method": "POST"})


def test_legacy_set_noop_and_sticky_note_preserve_expected_data_without_executable_note() -> None:
    result = convert(fixture("schedule-set"))
    template, report = result["template"], result["report"]
    assert report["counts"]["converted"] == 4
    all_steps = steps(template)
    set_step = find(all_steps, lambda s: s["displayName"] == "Prepare")
    pass_step = find(all_steps, lambda s: s["displayName"] == "Ready")
    data = run_code(set_step)
    assert data == {"message": "Daily digest", "limit": 10, "preview": True}
    assert run_code(pass_step, {"data": data}) == data
    assert template["flows"][0]["notes"][0]["content"] == "Synthetic fixture: no service calls."
    assert len(all_steps) == 3


def test_credentials_pin_data_arbitrary_code_and_unknown_parameters_never_enter_artifacts() -> None:
    result = convert(fixture("manual-unsupported"))
    assert result["report"]["counts"] == {"converted": 0, "partial": 2, "unsupported": 1}
    assert result["template"]["flows"][0]["trigger"]["type"] == "EMPTY"
    output = js(result) + report_markdown(result["report"])
    for secret in ["SYNTHETIC_CODE_MUST_NOT_RUN_OR_BE_EXPORTED", "SYNTHETIC_SECRET_DO_NOT_EXPORT",
                   "SYNTHETIC_CREDENTIAL_ID_DO_NOT_EXPORT", "SYNTHETIC_PIN_DATA_DO_NOT_EXPORT"]:
        assert secret not in output
    assert result["report"]["credentials"] == [{"node": "CRM", "name": "Example CRM connection", "type": "exampleApi"}]
    for s in [s for s in steps(result["template"]) if s["type"] == "CODE"]:
        with pytest.raises(JsError, match="n8n2ap:"):
            run_code(s)


SCHEDULES = [
    ("scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}}, "*/15 * * * *"),
    ("scheduleTrigger", {"rule": {"interval": [{"field": "hours", "hoursInterval": 6, "triggerAtMinute": 10}]}}, "10 */6 * * *"),
    ("scheduleTrigger", {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 9, "triggerAtMinute": 20}]}},
     "20 9 * * *"),
    ("scheduleTrigger", {"rule": {"interval": [{"field": "cronExpression", "expression": "0 0 8 * * 1-5"}]}}, "0 8 * * 1-5"),
    ("cron", {"triggerTimes": {"item": [{"mode": "custom", "cronExpression": "*/10 * * * *"}]}}, "*/10 * * * *"),
    ("cron", {"triggerTimes": {"item": [{"mode": "custom", "cronExpression": "0 17 8 * * 1-5"}]}}, "17 8 * * 1-5"),
]


@pytest.mark.parametrize(("kind", "params", "expected"), SCHEDULES, ids=[f"{k} {e}" for k, _, e in SCHEDULES])
def test_schedule(kind: str, params: dict[str, Any], expected: str) -> None:
    source = workflow(node("Clock", kind, params))
    source["id"] = "synthetic-schedule-39"  # Pinned source phase for Clock is second=0.
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == expected


UNSUPPORTED_SCHEDULES = [
    {"rule": {"interval": [{"field": "minutes", "minutesInterval": 7}]}},
    {"rule": {"interval": [{"field": "cronExpression", "expression": "1 * * * * *"}]}},
    {"rule": {"interval": [{"field": "cronExpression", "expression": "0 99 * * *"}]}},
    {"rule": {"interval": [{"field": "cronExpression", "expression": "@hourly"}]}},
    {"rule": {"interval": [{"field": "days", "daysInterval": 2}]}},
    {"rule": {"interval": [{"field": "seconds", "secondsInterval": 30}]}},
    {"rule": {"interval": [{"field": "minutes"}, {"field": "hours"}]}},
]


@pytest.mark.parametrize("params", UNSUPPORTED_SCHEDULES, ids=[js(p) for p in UNSUPPORTED_SCHEDULES])
def test_unsupported_schedule(params: dict[str, Any]) -> None:
    source = workflow(node("Clock", "scheduleTrigger", params, 1.2))
    source["id"] = "synthetic-schedule-39"
    result = convert(source)
    assert result["report"]["nodes"][0]["status"] == "partial"
    assert result["template"]["flows"][0]["trigger"]["type"] == "EMPTY"


def test_missing_or_invalid_timezone_blocks_scheduling() -> None:
    for settings in [{}, {"timezone": "Not/AZone"}]:
        w = workflow(start())
        w["settings"] = settings
        assert convert(w)["template"]["flows"][0]["trigger"]["type"] == "EMPTY"


def test_post_http_maps_json_headers_query_and_millisecond_timeout() -> None:
    w = workflow(start(), node("Post", "httpRequest", {
        "method": "POST", "url": "https://example.test", "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}]},
        "sendBody": True, "specifyBody": "json", "jsonBody": '{"count":3,"value":"={{ $json.foo }}"}',
        "options": {"timeout": 1250}}, 4.2))
    result = convert(w)
    step = find(steps(result["template"]), lambda s: s["displayName"] == "Post")
    assert result["report"]["complete"] is True
    assert step["type"] == "PIECE"
    assert step["settings"]["input"]["body"] == {"data": {"count": 3, "value": "{{trigger.output.foo}}"}}
    assert step["settings"]["input"]["headers"] == {"Accept": "application/json"}
    assert step["settings"]["input"]["timeout"] == 1.25


def test_http_pair_body_and_json_headers_query() -> None:
    result = convert(workflow(start(), node("Post", "httpRequest", {
        "method": "POST", "url": "https://example.test", "sendBody": True,
        "bodyParameters": {"parameters": [{"name": "x", "value": 3}]}, "sendHeaders": True, "specifyHeaders": "json",
        "jsonHeaders": '{"Accept":"application/json"}', "sendQuery": True, "specifyQuery": "json",
        "jsonQuery": '{"page":2}'}, 4.2)))
    input_ = find(steps(result["template"]), lambda s: s["type"] == "PIECE")["settings"]["input"]
    assert result["report"]["complete"] is True
    assert input_["body"] == {"data": {"x": 3}}
    assert input_["queryParams"] == {"page": 2}


HTTP_FAILURES = [
    {"options": {"pagination": {"enabled": True}}}, {"options": {"redirect": {"followRedirects": False}}},
    {"authentication": "genericCredentialType"}, {"method": "TRACE"}, {"url": "file:///etc/passwd"},
    {"sendBody": True, "contentType": "multipart-form-data"}, {"sendBody": True, "specifyBody": "json", "jsonBody": "not json"},
    {"sendHeaders": True, "headerParameters": {"parameters": [{"name": "x", "value": 1}, {"name": "x", "value": 2}]}},
    {"url": "={{ $env.URL }}"}, {"options": {"timeout": -1}},
]


@pytest.mark.parametrize("params", HTTP_FAILURES, ids=[js(p) for p in HTTP_FAILURES])
def test_http_fails_closed(params: dict[str, Any]) -> None:
    result = convert(workflow(start(), node("Request", "httpRequest", {"url": "https://example.test", **params}, 4.2)))
    assert result["report"]["nodes"][1]["status"] == "partial"
    s = find(steps(result["template"]), lambda s: s["displayName"] == "Request")
    assert s["type"] == "CODE"
    with pytest.raises(JsError, match="Migration blocked"):
        run_code(s)


def test_set_preserving_fields_and_runtime_type_validation() -> None:
    w = workflow(start(), node("Set", "set", {"assignments": {"assignments": [
        {"name": "x", "value": "={{ $json.x }}", "type": "number"}]}, "includeOtherFields": True}, 3.4))
    result = convert(w)
    s = find(steps(result["template"]), lambda s: s["displayName"] == "Set")
    assert result["report"]["complete"] is True
    assert run_code(s, {"data": {"old": 1}, "values": [{"name": "x", "type": "number", "value": 2}]}) == {"old": 1, "x": 2}
    with pytest.raises(JsError, match="expected number"):
        run_code(s, {"data": {}, "values": [{"name": "x", "type": "number", "value": "2"}]})


SET_ROWS = [{"name": "a.b", "value": "x", "type": "string"}, {"name": "x", "value": "false", "type": "boolean"},
            {"name": "x", "value": "2", "type": "number"}, {"name": "x", "value": [], "type": "object"},
            {"name": "x", "value": {}, "type": "array"}]


@pytest.mark.parametrize("row", SET_ROWS, ids=[js(r) for r in SET_ROWS])
def test_set_unsupported_value(row: dict[str, Any]) -> None:
    result = convert(workflow(start(), node("Set", "set", {"assignments": {"assignments": [row]}}, 3.4)))
    assert result["report"]["nodes"][1]["status"] == "partial"


def test_modern_or_if_and_legacy_boolean_conditions_produce_correct_router_condition_groups() -> None:
    w = fixture("webhook-http-if")
    p = w["nodes"][2]["parameters"]
    p["conditions"]["combinator"] = "or"
    p["conditions"]["conditions"].append({"leftValue": "={{ $json.count }}", "rightValue": 4,
                                          "operator": {"type": "number", "operation": "gt"}})
    router = find(steps(convert(w)["template"]), lambda s: s["type"] == "ROUTER")
    assert len(router["settings"]["branches"][0]["conditions"]) == 2
    assert router["settings"]["branches"][0]["conditions"][1][0]["operator"] == "NUMBER_IS_GREATER_THAN"
    w["nodes"][2]["typeVersion"] = 1
    p["conditions"] = {"boolean": [{"value1": "={{ $json.ok }}", "value2": True, "operation": "equal"}]}
    router = find(steps(convert(w)["template"]), lambda s: s["type"] == "ROUTER")
    assert router["settings"]["branches"][0]["conditions"][0][0]["operator"] == "BOOLEAN_IS_TRUE"


def test_strict_if_unsupported_comparison_cannot_accidentally_route_to_false_branch() -> None:
    w = fixture("webhook-http-if")
    w["nodes"][2]["parameters"]["conditions"]["conditions"][0]["operator"]["operation"] = "regex"
    result = convert(w)
    s = find(steps(result["template"]), lambda s: s["displayName"] == "Is paid")
    assert s["type"] == "CODE"
    with pytest.raises(JsError, match="blocked"):
        run_code(s)
    assert result["report"]["nodes"][3]["emitted"] is False
    assert result["report"]["nodes"][3]["status"] == "unsupported"


def _condition(name: str) -> dict[str, Any]:
    return node(name, "if", {"conditions": {"number": [{"value1": "={{ $json.x }}", "value2": 1, "operation": "equal"}]}})


def test_diamond_merge_is_emitted_once_after_if_and_fails_explicitly() -> None:
    w = workflow(start(), _condition("Choice"), node("True", "noOp"), node("False", "noOp"),
                 node("Merge", "merge", {"mode": "append"}, 3))
    w["connections"]["Choice"] = {"main": [[edge("True")], [edge("False")]]}
    w["connections"]["True"] = {"main": [[edge("Merge", 0)]]}
    w["connections"]["False"] = {"main": [[edge("Merge", 1)]]}
    result = convert(w)
    router = find(steps(result["template"]), lambda s: s["type"] == "ROUTER")
    assert router["children"][0]["displayName"] == "True"
    assert router["children"][1]["displayName"] == "False"
    assert router["nextAction"]["displayName"] == "Merge"
    assert len([s for s in steps(result["template"]) if s["displayName"] == "Merge"]) == 1
    assert result["report"]["nodes"][-1]["status"] == "unsupported"
    with pytest.raises(JsError, match="blocked"):
        run_code(router["nextAction"])


def test_cross_branch_node_reference_is_flagged_instead_of_emitting_an_unavailable_step_reference() -> None:
    w = fixture("webhook-http-if")
    w["nodes"].append(node("False", "set", {"assignments": {"assignments": [
        {"name": "x", "value": '={{ $("Receipt").item.json.orderId }}', "type": "string"}]}}, 3.4))
    w["connections"]["Is paid"]["main"][1] = [edge("False")]
    result = convert(w)
    assert result["report"]["nodes"][-1]["status"] == "partial"
    assert re.search(r"not guaranteed", " ".join(result["report"]["nodes"][-1]["issues"]))


def test_non_if_fanout_blocks_execution_and_reports_every_non_emitted_downstream_node() -> None:
    w = workflow(start(), node("A", "noOp"), node("B", "noOp"), node("C", "noOp"))
    w["connections"]["A"]["main"][0].append(edge("C"))
    result = convert(w)
    s = find(steps(result["template"]), lambda s: s["displayName"] == "A")
    assert s["type"] == "CODE"
    with pytest.raises(JsError, match="blocked"):
        run_code(s)
    assert all(r["status"] == "unsupported" and not r["emitted"] for r in result["report"]["nodes"][2:])


def test_multiple_triggers_require_override_and_unselected_graphs_remain_in_report() -> None:
    w = workflow(start())
    w["nodes"].append(node("Other", "manualTrigger"))
    with pytest.raises(Exception, match="--trigger"):
        convert(w)
    result = convert(w, trigger="Start")
    assert result["report"]["nodes"][1]["status"] == "unsupported"
    assert convert(w, trigger="Other")["template"]["flows"][0]["trigger"]["type"] == "EMPTY"
    with pytest.raises(Exception, match="exactly one"):
        convert(w, trigger="missing")


def _absent_target() -> dict[str, Any]:
    w = workflow(start())
    w["connections"]["Start"] = {"main": [[edge("absent")]]}
    return w


def _incoming_trigger() -> dict[str, Any]:
    w = workflow(start(), node("A", "noOp"))
    w["connections"]["A"] = {"main": [[edge("Start")]]}
    return w


def _disconnected_cycle() -> dict[str, Any]:
    w = workflow(start())
    w["nodes"].extend([node("A", "noOp"), node("B", "noOp")])
    w["connections"]["A"] = {"main": [[edge("B")]]}
    w["connections"]["B"] = {"main": [[edge("A")]]}
    return w


def _too_deep() -> dict[str, Any]:
    w = workflow(start())
    nested: dict[str, Any] = {}
    w["extra"] = nested
    for _ in range(42):
        nested["x"] = {}
        nested = nested["x"]
    return w


INPUT_FAILURES: list[tuple[str, Callable[[], Any]]] = [
    ("null", lambda: None),
    ("array", lambda: []),
    ("no nodes", lambda: {"nodes": [], "connections": {}}),
    ("array connections", lambda: {"nodes": [start()], "connections": []}),
    ("duplicate names", lambda: workflow(start(), start())),
    ("absent target", _absent_target),
    ("incoming trigger cycle", _incoming_trigger),
    ("disconnected cycle", _disconnected_cycle),
    ("__proto__ key", lambda: parse_json('{"nodes":[],"connections":{},"__proto__":{"polluted":true}}')),
    ("42-level nesting", _too_deep),
    ("501 nodes", lambda: workflow(*[node(f"N{i}", "noOp") for i in range(501)])),
]


@pytest.mark.parametrize("make", [m for _, m in INPUT_FAILURES], ids=[n for n, _ in INPUT_FAILURES])
def test_input_failure(make: Callable[[], Any]) -> None:
    with pytest.raises(InputError):
        convert(make())


def _set_trigger_type(t: dict[str, Any]) -> None:
    t["flows"][0]["trigger"]["type"] = "CODE"


def _set_code_number(t: dict[str, Any]) -> None:
    find(steps(t), lambda s: s["type"] == "CODE")["settings"]["sourceCode"]["code"] = 42


def _set_latest_version(t: dict[str, Any]) -> None:
    find(steps(t), lambda s: s["type"] == "PIECE")["settings"]["pieceVersion"] = "latest"


def _set_router_children(t: dict[str, Any]) -> None:
    find(steps(t), lambda s: s["type"] == "ROUTER")["children"] = "not children"


def test_schema_independently_rejects_invalid_trigger_code_piece_version_and_router_shape() -> None:
    for mutate in [_set_trigger_type, _set_code_number, _set_latest_version, _set_router_children]:
        t = convert(fixture("webhook-http-if"))["template"]
        mutate(t)
        assert schema_valid(t) is False
        with pytest.raises(ConversionError, match="validation failed"):
            validate_template(t)


def test_all_output_step_names_are_unique_markdown_escapes_user_controlled_node_titles() -> None:
    w = workflow(start(), node("<script>|x\nY", "noOp"))
    result = convert(w)
    names = [s["name"] for s in steps(result["template"])]
    assert len(set(names)) == len(names)
    assert "<script>" not in report_markdown(result["report"])


def test_http_v3_v4_default_redirect_and_five_minute_timeout_semantics() -> None:
    for version in [3, 4.2]:
        result = convert(workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test"}, version)))
        http = find(steps(result["template"]), lambda s: s["type"] == "PIECE")
        assert http["settings"]["input"]["followRedirects"] is (version >= 4)
        assert http["settings"]["input"]["timeout"] == 300
    result = convert(workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test"}, 1)))
    assert result["report"]["nodes"][1]["status"] == "partial"


def test_strict_if_operand_type_guard_prevents_implicit_numeric_string_boolean_coercion() -> None:
    result = convert(fixture("webhook-http-if"))
    guard = find(steps(result["template"]), lambda s: s["name"] == "if_type_guard_step_3")
    assert run_code(guard, {"checks": [{"type": "string", "value": "paid"}, {"type": "number", "value": 42},
                                       {"type": "boolean", "value": False}]}) is True
    for check in [{"type": "number", "value": "42"}, {"type": "boolean", "value": "false"}, {"type": "string", "value": None}]:
        with pytest.raises(JsError, match="type mismatch"):
            run_code(guard, {"checks": [check]})


def test_legacy_if_larger_smaller_aliases_map_to_numeric_operators() -> None:
    for op, expected in [("larger", "NUMBER_IS_GREATER_THAN"), ("smaller", "NUMBER_IS_LESS_THAN")]:
        w = workflow(start(), node("If", "if", {"conditions": {"number": [{"value1": "={{ $json.x }}", "value2": 1, "operation": op}]}}))
        router = find(steps(convert(w)["template"]), lambda s: s["type"] == "ROUTER")
        assert router["settings"]["branches"][0]["conditions"][0][0]["operator"] == expected


def test_unstructured_overlapping_branches_are_rejected_instead_of_duplicated() -> None:
    w = workflow(start(), _condition("Outer"), _condition("Inner"), node("Other", "noOp"), node("Shared", "noOp"),
                 node("Separate", "noOp"))
    w["connections"]["Outer"] = {"main": [[edge("Inner")], [edge("Other")]]}
    w["connections"]["Inner"] = {"main": [[edge("Shared")], [edge("Separate")]]}
    w["connections"]["Other"] = {"main": [[edge("Shared")]]}
    del w["connections"]["Shared"]
    with pytest.raises(ConversionError, match="Unstructured graph"):
        convert(w)


def test_500_node_upper_bound_handles_a_real_linear_graph_and_input_stays_immutable() -> None:
    w = workflow(start(), *[node(f"N{i}", "noOp") for i in range(499)])
    before = clone(w)
    result = convert(w)
    assert result["report"]["total"] == 500
    assert result["report"]["counts"]["converted"] == 500
    assert len(steps(result["template"])) == 500
    assert w == before


# --------------------------------------------------------------------------- Python-only additions


@pytest.mark.parametrize("name", ["webhook-http-if", "schedule-set", "manual-unsupported"])
def test_golden_serialization_is_byte_identical_to_json_stringify(name: str) -> None:
    """The goldens are ``JSON.stringify(result, null, 2)`` output; key order and numbers must match."""
    assert stringify(convert(fixture(name)), 2) + "\n" == golden_text(name)
