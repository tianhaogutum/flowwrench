"""Port of ``test/security.test.js``."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from flowwrench.n8n2ap import convert

from .helpers import JsError, fixture, js, node, run_code, start, steps, workflow

SENTINEL = "SYNTHETIC_PRIVATE_VALUE_987654"
SECRETS: list[dict[str, Any]] = [
    {"url": "https://example.test", "sendHeaders": True,
     "headerParameters": {"parameters": [{"name": "Authorization", "value": f"Bearer {SENTINEL}"}]}},
    {"url": "https://example.test", "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Api-Key", "value": SENTINEL}]}},
    {"url": f"https://user:{SENTINEL}@example.test/"},
    {"url": f"https://example.test/?access_token={SENTINEL}"},
    {"url": "https://example.test", "sendBody": True, "specifyBody": "json",
     "jsonBody": json.dumps({"nested": {"password": SENTINEL}}, separators=(",", ":"))},
    {"url": "https://example.test", "sendBody": True, "bodyParameters": {"parameters": [{"name": "refreshToken", "value": SENTINEL}]}},
    {"url": "https://example.test", "sendQuery": True, "queryParameters": {"parameters": [{"name": "api_key", "value": SENTINEL}]}},
]


def find(items: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(item for item in items if predicate(item))


@pytest.mark.parametrize("params", SECRETS, ids=[f"secret boundary {i + 1}" for i in range(len(SECRETS))])
def test_secret_boundary(params: dict[str, Any]) -> None:
    result = convert(workflow(start(), node("Request", "httpRequest", params, 4.2)))
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert re.search(r"Potential secret", " ".join(result["report"]["nodes"][1]["issues"]))
    assert SENTINEL not in js(result)
    s = find(steps(result["template"]), lambda s: s["displayName"] == "Request")
    assert s["type"] == "CODE"
    with pytest.raises(JsError, match="blocked"):
        run_code(s)


def test_authentication_on_webhook_creates_empty_entry_instead_of_unauthenticated_executable_trigger() -> None:
    w = fixture("webhook-http-if")
    w["nodes"][0]["parameters"]["authentication"] = "headerAuth"
    w["nodes"][0]["credentials"] = {"httpHeaderAuth": {"id": SENTINEL, "name": "Inbound auth"}}
    result = convert(w)
    trigger = result["template"]["flows"][0]["trigger"]
    assert trigger["type"] == "EMPTY"
    assert trigger["valid"] is False
    assert SENTINEL not in js(result)
    with pytest.raises(JsError, match="require manual migration"):
        run_code(trigger["nextAction"])


def test_workflow_settings_and_ai_side_channels_are_not_silently_discarded() -> None:
    w = workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test"}, 4.2))
    w["settings"]["errorWorkflow"] = "some-other-workflow"
    result = convert(w)
    assert result["report"]["complete"] is False
    assert result["report"]["workflowIssues"]
    with pytest.raises(JsError, match="require manual migration"):
        run_code(result["template"]["flows"][0]["trigger"]["nextAction"])
    w2 = workflow(start(), node("Tool", "noOp"))
    w2["connections"]["Start"] = {"ai_tool": [[{"node": "Tool", "type": "ai_tool", "index": 0}]]}
    result2 = convert(w2)
    assert all(r["status"] != "converted" for r in result2["report"]["nodes"])


def test_literal_activepieces_template_injection_does_not_escape_expression_allowlist() -> None:
    w = workflow(start(), node("Set", "set", {"assignments": {"assignments": [
        {"name": "attack", "type": "string", "value": "{{process.env.SECRET}}"}]}}, 3.4))
    result = convert(w)
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert "process.env.SECRET" not in js(result)
    with pytest.raises(JsError, match="blocked"):
        run_code(find(steps(result["template"]), lambda s: s["displayName"] == "Set"))


def test_unsafe_property_names_encoded_inside_json_strings_cannot_reach_templates() -> None:
    w = workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test", "method": "POST", "sendBody": True,
                                                       "specifyBody": "json", "jsonBody": '{"__proto__":{"polluted":true}}'}, 4.2))
    result = convert(w)
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert "polluted" not in js(result["template"])


def _retry(n: dict[str, Any]) -> None:
    n["retryOnFail"] = True


def _continue(n: dict[str, Any]) -> None:
    n["continueOnFail"] = True


def _on_error(n: dict[str, Any]) -> None:
    n["onError"] = "continueRegularOutput"


def _future(n: dict[str, Any]) -> None:
    n["typeVersion"] = 99


def _disabled(n: dict[str, Any]) -> None:
    n["disabled"] = True


def test_unrecognized_execution_options_and_future_node_versions_block_migration() -> None:
    for modifier in [_retry, _continue, _on_error, _future, _disabled]:
        w = workflow(start(), node("N", "noOp"))
        modifier(w["nodes"][1])
        result = convert(w)
        assert result["report"]["nodes"][1]["status"] == "partial"


def test_note_secrets_are_omitted_and_reflected_in_coverage() -> None:
    w = workflow(start())
    w["nodes"].append(node("Note", "stickyNote", {"content": f"Bearer {SENTINEL}"}))
    result = convert(w)
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert SENTINEL not in js(result)


def test_unknown_false_options_are_flagged_rather_than_silently_discarded() -> None:
    result = convert(workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test",
                                                                    "options": {"customBehavior": False}}, 4.2)))
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert re.search(r"customBehavior", " ".join(result["report"]["nodes"][1]["issues"]))


def test_deep_encoded_json_and_non_finite_numbers_are_explicitly_rejected_or_blocked() -> None:
    encoded = "[" * 50 + "0" + "]" * 50
    result = convert(workflow(start(), node("HTTP", "httpRequest", {"url": "https://example.test", "method": "POST",
                                                                    "sendBody": True, "specifyBody": "json",
                                                                    "jsonBody": encoded}, 4.2)))
    assert result["report"]["nodes"][1]["status"] == "partial"
    assert encoded not in js(result)
    w = workflow(start())
    w["extra"] = float("inf")
    with pytest.raises(Exception, match="Non-finite"):
        convert(w)
