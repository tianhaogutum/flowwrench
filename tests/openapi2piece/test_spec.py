"""Port of test/spec.test.js."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from flowwrench.openapi2piece import (
    UNDEFINED,
    GenerationError,
    SpecError,
    build_model,
    identifier,
    normalize_schema,
    parse_spec,
    property_expression,
    render_piece,
    resolve_refs,
)

from .helpers import SNAPSHOTS, base_spec, fixture_json, fixture_text

INF = float("inf")
NAN = float("nan")


def parsed(doc: Any) -> dict[str, Any]:
    return parse_spec(json.dumps(doc))


def model(doc: Any) -> dict[str, Any]:
    return build_model(parsed(doc), name="sample")


def skipped(doc: Any, pattern: str) -> None:
    with pytest.raises(GenerationError) as info:
        model(doc)
    assert re.search(pattern, info.value.report["skipped"][0]["reason"]), info.value.report["skipped"][0]["reason"]


def throws(fn: Any, pattern: str | None = None) -> None:
    with pytest.raises(SpecError) as info:
        fn()
    if pattern is not None:
        assert re.search(pattern, str(info.value)), str(info.value)


def expression(schema: dict[str, Any], display_name: str) -> str:
    return property_expression(schema, display_name=display_name, required=True)


def ids(result: dict[str, Any]) -> list[str]:
    return [a["id"] for a in result["actions"]]


def find(result: dict[str, Any], action_id: str) -> dict[str, Any]:
    return next(a for a in result["actions"] if a["id"] == action_id)


def with_body(schema: Any, openapi: str = "3.1.0") -> dict[str, Any]:
    return base_spec(
        openapi=openapi,
        paths={"/body": {"post": {"requestBody": {"content": {"application/json": {"schema": schema}}}, "responses": {"200": {}}}}},
    )


def send_doc(schema: Any, responses: dict[str, Any] | None = None, openapi: str = "3.0.4") -> dict[str, Any]:
    return base_spec(
        openapi=openapi,
        paths={
            "/send": {
                "post": {
                    "operationId": "send",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}},
                    "responses": responses if responses is not None else {"200": {"description": "OK"}},
                }
            }
        },
    )


def test_local_references_parameters_schemas_escaped_json_pointer_siblings_and_reuse() -> None:
    root = {"defs": {"a/b~c": {"type": "string"}}, "p": {"$ref": "#/defs/a~1b~0c", "description": "label"}, "q": {"$ref": "#/defs/a~1b~0c"}}
    result = resolve_refs(root)
    assert dict(result["p"]) == {"type": "string", "description": "label"}
    assert result["q"]["type"] == "string"
    text = '{"defs":{"__proto__":{"type":"string"}},"p":{"$ref":"#/defs/__proto__"}}'
    hostile = json.loads(text)
    assert resolve_refs(hostile)["p"]["type"] == "string"
    # JS checks `{}.polluted === undefined` (prototype pollution); Python has no prototype,
    # so check the hostile input itself is untouched instead.
    assert hostile == json.loads(text)


def test_reference_boundaries_remote_missing_malformed_pointers_cycles_and_structural_siblings() -> None:
    for root, pattern in [
        ({"p": {"$ref": "https://invalid.test/schema.json"}}, r"Only local"),
        ({"p": {"$ref": "#/missing"}}, r"Unresolved"),
        ({"p": {"$ref": "#/bad~2"}}, r"escape"),
        ({"p": {"$ref": "#/%xx"}}, r"encoding"),
        ({"p": {"$ref": "#/p"}}, r"Circular"),
        ({"defs": {"type": "string"}, "p": {"$ref": "#/defs", "maxLength": 3}}, r"siblings"),
    ]:
        throws(lambda: resolve_refs(root), pattern)
    big: dict[str, Any] = {"value": "x" * (1024 * 1024)}
    for i in range(20):
        big[f"r{i}"] = {"$ref": "#/node"}
    big["node"] = {"value": big["value"]}
    throws(lambda: resolve_refs(big), r"16 MiB")


def test_parsing_rejects_old_versions_duplicate_yaml_keys_malformed_structures_and_aliases() -> None:
    throws(lambda: parsed({"swagger": "2.0"}), r"Only OpenAPI")
    throws(lambda: parsed({**base_spec(), "openapi": "3.2.0"}), r"Only OpenAPI")
    throws(lambda: parse_spec("openapi: 3.0.3\nopenapi: 3.1.0"), r"syntax")
    throws(lambda: parse_spec('openapi: 3.1.0\ninfo: {title: !unknown hello, version: "1"}\npaths: {}'), r"tags")
    throws(lambda: parsed({**base_spec(), "paths": []}), r"paths")
    throws(lambda: parse_spec('openapi: 3.1.0\ninfo: {title: X, version: "1"}\npaths: &p {x: *p}'), r"Circular|alias")


def test_schema_property_mapping_table_and_nullable_3_0_3_1_schemas() -> None:
    cases = [
        ({"type": "string"}, "ShortText"), ({"type": "integer"}, "Number"), ({"type": "number"}, "Number"),
        ({"type": "boolean"}, "Checkbox"), ({"type": "object"}, "Object"), ({"type": "array", "items": {"type": "string"}}, "Array"),
        ({"type": "string", "enum": ["a", "b"]}, "StaticDropdown"),
    ]
    for schema, method in cases:
        source = expression(normalize_schema(schema), "X")
        assert source.startswith(f"Property.{method}("), source
        assert re.search(r'"required":true', source)
    assert normalize_schema({"type": ["string", "null"]})["nullable"] is True
    assert normalize_schema({"type": "integer", "nullable": True})["nullable"] is True
    assert normalize_schema({"type": "number", "minimum": 2, "exclusiveMinimum": True})["exclusiveMinimum"] == 2


def test_unsupported_schema_behavior_is_explicit_including_dangerous_patterns_and_binary() -> None:
    for schema in [
        {"type": "string", "pattern": "(a+)+$"}, {"oneOf": []},
        {"type": "string", "format": "binary"}, {"type": ["integer", "string"]}, {"type": "array"}, {"type": "object", "required": True},
    ]:
        throws(lambda: normalize_schema(schema))


def test_openapi_3_0_request_projection_excludes_readonly_fields_without_mutating_shared_response_schemas() -> None:
    raw = fixture_json("request-projection.json")
    spec = parsed(raw)
    before = json.dumps(spec)
    # JS deep-freezes the spec; here an untouched deep copy proves the same.
    frozen = copy.deepcopy(spec)
    rendered = render_piece(spec, name="widgets")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["createwidget", "getwidget"]
    assert result["report"]["skipped"] == []
    schema = result["actions"][0]["body"]["schema"]
    assert schema["required"] == ["name"]
    assert schema["readOnlyProperties"] == ["id"]
    assert "id" not in schema["properties"]
    assert schema["properties"]["secret"]["minLength"] == 3
    assert schema["properties"]["nested"]["required"] == ["label"]
    assert schema["properties"]["nested"]["readOnlyProperties"] == ["createdAt"]
    assert "createdAt" not in schema["properties"]["nested"]["properties"]
    assert schema["properties"]["nested"]["additionalProperties"] is False
    assert schema["properties"]["items"]["items"]["required"] == ["value"]
    assert schema["properties"]["items"]["items"]["readOnlyProperties"] == ["id"]
    assert re.search(r"Property.Object\(", files["src/lib/actions/createwidget.ts"])
    assert re.search(r'"body".*"required":true', files["src/lib/actions/createwidget.ts"])
    assert json.dumps(spec) == before
    assert spec == frozen
    assert spec["components"]["schemas"]["Widget"]["required"] == ["id", "name"]
    assert spec["paths"]["/widgets/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["id"]["readOnly"] is True
    assert spec["paths"]["/widgets"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]["properties"]["secret"]["writeOnly"] is True
    assert raw["paths"]["/widgets"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/Widget"


def test_request_projection_retains_writeonly_requirements_and_explicit_unsupported_schema_boundaries() -> None:
    def body30(schema: Any) -> dict[str, Any]:
        return with_body(schema, "3.0.4")

    fields = {"type": "object", "required": ["id", "secret"], "properties": {
        "id": {"type": "integer", "readOnly": True}, "secret": {"type": "string", "writeOnly": True, "enum": ["abc"], "minLength": 3},
        "editable": {"type": "boolean", "readOnly": False},
    }}
    result = model(body30({"type": "array", "items": {"type": "object", "additionalProperties": fields}}))
    schema = result["actions"][0]["body"]["schema"]["items"]["additionalProperties"]
    assert schema["required"] == ["secret"]
    assert schema["properties"]["secret"]["enum"] == ["abc"]
    assert schema["properties"]["secret"]["minLength"] == 3
    assert schema["properties"]["editable"]["type"] == "boolean"
    for case, reason in [
        ({"type": "object", "properties": {"id": {"type": "string", "readOnly": True, "writeOnly": True}}}, r"id: readOnly and writeOnly cannot both be true"),
        ({"type": "object", "properties": {"id": {"type": "string", "readOnly": "true"}}}, r"readOnly must be a boolean"),
        ({"type": "object", "properties": {"id": {"type": "string", "readOnly": True, "writeOnly": 1}}}, r"writeOnly must be a boolean"),
        ({"type": "object", "properties": {"id": {"type": "string", "readOnly": True, "pattern": "x"}}}, r"unsupported schema keyword pattern"),
        ({"type": "object", "readOnly": True}, r"readOnly request schemas are unsupported"),
        ({"type": "array", "items": {"type": "object", "readOnly": True}}, r"readOnly request schemas are unsupported"),
        ({"type": "object", "allOf": [fields]}, r"unsupported schema keyword allOf"),
        ({"type": "object", "anyOf": [fields]}, r"unsupported schema keyword anyOf"),
        ({"oneOf": [fields]}, r"readOnly request schemas are unsupported"),
        ({"type": "object", "oneOf": [{"type": "object"}], "properties": fields["properties"]}, r"readOnly request schemas are unsupported"),
        ({"type": "object", "properties": {"id": {"readOnly": True, "oneOf": [{"type": "string"}]}}}, r"readOnly request schemas are unsupported"),
    ]:
        skipped(body30(case), reason)
    skipped({**body30(fields), "openapi": "3.1.0"}, r"readOnly request schemas are unsupported")
    skipped(base_spec(openapi="3.0.4", paths={"/hello": {"get": {"responses": {"200": {}}, "parameters": [
        {"in": "query", "name": "q", "schema": {"type": "string", "readOnly": True}},
    ]}}}), r"readOnly request schemas are unsupported")
    raw = fixture_json("request-projection.json")
    raw["components"]["schemas"]["ServerId"] = {"$ref": "https://example.test/id.json"}
    throws(lambda: parsed(raw), r"Only local JSON Pointer")
    raw["components"]["schemas"]["ServerId"] = {"$ref": "#/components/schemas/ServerId"}
    throws(lambda: parsed(raw), r"Circular")


def test_oneof_bodies_generate_compatible_controls_and_visibly_reject_unrepresentable_alternatives() -> None:
    spec = parse_spec(fixture_text("request-alternatives.json"))
    rendered = render_piece(spec, name="alternatives")
    result, files = rendered.model, rendered.files
    assert len(result["actions"]) == 1
    assert len(result["actions"][0]["body"]["schema"]["oneOf"]) == 3
    assert re.search(r"Property.Object\(", files["src/lib/actions/notify.ts"])
    assert re.search(r"oneOf.*incompatible.*object.*string", result["report"]["skipped"][0]["reason"])
    source = expression(normalize_schema({"oneOf": [{"type": "integer"}, {"type": "number"}]}), "N")
    assert re.search(r"Property.Number\(", source)
    for schema, reason in [
        ({"oneOf": []}, r"nonempty array"),
        ({"oneOf": [{"type": "object"}, {"type": "string", "pattern": "x"}]}, r"oneOf\[1\].*pattern"),
        ({"oneOf": [{"type": "object"}], "nullable": True}, r"oneOf.*nullable"),
        ({"oneOf": [{"type": "object"} for _ in range(17)]}, r"at most 16"),
        ({"oneOf": [{"type": "null"}]}, r"null-only"),
    ]:
        skipped(with_body(schema), reason)
    skipped(base_spec(paths={"/hello": {"get": {"responses": {"200": {}}, "parameters": [
        {"in": "query", "name": "q", "schema": {"oneOf": [{"type": "integer"}, {"type": "string"}]}},
    ]}}}), r"scalar")


def test_oneof_object_wrapper_constraints_generate_an_intersection_and_preserve_unsupported_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-constraints.json"))
    rendered = render_piece(spec, name="constrained")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["notify"]
    assert re.search(r"Property.Object\(", files["src/lib/actions/notify.ts"])
    assert re.search(r"unsupported schema keyword minProperties", result["report"]["skipped"][0]["reason"])
    wrapper = result["actions"][0]["body"]["schema"]["constraints"]
    assert wrapper["type"] == "object"
    assert wrapper["required"] == ["requestId"]
    assert wrapper["properties"]["requestId"]["minLength"] == 1
    assert wrapper["additionalProperties"] is False
    branches = {"oneOf": [{"type": "object", "required": ["a"]}]}
    for siblings, reason in [
        ({"required": ["a"]}, r"oneOf.*explicit type: object"),
        ({"type": "null"}, r"oneOf.*explicit type: object"),
        ({"type": ["object", "string"]}, r"oneOf.*explicit type: object"),
        ({"type": ["object", "null"], "enum": [1]}, r"oneOf.*sibling enum"),
        ({"type": ["object", "null"], "items": {"type": "string"}}, r"oneOf.*sibling items"),
        ({"type": ["object", "null"], "minItems": 1}, r"oneOf.*sibling minItems"),
        ({"type": "object", "nullable": True, "minLength": 1}, r"oneOf.*sibling minLength"),
        ({"type": "object", "required": "a"}, r"invalid required list"),
        ({"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}}, r"wrapper.a.*pattern"),
        ({"type": "object", "additionalProperties": {"type": "string", "pattern": "x"}}, r"additionalProperties.*pattern"),
        ({"type": "object", "minLength": 1}, r"oneOf.*sibling minLength"),
        ({"type": "object", "anyOf": [{"type": "object"}]}, r"unsupported schema keyword anyOf"),
    ]:
        skipped(with_body({**branches, **siblings}), reason)
    # Annotations alone must not introduce an extra validation schema.
    assert "constraints" not in normalize_schema({**branches, "title": "Label"})


def test_oneof_string_wrapper_constraints_generate_a_string_control_and_retain_unsupported_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-strings.json"))
    for openapi in ["3.0.3", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="strings")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["overlap", "sendstring"]
        assert re.search(r"Property.ShortText\(", files["src/lib/actions/sendstring.ts"])
        assert result["actions"][1]["body"]["schema"]["constraints"] == {"type": "string", "nullable": False, "minLength": 3}
        assert re.search(r"oneOf.*sibling minimum", result["report"]["skipped"][0]["reason"])
    one_of = [{"type": "string"}]
    string = normalize_schema({"type": "string", "minLength": 0, "maxLength": 3, "oneOf": one_of, "title": "Label"})
    assert string["constraints"]["maxLength"] == 3
    assert string["constraints"]["minLength"] == 0
    assert normalize_schema({"type": "string", "oneOf": one_of})["constraints"]["type"] == "string"
    for siblings, reason in [
        ({"minLength": 3}, r"oneOf.*sibling minLength"),
        ({"type": "string", "minLength": -1}, r"wrapper.*invalid minLength"),
        ({"type": "string", "maxLength": 1.5}, r"wrapper.*invalid maxLength"),
        ({"type": "null"}, r"explicit type: object \(optionally nullable\)"),
        ({"type": "string", "items": {"type": "string"}}, r"oneOf.*sibling items"),
        ({"type": "string", "required": ["a"]}, r"oneOf.*sibling required"),
        ({"type": "string", "minimum": 1}, r"oneOf.*sibling minimum"),
        ({"type": "string", "pattern": "x"}, r"unsupported schema keyword pattern"),
        ({"type": "string", "format": "binary"}, r"binary"),
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), reason)


def test_oneof_string_enum_wrapper_intersects_independent_enums_and_preserves_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-string-enums.json"))
    original = json.dumps(spec)
    for openapi in ["3.0.4", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="stringenums")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["nested", "overlap", "sendstring", "unicode"]
        schema = find(result, "sendstring")["body"]["schema"]
        assert schema["constraints"] == {"type": "string", "nullable": False, "minLength": 3, "enum": ["ab", "abc", "xyz", "none"]}
        assert [b.get("enum") for b in schema["oneOf"]] == [["ab", "abc"], ["xyz", "other"]]
        assert "enum" not in schema
        assert re.search(r"Property.ShortText\(", files["src/lib/actions/sendstring.ts"])
        assert not re.search(r"StaticDropdown", files["src/lib/actions/sendstring.ts"])
        assert re.search(r"oneOf.*sibling minimum", result["report"]["skipped"][0]["reason"])
    assert json.dumps(spec) == original
    one_of = [{"type": "string"}]
    for value, reason in [
        ([], r"nonempty scalar enums"), ("abc", r"nonempty scalar enums"),
        (["abc", {}], r"scalar enums"), (["abc", []], r"scalar enums"),
        (["abc", 1], r"enum values must match"), (["abc", True], r"enum values must match"),
        (["abc", None], r"enum values must match"),
    ]:
        throws(lambda: normalize_schema({"type": "string", "enum": value, "oneOf": one_of}), reason)
    for siblings in [
        {"enum": ["abc"]}, {"type": "object", "enum": ["abc"]}, {"type": "array", "enum": ["abc"]},
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), r"oneOf.*sibling enum")
    throws(lambda: normalize_schema({"type": "string", "enum": ["abc"], "minimum": 1, "oneOf": one_of}), r"sibling minimum")
    throws(lambda: expression(normalize_schema({"type": "string", "enum": ["abc"],
        "oneOf": [{"type": "string"}, {"type": "number"}],
    }), "S"), r"incompatible.*number, string")


def test_oneof_numeric_wrapper_constraints_generate_numeric_controls_and_retain_unsupported_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-numbers.json"))
    for openapi in ["3.0.4", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="numbers")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["sendinteger", "nested", "sendnumber", "overlap"]
        assert re.search(r"Property.Number\(", files["src/lib/actions/sendnumber.ts"])
        assert re.search(r"Property.Number\(", files["src/lib/actions/sendinteger.ts"])
        assert result["actions"][2]["body"]["schema"]["constraints"] == {"type": "number", "nullable": False, "minimum": 1}
        assert result["actions"][1]["body"]["schema"]["properties"]["composed"]["oneOf"][0]["constraints"]["minimum"] == 1
        assert re.search(r"oneOf.*sibling minLength", result["report"]["skipped"][0]["reason"])
    one_of = [{"type": "number"}]
    for kind in ["number", "integer"]:
        assert normalize_schema({"type": kind, "oneOf": one_of})["constraints"]["type"] == kind
        bounds = normalize_schema({"type": kind, "minimum": 0, "maximum": 3, "exclusiveMinimum": True, "exclusiveMaximum": False, "oneOf": one_of})
        assert bounds["constraints"]["exclusiveMinimum"] == 0
        assert bounds["constraints"]["maximum"] == 3
        assert "exclusiveMaximum" not in bounds["constraints"]
        assert normalize_schema({"type": kind, "exclusiveMinimum": 0.5, "exclusiveMaximum": 2.5, "oneOf": one_of})["constraints"]["exclusiveMaximum"] == 2.5
    for siblings, reason in [
        ({"minimum": 1}, r"oneOf.*sibling minimum"),
        ({"type": "number", "minimum": "1"}, r"wrapper.*invalid minimum"),
        ({"type": "number", "maximum": INF}, r"wrapper.*invalid maximum"),
        ({"type": "integer", "exclusiveMinimum": True}, r"wrapper.*exclusive bound requires minimum"),
        ({"type": "number", "exclusiveMaximum": True}, r"wrapper.*exclusive bound requires minimum/maximum"),
        ({"type": "number", "exclusiveMinimum": None}, r"wrapper.*invalid exclusiveMinimum"),
        ({"type": "null"}, r"oneOf.*explicit type"),
        ({"type": "number", "minLength": 1}, r"oneOf.*sibling minLength"),
        ({"type": "number", "properties": {}}, r"oneOf.*sibling properties"),
        ({"type": "number", "multipleOf": 2}, r"unsupported schema keyword multipleOf"),
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), reason)
    throws(lambda: expression(normalize_schema({"type": "number", "oneOf": [
        {"type": "number"}, {"type": "string"},
    ]}), "N"), r"incompatible.*number, string")


def test_oneof_numeric_enum_wrapper_intersects_independent_schemas_and_preserves_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-numeric-enums.json"))
    original = json.dumps(spec)
    for openapi in ["3.0.4", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="numericenums")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["fractional", "sendinteger", "nested", "sendnumber", "overlap"]
        schema = find(result, "sendnumber")["body"]["schema"]
        assert schema["constraints"] == {"type": "number", "nullable": False, "enum": [1, 2, 4]}
        assert [b.get("enum") for b in schema["oneOf"]] == [[0, 1], [2, 3]]
        assert "enum" not in schema
        assert re.search(r"Property.Number\(", files["src/lib/actions/sendnumber.ts"])
        assert not re.search(r"StaticDropdown", files["src/lib/actions/sendnumber.ts"])
        assert re.search(r"oneOf.*sibling minLength", result["report"]["skipped"][0]["reason"])
    assert json.dumps(spec) == original
    one_of = [{"type": "number", "enum": [0, 1, 1.5, 2]}]
    for kind in ["number", "integer"]:
        schema = normalize_schema({"type": kind, "enum": [1, 2], "minimum": 0, "maximum": 3, "oneOf": one_of})
        assert schema["constraints"]["enum"] == [1, 2]
        assert schema["oneOf"][0]["enum"] == [0, 1, 1.5, 2]
    assert normalize_schema({"type": "number", "enum": [0.5, 1.5], "oneOf": one_of})["constraints"]["enum"] == [0.5, 1.5]
    for siblings, reason in [
        ({"type": "number", "enum": []}, r"wrapper.*nonempty scalar enums"),
        ({"type": "number", "enum": [1, {}]}, r"wrapper.*scalar enums"),
        ({"type": "number", "enum": ["1"]}, r"wrapper.*enum values must match"),
        ({"type": "number", "enum": [True]}, r"wrapper.*enum values must match"),
        ({"type": "number", "enum": [None]}, r"wrapper.*enum values must match"),
        ({"type": "integer", "enum": [1, 1.5]}, r"wrapper.*enum values must match"),
        ({"enum": [1]}, r"oneOf.*sibling enum"),
        ({"type": "object", "enum": [True]}, r"oneOf.*sibling enum"),
        ({"type": "number", "enum": [1], "minLength": 1}, r"oneOf.*sibling minLength"),
        ({"type": "number", "enum": [1], "items": {"type": "integer"}}, r"oneOf.*sibling items"),
        ({"type": "number", "enum": [1], "required": ["x"]}, r"oneOf.*sibling required"),
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), reason)
    throws(lambda: expression(normalize_schema({"type": "number", "enum": [1],
        "oneOf": [{"type": "number"}, {"type": "string"}],
    }), "N"), r"incompatible.*number, string")


def test_oneof_array_wrapper_constraints_generate_an_array_control_and_retain_unsupported_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-arrays.json"))
    for openapi in ["3.0.4", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="arrays")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["sendarray", "independent", "nested", "overlap"]
        assert re.search(r"Property.Array\(", files["src/lib/actions/sendarray.ts"])
        assert re.search(r'"properties":\{\}', files["src/lib/actions/sendarray.ts"])
        assert result["actions"][0]["body"]["schema"]["constraints"] == {
            "type": "array", "nullable": False, "minItems": 2, "items": {"type": "integer", "nullable": False, "minimum": 0},
        }
        assert result["actions"][2]["body"]["schema"]["properties"]["composed"]["oneOf"][0]["constraints"]["minItems"] == 2
        assert re.search(r"oneOf.*sibling minLength", result["report"]["skipped"][0]["reason"])
    one_of = [{"type": "array", "items": {"type": "integer"}}]
    assert normalize_schema({"type": "array", "items": {"type": "integer"}, "minItems": 0, "maxItems": 0, "oneOf": one_of})["constraints"]["maxItems"] == 0
    for siblings, reason in [
        ({"items": {"type": "integer"}}, r"oneOf.*sibling items"),
        ({"minItems": 1}, r"oneOf.*sibling minItems"),
        ({"type": "array"}, r"wrapper.items must be an object"),
        ({"type": "array", "items": False}, r"wrapper.items must be an object"),
        ({"type": "array", "items": {"type": "integer", "multipleOf": 2}}, r"wrapper.items.*unsupported schema keyword multipleOf"),
        ({"type": "array", "items": {"type": "integer"}, "minItems": -1}, r"wrapper.*invalid minItems"),
        ({"type": "array", "items": {"type": "integer"}, "maxItems": 1.5}, r"wrapper.*invalid maxItems"),
        ({"type": "array", "nullable": True}, r"wrapper.items must be an object"),
        ({"type": "array", "enum": [1]}, r"oneOf.*sibling enum"),
        ({"type": ["array", "null"]}, r"wrapper.items must be an object"),
        ({"type": "null"}, r"oneOf.*explicit type"),
        ({"type": "array", "minimum": 1}, r"oneOf.*sibling minimum"),
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), reason)
    throws(lambda: expression(normalize_schema({"type": "array", "items": {"type": "integer"}, "oneOf": [
        {"type": "array", "items": {"type": "integer"}}, {"type": "string"},
    ]}), "A"), r"incompatible.*array, string")


def test_oneof_uniqueitems_arrays_retain_independent_constraints_controls_and_unsupported_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-unique-arrays.json"))
    for openapi in ["3.0.4", "3.1.0"]:
        before = json.dumps(spec)
        rendered = render_piece({**spec, "openapi": openapi}, name="unique")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["allowduplicates", "branchunique", "jsonvalues", "nested", "overlap", "sendunique"]
        schema = find(result, "sendunique")["body"]["schema"]
        assert schema["constraints"]["uniqueItems"] is True
        assert "uniqueItems" not in schema["oneOf"][0]
        assert [b["items"].get("enum") for b in schema["oneOf"]] == [[0, 1], [2, 3]]
        assert result["actions"][0]["body"]["schema"]["constraints"]["uniqueItems"] is False
        assert re.search(r"Property.Array\(", files["src/lib/actions/sendunique.ts"])
        assert re.search(r'"properties":\{\}', files["src/lib/actions/sendunique.ts"])
        assert re.search(r"oneOf.*sibling minLength", result["report"]["skipped"][0]["reason"])
        assert json.dumps(spec) == before
    items = {"type": "integer"}
    one_of = [{"type": "array", "items": items}]
    for unique_items in [True, False]:
        assert normalize_schema({"type": "array", "items": items, "uniqueItems": unique_items})["uniqueItems"] is unique_items
        assert normalize_schema({"type": "array", "items": items, "uniqueItems": unique_items, "oneOf": one_of})["constraints"]["uniqueItems"] is unique_items
    for unique_items in [None, 0, 1, "true", [], {}]:
        throws(lambda: normalize_schema({"type": "array", "items": items, "uniqueItems": unique_items, "oneOf": one_of}), r"uniqueItems must be a boolean")
    for siblings, reason in [
        ({"uniqueItems": True}, r"oneOf.*sibling uniqueItems"),
        ({"type": "string", "uniqueItems": True}, r"oneOf.*sibling uniqueItems"),
        ({"type": "array", "items": items, "uniqueItems": True, "enum": [1]}, r"oneOf.*sibling enum"),
        ({"type": "array", "items": items, "uniqueItems": True, "contains": items}, r"unsupported schema keyword contains"),
        ({"type": "array", "uniqueItems": True}, r"wrapper.items must be an object"),
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), reason)
    throws(lambda: normalize_schema({"type": "number", "uniqueItems": True}), r"uniqueItems requires type: array")


def test_names_are_safe_and_collisions_are_stable_regardless_of_input_path_order() -> None:
    assert identifier("../../hello; process.exit()") == "hello_process_exit"
    assert identifier("123") == "op_123"
    assert identifier("你好") == "action"

    def op(operation_id: str) -> dict[str, Any]:
        return {"get": {"operationId": operation_id, "responses": {"200": {"description": "OK"}}}}

    a = base_spec(paths={"/c": op("foo_bar_2"), "/b": op("foo bar"), "/a": op("foo-bar")})
    b = base_spec(paths=dict(reversed(list(a["paths"].items()))))
    assert model(a)["report"] == model(b)["report"]
    assert ids(model(a)) == ["foo_bar", "foo_bar_2", "foo_bar_2_2"]
    assert len(model(a)["report"]["warnings"]) == 3


def test_operation_level_parameter_overrides_server_variables_filters_and_max_actions() -> None:
    doc = parsed(base_spec(servers=[{"url": "https://{host}/v1", "variables": {"host": {"default": "demo.example.test"}}}],
        paths={"/hello": {"parameters": [{"name": "q", "in": "query", "required": False, "schema": {"type": "string"}}],
            "get": {"operationId": "hello", "tags": ["one"], "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}}}))
    result = build_model(doc, name="sample", include_tags=["one"], include_ops=["GET /hello"], max_actions=1)
    assert result["actions"][0]["props"][0]["schema"]["type"] == "integer"
    assert result["actions"][0]["props"][0]["required"] is True
    assert result["actions"][0]["baseUrl"] == "https://demo.example.test/v1"
    with pytest.raises(GenerationError, match="No operations"):
        build_model(doc, name="sample", include_tags=["none"])
    throws(lambda: build_model(doc, name="../sample"), r"slug")
    throws(lambda: build_model(doc, name="sample", max_actions=0), r"positive")
    fixture = parse_spec(fixture_text("petstore.yaml"))
    report = build_model(fixture, name="petstore", max_actions=1)["report"]
    assert len([s for s in report["skipped"] if "--max-actions" in s["reason"]]) == 3


def test_path_relative_servers_require_an_explicit_base_url_and_reject_unsafe_references() -> None:
    for url in ["/v2", "v2", "./v2", "v2/{version}"]:
        doc = base_spec(servers=[{"url": url, "variables": {"version": {"default": "beta"}}}])
        rendered = render_piece(parsed(doc), name="relative")
        assert rendered.model["actions"][0]["baseUrl"] is None, url
        source = rendered.files["src/lib/actions/hello.ts"]
        assert re.search(r'base_url: Property.ShortText\(.*"required":true', source)
        assert not re.search(r'"defaultValue"', source)
    for url in ["//evil.test/v2", r"\\evil.test/v2", "v2?key=x", "v2#x", "../v2",
                "/v2/../private", "v2/%2e%2e/private", "v2%3fkey=x", "v2%5cprivate", "v2%xx", "file:///tmp/spec"]:
        skipped(base_spec(servers=[{"url": url}]), r"Server URL")


def test_unsupported_operations_include_actionable_report_reasons() -> None:
    def with_op(op: dict[str, Any]) -> dict[str, Any]:
        return base_spec(paths={"/hello": {"get": {"operationId": "hello", "responses": {"200": {"description": "OK"}}, **op}}})

    skipped(with_op({"parameters": [{"name": "s", "in": "cookie", "schema": {"type": "string"}}]}), r"Cookie")
    skipped(with_op({"parameters": [{"name": "s", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}}]}), r"scalar")
    skipped(with_op({"parameters": [{"name": "Host", "in": "header", "schema": {"type": "string"}}]}), r"Reserved")
    skipped(with_op({"parameters": [{"name": "q", "in": "query", "style": "deepObject", "schema": {"type": "string"}}]}), r"serialization")
    skipped(with_op({"security": [{"one": []}, {"two": []}]}), r"Undefined")
    skipped(with_op({"tags": {}}), r"metadata")
    skipped(base_spec(paths={"/{id}": {"get": {"responses": {"200": {}}}}}), r"template")
    skipped(base_spec(paths={"/%2e%2e/x": {"get": {"responses": {"200": {}}}}}), r"traversal")
    skipped(base_spec(paths={"/hello": {"options": {"responses": {"200": {}}}}}), r"HttpMethod")
    skipped(base_spec(servers=[{"url": "https://user:password@example.test"}]), r"credentials")


def test_auth_mapping_covers_none_secrettext_basicauth_oauth2_and_combined_customauth() -> None:
    configs = [
        ("none", {}, [], "None"),
        ("apiKey", {"key": {"type": "apiKey", "in": "query", "name": "api_key"}}, [{"key": []}], "SecretText"),
        ("bearer", {"token": {"type": "http", "scheme": "bearer"}}, [{"token": []}], "SecretText"),
        ("basic", {"basic": {"type": "http", "scheme": "basic"}}, [{"basic": []}], "BasicAuth"),
        ("oauth2", {"oauth": {"type": "oauth2", "flows": {"authorizationCode": {"authorizationUrl": "https://example.test/auth", "tokenUrl": "https://example.test/token", "scopes": {"read": "Read"}}}}}, [{"oauth": ["read"]}], "OAuth2"),
        ("custom", {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}, "basic": {"type": "http", "scheme": "basic"}}, [{"key": [], "basic": []}], "CustomAuth"),
    ]
    for kind, security_schemes, security, method in configs:
        spec = parsed(base_spec(components={"securitySchemes": security_schemes}, security=security))
        result = render_piece(spec, name="sample")
        assert result.model["auth"]["kind"] == kind
        assert re.search(rf"PieceAuth\.{method}\(", result.files["src/lib/auth.ts"])
    skipped(base_spec(components={"securitySchemes": {"key": {"type": "apiKey", "in": "cookie", "name": "k"}}}, security=[{"key": []}]), r"header/query")
    skipped(base_spec(security=[{"nonexistent": []}]), r"Undefined")
    skipped(base_spec(components={"securitySchemes": {"a": {"type": "http", "scheme": "bearer"}, "b": {"type": "http", "scheme": "basic"}}}, security=[{"a": [], "b": []}]), r"same credential")


def test_authentication_alternatives_generate_an_explicit_sorted_choice_and_retain_all_loss_reports() -> None:
    spec = parse_spec(fixture_text("auth-alternatives.json"))
    rendered = render_piece(spec, name="authchoices")
    result, files = rendered.model, rendered.files
    assert [a["name"] for a in result["report"]["generated"]] == ["optional", "compound", "items", "keyonly", "public"]
    assert result["auth"]["kind"] == "custom"
    assert find(result, "items")["securityChoice"] is True
    assert [s["name"] for s in find(result, "items")["security"]] == ["Bearer", "HeaderKey", "QueryKey"]
    assert re.search(r"auth_scheme: Property.StaticDropdown\(", files["src/lib/actions/items.ts"])
    assert not re.search(r"auth_scheme:", files["src/lib/actions/keyonly.ts"])
    assert len(find(result, "public")["security"]) == 0
    assert [s["operationId"] for s in result["report"]["skipped"]] == ["colliding", "cookies", "unsupported"]
    assert re.search(r"same credential location", result["report"]["skipped"][0]["reason"])
    assert re.search(r"Cookie", result["report"]["skipped"][1]["reason"])
    assert re.search(r"Unsupported security scheme", result["report"]["skipped"][2]["reason"])
    assert re.search(r"visible ShortText", result["report"]["warnings"][0]["message"])
    # Security object/alternative ordering has no priority semantics in OpenAPI OR.
    reordered = copy.deepcopy(spec)
    reordered["security"].reverse()
    reordered["components"]["securitySchemes"] = dict(reversed(list(spec["components"]["securitySchemes"].items())))
    assert render_piece(reordered, name="authchoices").files == files
    overridden = base_spec(components=spec["components"], security=[])
    overridden["paths"]["/hello"]["get"]["security"] = [{"HeaderKey": []}, {"Bearer": []}]
    for openapi in ["3.0.3", "3.1.0"]:
        choice = model({**overridden, "openapi": openapi})
        assert choice["actions"][0]["securityChoice"] is True
        assert [s["name"] for s in choice["actions"][0]["security"]] == ["Bearer", "HeaderKey"]

    def with_security(security: Any) -> dict[str, Any]:
        return base_spec(components=spec["components"], security=security)

    for security, reason in [
        ([{"HeaderKey": []}, {"Unknown": []}], r"Undefined"),
        ([{"HeaderKey": []}, {"Bearer": ["invalid"]}], r"Only OAuth2"),
        ([{"HeaderKey": []}, {"Bearer": "invalid"}], r"string array"),
        ([{"HeaderKey": []}, {"HeaderKey": []}], r"distinct"),
        ([None, {"Bearer": []}], r"must be an object"),
        ([{"HeaderKey": []} for _ in range(17)], r"at most 16"),
    ]:
        skipped(with_security(security), reason)
    conflict = with_security([{"HeaderKey": []}, {"Bearer": []}])
    conflict["paths"]["/hello"]["get"]["parameters"] = [{"in": "header", "name": "x-api-key", "schema": {"type": "string"}}]
    skipped(conflict, r"conflicts")


def test_anonymous_or_alternatives_use_explicit_canonical_requirements_and_optional_connection_metadata() -> None:
    fixture = parse_spec(fixture_text("auth-alternatives.json"))
    spec = base_spec(components=fixture["components"], security=[{}, {"HeaderKey": [], "QueryKey": []}])
    for openapi in ["3.0.3", "3.1.0"]:
        rendered = render_piece(parsed({**spec, "openapi": openapi}), name="anonymous")
        result, files = rendered.model, rendered.files
        assert result["actions"][0]["securityRequirements"] == [
            {"value": '["HeaderKey","QueryKey"]', "names": ["HeaderKey", "QueryKey"]},
            {"value": "[]", "names": []},
        ]
        source = files["src/lib/actions/hello.ts"]
        assert re.search(r"auth_requirement: Property.StaticDropdown\(", source)
        assert re.search(r'"label":"Anonymous","value":"\[\]"', source)
        assert re.search(r"requireAuth: false", source)
        assert not re.search(r"auth_scheme:", source)
        reordered = {**spec, "openapi": openapi, "security": [{"QueryKey": [], "HeaderKey": []}, {}]}
        assert render_piece(parsed(reordered), name="anonymous").files == files
    # An anonymous alternative also forces requirement selection beside a singleton.
    singleton = render_piece(parsed({**spec, "security": [{}, {"Bearer": []}]}), name="single")
    assert singleton.model["auth"]["kind"] == "bearer"
    assert [g["value"] for g in singleton.model["actions"][0]["securityRequirements"]] == ['["Bearer"]', "[]"]
    assert re.search(r"requireAuth: false", singleton.files["src/lib/actions/hello.ts"])
    # Empty and single anonymous security retain unconditional anonymous behavior.
    for security in [[], [{}]]:
        result = render_piece(parsed({**spec, "security": security}), name="public")
        assert result.model["auth"]["kind"] == "none"
        assert "securityRequirements" not in result.model["actions"][0]
        assert not re.search(r"auth_requirement:", result.files["src/lib/actions/hello.ts"])
    # An anonymous alternative never rescues an unsupported or colliding group.
    for security, reason in [
        ([{}, {}], r"distinct"),
        ([{}, {"Unknown": []}], r"Undefined"),
        ([{}, {"Digest": []}], r"Unsupported"),
        ([{}, {"HeaderKey": [], "HeaderAlias": []}], r"same credential location"),
        ([{}, {"QueryKey": ["invalid"]}], r"Only OAuth2"),
        ([{}, None], r"must be an object"),
    ]:
        skipped({**spec, "security": security}, reason)


def test_and_groups_within_or_use_explicit_canonical_requirements_and_per_group_collision_checks() -> None:
    fixture = parse_spec(fixture_text("auth-alternatives.json"))
    spec = base_spec(components=fixture["components"], security=[{"HeaderKey": [], "QueryKey": []}, {"Bearer": []}])
    rendered = render_piece(parsed(spec), name="groups")
    result, files = rendered.model, rendered.files
    choice = result["actions"][0]
    assert choice["securityChoice"] is True
    assert choice["securityRequirements"] == [
        {"value": '["Bearer"]', "names": ["Bearer"]},
        {"value": '["HeaderKey","QueryKey"]', "names": ["HeaderKey", "QueryKey"]},
    ]
    assert re.search(r"auth_requirement: Property.StaticDropdown\(", files["src/lib/actions/hello.ts"])
    assert not re.search(r"auth_scheme:", files["src/lib/actions/hello.ts"])
    reordered = copy.deepcopy(spec)
    reordered["security"] = [{"Bearer": []}, {"QueryKey": [], "HeaderKey": []}]
    assert render_piece(parsed(reordered), name="groups").files == files
    for openapi in ["3.0.3", "3.1.0"]:
        shared = model({**spec, "openapi": openapi, "security": [{"HeaderKey": [], "QueryKey": []}, {"HeaderKey": [], "Bearer": []}]})
        assert len(shared["actions"][0]["securityRequirements"]) == 2
        assert [s["name"] for s in shared["actions"][0]["security"]] == ["Bearer", "HeaderKey", "QueryKey"]
    schemes = {**fixture["components"]["securitySchemes"],
        "HeaderAlias": {"type": "apiKey", "in": "header", "name": "x-api-key"},
        "QueryAlias": {"type": "apiKey", "in": "query", "name": "api_key"},
        "Authorization": {"type": "apiKey", "in": "header", "name": "authorization"},
        "Basic": {"type": "http", "scheme": "basic"},
    }
    for group in [{"HeaderKey": [], "HeaderAlias": []}, {"QueryKey": [], "QueryAlias": []},
                  {"Bearer": [], "Basic": []}, {"Bearer": [], "Authorization": []}]:
        skipped({**spec, "components": {"securitySchemes": schemes}, "security": [group, {"HeaderKey": []}]}, r"same credential location")
    for security, reason in [
        ([{"HeaderKey": [], "QueryKey": []}, {"QueryKey": [], "HeaderKey": []}], r"distinct"),
        ([{"HeaderKey": [], "QueryKey": []}, {"Unknown": []}], r"Undefined"),
        ([{"HeaderKey": [], "QueryKey": []}, {"Digest": []}], r"Unsupported"),
        ([{"HeaderKey": [], "QueryKey": ["invalid"]}, {"Bearer": []}], r"Only OAuth2"),
    ]:
        skipped({**spec, "security": security}, reason)
    oauth = {"type": "oauth2", "flows": {"authorizationCode": {
        "authorizationUrl": "https://auth.example.test/authorize", "tokenUrl": "https://auth.example.test/token",
        "scopes": {"read": "Read", "write": "Write", "unused": "Unused"},
    }}}
    with_oauth = {**spec, "components": {"securitySchemes": {**schemes, "OAuth": oauth}},
        "security": [{"HeaderKey": [], "OAuth": ["write"]}, {"QueryKey": [], "OAuth": ["read"]}]}
    scopes = model(with_oauth)
    assert next(s for s in scopes["actions"][0]["security"] if s["name"] == "OAuth")["scopes"] == ["read", "write"]
    assert next(s for s in scopes["auth"]["schemes"] if s["name"] == "OAuth")["scopes"] == ["read", "write"]
    skipped({**with_oauth, "security": [{"HeaderKey": [], "OAuth": ["read"]}, {"HeaderKey": [], "OAuth": ["write"]}]}, r"scope variants")
    skipped({**with_oauth, "security": [{"HeaderKey": [], "OAuth": ["unknown"]}, {"Bearer": []}]}, r"undefined scope")
    case_sensitive = model({**spec, "components": {"securitySchemes": {**schemes,
        "QueryUpper": {"type": "apiKey", "in": "query", "name": "API_KEY"}}},
        "security": [{"QueryKey": [], "QueryUpper": []}, {"Bearer": []}]})
    assert len(case_sensitive["actions"][0]["securityRequirements"]) == 2
    unusual = model({**spec, "components": {"securitySchemes": {
        "A": schemes["HeaderKey"], "B": schemes["QueryKey"], '["A","B"]': schemes["Bearer"],
    }}, "security": [{"A": [], "B": []}, {'["A","B"]': []}]})
    assert len({g["value"] for g in unusual["actions"][0]["securityRequirements"]}) == 2
    conflict = copy.deepcopy(spec)
    conflict["paths"]["/hello"]["get"]["parameters"] = [{"in": "query", "name": "api_key", "schema": {"type": "string"}}]
    skipped(conflict, r"conflicts with an ordinary parameter")


def test_full_generated_file_snapshot_and_byte_for_byte_deterministic_output() -> None:
    spec = parse_spec(fixture_text("petstore.yaml"))
    first = render_piece(spec, name="petstore")
    second = render_piece(spec, name="petstore")
    assert first.files == second.files
    snapshot = json.loads((SNAPSHOTS / "petstore.json").read_text(encoding="utf-8"))
    assert list(first.files) == list(snapshot)
    assert first.files == snapshot
    assert len(first.model["report"]["generated"]) == 3
    assert len(first.model["report"]["skipped"]) == 2
    assert len(first.model["report"]["warnings"]) == 1


def test_oneof_boolean_enum_wrapper_intersects_independent_enums_and_preserves_diagnostics() -> None:
    spec = parse_spec(fixture_text("oneof-boolean-enums.json"))
    original = json.dumps(spec)
    for openapi in ["3.0.4", "3.1.0"]:
        rendered = render_piece({**spec, "openapi": openapi}, name="booleanenums")
        result, files = rendered.model, rendered.files
        assert ids(result) == ["nested", "overlap", "sendfalse", "sendtrue", "typeonly", "zero"]
        schema = find(result, "sendtrue")["body"]["schema"]
        assert schema["constraints"] == {"type": "boolean", "nullable": False, "enum": [True]}
        assert [b.get("enum") for b in schema["oneOf"]] == [[True], [False]]
        assert "enum" not in schema
        for action_id in ["sendtrue", "sendfalse", "typeonly"]:
            assert re.search(r"Property.Checkbox\(", files[f"src/lib/actions/{action_id}.ts"])
            assert not re.search(r"StaticDropdown", files[f"src/lib/actions/{action_id}.ts"])
        assert re.search(r"oneOf.*sibling minLength", result["report"]["skipped"][0]["reason"])
    assert json.dumps(spec) == original
    one_of = [{"type": "boolean"}]
    for value, reason in [
        ([], r"nonempty scalar enums"), (True, r"nonempty scalar enums"),
        ([True, {}], r"scalar enums"), ([False, []], r"scalar enums"),
        ([True, "true"], r"enum values must match"), ([False, 0], r"enum values must match"),
        ([True, None], r"enum values must match"),
    ]:
        throws(lambda: normalize_schema({"type": "boolean", "enum": value, "oneOf": one_of}), reason)
        throws(lambda: normalize_schema({"type": "boolean", "oneOf": [{"type": "boolean", "enum": value}]}), reason)
    assert normalize_schema({"type": "boolean", "oneOf": one_of})["constraints"] == {"type": "boolean", "nullable": False}
    for siblings in [
        {"enum": [True]}, {"type": "object", "enum": [True]}, {"type": "array", "enum": [True]},
    ]:
        throws(lambda: normalize_schema({**siblings, "oneOf": one_of}), r"oneOf.*sibling enum")
    for siblings in [
        {"minimum": 0}, {"minLength": 1},
        {"items": {"type": "boolean"}}, {"required": ["a"]}, {"uniqueItems": True},
    ]:
        throws(lambda: normalize_schema({"type": "boolean", **siblings, "oneOf": one_of}), r"oneOf.*sibling")
    throws(lambda: expression(normalize_schema({"type": "boolean", "enum": [True],
        "oneOf": [{"type": "boolean"}, {"type": "string"}],
    }), "B"), r"incompatible.*boolean, string")


def test_oneof_nullable_boolean_wrapper_intersects_enums_and_complete_branches_with_compatible_controls() -> None:
    spec = parse_spec(fixture_text("oneof-nullable-booleans.json"))
    original = json.dumps(spec)
    rendered = render_piece(spec, name="nullablebooleans")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["composed", "enumexcludes", "falseallowed", "nested", "overlap", "send30", "send31", "zero"]
    for action_id in ["send30", "send31"]:
        schema = find(result, action_id)["body"]["schema"]
        assert schema["constraints"] == {"type": "boolean", "nullable": True, "enum": [True, None]}
        assert schema["nullable"] is False  # Composition must still count null branch matches.
        assert "enum" not in schema
        assert re.search(r"Property.Checkbox\(", files[f"src/lib/actions/{action_id}.ts"])
        assert not re.search(r"StaticDropdown", files[f"src/lib/actions/{action_id}.ts"])
    assert [b.get("enum") for b in find(result, "send31")["body"]["schema"]["oneOf"]] == [[True], None]
    assert len(result["report"]["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in result["report"]["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in result["report"]["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    # OpenAPI 3.0 spelling is supported in an actual 3.0 document too.
    doc30 = base_spec(openapi="3.0.4", paths={"/send30": spec["paths"]["/send30"]})
    assert render_piece(doc30, name="nullable30").model["actions"][0]["body"]["schema"]["constraints"]["nullable"] is True
    assert json.dumps(spec) == original
    one_of = [{"type": "boolean"}, {"type": "null"}]
    for kind in [["boolean", "null"], ["null", "boolean"]]:
        assert normalize_schema({"type": kind, "oneOf": one_of})["constraints"]["nullable"] is True
        for value, reason in [
            ([], r"nonempty scalar enums"), (True, r"nonempty scalar enums"), ([True, {}], r"scalar enums"),
            ([None, []], r"scalar enums"), ([True, "true"], r"enum values must match"), ([False, 0], r"enum values must match"),
        ]:
            throws(lambda: normalize_schema({"type": kind, "enum": value, "oneOf": one_of}), reason)
            throws(lambda: normalize_schema({"type": kind, "oneOf": [{"type": kind, "enum": value}]}), reason)
    for kind in [[], ["boolean"], ["null"], ["boolean", "boolean"], ["boolean", "null", "null"], ["boolean", "null", "string"], ["boolean", 0], ["boolean", None]]:
        throws(lambda: normalize_schema({"type": kind, "oneOf": one_of}), r"explicit type")
    throws(lambda: normalize_schema({"type": "boolean", "nullable": "true", "oneOf": one_of}), r"nullable must be a boolean")
    for sibling in [{"minimum": 0}, {"minLength": 1}, {"properties": {}}, {"items": {"type": "boolean"}}, {"required": []}, {"uniqueItems": True}]:
        throws(lambda: normalize_schema({"type": ["boolean", "null"], **sibling, "oneOf": one_of}), r"oneOf.*sibling")
    for kind in ["object"]:
        throws(lambda: normalize_schema({"type": kind, "nullable": True, "minLength": 1, "oneOf": one_of}), r"oneOf.*sibling minLength")
        throws(lambda: normalize_schema({"type": [kind, "string"], "oneOf": one_of}), r"explicit type")
    throws(lambda: expression(normalize_schema({"type": ["boolean", "null"], "oneOf": [
        {"type": "boolean"}, {"type": "string"}, {"type": "null"},
    ]}), "B"), r"incompatible.*boolean, string")


def test_oneof_nullable_boolean_wrapper_rejects_duplicate_type_entries_in_complete_branches() -> None:
    for kind in [["boolean", "null", "null"], ["null", "boolean", "null"]]:
        branch = {"type": kind, "enum": [True, None]}
        for schema in [
            branch,
            {"type": ["boolean", "null"], "oneOf": [branch]},
            {"type": "boolean", "nullable": True, "oneOf": [branch]},
            {"type": "object", "properties": {"value": branch}},
            {"type": "array", "items": branch},
            {"type": "object", "additionalProperties": branch},
        ]:
            throws(lambda: normalize_schema(schema), r"single type plus null")


def test_oneof_nullable_string_wrapper_model_regression_retains_independent_enum_length_and_complete_branches() -> None:
    one_of = [{"type": "string", "enum": ["ab", "abc"]}, {"type": "null"}]
    for wrapper in [{"type": "string", "nullable": True}, {"type": ["string", "null"]}, {"type": ["null", "string"]}]:
        raw = {**wrapper, "enum": ["ab", "abc", None], "minLength": 3, "oneOf": one_of}
        before = json.dumps(raw)
        schema = normalize_schema(raw)
        assert schema["constraints"] == {"type": "string", "nullable": True, "minLength": 3, "enum": ["ab", "abc", None]}
        assert [b.get("enum") for b in schema["oneOf"]] == [["ab", "abc"], None]
        assert schema["nullable"] is False
        assert "enum" not in schema
        assert re.search(r"^Property.ShortText\(", expression(schema, "Body"))
        assert json.dumps(raw) == before
        assert render_piece(send_doc(raw), name="nullablestring").model["actions"][0]["body"]["schema"]["constraints"]["nullable"] is True


def test_oneof_nullable_string_wrapper_fixture_and_unsupported_schema_boundaries() -> None:
    spec = parse_spec(fixture_text("oneof-nullable-strings.json"))
    before = json.dumps(spec)
    rendered = render_piece(spec, name="nullablestrings")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["composed", "empty", "enumexcludes", "independent", "nested", "overlap", "send30", "send31", "unicode", "zero"]
    for action_id in ["send30", "send31", "empty", "unicode"]:
        assert re.search(r"Property.ShortText\(", files[f"src/lib/actions/{action_id}.ts"])
        assert not re.search(r"StaticDropdown", files[f"src/lib/actions/{action_id}.ts"])
    assert len(result["report"]["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in result["report"]["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in result["report"]["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    assert json.dumps(spec) == before
    one_of = [{"type": "string"}, {"type": "null"}]
    for kind in [["string", "null"], ["null", "string"]]:
        for value, reason in [
            ([], r"nonempty scalar enums"), ("abc", r"nonempty scalar enums"), ([None, {}], r"scalar enums"),
            ([None, []], r"scalar enums"), (["abc", 0], r"enum values must match"), (["abc", True], r"enum values must match"),
        ]:
            throws(lambda: normalize_schema({"type": kind, "enum": value, "oneOf": one_of}), reason)
            throws(lambda: normalize_schema({"type": kind, "oneOf": [{"type": kind, "enum": value}]}), reason)
        for sibling in [{"minimum": 0}, {"minItems": 1}, {"properties": {}}, {"items": {"type": "string"}}, {"required": []}, {"uniqueItems": True}]:
            throws(lambda: normalize_schema({"type": kind, **sibling, "oneOf": one_of}), r"oneOf.*sibling")
        for sibling in [{"minLength": -1}, {"maxLength": 1.5}, {"minLength": "1"}]:
            throws(lambda: normalize_schema({"type": kind, **sibling, "oneOf": one_of}), r"wrapper.*invalid")
    for kind in [[], ["string"], ["null"], ["string", "string"], ["string", "null", "null"], ["null", "string", "null"], ["string", "null", "boolean"], ["string", 0], ["string", None]]:
        throws(lambda: normalize_schema({"type": kind, "oneOf": one_of}), r"explicit type")
    for kind in [["string", "null", "null"], ["null", "string", "null"]]:
        branch = {"type": kind, "enum": ["abc", None]}
        for schema in [branch, {"type": ["string", "null"], "oneOf": [branch]},
                       {"type": "object", "properties": {"value": branch}}, {"type": "array", "items": branch},
                       {"type": "object", "additionalProperties": branch}]:
            throws(lambda: normalize_schema(schema), r"single type plus null")
    throws(lambda: normalize_schema({"type": "string", "nullable": "true", "oneOf": one_of}), r"nullable must be a boolean")
    for kind in ["object"]:
        throws(lambda: normalize_schema({"type": kind, "nullable": True, "minLength": 1, "oneOf": one_of}), r"oneOf.*sibling minLength")
        throws(lambda: normalize_schema({"type": [kind, "string"], "oneOf": one_of}), r"explicit type")
    throws(lambda: expression(normalize_schema({"type": ["string", "null"], "oneOf": [
        {"type": "string"}, {"type": "boolean"}, {"type": "null"},
    ]}), "Body"), r"incompatible.*boolean, string")
    throws(lambda: expression(normalize_schema({"type": ["string", "null"], "oneOf": [{"type": "null"}]}), "Body"), r"null-only")
    deep: dict[str, Any] = {"type": ["string", "null"], "oneOf": one_of}
    for _ in range(31):
        deep = {"type": "array", "items": deep}
    throws(lambda: normalize_schema(deep), r"schema nesting limit")


def test_oneof_nullable_numeric_wrapper_model_regression_retains_independent_enums_bounds_and_complete_branches() -> None:
    for kind in ["number", "integer"]:
        for wrapper in [{"type": kind, "nullable": True}, {"type": [kind, "null"]}, {"type": ["null", kind]}]:
            raw = {**wrapper, "enum": [0, 1, None], "minimum": 1,
                   "oneOf": [{"type": kind, "enum": [0, 1]}, {"type": "null"}]}
            before = json.dumps(raw)
            schema = normalize_schema(raw)
            assert schema["constraints"] == {"type": kind, "nullable": True, "minimum": 1, "enum": [0, 1, None]}
            assert [b.get("enum") for b in schema["oneOf"]] == [[0, 1], None]
            assert schema["nullable"] is False
            assert "enum" not in schema
            assert re.search(r"^Property.Number\(", expression(schema, "Body"))
            assert json.dumps(raw) == before
            assert render_piece(send_doc(raw), name="nullablenumber").model["actions"][0]["body"]["schema"]["constraints"]["nullable"] is True


def test_numeric_enum_schema_rejects_nonfinite_members_instead_of_serializing_them_to_null() -> None:
    for value in [NAN, INF, -INF]:
        throws(lambda: normalize_schema({"type": "number", "nullable": True, "enum": [1, value]}), r"finite")
        throws(lambda: normalize_schema({"type": "number", "oneOf": [{"type": "number", "enum": [value]}]}), r"finite")


def test_oneof_nullable_numeric_wrapper_fixture_and_unsupported_schema_boundaries() -> None:
    spec = parse_spec(fixture_text("oneof-nullable-numbers.json"))
    before = json.dumps(spec)
    rendered = render_piece(spec, name="nullablenumbers")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["composed", "enumexcludes", "fractional", "independent", "integer30", "integer31", "nested", "overlap", "send30", "send31", "zero", "zeroallowed"]
    for action in [a for a in result["actions"] if a["id"] != "nested"]:
        assert re.search(r"Property.Number\(", files[f"src/lib/actions/{action['id']}.ts"])
        assert not re.search(r"StaticDropdown", files[f"src/lib/actions/{action['id']}.ts"])
    assert len(result["report"]["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in result["report"]["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in result["report"]["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    assert json.dumps(spec) == before
    for scalar in ["number", "integer"]:
        one_of = [{"type": scalar}, {"type": "null"}]
        for kind in [[scalar, "null"], ["null", scalar]]:
            cases: list[tuple[Any, str]] = [
                ([], r"nonempty scalar enums"), (1, r"nonempty scalar enums"), ([None, {}], r"scalar enums"),
                ([None, []], r"scalar enums"), ([1, "1"], r"enum values must match"), ([0, False], r"enum values must match"),
                ([1, NAN], r"finite"), ([INF, None], r"finite"),
            ]
            if scalar == "integer":
                cases.append(([0.5, None], r"enum values must match"))
            for value, reason in cases:
                throws(lambda: normalize_schema({"type": kind, "enum": value, "oneOf": one_of}), reason)
                throws(lambda: normalize_schema({"type": kind, "oneOf": [{"type": kind, "enum": value}]}), reason)
            for sibling in [{"minLength": 0}, {"minItems": 1}, {"properties": {}}, {"items": {"type": "number"}}, {"required": []}, {"uniqueItems": True}]:
                throws(lambda: normalize_schema({"type": kind, **sibling, "oneOf": one_of}), r"oneOf.*sibling")
            for sibling in [{"minimum": "1"}, {"maximum": INF}, {"exclusiveMinimum": None}, {"exclusiveMaximum": True}]:
                throws(lambda: normalize_schema({"type": kind, **sibling, "oneOf": one_of}), r"wrapper.*(invalid|exclusive bound requires)")
            throws(lambda: normalize_schema({"type": kind, "nullable": "true", "oneOf": one_of}), r"nullable must be a boolean")
        for kind in [[], [scalar], ["null"], [scalar, scalar], [scalar, "null", "null"], ["null", scalar, "null"], [scalar, "null", "boolean"], ["number", "integer", "null"], [scalar, 0], [scalar, None]]:
            throws(lambda: normalize_schema({"type": kind, "oneOf": one_of}), r"explicit type")
        throws(lambda: expression(normalize_schema({"type": [scalar, "null"], "oneOf": [
            {"type": scalar}, {"type": "string"}, {"type": "null"},
        ]}), "N"), r"incompatible.*number, string")
        throws(lambda: expression(normalize_schema({"type": [scalar, "null"], "oneOf": [{"type": "null"}]}), "N"), r"null-only")
        deep: dict[str, Any] = {"type": [scalar, "null"], "oneOf": one_of}
        for _ in range(31):
            deep = {"type": "array", "items": deep}
        throws(lambda: normalize_schema(deep), r"schema nesting limit")
    for kind in ["object"]:
        throws(lambda: normalize_schema({"type": kind, "nullable": True, "minLength": 1, "oneOf": [{"type": "null"}]}), r"oneOf.*sibling minLength")
        throws(lambda: normalize_schema({"type": [kind, "string"], "oneOf": [{"type": "null"}]}), r"explicit type")


def test_oneof_nullable_array_wrapper_model_regression_retains_independent_items_length_uniqueness_and_complete_branches() -> None:
    for wrapper in [{"type": "array", "nullable": True}, {"type": ["array", "null"]}, {"type": ["null", "array"]}]:
        raw = {**wrapper, "minItems": 2, "maxItems": 3, "uniqueItems": True,
               "items": {"type": "integer", "minimum": 0},
               "oneOf": [{"type": "array", "items": {"type": "integer", "enum": [0, 1]}}, {"type": "null"}]}
        before = json.dumps(raw)
        schema = normalize_schema(raw)
        assert schema["constraints"] == {"type": "array", "nullable": True, "minItems": 2, "maxItems": 3,
            "uniqueItems": True, "items": {"type": "integer", "nullable": False, "minimum": 0}}
        assert schema["nullable"] is False
        assert "uniqueItems" not in schema["oneOf"][0]
        assert "minItems" not in schema["oneOf"][0]
        assert schema["oneOf"][0]["items"]["enum"] == [0, 1]
        assert "minimum" not in schema["oneOf"][0]["items"]
        assert re.search(r"^Property.Array\(", expression(schema, "Body"))
        assert json.dumps(raw) == before
        assert render_piece(send_doc(raw), name="nullablearray").model["actions"][0]["body"]["schema"]["constraints"]["nullable"] is True


def test_oneof_nullable_array_wrapper_fixture_and_unsupported_schema_boundaries() -> None:
    spec = parse_spec(fixture_text("oneof-nullable-arrays.json"))
    before = json.dumps(spec)
    rendered = render_piece(spec, name="nullablearrays")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["arrayoverlap", "branchunique", "composed", "empty", "independent", "jsonvalues", "nested", "overlap", "send30", "send31", "zero"]
    for action in [a for a in result["actions"] if a["id"] != "nested"]:
        assert re.search(r"Property.Array\(", files[f"src/lib/actions/{action['id']}.ts"])
        assert re.search(r'"properties":\{\}', files[f"src/lib/actions/{action['id']}.ts"])
    assert len(result["report"]["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in result["report"]["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in result["report"]["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    assert json.dumps(spec) == before
    items = {"type": "integer"}
    one_of = [{"type": "array", "items": items}, {"type": "null"}]
    for wrapper in [{"type": "array", "nullable": True}, {"type": ["array", "null"]}, {"type": ["null", "array"]}]:
        for extra, reason in [
            ({"items": UNDEFINED}, r"wrapper.items must be an object"), ({"items": False}, r"wrapper.items must be an object"),
            ({"items": {}}, r"explicit supported schema type"), ({"items": {"type": "integer", "multipleOf": 2}}, r"unsupported schema keyword multipleOf"),
            ({"minItems": -1}, r"invalid minItems"), ({"maxItems": 1.5}, r"invalid maxItems"), ({"maxItems": INF}, r"invalid maxItems"),
            ({"uniqueItems": "true"}, r"uniqueItems must be a boolean"), ({"uniqueItems": None}, r"uniqueItems must be a boolean"),
            ({"nullable": "true"}, r"nullable must be a boolean"), ({"enum": [None]}, r"oneOf.*sibling enum"),
            ({"minimum": 0}, r"oneOf.*sibling minimum"), ({"minLength": 1}, r"oneOf.*sibling minLength"),
            ({"properties": {}}, r"oneOf.*sibling properties"), ({"required": []}, r"oneOf.*sibling required"),
            ({"contains": items}, r"unsupported schema keyword contains"), ({"allOf": []}, r"unsupported schema keyword allOf"),
        ]:
            throws(lambda: normalize_schema({**wrapper, "items": items, "oneOf": one_of, **extra}), reason)
        throws(lambda: expression(normalize_schema({**wrapper, "items": items, "oneOf": [*one_of, {"type": "string"}]}), "A"),
               r"incompatible.*array, string")
        throws(lambda: expression(normalize_schema({**wrapper, "items": items, "oneOf": [{"type": "null"}]}), "A"), r"null-only")
    for kind in [[], ["array"], ["null"], ["array", "array"], ["array", "null", "null"], ["null", "array", "null"], ["array", "null", "string"], ["array", 0], ["array", None]]:
        throws(lambda: normalize_schema({"type": kind, "items": items, "oneOf": one_of}), r"oneOf.*(sibling|explicit type)")
    for kind in [["array", "null", "null"], ["null", "array", "null"]]:
        branch = {"type": kind, "items": items}
        for schema in [branch, {"type": ["array", "null"], "items": items, "oneOf": [branch]},
                       {"type": "object", "properties": {"value": branch}}, {"type": "array", "items": branch},
                       {"type": "object", "additionalProperties": branch}]:
            throws(lambda: normalize_schema(schema), r"single type plus null")
    throws(lambda: normalize_schema({"type": "object", "nullable": True, "minLength": 1, "oneOf": one_of}), r"oneOf.*sibling minLength")
    throws(lambda: normalize_schema({"type": ["object", "string"], "oneOf": one_of}), r"explicit type")
    deep: dict[str, Any] = {"type": ["array", "null"], "items": items, "oneOf": one_of}
    for _ in range(31):
        deep = {"type": "array", "items": deep}
    throws(lambda: normalize_schema(deep), r"schema nesting limit")


def test_oneof_nullable_object_wrapper_model_regression_keeps_wrapper_and_complete_branches_independent() -> None:
    for wrapper in [{"type": "object", "nullable": True}, {"type": ["object", "null"]}, {"type": ["null", "object"]}]:
        raw = {**wrapper, "required": ["requestId"], "properties": {"requestId": {"type": "string", "minLength": 1}},
               "oneOf": [{"type": "object", "required": ["email"], "properties": {"email": {"type": "string", "minLength": 1}}}, {"type": "null"}]}
        before = json.dumps(raw)
        schema = normalize_schema(raw)
        assert schema["constraints"]["type"] == "object"
        assert schema["constraints"]["nullable"] is True
        assert schema["constraints"]["required"] == ["requestId"]
        assert list(schema["constraints"]["properties"]) == ["requestId"]
        assert list(schema["oneOf"][0]["properties"]) == ["email"]
        assert schema["oneOf"][0]["required"] == ["email"]
        assert schema["constraints"]["additionalProperties"] is True
        assert schema["nullable"] is False
        assert re.search(r"^Property.Object\(", expression(schema, "Body"))
        for openapi in ["3.0.4", "3.1.0"]:
            doc = send_doc(raw, responses={"200": {}}, openapi=openapi)
            assert render_piece(doc, name="objects").model["actions"][0]["body"]["schema"]["constraints"]["nullable"] is True
        assert json.dumps(raw) == before


def test_oneof_nullable_object_wrapper_fixture_and_unsupported_schema_boundaries() -> None:
    spec = parse_spec(fixture_text("oneof-nullable-objects.json"))
    before = json.dumps(spec)
    rendered = render_piece(spec, name="nullableobjects")
    result, files = rendered.model, rendered.files
    assert ids(result) == ["closed", "composed", "nested", "overlap", "send30", "send31", "zero"]
    for action in result["actions"]:
        assert re.search(r"Property.Object\(", files[f"src/lib/actions/{action['id']}.ts"])
    assert len(result["report"]["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in result["report"]["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in result["report"]["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    assert json.dumps(spec) == before
    for action_id in ["send30", "send31"]:
        schema = find(result, action_id)["body"]["schema"]
        assert schema["type"] == "oneOf"
        assert schema["nullable"] is False  # Composition still counts the complete null branch.
        assert schema["constraints"]["type"] == "object"
        assert schema["constraints"]["nullable"] is True
        assert schema["constraints"]["required"] == ["requestId"]
        assert list(schema["constraints"]["properties"]) == ["requestId"]
        assert schema["constraints"]["additionalProperties"] is True
        assert schema["oneOf"][0]["required"] == ["email"]
        assert schema["oneOf"][0]["additionalProperties"] is True
        assert schema["oneOf"][1]["type"] == "null"
    # The closed branch stays closed: the wrapper's requestId is not merged into it.
    closed = find(result, "closed")["body"]["schema"]
    assert closed["constraints"]["additionalProperties"] is True
    assert closed["oneOf"][0]["additionalProperties"] is False
    assert "requestId" not in closed["oneOf"][0]["properties"]
    # Invalid combinations keep their own diagnostics regardless of the object wrapper.
    branch, null_branch = {"type": "object", "required": ["email"]}, {"type": "null"}
    for wrapper, reason in [
        ({"type": ["object", "null"], "enum": [{}]}, r"oneOf.*sibling enum"),
        ({"type": ["object", "null", "null"], "required": ["requestId"]}, r"oneOf.*explicit type"),
        ({"type": ["object", "string"], "required": ["requestId"]}, r"oneOf.*explicit type"),
        ({"type": ["object", "null"], "minimum": 1}, r"oneOf.*sibling minimum"),
        ({"type": ["object", "null"], "properties": {"requestId": {"type": "string", "pattern": "x"}}}, r"wrapper.requestId.*pattern"),
        ({"type": ["object", "null"], "additionalProperties": {"type": "string", "pattern": "x"}}, r"additionalProperties.*pattern"),
    ]:
        throws(lambda: normalize_schema({**wrapper, "oneOf": [branch, null_branch]}), reason)
    throws(lambda: normalize_schema({"type": ["object", "null"], "oneOf": [{"type": ["object", "null", "null"]}]}), r"single type plus null")
    throws(lambda: expression(normalize_schema({"type": ["object", "null"], "oneOf": [null_branch]}), "O"), r"null-only")
    throws(lambda: expression(normalize_schema({"type": ["object", "null"], "oneOf": [branch, {"type": "string"}, null_branch]}), "O"),
           r"incompatible.*object, string")
    deep: dict[str, Any] = {"type": ["object", "null"], "required": ["requestId"], "oneOf": [branch, null_branch]}
    for _ in range(31):
        deep = {"type": "object", "properties": {"value": deep}}
    throws(lambda: normalize_schema(deep), r"schema nesting limit")
