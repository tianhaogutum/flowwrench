"""Per-file checks. Each one calls the read-only Python API of one flowwrench tool.

Every checker is a generator ``(source, options) -> Finding...``. Expected
input problems (the tool's own error type) become findings here; anything else
propagates and is turned into an ``internal`` finding by the registry. Tool
packages are imported inside the checker so that a broken tool only affects
its own check.
"""
from __future__ import annotations

from collections import Counter
import copy
from typing import Iterator

from .discover import Source
from .model import ERROR, INFO, WARNING, Finding


def _strip_path(message: str, source: Source) -> str:
    """Tools prefix messages with the path they were given; the report groups by file."""
    for prefix in (f"{source.path}: ", f"{source.path.absolute()}: "):
        if message.startswith(prefix):
            return message[len(prefix):]
    return message


def _names(names, limit: int = 4) -> str:
    names = list(names)
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _n8n_workflows(source: Source, load) -> list[tuple[str, dict]]:
    """(pointer prefix, workflow) pairs: one for a workflow, several for a bundle."""
    if source.kind == "n8n-bundle":
        return [(f"/{index}", workflow) for index, workflow in enumerate(source.data)]
    return [("", load())]


# --------------------------------------------------------------------------- n8n-refs

REFS_HINTS = {
    "R001": "rename the reference to an existing node, or use `flowwrench n8n-refs rename` next time",
    "R002": "the referenced node runs after (or beside) this one; reconnect the nodes or reference an upstream node",
    "R003": "on some branches the referenced node never ran; guard the expression or reference a node on every path",
    "R004": "item pairing may break across merges/aggregations; use .first()/.all() or $input instead of .item",
    "R005": "enable the referenced node or stop referencing it",
    "R006": "fix the duplicate node name or remove the dangling connection",
    "R007": "this expression or channel cannot be analysed statically; review it by hand",
}


# Shorter wording for the reference rules; the node is shown next to the message.
REFS_MESSAGES = {
    "R001": "references missing node '{ref}'",
    "R002": "references '{ref}', which is not upstream of this node",
    "R003": "references '{ref}', which does not run on every path to this node",
    "R005": "references disabled node '{ref}'",
}


def _refs_message(result) -> str:
    template = REFS_MESSAGES.get(result.rule)
    if result.reference is None:
        return result.message
    if template is None:  # R004 lists its hazards; R006 names a connection target
        return f"{result.message} (reference '{result.reference}')"
    return template.format(ref=result.reference)


def n8n_refs(source: Source, options) -> Iterator[Finding]:
    from flowwrench.n8n_refs.core import WorkflowError, analyze, load_workflow

    def invalid(exc, pointer=None):
        return Finding("n8n-refs", "invalid-workflow", WARNING, f"n8n-refs cannot analyse this workflow: {exc}",
                       source.display, pointer=pointer or None,
                       hint="re-export the workflow from n8n; references in it were not checked")

    try:
        workflows = _n8n_workflows(source, lambda: load_workflow(source.path))
    except WorkflowError as exc:
        yield invalid(exc)
        return
    for prefix, workflow in workflows:
        try:
            results = analyze(workflow)
        except WorkflowError as exc:
            yield invalid(exc, prefix)
            continue
        for result in results:
            yield Finding("n8n-refs", result.rule, ERROR if result.severity == "error" else WARNING, _refs_message(result),
                          source.display, pointer=prefix + result.pointer, node=result.node,
                          hint=REFS_HINTS.get(result.rule))


# --------------------------------------------------------------------------- n8n2ap

