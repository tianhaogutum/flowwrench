"""Port of ``test/expressions.test.js``."""

from __future__ import annotations

from typing import Any

import pytest

from flowwrench.n8n2ap import translate_expression

from .helpers import js

CONTEXT: dict[str, Any] = {
    "current": {"ref": "step_2.output.body"},
    "sources": {"Input": {"ref": "trigger.output", "webhook": True}, "Fetch": {"ref": "step_2.output.body"}},
    "allowed": {"Input", "Fetch"},
}

POSITIVES: list[tuple[Any, Any]] = [
    ("={{ $json.a.b }}", "{{step_2.output.body.a.b}}"),
    ("={{ $json }}", "{{step_2.output.body}}"),
    ("={{ $('Input').item.json.body.id }}", "{{trigger.output.body.id}}"),
    ('={{ $node["Input"].json.query.region }}', "{{trigger.output.queryParams.region}}"),
    ("={{ $node['Fetch'].json.id }}", "{{step_2.output.body.id}}"),
    ("=ID: {{ $json.id }} / {{ $json.status }}", "ID: {{step_2.output.body.id}} / {{step_2.output.body.status}}"),
    ("ordinary $json.text is literal", "ordinary $json.text is literal"),
    (42, 42), (False, False), (None, None),
]


@pytest.mark.parametrize(("value", "expected"), POSITIVES, ids=[js(v) for v, _ in POSITIVES])
def test_expression_translation(value: Any, expected: Any) -> None:
    assert translate_expression(value, **CONTEXT) == {"value": expected, "issues": []}


NEGATIVES = [
    "={{ $json.a + 1 }}", "={{ $env.TOKEN }}", '={{ $json["a"] }}', "={{ $json.a.toUpperCase() }}",
    "={{ $json.a[0] }}", '={{ $items("Input") }}', '={{ $("Missing").item.json.x }}',
    "={{ $json.__proto__.x }}", "={{ $json.constructor }}", "= $json.x", "={{ $json.x }",
    "{{step_99.output.secret}}", "=x {{ $json.x }} {{ incomplete", '={{ $("Input").item.json.params }}',
    "={{ process.exit() }}", "={{ (() => 42)() }}",
]


@pytest.mark.parametrize("value", NEGATIVES)
def test_flags_expression(value: str) -> None:
    result = translate_expression(value, **CONTEXT)
    assert result["issues"]
    assert result["value"] is None


def test_nested_values_and_sibling_forward_references() -> None:
    assert translate_expression({"body": ["={{ $json.x }}", 3]}, **CONTEXT)["value"] == {"body": ["{{step_2.output.body.x}}", 3]}
    assert translate_expression('={{ $("Fetch").item.json.x }}', **{**CONTEXT, "allowed": {"Input"}})["issues"]
    assert translate_expression("={{ $json.x }}")["issues"]
    assert translate_expression({"={{ $json.x }}": "value"}, **CONTEXT)["issues"]
