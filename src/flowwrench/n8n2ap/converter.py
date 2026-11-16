"""Workflow -> Activepieces template + migration report (port of ``lib/converter.js``)."""

from __future__ import annotations

import re
from typing import Any, TypedDict

from .connections import UNSET, connection_bindings, http_auth_input
from .expressions import translate_expression
from .graph import TRIGGERS, build_graph, node_kind
from .input import ConversionError, InputError, LIMITS, find_secrets, validate_input
from .jscompat import (
    OBJECT_PROTOTYPE_KEYS,
    coalesce,
    is_array,
    is_finite_number,
    is_object,
    js_items,
    js_round,
    js_string,
    recursion_headroom,
    truthy,
    utf16_slice,
    utf16_sort_key,
)
from .mappers import (
    AP_COMMIT,
    EPOCH,
    base,
    check_keys,
    code_step,
    fail_step,
    map_http,
    map_if,
    map_pass,
    map_set,
    map_trigger,
)
from .validate import validate_template

__all__ = ["ConversionResult", "InputError", "LIMITS", "UNSET", "convert", "report_markdown", "translate_expression"]

TOOL_NAME = "n8n2ap"  # Data format identifier written into templates and reports; never renamed.
TOOL_VERSION = "0.1.0"

MAX_VERSIONS: dict[str, float] = {"webhook": 2.1, "scheduleTrigger": 1.3, "cron": 1, "manualTrigger": 1,
                                  "httpRequest": 4.4, "if": 2.3, "set": 3.4, "code": 2, "noOp": 1, "stickyNote": 1}
_NOOP_TYPES = frozenset({"if"})
_WORKFLOW_SETTINGS = ["timezone", "executionOrder", "saveManualExecutions", "saveExecutionProgress",
                      "saveDataErrorExecution", "saveDataSuccessExecution", "callerPolicy", "availableInMCP"]
_EXECUTION_OPTIONS = ("continueOnFail", "retryOnFail", "alwaysOutputData", "executeOnce")

RESPONSE_GUARD_CODE = (
    "export const code = async (inputs) => { if (inputs.data === null || typeof inputs.data !== 'object' "
    "|| Array.isArray(inputs.data)) throw new Error('n8n2ap: only single JSON object HTTP responses are supported');"
    " return inputs.data; };"
)
IF_GUARD_CODE = (
    "export const code = async (inputs) => { for (const entry of inputs.checks) { if (typeof entry.value !== "
    "entry.type || (entry.type === 'number' && !Number.isFinite(entry.value))) throw new Error('n8n2ap: IF operand"
    " type mismatch'); } return true; };"
)
WEBHOOK_GUARD_CODE = (
    "export const code = async (inputs) => { if (inputs.data?.method !== inputs.method) throw new Error('n8n2ap:"
    " unexpected or missing webhook method'); return { body: inputs.data.body, headers: inputs.data.headers, query:"
    " inputs.data.queryParams }; };"
)
WEBHOOK_NOTE = (
    "Use the new Activepieces webhook URL; original path/response envelope is not preserved. Method is checked and "
    "body/headers/query are normalized by a generated guard; other n8n webhook metadata is unsupported."
)
LIMITATIONS = [
    "Single JSON payload only: n8n item arrays, binary data and paired-item semantics are not reproduced.",
    "Structural validation uses a local JSON Schema subset derived from pinned upstream Zod schemas; no live import or"
    " piece availability check.",
    "Secret detection is heuristic; inspect all artifacts before sharing. Credential IDs, pinData, staticData and raw"
    " Code sources are never copied.",
    "Converted means supported within this subset, not proof of behavioral equivalence. Webhook URL and response"
    " envelope change.",
]


class ConversionResult(TypedDict):
    template: dict[str, Any]
    report: dict[str, Any]


def _untested_version(kind: str, version: Any) -> bool:
    maximum = MAX_VERSIONS.get(kind)
    if maximum is not None:
        return version > maximum or version < 1
    # ``MAX_VERSIONS[kind]`` on a JavaScript object literal also finds inherited
    # Object.prototype members (truthy, compare as NaN): only ``< 1`` can hold.
    return kind in OBJECT_PROTOTYPE_KEYS and version < 1