def n8n2ap(source: Source, options) -> Iterator[Finding]:
    from flowwrench.n8n2ap import ConversionError, InputError, convert
    from flowwrench.n8n2ap.cli import CliError, read_json_input

    try:
        workflows = _n8n_workflows(source, lambda: read_json_input(str(source.path)))
    except CliError as exc:
        yield Finding("n8n2ap", "unreadable", INFO, f"n8n2ap cannot read this workflow: {exc}", source.display)
        return
    for prefix, workflow in workflows:
        try:
            # convert() validates and reads only; copy so no other check can observe changes.
            report = convert(copy.deepcopy(workflow))["report"]
        except (InputError, ConversionError) as exc:
            yield Finding("n8n2ap", "not-convertible", INFO,
                          f"Activepieces migration: not convertible as-is ({exc})", source.display,
                          pointer=prefix or None)
            continue
        counts, total = report["counts"], report["total"]
        message = (f"Activepieces migration: {counts['converted']}/{total} nodes convert as-is "
                   f"({report['coveragePercent']:g}%)")
        unsupported = [node["name"] for node in report["nodes"] if node["status"] == "unsupported"]
        if counts["partial"]:
            message += f"; {counts['partial']} partial"
        if unsupported:
            message += f"; {len(unsupported)} unsupported: {_names(unsupported)}"
        yield Finding("n8n2ap", "ready" if report["complete"] else "manual-work", INFO, message, source.display,
                      pointer=prefix or None,
                      hint=None if report["complete"] else
                      "`flowwrench n8n2ap` writes the flow plus a report of every node that needs manual work")


# --------------------------------------------------------------------------- Node-RED

NODERED_ISSUES = {
    # kind: (severity, message template)
    "dangling-wire": (ERROR, "wire to node '{target}' that {reason}"),
    "dangling-subflow-wire": (ERROR, "subflow port wire to node '{target}' that {reason}"),
    "missing-config": (ERROR, "uses config node '{target}', which is not in this export"),
    "missing-tab": (ERROR, "belongs to flow tab '{target}', which is not in this export"),
    "missing-subflow": (ERROR, "is an instance of subflow '{target}', which is not in this export"),
    "missing-group": (WARNING, "belongs to group '{target}', which is not in this export"),
    "output-port-count": (WARNING, "has more wire ports than its {target} declared outputs"),
}
NODERED_REASONS = {
    "missing-target": "does not exist",
    "non-runtime-target": "is a config or tab node, not a runtime node",
    "wrong-flow": "is on another flow tab",
    "self-input-reference": "is the subflow itself",
    "input-port-out-of-range": "has no such input port",
    "output-port-out-of-range": "has no such output port",
}


def nodered_integrity(source: Source, options) -> Iterator[Finding]:
    from flowwrench.nodered_merge import InputError, check_integrity, read_flow

    try:
        flow = read_flow(str(source.path))
        # check_integrity prunes invalid wires of this in-memory copy only; nothing is written.
        issues = check_integrity(flow)
    except InputError as exc:
        yield Finding("nodered", "invalid-flow", ERROR, _strip_path(str(exc), source), source.display,
                      hint="export the flows again (Menu > Export > all flows, JSON)")
        return
    nodes = {node["id"]: node for node in flow}
    for issue in issues:
        severity, template = NODERED_ISSUES.get(issue["kind"], (WARNING, "integrity issue with '{target}'"))
        reason = NODERED_REASONS.get(issue.get("reason") or "", "is not a valid wire target")
        node = nodes.get(issue["nodeId"], {})
        label = node.get("name") or f"{node.get('type', 'node')} {issue['nodeId']}"
        yield Finding("nodered", issue["kind"], severity,
                      template.format(target=issue["target"], reason=reason), source.display,
                      pointer=f"/{_index(flow, issue['nodeId'])}{issue['property']}", node=str(label),
                      hint="a partial export or a bad merge; `flowwrench nodered-merge` keeps wires consistent")


def _index(flow: list, node_id: str) -> int:
    for index, node in enumerate(flow):
        if node.get("id") == node_id:
            return index
    return 0


# --------------------------------------------------------------------------- Activepieces

