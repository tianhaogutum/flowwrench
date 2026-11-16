"""Port of ``test/connections.test.js``."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from flowwrench.n8n2ap import ConversionError, InputError, convert, report_markdown, validate_template

from .helpers import JsError, clone, edge, fixture, index_of, js, mapping, run_code, schema_valid, steps


def find(items: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(item for item in items if predicate(item))


def test_explicit_basic_binding_preserves_provenance_and_real_http_structure_without_credentials() -> None:
    source, connection_map = fixture("http-basic"), mapping()
    original = clone({"source": source, "connectionMap": connection_map})
    result = convert(source, connection_map=connection_map)
    all_steps = steps(result["template"])
    http = find(all_steps, lambda s: s["displayName"] == "Fetch")
    assert http["type"] == "PIECE"
    assert http["settings"]["input"]["authType"] == "BASIC"
    assert http["settings"]["input"]["authFields"] == {
        "username": "{{connection_crm_basic.output.username}}", "password": "{{connection_crm_basic.output.password}}",
    }
    guard = find(all_steps, lambda s: s["name"] == "connection_crm_basic")
    assert index_of(all_steps, guard) < index_of(all_steps, http)
    with pytest.raises(JsError, match="Configure connection placeholder crm_basic"):
        run_code(guard)
    assert result["report"]["complete"] is False
    assert result["template"]["flows"][0]["valid"] is False
    assert result["report"]["connectionPlaceholders"] == [{
        "source": {"node": "Fetch", "type": "httpBasicAuth", "name": "Example CRM"},
        "placeholder": "crm_basic", "step": "connection_crm_basic", "fields": ["username", "password"], "status": "unresolved",
    }]
    assert "SYNTHETIC_SOURCE_CREDENTIAL_ID" not in js(result)
    assert re.search(r"crm_basic.*connection_crm_basic", report_markdown(result["report"]))
    assert schema_valid(result["template"])
    assert convert(source, connection_map=connection_map) == result
    assert {"source": source, "connectionMap": connection_map} == original


def test_bearer_binding_uses_pinned_http_token_auth_and_preserves_downstream_response_references() -> None:
    source, connection_map = fixture("webhook-http-if"), mapping()
    source["nodes"][1]["parameters"]["authentication"] = "genericCredentialType"
    source["nodes"][1]["parameters"]["genericAuthType"] = "httpBearerAuth"
    source["nodes"][1]["credentials"] = {"httpBearerAuth": {"name": "Example CRM", "id": "OMIT_ID"}}
    connection_map["bindings"][0]["source"] = {"node": source["nodes"][1]["name"], "type": "httpBearerAuth", "name": "Example CRM"}
    result = convert(source, connection_map=connection_map)
    all_steps = steps(result["template"])
    http = find(all_steps, lambda s: s["type"] == "PIECE")
    assert http["settings"]["input"]["authType"] == "BEARER_TOKEN"
    assert http["settings"]["input"]["authFields"] == {"token": "{{connection_crm_basic.output.token}}"}
    router = find(all_steps, lambda s: s["type"] == "ROUTER")
    assert router["settings"]["branches"][0]["conditions"][0][0]["firstValue"] == "{{step_2.output.body.status}}"
    assert result["report"]["connectionPlaceholders"][0]["fields"] == ["token"]


def _missing(m: dict[str, Any]) -> Any:
    m["bindings"] = []
    return m


def _duplicate(m: dict[str, Any]) -> Any:
    m["bindings"].append(clone(m["bindings"][0]))
    return m


def _stale(m: dict[str, Any]) -> Any:
    m["bindings"][0]["source"]["node"] = "Old Fetch"
    return m


def _wrong_name(m: dict[str, Any]) -> Any:
    m["bindings"][0]["source"]["name"] = "Other CRM"
    return m


def _wrong_type(m: dict[str, Any]) -> Any:
    m["bindings"][0]["source"]["type"] = "httpBearerAuth"
    return m


def _unsafe_alias(m: dict[str, Any]) -> Any:
    m["bindings"][0]["placeholder"] = "{{env.SECRET}}"
    return m


def _literal(m: dict[str, Any]) -> Any:
    m["bindings"][0]["password"] = "NEVER_ECHO"
    return m


def _version(m: dict[str, Any]) -> Any:
    m["formatVersion"] = 2
    return m


REFUSALS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("missing binding", _missing), ("duplicate binding", _duplicate), ("stale source node", _stale),
    ("wrong credential name", _wrong_name), ("wrong credential type", _wrong_type), ("unsafe alias", _unsafe_alias),
    ("literal credentials", _literal), ("unknown version", _version), ("non-object", lambda m: None),
]


@pytest.mark.parametrize("mutate", [m for _, m in REFUSALS], ids=[label for label, _ in REFUSALS])
def test_connection_map_refuses(mutate: Callable[[dict[str, Any]], Any]) -> None:
    m = mutate(mapping())
    with pytest.raises(InputError):
        convert(fixture("http-basic"), connection_map=m)


def test_alias_collisions_across_distinct_source_bindings_are_ambiguous() -> None:
    source, m = fixture("http-basic"), mapping()
    other = clone(source["nodes"][1])
    other["name"] = "Other"
    other["id"] = "other"
    source["nodes"].append(other)
    other["credentials"]["httpBasicAuth"] = {"name": "Other CRM", "id": "OTHER_SOURCE_ID"}
    m["bindings"].append({**clone(m["bindings"][0]), "source": {**m["bindings"][0]["source"], "node": "Other", "name": "Other CRM"}})
    with pytest.raises(Exception, match=r"[Aa]mbiguous|[Dd]uplicate"):
        convert(source, connection_map=m)


@pytest.mark.parametrize("cred_type", ["httpBasicAuth", "httpBearerAuth"])
def test_shared_identity_emits_one_unresolved_step_and_two_exact_provenance_rows(cred_type: str) -> None:
    source, connection_map = fixture("http-shared"), mapping("http-shared")
    for n in source["nodes"][1:]:
        n["parameters"]["genericAuthType"] = cred_type
        n["credentials"] = {cred_type: n["credentials"]["httpBasicAuth"]}
    for b in connection_map["bindings"]:
        b["source"]["type"] = cred_type
    original = clone({"source": source, "connectionMap": connection_map})
    result = convert(source, connection_map=connection_map)
    all_steps = steps(result["template"])
    guards = [s for s in all_steps if s["name"] == "connection_crm_basic"]
    assert len(guards) == 1
    with pytest.raises(JsError, match="Configure connection placeholder crm_basic"):
        run_code(guards[0])
    http = [s for s in all_steps if s["settings"].get("actionName") == "send_request"]
    assert len(http) == 2
    fields = ["username", "password"] if cred_type == "httpBasicAuth" else ["token"]
    for step in http:
        assert index_of(all_steps, guards[0]) < index_of(all_steps, step)
        assert step["settings"]["input"]["authType"] == ("BASIC" if cred_type == "httpBasicAuth" else "BEARER_TOKEN")
        assert step["settings"]["input"]["authFields"] == {f: f"{{{{connection_crm_basic.output.{f}}}}}" for f in fields}
    assert result["report"]["connectionPlaceholders"] == [
        {**b, "step": "connection_crm_basic", "fields": fields, "status": "unresolved"} for b in connection_map["bindings"]
    ]
    assert result["report"]["coveragePercent"] == 100
    assert result["report"]["complete"] is False
    assert result["template"]["flows"][0]["valid"] is False
    markdown = report_markdown(result["report"])
    for name in ["Fetch", "Fetch again"]:
        assert f"| {name} | {cred_type} | Example CRM | crm_basic | connection_crm_basic |" in markdown
    assert "SYNTHETIC_SHARED_CREDENTIAL_ID" not in js(result)
    assert "SYNTHETIC_SHARED_CREDENTIAL_ID" not in markdown
    assert schema_valid(result["template"])
    assert convert(source, connection_map=connection_map) == result
    assert {"source": source, "connectionMap": connection_map} == original


def test_one_shared_placeholder_precedes_http_actions_on_both_if_branches() -> None:
    source, condition = fixture("http-shared"), fixture("webhook-http-if")["nodes"][2]
    condition["parameters"]["conditions"]["conditions"][0]["leftValue"] = "paid"
    source["nodes"].append(condition)
    source["connections"] = {
        "Webhook": {"main": [[edge("Is paid")]]},
        "Is paid": {"main": [[edge("Fetch")], [edge("Fetch again")]]},
    }
    result = convert(source, connection_map=mapping("http-shared"))
    all_steps = steps(result["template"])
    assert len([s for s in all_steps if s["name"] == "connection_crm_basic"]) == 1
    router = find(all_steps, lambda s: s["type"] == "ROUTER")
    assert [s["settings"]["input"]["authFields"]["username"] for s in router["children"]] == \
        ["{{connection_crm_basic.output.username}}"] * 2
    assert result["report"]["coveragePercent"] == 100
    assert result["report"]["complete"] is False
    assert schema_valid(result["template"])


IDENTITIES = [(None, None), (None, "SYNTHETIC_SHARED_CREDENTIAL_ID"), ("SYNTHETIC_SHARED_CREDENTIAL_ID", None)]


@pytest.mark.parametrize("ids", IDENTITIES, ids=["/".join("known" if i else "absent" for i in ids) for ids in IDENTITIES])
def test_shared_alias_refuses_unproven_identity(ids: tuple[str | None, str | None]) -> None:
    source = fixture("http-shared")
    for i, n in enumerate(source["nodes"][1:]):
        if ids[i] is None:
            del n["credentials"]["httpBasicAuth"]["id"]
    with pytest.raises(Exception, match=r"Ambiguous.*placeholder"):
        convert(source, connection_map=mapping("http-shared"))
    separate = mapping("http-shared")
    separate["bindings"][1]["placeholder"] = "other"
    assert len(convert(source, connection_map=separate)["report"]["connectionPlaceholders"]) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["reverse=false", "reverse=true"])
@pytest.mark.parametrize("separate_alias", [False, True], ids=["separateAlias=false", "separateAlias=true"])
def test_conflicting_same_name_source_ids_fail_privately(separate_alias: bool, reverse: bool) -> None:
    source, connection_map = fixture("http-shared"), mapping("http-shared")
    source["nodes"][2]["credentials"]["httpBasicAuth"]["id"] = "PRIVATE_CONFLICT_ID"
    # The conflict must also be checked on disconnected/unselected nodes.
    del source["connections"]["Fetch"]
    if separate_alias:
        connection_map["bindings"][1]["placeholder"] = "other"
    if reverse:
        source["nodes"].reverse()
        connection_map["bindings"].reverse()
    with pytest.raises(InputError) as caught:
        convert(source, connection_map=connection_map)
    message = str(caught.value)
    assert re.search(r"Conflicting source credential IDs", message)
    assert not re.search(r"SYNTHETIC_SHARED_CREDENTIAL_ID|PRIVATE_CONFLICT_ID", message)


DIFFERENT = [("httpBasicAuth", "Other CRM"), ("httpBearerAuth", "Example CRM"), ("httpBearerAuth", "Other CRM")]


@pytest.mark.parametrize(("cred_type", "name"), DIFFERENT, ids=[f"{t}, {n}" for t, n in DIFFERENT])
def test_shared_alias_refuses_different_credential_names_types(cred_type: str, name: str) -> None:
    source, connection_map = fixture("http-shared"), mapping("http-shared")
    source["nodes"][2]["parameters"]["genericAuthType"] = cred_type
    source["nodes"][2]["credentials"] = {cred_type: {"id": "SYNTHETIC_SHARED_CREDENTIAL_ID", "name": name}}
    connection_map["bindings"][1]["source"] = {"node": "Fetch again", "type": cred_type, "name": name}
    with pytest.raises(Exception, match=r"Ambiguous.*placeholder"):
        convert(source, connection_map=connection_map)


def test_sharing_is_explicit_and_still_requires_one_exact_binding_per_node() -> None:
    source, connection_map = fixture("http-shared"), mapping("http-shared")
    connection_map["bindings"].pop()
    with pytest.raises(Exception, match="Missing connection binding"):
        convert(source, connection_map=connection_map)
    connection_map["bindings"].append(clone(connection_map["bindings"][0]))
    with pytest.raises(Exception, match="duplicate source"):
        convert(source, connection_map=connection_map)
    separate = mapping("http-shared")
    separate["bindings"][1]["placeholder"] = "other"
    names = [s["name"] for s in steps(convert(source, connection_map=separate)["template"])]
    assert len([n for n in names if n.startswith("connection_")]) == 2


def _set_param(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(w: dict[str, Any]) -> None:
        w["nodes"][1]["parameters"][key] = value
    return mutate


def _set_credential(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(w: dict[str, Any]) -> None:
        w["nodes"][1]["credentials"]["httpBasicAuth"][key] = value
    return mutate


def _replace_credential(w: dict[str, Any]) -> None:
    w["nodes"][1]["credentials"]["httpBasicAuth"] = "not-a-reference"


def _future_version(w: dict[str, Any]) -> None:
    w["nodes"][1]["typeVersion"] = 99


def test_a_mapping_cannot_bypass_unsupported_auth_credential_data_versions_or_secret_detection() -> None:
    for mutate in [_set_param("genericAuthType", "httpDigestAuth"), _set_param("authentication", "none"),
                   _set_credential("password", "NEVER_ECHO"), _replace_credential, _set_credential("id", ""),
                   _set_credential("id", "   "), _set_credential("id", 42)]:
        source = fixture("http-basic")
        mutate(source)
        with pytest.raises(InputError):
            convert(source, connection_map=mapping())
    for mutate in [_future_version, _set_param("url", "https://example.test/?token=NEVER_ECHO")]:
        source = fixture("http-basic")
        mutate(source)
        result = convert(source, connection_map=mapping())
        assert find(steps(result["template"]), lambda s: s["displayName"] == "Fetch")["type"] == "CODE"
        assert "NEVER_ECHO" not in js(result)


def test_http_auth_input_validation_rejects_empty_mixed_or_literal_auth_fields_without_secrets() -> None:
    template = convert(fixture("webhook-http-if"))["template"]
    for input_ in [{"authType": "BASIC", "authFields": {}},
                   {"authType": "BEARER_TOKEN", "authFields": {"token": "LITERAL_SECRET"}},
                   {"authType": "NONE", "authFields": {"password": "LITERAL_SECRET"}},
                   {"authType": "MADE_UP", "authFields": {}}]:
        broken = clone(template)
        find(steps(broken), lambda s: s["type"] == "PIECE")["settings"]["input"].update(input_)
        with pytest.raises(ConversionError, match="HTTP auth"):
            validate_template(broken)


def _absent_placeholder(t: dict[str, Any]) -> None:
    fetch = find(steps(t), lambda s: s["displayName"] == "Fetch")
    fetch["settings"]["input"]["authFields"]["username"] = "{{connection_absent.output.username}}"


def _renamed_guard(t: dict[str, Any]) -> None:
    find(steps(t), lambda s: s["name"] == "connection_crm_basic")["name"] = "renamed"


def _wrong_field(t: dict[str, Any]) -> None:
    fetch = find(steps(t), lambda s: s["displayName"] == "Fetch")
    fetch["settings"]["input"]["authFields"]["username"] = "{{connection_crm_basic.output.password}}"


def _later_placeholder(t: dict[str, Any]) -> None:
    g = find(steps(t), lambda s: s["name"] == "connection_crm_basic")
    trigger = t["flows"][0]["trigger"]
    trigger["nextAction"] = g["nextAction"]
    del g["nextAction"]
    http = find(steps(t), lambda s: s["displayName"] == "Fetch")
    http["nextAction"] = g  # A later placeholder cannot supply the HTTP auth.


def test_auth_references_must_resolve_to_one_earlier_placeholder_on_the_execution_path() -> None:
    result = convert(fixture("http-basic"), connection_map=mapping())
    for mutate in [_absent_placeholder, _renamed_guard, _wrong_field, _later_placeholder]:
        invalid = clone(result["template"])
        mutate(invalid)
        with pytest.raises(ConversionError, match="HTTP auth"):
            validate_template(invalid)
    mixed = clone(result["template"])
    guard = find(steps(mixed), lambda s: s["name"] == "connection_crm_basic")
    guard["nextAction"] = {**clone(guard), "name": "connection_other"}
    find(steps(mixed), lambda s: s["name"] == "connection_other")["nextAction"] = \
        result["template"]["flows"][0]["trigger"]["nextAction"]["nextAction"]
    find(steps(mixed), lambda s: s["displayName"] == "Fetch")["settings"]["input"]["authFields"]["password"] = \
        "{{connection_other.output.password}}"
    with pytest.raises(ConversionError, match=r"HTTP auth.*ambiguous"):
        validate_template(mixed)


def test_mapping_requires_every_credential_even_behind_an_unselected_path_and_exact_node_identity_disambiguates() -> None:
    source, m = fixture("http-basic"), mapping()
    other = clone(source["nodes"][1])
    other["name"] = "Other"
    other["id"] = "other"
    source["nodes"].append(other)
    with pytest.raises(Exception, match="Missing connection binding"):
        convert(source, connection_map=m)
    m["bindings"].append({"source": {"node": "Other", "type": "httpBasicAuth", "name": "Example CRM"}, "placeholder": "other"})
    result = convert(source, connection_map=m)
    assert [b["source"]["node"] for b in result["report"]["connectionPlaceholders"]] == ["Fetch", "Other"]
    assert result["report"]["nodes"][-1]["emitted"] is False
    assert result["report"]["complete"] is False
