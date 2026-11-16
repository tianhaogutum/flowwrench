"""Port of ``test/schedule-weekly-cases.js``.

Shared library/installed-CLI contract inputs. Expected crons also have an
independent pinned-source oracle in ``oracles/schedule-defaults.json``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .helpers import js

MISSING: Any = object()  # JavaScript ``undefined``: the property is deleted


def js_string(value: Any) -> str:
    """``String(id)`` for the identity case names."""
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    return str(value)


def set_identity(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(s: dict[str, Any]) -> None:
        target = s if key == "workflow" else s["nodes"][0]
        if value is MISSING:
            target.pop("id", None)
        else:
            target["id"] = value
    return change


def set_source_id(value: str) -> Callable[[dict[str, Any]], None]:
    def change(s: dict[str, Any]) -> None:
        s["id"] = value
    return change


def zero_derived_clock(s: dict[str, Any]) -> None:
    s["id"] = "synthetic-clock-268"
    s["nodes"][0]["id"] = "source-clock-id"


def delete_timezone(s: dict[str, Any]) -> None:
    del s["settings"]["timezone"]


IDENTITY_VARIANTS = [(key, value) for key in ("workflow", "node") for value in (MISSING, "", None)
                     if not (key == "node" and value is None)]
INVALID_CLOCKS = [(key, value) for key in ("triggerAtHour", "triggerAtMinute")
                  for value in (24 if key == "triggerAtHour" else 60, -1, "0", False, {}, [], 1.5, "=0")]

WEEKLY_CASES: list[dict[str, Any]] = [
    {"name": "omitted defaults", "rule": {"field": "weeks"}, "cron": "43 10 * * 0"},
    {"name": "null interval", "rule": {"field": "weeks", "weeksInterval": None}, "cron": "43 10 * * 0"},
    {"name": "null weekdays", "rule": {"field": "weeks", "triggerAtDay": None}, "cron": "43 10 * * 0"},
    {"name": "null defaults", "rule": {"field": "weeks", "weeksInterval": None, "triggerAtDay": None, "triggerAtHour": None,
                                       "triggerAtMinute": None}, "cron": "43 10 * * 0"},
    {"name": "explicit one", "rule": {"field": "weeks", "weeksInterval": 1}, "cron": "43 10 * * 0"},
    {"name": "weekday and clock overrides", "rule": {"field": "weeks", "triggerAtDay": [1, 3, 5], "triggerAtHour": 8,
                                                     "triggerAtMinute": 17}, "cron": "17 8 * * 1,3,5"},
    {"name": "midnight Sunday", "rule": {"field": "weeks", "triggerAtDay": [0], "triggerAtHour": 0, "triggerAtMinute": 0},
     "cron": "0 0 * * 0"},
    {"name": "empty weekdays wildcard", "rule": {"field": "weeks", "triggerAtDay": []}, "cron": "43 10 * * *"},
    {"name": "empty weekdays midnight", "rule": {"field": "weeks", "triggerAtDay": [], "triggerAtHour": 0, "triggerAtMinute": 0},
     "cron": "0 0 * * *"},
    {"name": "unsorted duplicate weekdays", "rule": {"field": "weeks", "triggerAtDay": [6, 0, 6]}, "cron": "43 10 * * 6,0,6"},
    {"name": "null hour explicit minute", "rule": {"field": "weeks", "triggerAtHour": None, "triggerAtMinute": 0},
     "cron": "0 10 * * 0"},
    {"name": "null minute explicit hour", "rule": {"field": "weeks", "triggerAtHour": 0, "triggerAtMinute": None},
     "cron": "43 0 * * 0"},
    {"name": "zero derived clock", "rule": {"field": "weeks"}, "change": zero_derived_clock, "cron": "0 0 * * 0"},
    *[{"name": f"unsupported interval {js(v)}", "rule": {"field": "weeks", "weeksInterval": v}, "issue": "cadence is unsupported"}
      for v in (0, 2, -1, 1.5, "1", False, [], {}, "=1")],
    *[{"name": f"invalid weekdays {js(v)}", "rule": {"field": "weeks", "triggerAtDay": v}, "issue": "weekday"}
      for v in ("0", 0, False, {}, [7], [-1], [1.5], ["1"], [None], [True], [{}], [[]], ["=1"])],
    *[{"name": f"invalid {key} {js(value)}", "rule": {"field": "weeks", key: value}, "issue": "integer.*out of range"}
      for key, value in INVALID_CLOCKS],
    *[{"name": f"missing {key} identity {js_string(value)}", "rule": {"field": "weeks", "triggerAtHour": 0, "triggerAtMinute": 0},
       "change": set_identity(key, value), "issue": "second.*source identity"} for key, value in IDENTITY_VARIANTS],
    {"name": "nonzero second explicit clocks", "rule": {"field": "weeks", "triggerAtHour": 0, "triggerAtMinute": 0},
     "change": set_source_id("review-source-011"), "issue": "second.*five-field cron"},
    {"name": "nonzero second derived clocks", "rule": {"field": "weeks"}, "change": set_source_id("review-source-011"),
     "issue": "second.*five-field cron"},
    {"name": "missing timezone", "rule": {"field": "weeks"}, "change": delete_timezone, "issue": "timezone is unknown"},
    *[{"name": f"unsupported field {field}", "rule": {"field": field}, "issue": "cadence is unsupported"}
      for field in ("seconds", "=weeks", '={{ "weeks" }}')],
]
