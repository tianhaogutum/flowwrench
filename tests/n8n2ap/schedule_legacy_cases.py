"""Port of ``test/schedule-legacy-cases.js``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from .helpers import JsError, clone, fixture, run_code

RANDOM_SECOND_ISSUE = r"Legacy cron preset.*runtime-random second.*export"

_PRESETS: list[dict[str, Any]] = [
    {"mode": "everyMinute"},
    {"mode": "everyHour", "minute": 17},
    {"mode": "everyDay", "hour": 8, "minute": 17},
    {"mode": "everyWeek", "hour": 8, "minute": 17, "weekday": 2},
    *[rule for mode in ("everyHour", "everyDay", "everyWeek") for rule in (
        {"mode": mode},
        {"mode": mode, "hour": 0, "minute": 0, "weekday": 0},
        {"mode": mode, "hour": None, "minute": None, "weekday": None},
    )],
    # A custom expression on a preset does not replace its random-second branch.
    {"mode": "everyDay", "hour": 8, "minute": 17, "cronExpression": "0 17 8 * * *"},
]

LEGACY_CASES: list[dict[str, Any]] = [
    *[{"name": f"preset {i} {rule['mode']}: {identity}", "rule": rule, "identity": identity, "issue": RANDOM_SECOND_ISSUE}
      for i, rule in enumerate(_PRESETS)
      for identity in ("modern zero-second IDs", "missing IDs", "modern nonzero-second IDs")],
    *[{"name": f"custom {name}", "rule": {"mode": "custom", "cronExpression": expression}, "identity": "missing IDs", "cron": cron}
      for name, expression, cron in [
          ("five fields", "17 8 * * 1-5", "17 8 * * 1-5"),
          ("zero seconds", "0 17 8 * * 1-5", "17 8 * * 1-5"),
          ("midnight", "0 0 0 * * *", "0 0 * * *"),
          ("numeric ranges and steps", "0 */10 8-17 * * 1,3,5", "*/10 8-17 * * 1,3,5"),
          ("whitespace", "  0  17 8 * * 1-5  ", "17 8 * * 1-5"),
      ]],
    *[{"name": f"invalid custom {expression}", "rule": {"mode": "custom", "cronExpression": expression},
       "issue": "Only numeric five-field cron"} for expression in ("3 17 8 * * *", "* * * * * *", "17 24 * * *", "@daily")],
    {"name": "missing mode", "rule": {}, "issue": "cadence is unsupported"},
    {"name": "unsupported everyX", "rule": {"mode": "everyX", "unit": "minutes", "value": 5},
     "issue": "Unsupported cron option|cadence is unsupported"},
    {"name": "unsupported everyMonth", "rule": {"mode": "everyMonth", "hour": 8, "minute": 17, "dayOfMonth": 9},
     "issue": "Unsupported cron option|cadence is unsupported"},
]


def legacy_source(c: dict[str, Any]) -> dict[str, Any]:
    source = fixture("legacy-cron-daily")
    source["nodes"][0]["parameters"]["triggerTimes"]["item"] = [clone(c["rule"])]
    if c.get("identity") == "missing IDs":
        del source["id"]
        del source["nodes"][0]["id"]
    if c.get("identity") == "modern nonzero-second IDs":
        source["id"] = "review-source-011"
    return source


def assert_legacy_result(result: dict[str, Any], c: dict[str, Any]) -> None:
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
        assert trigger["settings"]["input"]["cronExpression"] == c["cron"]
        assert trigger["settings"]["input"]["timezone"] == "UTC"