def ap_promote(source: Source, options) -> Iterator[Finding]:
    from flowwrench.ap_promote.core import Flow, pieces, scrub
    from flowwrench.ap_promote.jsondoc import InvalidInput

    try:
        flow = Flow.load(source.path)
    except InvalidInput as exc:
        yield Finding("ap-promote", "unsupported-flow", WARNING, f"ap-promote cannot audit this flow: {exc}",
                      source.display, hint="connections in this flow were not inventoried")
        return
    references = flow.references
    if references:
        ids = Counter(reference.external_id for reference in references)
        listed = _names(f"{name} (x{count})" if count > 1 else name for name, count in sorted(ids.items()))
        yield Finding("ap-promote", "connections", INFO,
                      f"{len(references)} connection reference(s) to {len(ids)} connection(s): {listed}",
                      source.display,
                      hint="remap them for the target project with `flowwrench ap-promote plan` / `apply`")
    unpinned: dict[tuple, list[dict]] = {}
    for record in pieces(flow):
        if record["status"] == "unpinned":
            unpinned.setdefault((record["pieceName"], record["pieceVersion"]), []).append(record)
    for (piece, version), records in sorted(unpinned.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        first = records[0]
        steps = sorted({record["step"] for record in records})
        also = f" (used by {len(steps)} steps: {_names(steps)})" if len(steps) > 1 else ""
        yield Finding("ap-promote", "unpinned", WARNING,
                      f"{piece} is not pinned to an exact version ({version or 'no version'}){also}",
                      source.display, pointer=first["pointer"], node=first["step"],
                      hint="the target instance may resolve a different piece version; pin x.y.z before promoting")
    try:
        _, removed = scrub(flow)  # returns edited text; nothing is written
    except InvalidInput:
        removed = []
    if removed:
        yield Finding("ap-promote", "sample-data", INFO,
                      f"{len(removed)} step(s) still carry test sample data references", source.display,
                      pointer=removed[0], hint="`flowwrench ap-promote scrub` removes them before you share the flow")


# --------------------------------------------------------------------------- Windmill

def windmill(source: Source, options) -> Iterator[Finding]:
    from flowwrench.windmill_dryrun import FormatError, read_data, validate_flow

    try:
        validate_flow(read_data(str(source.path)))
    except FormatError as exc:
        yield Finding("windmill", "openflow", WARNING,
                      f"not valid in the supported OpenFlow subset: {_strip_path(exc.message, source)}",
                      source.display,
                      hint="see docs/windmill-dryrun.md for the supported subset; inline scripts must be resolved")


# --------------------------------------------------------------------------- Huginn

def huginn(source: Source, options) -> Iterator[Finding]:
    from flowwrench.huginn.core import lint, load_scenario, required_variables
    from flowwrench.huginn.formats import KitError, LocatedValidationError

    hint = "Huginn rejects or mis-imports this scenario; fix it before importing"
    try:
        doc = load_scenario(source.path, check_schedules=False, allow_templates=True)
        variables = required_variables(doc)
    except LocatedValidationError as exc:
        yield Finding("huginn", "invalid-scenario", ERROR, exc.reason, source.display, pointer=exc.pointer or None,
                      hint=hint)
        return
    except KitError as exc:
        yield Finding("huginn", "invalid-scenario", ERROR, str(exc), source.display, hint=hint)
        return
    if variables:
        yield Finding("huginn", "template-variables", INFO,
                      f"template needs {len(variables)} variable(s): {_names(variables)}", source.display,
                      hint="`flowwrench huginn render --vars FILE` produces an importable scenario")
    try:
        result = lint(doc)
    except KitError:
        if variables:  # unresolved placeholders can stand in for typed fields; lint the rendered file
            return
        raise
    names = {agent["guid"]: agent.get("name") or agent["guid"] for agent in doc["agents"]}
    for issue in result["issues"]:
        code = issue["code"]
        if code == "unknown-schedule":
            yield Finding("huginn", code, ERROR, "unknown schedule; Huginn will not run this agent on a timer",
                          source.display, node=str(names.get(issue["guid"], issue["guid"])),
                          hint="use one of Huginn's schedule names, e.g. every_1h, midnight or 8am")
        elif code == "link-cycle":
            members = _names(str(names.get(guid, guid)) for guid in issue["guids"])
            kind = "control link" if issue.get("kind") == "control_links" else "event link"
            yield Finding("huginn", code, WARNING, f"{kind} cycle between {members}; events can loop forever",
                          source.display)
        elif code == "orphan-agent":
            yield Finding("huginn", code, WARNING, "agent is not linked to any other agent", source.display,
                          node=str(names.get(issue["guid"], issue["guid"])))
        else:
            yield Finding("huginn", code, ERROR if issue.get("severity") == "error" else WARNING,
                          code.replace("-", " "), source.display)
