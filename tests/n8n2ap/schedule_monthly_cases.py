"""Port of ``test/schedule-monthly-cases.js``.

Shared library / freshly installed CLI contracts. Valid crons are independently
captured from hash-verified pinned source helpers in ``oracles/schedule-defaults.json``.
"""

from __future__ import annotations

from typing import Any

from .helpers import js, same
from .schedule_weekly_cases import (
    IDENTITY_VARIANTS,
    INVALID_CLOCKS,
    MISSING,
    delete_timezone,
    js_string,
    set_identity,
    set_source_id,
    zero_derived_clock,
)


def _rule(**fields: Any) -> dict[str, Any]:
    return {"field": "months", **{key: value for key, value in fields.items() if value is not MISSING}}


_EXPLICIT_CLOCK = {"triggerAtDayOfMonth": 1, "triggerAtHour": 0, "triggerAtMinute": 0}

MONTHLY_CASES: list[dict[str, Any]] = [
    *[{"name": f"interval {js_string(interval)} day {js_string(day)}",
       "rule": _rule(monthsInterval=interval, triggerAtDayOfMonth=day), "cron": "43 10 9 */1 *"}
      for interval in (MISSING, None, 1) for day in (MISSING, None, 9)],
    *[{"name": f"explicit day {day} and clock",
       "rule": _rule(triggerAtDayOfMonth=day, triggerAtHour=8, triggerAtMinute=17), "cron": f"17 8 {day} */1 *"}
      for day in (1, 28, 29, 30, 31)],
    {"name": "all null defaults", "rule": _rule(monthsInterval=None, triggerAtDayOfMonth=None, triggerAtHour=None,
                                                triggerAtMinute=None), "cron": "43 10 9 */1 *"},
    {"name": "midnight", "rule": _rule(triggerAtHour=0, triggerAtMinute=0), "cron": "0 0 9 */1 *"},
    {"name": "null hour zero minute", "rule": _rule(triggerAtHour=None, triggerAtMinute=0), "cron": "0 10 9 */1 *"},
    {"name": "null minute zero hour", "rule": _rule(triggerAtHour=0, triggerAtMinute=None), "cron": "43 0 9 */1 *"},
    {"name": "zero derived clocks and distinct day", "rule": _rule(), "change": zero_derived_clock, "cron": "0 0 15 */1 *"},
    *[{"name": f"unsupported interval {js(v)}", "rule": _rule(monthsInterval=v), "issue": "cadence is unsupported"}
      for v in (0, 5, 7, 8, 9, 10, 11, 13, 24, -1, 1.5, "1", "2", True, False, [], [2], {}, "=1", "={{ 2 }}")],
    *[{"name": f"invalid day {js(v)}", "rule": _rule(triggerAtDayOfMonth=v), "issue": "integer.*out of range"}
      for v in (0, 32, -1, 1.5, "9", False, True, [], [9], {}, "=9", "={{ 9 }}")],
    *[{"name": f"invalid {key} {js(value)}", "rule": _rule(**{key: value}), "issue": "integer.*out of range"}
      for key, value in INVALID_CLOCKS],
    *[{"name": f"missing {key} identity {js_string(value)} explicit {'true' if explicit else 'false'}",
       "rule": _rule(**(_EXPLICIT_CLOCK if explicit else {})), "change": set_identity(key, value),
       "issue": "second.*source identity"}
      for key, value in IDENTITY_VARIANTS for explicit in (False, True)],
    *[{"name": f"nonzero second explicit {'true' if explicit else 'false'}", "rule": _rule(**(_EXPLICIT_CLOCK if explicit else {})),
       "change": set_source_id("review-source-011"), "issue": "second.*five-field cron"} for explicit in (False, True)],
    {"name": "missing timezone", "rule": _rule(), "change": delete_timezone, "issue": "timezone is unknown"},
    *[{"name": f"unsupported field {field}", "rule": {"field": field}, "issue": "cadence is unsupported"}
      for field in ("seconds", "=months", '={{ "months" }}')],
]

# Reuse the single-month day/clock/identity contracts for every accepted
# divisor. Intervals are not coerced; the exact source */N field survives.
_ONE_MONTH_CONTRACTS = [c for c in MONTHLY_CASES
                        if c["rule"].get("monthsInterval") is None or same(c["rule"].get("monthsInterval"), 1)]
for _months in (2, 3, 4, 6, 12):
    for _c in [c for c in _ONE_MONTH_CONTRACTS if c["rule"]["field"] == "months"]:
        _case = {**_c, "name": f"divisor {_months}: {_c['name']}", "rule": {**_c["rule"], "monthsInterval": _months}}
        if "cron" in _c:
            _case["cron"] = _c["cron"].replace("*/1", f"*/{_months}", 1)
        MONTHLY_CASES.append(_case)