def convert(workflow: Any, *, trigger: str | None = None, connection_map: Any = UNSET) -> ConversionResult:
    """Convert one parsed n8n workflow export.

    ``trigger`` selects a trigger node by name or id. ``connection_map`` is the
    parsed connection-map document; leave it ``UNSET`` when no map was given
    (``None`` is a JSON ``null`` map and is rejected).
    """
    with recursion_headroom():
        return _convert(workflow, trigger, connection_map)


def _convert(workflow: Any, override: str | None, connection_map: Any) -> ConversionResult:
    validate_input(workflow)
    bindings = connection_bindings(workflow, connection_map)
    graph = build_graph(workflow, override)
    nodes, incoming, trigger, reachable = graph.nodes, graph.incoming, graph.trigger, graph.reachable
    main = graph.main
    sources: dict[str, Any] = {}
    records: dict[str, dict[str, Any]] = {}
    emitted: set[str] = set()
    notes: list[dict[str, Any]] = []
    step_names = {n["name"]: ("trigger" if n is trigger else f"step_{i + 1}") for i, n in enumerate(workflow["nodes"])}
    for n in workflow["nodes"]:
        kind = node_kind(n)
        issues: list[str] = []
        record: dict[str, Any] = {"name": n["name"], "type": n["type"], "status": "converted", "step": None,
                                  "emitted": False, "issues": issues, "credentials": []}
        for cred_type, value in js_items(coalesce(n.get("credentials"), {})):
            cred_name = value["name"] if is_object(value) and isinstance(value.get("name"), str) else "(unnamed)"
            record["credentials"].append({"type": cred_type, "name": cred_name})
        if record["credentials"] and n["name"] not in bindings:
            issues.append("Credentials omitted; recreate connections manually")
        for path in find_secrets(n["parameters"]):
            issues.append(f"Potential secret removed at {path}; no parameter values from this node are exported")
        if kind == "httpRequest" and coalesce(n.get("typeVersion"), 1) < 3:
            issues.append("HTTP Request versions before v3 require manual migration")
        if "typeVersion" in n and _untested_version(kind, n["typeVersion"]):
            issues.append("Untested node typeVersion")
        if n["name"] in graph.side_channels:
            issues.append("Non-main/AI connection channels are unsupported")
        node_incoming = incoming[n["name"]]
        if len(node_incoming) > 1 or any(e.input != 0 for e in node_incoming):
            issues.append("Multi-input/fan-in semantics require manual migration")
        if truthy(n.get("disabled")):
            issues.append("Disabled node requires manual review")
        for option in _EXECUTION_OPTIONS:
            if truthy(n.get(option)):
                issues.append(f"Node execution option {option} is unsupported")
        on_error = n.get("onError")
        if truthy(on_error) and on_error != "stopWorkflow":
            issues.append("Non-default node error handling is unsupported")
        records[n["name"]] = record

    workflow_issues: list[str] = []
    if "settings" in workflow and not is_object(workflow["settings"]):
        workflow_issues.append("Invalid workflow settings")
    else:
        check_keys(coalesce(workflow.get("settings"), {}), _WORKFLOW_SETTINGS, workflow_issues, "workflow setting")
    name = workflow.get("name")
    title = utf16_slice(name, 0, 200) if isinstance(name, str) and name else "Migrated n8n workflow"

    def mark(record: dict[str, Any], status: str, issue: str | None = None) -> None:
        if status == "unsupported" or record["status"] == "converted":
            record["status"] = status
        if issue:
            record["issues"].append(issue)

    # A sticky note is an annotation, never an executable step. Its original text is not trusted code.
    for n in [n for n in workflow["nodes"] if node_kind(n) == "stickyNote"]:
        r = records[n["name"]]
        params = n["parameters"]
        check_keys(params, ["content", "height", "width", "color"], r["issues"])
        content = coalesce(params.get("content"), "")
        if not isinstance(content, str):
            r["issues"].append("Note content must be text")
        if main(n["name"]) or incoming[n["name"]]:
            r["issues"].append("Executable connections to sticky notes are unsupported")
        pos = n.get("position")
        x = pos[0] if is_array(pos) and len(pos) > 0 and is_finite_number(pos[0]) else 0
        y = pos[1] if is_array(pos) and len(pos) > 1 and is_finite_number(pos[1]) else 0
        width = params.get("width")
        height = params.get("height")
        notes.append({
            "id": f"note_{len(notes) + 1}",
            "content": "[n8n2ap: note content omitted; see migration report]" if r["issues"] else content,
            "ownerId": None, "color": "yellow",
            "position": {"x": x, "y": y},
            "size": {"width": width if is_finite_number(width) and width > 0 else 300,
                     "height": height if is_finite_number(height) and height > 0 else 200},
            "createdAt": EPOCH, "updatedAt": EPOCH,
        })
        r["emitted"] = True
        if r["issues"]:
            mark(r, "partial")

    trigger_name: str = trigger["name"]
    trigger_kind = node_kind(trigger)
    trigger_record = records[trigger_name]
    ap_trigger = map_trigger(trigger, workflow, trigger_record["issues"])
    trigger_edges = main(trigger_name)
    if len(trigger_edges) > 1 or any(e.output != 0 for e in trigger_edges):
        trigger_record["issues"].append("Trigger fan-out/output ports are unsupported")
    if trigger_record["issues"]:
        mark(trigger_record, "partial")
        ap_trigger = {**base(trigger, "trigger"), "type": "EMPTY", "valid": False, "settings": {}}
    trigger_record["step"] = "trigger"
    trigger_record["emitted"] = True
    emitted.add(trigger_name)
    sources[trigger_name] = {
        "ref": ("webhook_method_guard.output" if trigger_kind == "webhook" and trigger_record["status"] == "converted"
                else "trigger.output"),
        "webhook": trigger_kind == "webhook", "canonical": True,
    }

    def build(name: str | None, stop: str | None, current: dict[str, Any] | None,
              available: frozenset[str]) -> dict[str, Any] | None:
        if not name or name == stop:
            return None
        n, r, step_name = nodes[name], records[name], step_names[name]
        kind = node_kind(n)
        if name in emitted:
            raise ConversionError(
                "Unstructured graph cannot be represented without duplicating nodes; split the workflow before converting"
            )
        emitted.add(name)
        r["emitted"] = True
        r["step"] = step_name
        issues: list[str] = r["issues"]

        def translate(value: Any) -> Any:
            result = translate_expression(value, current=current, sources=sources, allowed=available)
            issues.extend(result["issues"])
            return result["value"]

        edges = main(name)
        if kind == "if":
            if any(e.output > 1 for e in edges) or any(
                    len([e for e in edges if e.output == port]) > 1 for port in (0, 1)):
                issues.append("IF supports one connection per true/false output only")
        elif len(edges) > 1 or any(e.output != 0 for e in edges):
            issues.append("Non-IF fan-out/output ports are unsupported")
        if_checks: list[dict[str, Any]] = []
        if kind == "httpRequest":
            auth = http_auth_input(bindings[n["name"]]) if n["name"] in bindings else None
            step = map_http(n, step_name, translate, issues, auth)
        elif kind == "set":
            step = map_set(n, step_name, current, translate, issues)
        elif kind == "if":
            step, if_checks = map_if(n, step_name, translate, issues)
        elif kind == "noOp":
            step = map_pass(n, step_name, current, issues)
        elif kind == "code":
            mark(r, "partial", "n8n Code requires a manual API/item-model port; source code is intentionally omitted")
            step = fail_step(n, step_name, "Manually port the n8n Code node")
        else:
            if kind == "stickyNote":
                reason = "Connected annotation is not executable"
            elif kind in TRIGGERS:
                reason = "Additional trigger in execution graph"
            else:
                reason = "No mapper for this node type"
            mark(r, "unsupported", reason)
            step = fail_step(n, step_name, "Unsupported node; see migration report")
        if issues and r["status"] == "converted":
            mark(r, "partial")
        if r["status"] != "converted":
            step = fail_step(n, step_name, "Migration blocked; see this node in the migration report")
        # Only the executable, guaranteed data source may be referenced from a later step.
        body = ".body" if kind == "httpRequest" and r["status"] == "converted" else ""
        output: dict[str, Any] | None = {"ref": f"{step_name}.output{body}"}
        if kind in _NOOP_TYPES:
            output = current
        elif kind == "noOp" and current is not None and truthy(current.get("webhook")):
            output = {**output, "webhook": True, "canonical": current.get("canonical")}
        sources[name] = output
        next_available = available | {name}
        if kind == "if" and step["type"] == "ROUTER":
            a = next((e.to for e in edges if e.output == 0), None)
            b = next((e.to for e in edges if e.output == 1), None)
            join = graph.join(a, b)
            until = join if join is not None else stop
            step["children"] = [build(a, until, current, next_available), build(b, until, current, next_available)]
            if join is not None and join != stop:
                # Any fan-in has already been flagged on its node; it becomes a failing placeholder.
                following = build(join, stop, None, next_available)
                if following is not None:
                    step["nextAction"] = following
        elif kind != "if" and len(edges) == 1 and edges[0].output == 0:
            following = build(edges[0].to, stop, output, next_available)
            if following is not None:
                step["nextAction"] = following
        if kind == "httpRequest" and r["status"] == "converted":
            guard = code_step({"name": "Require a single JSON object response"}, f"response_guard_{step_name}",
                              RESPONSE_GUARD_CODE, {"data": f"{{{{{step_name}.output.body}}}}"})
            if step.get("nextAction") is not None:
                guard["nextAction"] = step["nextAction"]
            step["nextAction"] = guard
        if step["type"] == "ROUTER":
            guard = code_step({"name": "Validate IF operand types"}, f"if_type_guard_{step_name}", IF_GUARD_CODE,
                              {"checks": if_checks})
            guard["nextAction"] = step
            return guard
        return step

    first: dict[str, Any] | None = None
    if len(trigger_edges) == 1 and trigger_edges[0].output == 0:
        first = build(trigger_edges[0].to, None, sources[trigger_name], frozenset({trigger_name}))
    if trigger_record["status"] != "converted" or workflow_issues:
        guard = fail_step({"name": "Migration review required"}, "migration_guard",
                          "Trigger or workflow settings require manual migration")
        if first is not None:
            guard["nextAction"] = first
        first = guard
    elif trigger_kind == "webhook":
        guard = code_step({"name": "Enforce original webhook method"}, "webhook_method_guard", WEBHOOK_GUARD_CODE,
                          {"data": "{{trigger.output}}", "method": coalesce(trigger["parameters"].get("httpMethod"), "GET")})
        if first is not None:
            guard["nextAction"] = first
        first = guard
        trigger_record["notes"] = [WEBHOOK_NOTE]
    # Shared aliases supply one output before every branch. Keep per-node report
    # provenance, but emit each failing configuration step exactly once.
    placeholders: dict[str, dict[str, Any]] = {}
    for binding in bindings.values():
        placeholders.setdefault(binding["placeholder"], binding)
    for binding in reversed(list(placeholders.values())):
        guard = fail_step({"name": f"Configure connection: {binding['placeholder']}"}, binding["step"],
                          f"Configure connection placeholder {binding['placeholder']}; required output fields: "
                          f"{', '.join(binding['fields'])}")
        if first is not None:
            guard["nextAction"] = first
        first = guard
    if bindings:
        workflow_issues.append(
            "Connection placeholders are unresolved; configure their output fields in the target draft before running"
        )
    if first is not None:
        ap_trigger["nextAction"] = first
    for n in workflow["nodes"]:
        r = records[n["name"]]
        if not r["emitted"]:
            mark(r, "unsupported", "Not emitted: behind an unrepresentable branch; execution is blocked upstream"
                 if n["name"] in reachable else "Not reachable from the selected trigger")
        r["issues"] = list(dict.fromkeys(r["issues"]))
    report_nodes = list(records.values())
    counts = {"converted": 0, "partial": 0, "unsupported": 0}
    for r in report_nodes:
        counts[r["status"]] += 1
    complete = counts["partial"] == 0 and counts["unsupported"] == 0 and len(workflow_issues) == 0
    pieces = _collect_pieces(ap_trigger)
    template = {
        "name": title, "type": "CUSTOM",
        "summary": "Migrated offline from n8n; review the migration report before publishing.",
        "description": f"Generated by {TOOL_NAME} {TOOL_VERSION}. Single-payload migration; no workflow steps were executed.",
        "tags": [], "blogUrl": None, "metadata": None, "author": TOOL_NAME, "categories": [],
        "pieces": sorted(pieces, key=utf16_sort_key), "status": "PUBLISHED",
        "flows": [{"displayName": title, "valid": complete and ap_trigger["valid"], "schemaVersion": "28",
                   "trigger": ap_trigger, "notes": notes}],
    }
    report: dict[str, Any] = {
        "formatVersion": 1, "tool": TOOL_NAME, "toolVersion": TOOL_VERSION, "upstreamCommit": AP_COMMIT,
        "workflow": title, "trigger": trigger_name, "complete": complete, "total": len(report_nodes), "counts": counts,
        "coveragePercent": js_round(counts["converted"] / len(report_nodes) * 10000) / 100,
        "workflowIssues": workflow_issues, "nodes": report_nodes,
        "credentials": [{"node": r["name"], **c} for r in report_nodes for c in r["credentials"]],
    }
    if connection_map is not UNSET:
        report["connectionPlaceholders"] = list(bindings.values())
    report["limitations"] = list(LIMITATIONS)
    validate_template(template)
    return {"template": template, "report": report}


