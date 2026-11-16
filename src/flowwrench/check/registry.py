"""The list of checks, and the crash-isolating loop that runs them."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from typing import Callable, Iterable

from . import file_checks, schedule_checks
from .discover import Discovery, Source
from .model import ERROR, Finding

ISSUES_URL = "https://github.com/tianhaogutum/flowwrench/issues"
# Set to 1 to let a crashing check raise its traceback instead of reporting it.
DEBUG_ENV = "FLOWWRENCH_CHECK_DEBUG"


@dataclass(frozen=True)
class CheckOptions:
    default_timezone: str = "UTC"
    # UTC start date (YYYY-MM-DD) of the schedule horizons; None means today.
    from_date: str | None = None
    herd_threshold: int = 3

    def __post_init__(self):
        if self.from_date is None:
            object.__setattr__(self, "from_date", datetime.now(timezone.utc).date().isoformat())


@dataclass(frozen=True)
class Check:
    name: str
    tool: str
    summary: str
    kinds: frozenset[str]
    # Per-file checks take (source, options); cross-file checks take (sources, options).
    run: Callable[..., Iterable[Finding]] | None = None
    cross_file: bool = False
    rules: tuple[str, ...] = field(default=())


CHECKS: tuple[Check, ...] = (
    Check("parse", "-", "Exported workflow files that cannot be read or parsed",
          frozenset(), rules=("invalid-json", "invalid-yaml", "invalid-encoding", "unreadable", "unrecognised")),
    Check("n8n-refs", "n8n-refs",
          "n8n: $('Node') references to missing, disabled, downstream or not-always-run nodes; "
          "duplicate names and dangling connections",
          frozenset({"n8n", "n8n-bundle"}), file_checks.n8n_refs,
          rules=("R001", "R002", "R003", "R004", "R005", "R006", "R007", "invalid-workflow")),
    Check("n8n2ap", "n8n2ap", "n8n: how much of each workflow converts to Activepieces as-is (info only)",
          frozenset({"n8n", "n8n-bundle"}), file_checks.n8n2ap,
          rules=("ready", "manual-work", "not-convertible", "unreadable")),
    Check("nodered", "nodered-merge",
          "Node-RED: wires to missing nodes, missing config/tab/subflow/group nodes, extra output ports",
          frozenset({"node-red"}), file_checks.nodered_integrity,
          rules=("dangling-wire", "dangling-subflow-wire", "missing-config", "missing-tab", "missing-subflow",
                 "missing-group", "output-port-count", "invalid-flow")),
    Check("ap-promote", "ap-promote",
          "Activepieces: connection inventory, piece versions that are not pinned, leftover sample data",
          frozenset({"activepieces"}), file_checks.ap_promote,
          rules=("connections", "unpinned", "sample-data", "unsupported-flow")),
    Check("windmill", "windmill-dryrun",
          "Windmill: OpenFlow structure, module ids, transforms and loops (windmill-dryrun subset)",
          frozenset({"windmill-flow"}), file_checks.windmill, rules=("openflow",)),
    Check("huginn", "huginn",
          "Huginn: invalid scenarios, dangling links, unknown schedules, link cycles, orphan agents",
          frozenset({"huginn", "huginn-kit"}), file_checks.huginn,
          rules=("invalid-scenario", "unknown-schedule", "link-cycle", "orphan-agent", "template-variables")),
    Check("schedules", "schedules",
          "All platforms + cron: jobs that start in the same minute, DST-skipped or doubled runs, "
          "schedules that cannot be forecast",
          schedule_checks.KINDS, schedule_checks.schedules, cross_file=True,
          rules=("herd", "dst-skipped", "dst-ambiguous", "not-forecast", "no-timezone", "unreadable",
                 "note", "analysis-limit")),
)
BY_NAME = {check.name: check for check in CHECKS}


def select(only: Iterable[str] = (), skip: Iterable[str] = ()) -> list[Check]:
    """Checks to run, in registry order. Raises ValueError for unknown names."""
    only, skip = list(only), list(skip)
    unknown = sorted({name for name in [*only, *skip] if name not in BY_NAME})
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)} (see --list-checks)")
    return [check for check in CHECKS if (not only or check.name in only) and check.name not in skip]


def _internal(check: Check, exc: BaseException, source: Source | None) -> Finding:
    # Only the exception type: messages of unexpected errors may quote input values.
    return Finding(check.name, "internal", ERROR,
                   f"the {check.name} check crashed ({type(exc).__name__}); its results are missing, "
                   "other checks still ran", source.display if source else None,
                   hint=f"please report it at {ISSUES_URL} (set {DEBUG_ENV}=1 for the traceback)")


def _guarded(check: Check, args: tuple, source: Source | None) -> list[Finding]:
    try:
        return list(check.run(*args))
    except Exception as exc:  # noqa: BLE001 - one broken checker must not abort the run
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [_internal(check, exc, source)]


def run_checks(discovery: Discovery, checks: Iterable[Check] | None = None,
               options: CheckOptions | None = None) -> list[Finding]:
    """Run ``checks`` (default: all) over discovered sources; never raises for a checker bug."""
    checks = list(CHECKS if checks is None else checks)
    options = options or CheckOptions()
    names = {check.name for check in checks}
    findings = [finding for finding in discovery.findings if finding.check in names]
    for check in checks:
        if check.run is None:
            continue
        relevant = [source for source in discovery.sources if source.kind in check.kinds]
        if check.cross_file:
            if relevant:
                findings.extend(_guarded(check, (relevant, options), None))
            continue
        for source in relevant:
            findings.extend(_guarded(check, (source, options), source))
    return sorted(findings, key=Finding.sort_key)
