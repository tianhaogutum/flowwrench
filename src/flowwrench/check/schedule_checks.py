"""Cross-file schedule check built on ``flowwrench schedules`` (extract, analyze, dst).

Schedules are extracted file by file, so one malformed export only costs its
own records, then analysed together: a herd is many jobs from different
platforms starting in the same minute, which no single file shows.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import re
from typing import Iterator

from .discover import Source
from .model import INFO, WARNING, Finding

CHECK = "schedules"
HERD_DAYS = 7     # horizon for simultaneous starts (one full week of weekday patterns)
DST_DAYS = 370    # horizon for DST hazards (every transition of the coming year)
KINDS = frozenset({"n8n", "n8n-bundle", "node-red", "activepieces", "huginn", "windmill-schedule", "crontab"})
_LINE = re.compile(r"line (\d+)\Z")
_WARNING_LINE = re.compile(r":(\d+): ")
_INDEX_SUFFIX = re.compile(r"\[\d+(?::\d+)?\]\Z")


def _line(record) -> int | None:
    match = _LINE.match(record.node)
    return int(match[1]) if match else None


def _label(source: Source, record) -> str:
    """``Order sync / Every night``; extract's ``[index:rule]`` suffix is dropped for display."""
    if source.kind == "crontab":
        return record.node
    return f"{record.workflow} / {_INDEX_SUFFIX.sub('', record.node)}"


def _node(source: Source, record) -> str | None:
    # Crontab records are identified by their line number, shown as the location.
    return None if source.kind == "crontab" else _label(source, record)


def _names(names, limit: int = 4) -> str:
    names = list(dict.fromkeys(names))
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _extract(source: Source, options, InputError, extract):
    """Return (records, findings) for one file."""
    findings = []
    try:
        inventory = extract([str(source.path)], options.default_timezone)
    except InputError as exc:
        message = str(exc)
        if message.startswith("no schedules found"):
            return [], []
        for prefix in (f"{source.path}: ", f"{source.path.resolve()}: "):
            message = message.removeprefix(prefix)
        return [], [Finding(CHECK, "unreadable", WARNING, f"schedules cannot read this file: {message}",
                            source.display, hint="its schedules are missing from the herd and DST analysis")]
    for note in inventory.warnings:
        line = None
        text = note.removeprefix(str(source.path))
        match = _WARNING_LINE.match(text)
        if match:
            line, text = int(match[1]), text[match.end():]
        findings.append(Finding(CHECK, "note", INFO, text.lstrip(": "), source.display, line=line))
    records = []
    for record in inventory.schedules:
        # Identities hash the file *name*; make them unique across directories.
        identity = sha256(f"{source.display}\0{record.id}".encode()).hexdigest()[:20]
        records.append((source, replace(record, id=identity)))
    return records, findings


def _per_file_notes(records, options) -> Iterator[Finding]:
    unsupported: dict[tuple, list] = {}
    assumed: dict[str, list] = {}
    for source, record in records:
        if record.enabled and not record.supported:
            unsupported.setdefault((source.display, record.error), []).append((source, record))
        if record.enabled and record.supported and record.timezone_assumed:
            assumed.setdefault(source.display, []).append(record)
    for (display, error), items in unsupported.items():
        source, first = items[0]
        yield Finding(CHECK, "not-forecast", INFO,
                      f"{len(items)} schedule(s) left out of the herd/DST forecast: {error}", display,
                      line=_line(first), node=_names(_label(source, record) for source, record in items))
    for display, items in assumed.items():
        yield Finding(CHECK, "no-timezone", INFO,
                      (f"{len(items)} schedule has" if len(items) == 1 else f"{len(items)} schedules have")
                      + f" no timezone in the export; assumed {options.default_timezone}",
                      display, hint="pass --default-timezone with your server's IANA zone if it is not "
                      f"{options.default_timezone}")


def _herds(active, options, analysis) -> Iterator[Finding]:
    start, end = analysis.horizon(options.from_date, HERD_DAYS)
    report = analysis.analyze([record for _, record in active], start, end, 60)
    peak = report["peak"]
    if peak < options.herd_threshold or not report["peak_at"]:
        return
    at = datetime.fromisoformat(report["peak_at"].replace("Z", "+00:00"))
    minute = int((at - start) // timedelta(minutes=1))
    budget = analysis.Budget()
    by_file: dict[str, list] = {}
    for source, record in active:
        fires, _ = analysis.forecast(record, start, end, budget)
        if minute in fires:
            by_file.setdefault(source.display, []).append((source, record))
    when = at.strftime("%a %H:%M UTC")
    where = f" across {len(by_file)} files" if len(by_file) > 1 else ""
    for display, items in sorted(by_file.items()):
        labels = _names(_label(source, record) for source, record in items)
        message = f"{peak} schedules start at once ({when}){where}: {labels}"
        yield Finding(CHECK, "herd", WARNING, message, display, line=_line(items[0][1]),
                      hint="spread them with `flowwrench schedules suggest PATH... --max-shift 10m` "
                      "(read-only plan)")


def _dst(active, options, analysis) -> Iterator[Finding]:
    start, end = analysis.horizon(options.from_date, DST_DAYS)
    hazards = analysis.dst([record for _, record in active], start, end)["hazards"]
    owners = {record.id: (source, record) for source, record in active}
    grouped: dict[tuple, list] = {}
    for hazard in hazards:
        grouped.setdefault((hazard["id"], hazard["hazard"]), []).append(hazard)
    for (identity, kind), items in grouped.items():
        source, record = owners[identity]
        first = min(items, key=lambda item: item["local_time"])
        day, _, clock = first["local_time"].partition("T")
        cron = record.cron()
        daily = len(cron.hours) * len(cron.minutes) == 1
        more = f" ({len(items)} such times in the next {DST_DAYS} days)" if len(items) > 1 else ""
        if kind == "skipped":
            effect = f"does not exist on {day} (DST gap): the run is skipped"
        else:
            effect = f"happens twice on {day} (DST overlap): may run twice"
        yield Finding(CHECK, f"dst-{kind}", WARNING if daily else INFO,
                      f"{clock} {record.timezone} {effect}{more}", source.display,
                      line=_line(record), node=_node(source, record),
                      hint="move it outside 01:00-03:00 local time, or schedule it in UTC")


def schedules(sources: list[Source], options) -> Iterator[Finding]:
    from flowwrench.schedules import analysis
    from flowwrench.schedules.extract import extract
    from flowwrench.schedules.model import InputError

    records = []
    for source in sources:
        found, findings = _extract(source, options, InputError, extract)
        records.extend(found)
        yield from findings
    yield from _per_file_notes(records, options)
    active = [(source, record) for source, record in records if record.enabled and record.supported]
    if not active:
        return
    for analyse in (_herds, _dst):
        try:
            # Materialise inside the try: analysis limits raise while iterating.
            results = list(analyse(active, options, analysis))
        except InputError as exc:
            results = [Finding(CHECK, "analysis-limit", INFO, f"schedule analysis stopped: {exc}")]
        yield from results
