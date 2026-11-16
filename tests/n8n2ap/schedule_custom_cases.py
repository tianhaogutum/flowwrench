"""Port of ``test/schedule-custom-cases.js``.

A case's ``timezone`` key is absent for JavaScript ``undefined`` (UTC) and
``None`` for ``null`` (the workflow timezone setting is deleted).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from .helpers import JsError, node, oracle, run_code, workflow

NAMED_WEEKDAYS = [
    ("SUN", "0"), ("MON", "1"), ("TUE", "2"), ("WED", "3"), ("THU", "4"), ("FRI", "5"), ("SAT", "6"),
    ("MON-FRI", "1-5"), ("SUN-SAT", "0-7"), ("SUN-SUN", "0-0"), ("SAT-SAT", "6-6"),
    ("MON,WED,FRI", "1,3,5"), ("FRI,MON,MON", "5,1,1"), ("mOn-fRi", "1-5"),
    ("sun,7,MON-5", "0,7,1-5"), ("1-FRI,SAT", "1-5,6"), ("MON,*/2", "1,*/2"), ("SUN,MON,TUE,WED,THU,FRI,SAT", "0-7"),
    ("MON,*", "0-7"),
]

# Captured from the real pinned parsers, independently of converter expansion.
_NUMERIC_ORACLE = oracle("cron-numeric-week")
NUMERIC_WEEK_CASES: list[dict[str, Any]] = [
    {
        "name": f"numeric {c['expression']} / {s['prefix']} / {s['tz']}",
        "expression": f"{'0 ' if c['expression'].startswith('0 ') else ''}{s['prefix']} {c['expression'].split(' ')[-1]}",
        "cron": f"{s['prefix']} {c['canonical'].split(' ')[-1]}",
        "timezone": s["tz"],
    }
    for c in _NUMERIC_ORACLE["cases"]
    for s in c["scenarios"]
    if s["start"] == "2024-03-24T00:00:00Z"
]

_UNSUPPORTED = [
    "17 8 * * FUNDAY", "17 8 * * MONDAY", "17 8 * * MÓN", "17 8 * * JAN",
    "17 8 * * FRI-MON", "17 8 * * SAT-SUN", "17 8 * * MON/2", "17 8 * * MON-FRI/2",
    "17 8 * * MON#2", "17 8 * * MONL", "17 8 * * ?", "17 8 * * MON,,FRI",
    "17 8 * * ,MON", "17 8 * * MON-", "17 8 * * MON-8", "17 8 * * MON_FRI",
    "17 8 * JAN MON", "MON 8 * * 1", "17 MON * * 1", "17 8 MON * 1",
    "MON 17 8 * * 1", "3 17 8 * * MON", "* 17 8 * * MON", "00 17 8 * * MON",
    "0 17 8 * * MON 2026", "=0 17 8 * * MON", '={{ "0 17 8 * * MON" }}', "@weekly",
]
_ONLY_NUMERIC = "Only numeric five-field cron"

CUSTOM_CASES: list[dict[str, Any]] = [
    *NUMERIC_WEEK_CASES,
    *[{"name": f"{'six' if seconds else 'five'} fields {day}", "expression": f"{seconds}17 8 * * {day}",
       "cron": f"17 8 * * {numeric}"}
      for day, numeric in NAMED_WEEKDAYS for seconds in ("", "0 ")],
    {"name": "full week with constrained day-of-month", "expression": "0 17 8 15 3 SUN-SAT", "cron": "17 8 15 3 0-7"},
    {"name": "numeric unchanged", "expression": "0 */10 8-17 * */2 1,3,5", "cron": "*/10 8-17 * */2 1,3,5"},
    {"name": "numeric weekday step unchanged", "expression": "17 8 * * 1-5/2", "cron": "17 8 * * 1-5/2"},
    {"name": "whitespace and timezone", "expression": "  0  17 8 * * mon,wed,fri  ", "cron": "17 8 * * 1,3,5",
     "timezone": "Europe/Berlin"},
    *[{"name": f"unsupported {expression}", "expression": expression, "issue": _ONLY_NUMERIC} for expression in _UNSUPPORTED],
    *[{"name": f"invalid numeric full-week boundary {day}", "expression": f"17 8 15 * {day}", "issue": _ONLY_NUMERIC}
      for day in ("6-0", "0-8", "0-6/0", "0-6,", "0-6,BOGUS")],
    *[{"name": f"unsupported numeric full-week seconds {second}", "expression": f"{second} 17 8 15 * 0-6",
       "issue": _ONLY_NUMERIC} for second in ("3", "*", "00")],
    {"name": "numeric full-week unknown timezone", "expression": "0 17 8 15 * 0-6", "timezone": "Unknown/Zone",
     "issue": "Invalid schedule timezone"},
    {"name": "unknown timezone", "expression": "0 17 8 * * MON", "timezone": "Unknown/Zone", "issue": "Invalid schedule timezone"},
    {"name": "missing timezone", "expression": "0 17 8 * * MON", "timezone": None, "issue": "timezone is unknown"},
]


def custom_source(kind: str, c: dict[str, Any]) -> dict[str, Any]:
    if kind == "cron":
        parameters: dict[str, Any] = {"triggerTimes": {"item": [{"mode": "custom", "cronExpression": c["expression"]}]}}
    else:
        parameters = {"rule": {"interval": [{"field": "cronExpression", "expression": c["expression"]}]}}
    source = workflow(node("Clock", kind, parameters, 1 if kind == "cron" else 1.3))
    del source["nodes"][0]["id"]  # Explicit expressions must not require preset identity.
    if "timezone" in c and c["timezone"] is None:
        del source["settings"]["timezone"]
    else:
        source["settings"]["timezone"] = c["timezone"] if c.get("timezone") is not None else "UTC"
    return source


def assert_custom_result(result: dict[str, Any], c: dict[str, Any]) -> None:
    template, report = result["template"], result["report"]
    issue = c.get("issue")
    assert report["complete"] is (not issue)
    assert report["nodes"][0]["status"] == ("partial" if issue else "converted")
    assert template["flows"][0]["valid"] is (not issue)
    trigger = template["flows"][0]["trigger"]
    if issue:
        assert re.search(issue, " ".join(report["nodes"][0]["issues"]))
        assert trigger["type"] == "EMPTY"
        assert trigger["valid"] is False
        assert trigger["settings"] == {}
        assert trigger["nextAction"]["name"] == "migration_guard"
        with pytest.raises(JsError, match="manual migration"):
            run_code(trigger["nextAction"])
    else:
        assert trigger["type"] == "PIECE_TRIGGER"
        assert trigger["valid"] is True
        assert trigger["settings"]["triggerName"] == "cron_expression"
        assert trigger["settings"]["input"]["cronExpression"] == c["cron"]
        assert trigger["settings"]["input"]["timezone"] == (c["timezone"] if c.get("timezone") is not None else "UTC")
