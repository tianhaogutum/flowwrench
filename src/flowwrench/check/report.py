"""Text, JSON and SARIF 2.1.0 renderings of one check run."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import PurePath, PurePosixPath
from urllib.parse import quote

from flowwrench import __version__

from .discover import PLATFORMS, Source
from .model import ERROR, INFO, WARNING, Finding, at_or_above, counts
from .registry import BY_NAME, Check

FORMAT = "flowwrench.check/v1"
REPO_URL = "https://github.com/tianhaogutum/flowwrench"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_LEVEL = {ERROR: "error", WARNING: "warning", INFO: "note"}
LABEL = {ERROR: "error", WARNING: "warn", INFO: "info"}

# Short rule titles for SARIF; unlisted rules fall back to their check summary.
RULE_TITLES = {
    "parse/invalid-json": "Workflow export is not valid JSON",
    "parse/invalid-yaml": "Workflow export is not valid YAML",
    "n8n-refs/R001": "Unknown referenced node",
    "n8n-refs/R002": "Referenced node is not upstream",
    "n8n-refs/R003": "Referenced node does not run on every path",
    "n8n-refs/R004": "Paired-item lookup crosses a pairing hazard",
    "n8n-refs/R005": "Disabled node is referenced",
    "n8n-refs/R006": "Duplicate node name or dangling connection",
    "n8n-refs/R007": "Expression cannot be analysed statically",
    "n8n2ap/manual-work": "Workflow needs manual work to migrate to Activepieces",
    "nodered/dangling-wire": "Wire to a missing or invalid node",
    "nodered/missing-config": "Missing config node",
    "nodered/missing-tab": "Missing flow tab",
    "nodered/missing-subflow": "Missing subflow definition",
    "ap-promote/unpinned": "Piece version is not pinned",
    "ap-promote/connections": "Connection references to remap",
    "windmill/openflow": "OpenFlow outside the supported subset",
    "huginn/invalid-scenario": "Invalid Huginn scenario",
    "huginn/unknown-schedule": "Unknown Huginn schedule",
    "huginn/link-cycle": "Agent link cycle",
    "huginn/orphan-agent": "Orphan agent",
    "schedules/herd": "Many schedules start in the same minute",
    "schedules/dst-skipped": "Schedule falls into a DST gap",
    "schedules/dst-ambiguous": "Schedule falls into a DST overlap",
}


@dataclass
class Run:
    """Everything a renderer needs about one invocation."""

    paths: list[str]
    sources: list[Source]
    scanned: int
    checks: list[Check]
    findings: list[Finding]
    fail_on: str | None  # "error", "warning" or None (never)

    @property
    def failed(self) -> bool:
        return at_or_above(self.findings, self.fail_on)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary_line(run: Run) -> str:
    total = counts(run.findings)
    return " · ".join([_plural(len(run.sources), "file"), _plural(total[ERROR], "error"),
                       _plural(total[WARNING], "warning"), f"{total[INFO]} info"])


# --------------------------------------------------------------------------- text

def _safe(value) -> str:
    """Untrusted labels must not move the cursor or recolour the terminal."""
    return "".join(f"\\x{ord(c):02x}" if ord(c) < 32 or ord(c) == 127 else c for c in str(value))


class _Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text


SEVERITY_COLOR = {ERROR: "1;31", WARNING: "33", INFO: "36"}


def render_text(run: Run, *, color: bool = False, verbose: bool = False) -> str:
    style = _Style(color)
    by_platform = Counter(source.platform for source in run.sources)
    mix = ", ".join(f"{by_platform[p]} {p}" for p in PLATFORMS if by_platform[p])
    lines = [style("flowwrench check", "1") + f"  {', '.join(_safe(p) for p in run.paths)}  "
             + style(f"({_plural(len(run.sources), 'workflow export')}" + (f": {mix}" if mix else "")
                     + f"; {_plural(run.scanned, 'file')} inspected)", "2")]
    if not run.sources and not run.findings:
        lines += ["", "No n8n, Node-RED, Activepieces, Windmill, Huginn or crontab exports found.",
                  style("Point it at the folder you export workflows to, e.g. `flowwrench check exports/`.", "2")]
    kinds = {source.display: source.label for source in run.sources}
    groups: dict[str | None, list[Finding]] = {}
    for finding in run.findings:
        groups.setdefault(finding.file, []).append(finding)
    width = min(max((len(f.rule_id) for f in run.findings), default=0), 24)
    for file in sorted(groups, key=lambda name: (name is None, name or "")):
        title = style(_safe(file), "1") if file is not None else style("(whole run)", "1")
        kind = kinds.get(file) if file is not None else None
        lines += ["", title + (style(f"  {kind}", "2") if kind else "")]
        for finding in groups[file]:
            severity = style(LABEL[finding.severity].ljust(5), SEVERITY_COLOR[finding.severity])
            node = f"{_safe(finding.node)}: " if finding.node else ""
            # Line numbers are short and always shown; JSON Pointers only with -v (and in JSON/SARIF).
            where = (f"line {finding.line}" if finding.line is not None
                     else finding.pointer if verbose and finding.pointer else "")
            lines.append(f"  {severity}  {style(finding.rule_id.ljust(width), '2')}  {node}{_safe(finding.message)}"
                         + (style(f"  {_safe(where)}", "2") if where else ""))
            if verbose and finding.hint:
                lines.append(" " * (11 + width) + style(f"-> {_safe(finding.hint)}", "2"))
    clean = [source for source in run.sources if source.display not in groups]
    if clean:
        lines += ["", style(f"✓ {_plural(len(clean), 'file')} with no findings", "32")]
        if verbose:
            lines += [style(f"    {_safe(source.display)}", "2") for source in clean]
    lines += ["", style(summary_line(run), "1")]
    if run.findings and not verbose and any(f.hint for f in run.findings):
        lines.append(style("-v adds a fix hint and location to each finding; --format sarif for code scanning.", "2"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- JSON

def render_json(run: Run) -> str:
    per_file = Counter(finding.file for finding in run.findings)
    document = {
        "format": FORMAT,
        "tool": {"name": "flowwrench", "version": __version__},
        "paths": run.paths,
        "checks": [check.name for check in run.checks],
        "files": [{"path": source.display, "kind": source.kind, "platform": source.platform,
                   "findings": per_file[source.display]} for source in run.sources],
        "findings": [finding.to_dict() for finding in run.findings],
        "summary": {"files": len(run.sources), "inspected": run.scanned, **counts(run.findings)},
        "failOn": run.fail_on or "never",
        "failed": run.failed,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- SARIF

def _uri(path: str) -> dict:
    pure = PurePath(path)
    if pure.is_absolute():
        return {"uri": "file://" + quote(pure.as_posix(), safe="/")}
    return {"uri": quote(PurePosixPath(*pure.parts).as_posix(), safe="/"), "uriBaseId": "%SRCROOT%"}


def _rule(rule_id: str, finding: Finding) -> dict:
    check = BY_NAME.get(finding.check)
    title = RULE_TITLES.get(rule_id) or (check.summary if check else rule_id)
    rule = {"id": rule_id, "name": rule_id.replace("/", "-"), "shortDescription": {"text": title},
            "defaultConfiguration": {"level": SARIF_LEVEL[finding.severity]},
            "helpUri": f"{REPO_URL}/blob/main/docs/check.md",
            "properties": {"check": finding.check, "tool": check.tool if check else "-"}}
    if check:
        rule["fullDescription"] = {"text": check.summary}
    return rule


def _result(finding: Finding, rule_index: int) -> dict:
    text = (f"{finding.node}: " if finding.node else "") + finding.message
    location: dict = {"physicalLocation": {"artifactLocation": _uri(finding.file),
                                           "region": {"startLine": finding.line or 1}}}
    if finding.pointer:
        location["logicalLocations"] = [{"fullyQualifiedName": finding.pointer, "kind": "member"}]
    result = {"ruleId": finding.rule_id, "ruleIndex": rule_index, "level": SARIF_LEVEL[finding.severity],
              "message": {"text": text}, "locations": [location],
              "properties": {"check": finding.check, "severity": finding.severity}}
    if finding.pointer:
        result["properties"]["jsonPointer"] = finding.pointer
    if finding.hint:
        result["properties"]["hint"] = finding.hint
    return result


def render_sarif(run: Run) -> str:
    rules: list[dict] = []
    index: dict[str, int] = {}
    results, notifications = [], []
    for finding in run.findings:
        if finding.file is None:
            # Run-level notices have no artifact; SARIF consumers expect results to have one.
            notifications.append({"level": SARIF_LEVEL[finding.severity],
                                  "message": {"text": f"{finding.rule_id}: {finding.message}"}})
            continue
        if finding.rule_id not in index:
            index[finding.rule_id] = len(rules)
            rules.append(_rule(finding.rule_id, finding))
        results.append(_result(finding, index[finding.rule_id]))
    crashed = any(finding.rule == "internal" for finding in run.findings)
    document = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "flowwrench", "version": __version__, "semanticVersion": __version__,
                                "informationUri": REPO_URL, "rules": rules}},
            "results": results,
            "invocations": [{"executionSuccessful": not crashed, "toolExecutionNotifications": notifications}],
            "columnKind": "unicodeCodePoints",
        }],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


RENDERERS = {"json": render_json, "sarif": render_sarif}