def _collect_pieces(trigger: dict[str, Any]) -> set[str]:
    pieces: set[str] = set()
    stack: list[Any] = [trigger]
    while stack:
        step = stack.pop()
        if not step:
            continue
        piece_name = step.get("settings", {}).get("pieceName") if is_object(step.get("settings")) else None
        if truthy(piece_name):
            pieces.add(piece_name)
        stack.append(step.get("nextAction"))
        stack.extend(step.get("children") or ())
    return pieces


_MD_SPECIAL = re.compile(r"[\\`*_\[\]<>|]")
_LINE_BREAK = re.compile(r"[\r\n]")


def _escape_md(value: Any) -> str:
    return _LINE_BREAK.sub(" ", _MD_SPECIAL.sub(lambda m: "\\" + m.group(), js_string(value)))


def report_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    out = [f"# Migration report: {_escape_md(report['workflow'])}", "",
           f"Trigger: {_escape_md(report['trigger'])}", "",
           f"Coverage: **{js_string(report['coveragePercent'])}%** ({js_string(counts['converted'])}/"
           f"{js_string(report['total'])} fully converted; {js_string(counts['partial'])} partial; "
           f"{js_string(counts['unsupported'])} unsupported).", "",
           f"Ready within supported subset: **{'yes' if report['complete'] else 'no'}**. No steps executed.", "",
           "| Node | n8n type | Status | AP step | Emitted |", "| --- | --- | --- | --- | --- |"]
    for r in report["nodes"]:
        out.append(f"| {_escape_md(r['name'])} | {_escape_md(r['type'])} | {js_string(r['status'])} | "
                   f"{js_string(coalesce(r.get('step'), '—'))} | {'yes' if r['emitted'] else 'no'} |")
    out.extend(["", "## Review items", ""])
    for issue in report["workflowIssues"]:
        out.append(f"- Workflow: {_escape_md(issue)}")
    for r in report["nodes"]:
        for issue in [*r["issues"], *coalesce(r.get("notes"), [])]:
            out.append(f"- {_escape_md(r['name'])}: {_escape_md(issue)}")
    if report["complete"]:
        out.append("- No blocking migration issues found within the documented subset.")
    out.extend(["", "## Credentials to recreate (names and types only)", ""])
    for c in report["credentials"]:
        out.append(f"- {_escape_md(c['node'])}: {_escape_md(c['name'])} ({_escape_md(c['type'])})")
    if not report["credentials"]:
        out.append("- None referenced.")
    placeholders = report.get("connectionPlaceholders")
    if placeholders:
        out.extend(["", "## Connection placeholders (unresolved)", "",
                    "| Source node | Credential type | Credential name | Placeholder | AP step | Required output |",
                    "| --- | --- | --- | --- | --- | --- |"])
        for b in placeholders:
            source = b["source"]
            out.append(f"| {_escape_md(source['node'])} | {_escape_md(source['type'])} | {_escape_md(source['name'])} | "
                       f"{b['placeholder']} | {b['step']} | {', '.join(b['fields'])} |")
    out.extend(["", "## Limits", "", *(f"- {x}" for x in report["limitations"]), "",
                f"Upstream Activepieces commit: {report['upstreamCommit']}", ""])
    return "\n".join(out)
