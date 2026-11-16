"""Port of ``test/schedule-defaults.test.js``.

Original synthetic n8n exports following the pinned ScheduleTrigger rule format.
GenericFunctions.ts at f4a49c4 defaults minutesInterval to 5. Its SHA-256 phase
for synthetic-schedule-39:Clock is second=0; review-source-011:Clock has
second=3, minute=23, hour=3 (not midnight).
"""

from __future__ import annotations

import calendar
import json
import re
import sys
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from flowwrench.n8n2ap import InputError, convert

from .helpers import JsError, clone, coalesce, fixture, fixture_path, js, oracle, run_cli, run_code, same
from .schedule_monthly_cases import MONTHLY_CASES
from .schedule_weekly_cases import MISSING, WEEKLY_CASES, js_string

SCHEDULE_ORACLE = oracle("schedule-defaults")


def rule0(source: dict[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = source["nodes"][0]["parameters"]["rule"]["interval"][0]
    return rule


def test_omitted_minutes_interval_retains_five_minutes_for_a_representable_source_phase() -> None:
    source = fixture("schedule-default-minutes")
    before = clone(source)
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result["report"]["nodes"][0]["status"] == "converted"
    assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "*/5 * * * *"
    assert source == before
    explicit = clone(source)
    rule0(explicit)["minutesInterval"] = 5
    assert result == convert(explicit)


def test_pinned_preset_defaults_apply_across_supported_versions_including_null_interval() -> None:
    for version in [1, 1.1, 1.2, 1.3]:
        source = fixture("schedule-default-minutes")
        source["nodes"][0]["typeVersion"] = version
        rule0(source)["minutesInterval"] = None
        result = convert(source)
        assert result["report"]["complete"] is True
        assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "*/5 * * * *"


def assert_blocked(source: dict[str, Any], issue: str) -> None:
    result = convert(source)
    template, report = result["template"], result["report"]
    assert report["complete"] is False
    assert report["nodes"][0]["status"] == "partial"
    assert re.search(issue, " ".join(report["nodes"][0]["issues"]))
    trigger = template["flows"][0]["trigger"]
    assert trigger["type"] == "EMPTY"
    assert trigger["settings"] == {}
    assert template["flows"][0]["valid"] is False
    assert trigger["nextAction"]["name"] == "migration_guard"
    with pytest.raises(JsError, match="manual migration"):
        run_code(trigger["nextAction"])


def test_omitted_daily_clock_is_partial_and_blocked_instead_of_silently_becoming_midnight() -> None:
    assert_blocked(fixture("schedule-default-clock"), r"second.*five-field cron")


# Captured by scripts/schedule-source-oracle.js from hash-verified upstream
# withIntervalDefaults + toCronExpression, independent of the converter helpers.
@pytest.mark.parametrize("case", SCHEDULE_ORACLE["cases"],
                         ids=[f"{c['name']} => {c['sourceCron']}" for c in SCHEDULE_ORACLE["cases"]])
def test_source_oracle(case: dict[str, Any]) -> None:
    for type_version in [1, 1.1, 1.2, 1.3]:
        source = fixture("schedule-default-minutes")
        source["id"] = case["workflowId"]
        source["nodes"][0]["id"] = case["nodeId"]
        # The source ID, rather than the display name, controls the defaults.
        source["nodes"][0]["name"] = "Renamed schedule"
        source["nodes"][0]["typeVersion"] = type_version
        source["nodes"][0]["parameters"]["rule"]["interval"] = [clone(case["rule"])]
        before = clone(source)
        rule = case["rule"]
        if rule.get("field") in ("weeks", "months"):
            size = coalesce(rule.get("monthsInterval"), 1) if rule["field"] == "months" else coalesce(rule.get("weeksInterval"), 1)
            assert case["sourceRecurrence"]["activated"] is (not same(size, 1))
            if not same(size, 1):
                assert same(case["sourceRecurrence"]["intervalSize"], size)
        if not case["sourceCron"].startswith("0 "):
            assert_blocked(source, r"second.*five-field cron")
        else:
            result = convert(source)
            assert result["report"]["complete"] is True
            assert result["report"]["nodes"][0]["status"] == "converted"
            assert result["template"]["flows"][0]["valid"] is True
            assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == case["sourceCron"][2:]
            assert result == convert(source), "deterministic"
            serialized = js(result)
            assert case["workflowId"] not in serialized, "workflow ID stays local"
            assert case["nodeId"] not in serialized, "node ID stays local"
        assert source == before, "input remains unchanged"


IDENTITY_CASES = [(field, key, value) for field in ("hours", "days") for key in ("workflow", "node")
                  for value in (MISSING, "", None)
                  # Null workflow ID is allowed input; node IDs must be strings if present.
                  if not (key == "node" and value is None)]


@pytest.mark.parametrize(("field", "key", "value"), IDENTITY_CASES,
                         ids=[f"{f} omitted clock with {k} ID {js_string(v)}" for f, k, v in IDENTITY_CASES])
def test_omitted_clock_with_missing_identity_remains_blocked(field: str, key: str, value: Any) -> None:
    source = fixture("schedule-default-minutes")
    target = source if key == "workflow" else source["nodes"][0]
    if value is MISSING:
        del target["id"]
    else:
        target["id"] = value
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field}]
    assert_blocked(source, r"second.*source identity")
    issues = " ".join(convert(source)["report"]["nodes"][0]["issues"])
    assert re.search(r"triggerAtMinute.*source identity", issues)
    if field == "days":
        assert re.search(r"triggerAtHour.*source identity", issues)


INVALID_EXPLICIT = [{"field": "hours", "triggerAtMinute": 60}, {"field": "hours", "triggerAtMinute": "43"},
                    {"field": "days", "triggerAtHour": 24}, {"field": "days", "triggerAtMinute": -1}]


@pytest.mark.parametrize("rule", INVALID_EXPLICIT, ids=[js(r) for r in INVALID_EXPLICIT])
def test_invalid_explicit_clock_is_not_replaced_by_a_default(rule: dict[str, Any]) -> None:
    source = fixture("schedule-default-minutes")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [rule]
    assert_blocked(source, r"integer.*out of range")


@pytest.mark.parametrize(("field", "clock", "expected"), [
    ("days", {"triggerAtHour": 0, "triggerAtMinute": 0}, "0 0 * * *"),
    ("hours", {"triggerAtMinute": 0}, "0 */1 * * *"),
], ids=["days", "hours"])
def test_preset_preserves_explicit_zero_clock_fields(field: str, clock: dict[str, Any], expected: str) -> None:
    source = fixture("schedule-default-minutes")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field, **clock}]
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == expected


NONZERO = [{"field": "minutes"}, {"field": "hours", "triggerAtMinute": 0},
           {"field": "days", "triggerAtHour": 0, "triggerAtMinute": 0}]


@pytest.mark.parametrize("rule", NONZERO, ids=[r["field"] for r in NONZERO])
def test_nonzero_implicit_second_is_not_certified_as_complete(rule: dict[str, Any]) -> None:
    source = fixture("schedule-default-minutes")
    source["id"] = "review-source-011"
    source["nodes"][0]["parameters"]["rule"]["interval"] = [rule]
    assert_blocked(source, r"second.*five-field cron")


@pytest.mark.parametrize("key", ["workflow", "node"])
def test_missing_identity_requires_second_phase_review(key: str) -> None:
    source = fixture("schedule-default-minutes")
    if key == "workflow":
        del source["id"]
    else:
        del source["nodes"][0]["id"]
    assert_blocked(source, r"second.*source identity")


def test_explicit_zero_second_cron_needs_no_source_identity_or_inferred_clock() -> None:
    source = fixture("schedule-default-clock")
    del source["id"]
    del source["nodes"][0]["id"]
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": "cronExpression", "expression": "0 23 3 * * *"}]
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "23 3 * * *"


def test_strict_cli_writes_exact_default_schedules_and_blocked_nonzero_second_daily_artifacts(tmp_path: Path) -> None:
    for name, expected_exit, cron in [("schedule-default-minutes", 0, "*/5 * * * *"), ("schedule-default-hours", 0, "43 */1 * * *"),
                                      ("schedule-default-days", 0, "43 10 * * *"), ("schedule-default-clock", 2, None)]:
        output = tmp_path / f"{name}.json"
        result = run_cli("convert", fixture_path(name), "-o", output, "--strict")
        assert result.status == expected_exit, result.stderr
        template = json.loads(output.read_text(encoding="utf-8"))
        report = json.loads(Path(f"{output}.report.json").read_text(encoding="utf-8"))
        assert report["complete"] is (expected_exit == 0)
        if expected_exit == 0:
            assert template["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == cron
        else:
            assert template["flows"][0]["trigger"]["type"] == "EMPTY"
            assert re.search(r"second.*five-field cron", " ".join(report["nodes"][0]["issues"]))


# An omitted modern field selects days in the pinned withIntervalDefaults.
# Keep this normalization separate from legacy cron mode and explicit fields.
def test_omitted_modern_field_matches_the_daily_preset_without_changing_input() -> None:
    for days_interval in [MISSING, None, 1]:
        source = fixture("schedule-default-field")
        if days_interval is not MISSING:
            rule0(source)["daysInterval"] = days_interval
        before = clone(source)
        explicit = clone(source)
        rule0(explicit)["field"] = "days"
        result = convert(source)
        assert result["report"]["complete"] is True
        assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "43 10 * * *"
        assert result == convert(explicit)
        assert source == before


def _delete_workflow_id(s: dict[str, Any]) -> None:
    del s["id"]


def _delete_node_id(s: dict[str, Any]) -> None:
    del s["nodes"][0]["id"]


def _nonzero_second(s: dict[str, Any]) -> None:
    s["id"] = "review-source-011"


def _multi_day(s: dict[str, Any]) -> None:
    rule0(s)["daysInterval"] = 2


def _missing_timezone(s: dict[str, Any]) -> None:
    del s["settings"]["timezone"]


def _invalid_hour(s: dict[str, Any]) -> None:
    rule0(s)["triggerAtHour"] = 24


OMITTED_FIELD_FAILURES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("missing workflow ID", _delete_workflow_id, r"second.*source identity"),
    ("missing node ID", _delete_node_id, r"second.*source identity"),
    ("nonzero second", _nonzero_second, r"second.*five-field cron"),
    ("multi-day recurrence", _multi_day, r"cadence is unsupported"),
    ("missing timezone", _missing_timezone, r"timezone is unknown"),
    ("invalid explicit hour", _invalid_hour, r"integer.*out of range"),
]
DEFAULT_FIELD_FIXTURES = ["schedule-default-field", "schedule-default-null-field", "schedule-default-empty-field",
                          "schedule-default-unknown-field", "schedule-default-number-field", "schedule-default-false-field",
                          "schedule-default-true-field", "schedule-default-object-field",
                          "schedule-default-nonempty-object-field", "schedule-default-array-field",
                          "schedule-default-nonempty-array-field"]


@pytest.mark.parametrize(("change", "issue"), [(c, i) for _, c, i in OMITTED_FIELD_FAILURES],
                         ids=[n for n, _, _ in OMITTED_FIELD_FAILURES])
@pytest.mark.parametrize("fixture_name", DEFAULT_FIELD_FIXTURES)
def test_omitted_field_failure_remains_partial_and_blocked(fixture_name: str, change: Callable[[dict[str, Any]], None],
                                                           issue: str) -> None:
    source = fixture(fixture_name)
    change(source)
    assert_blocked(source, issue)


@pytest.mark.parametrize("field", ["seconds", "=hours", '={{ "hours" }}'])
def test_explicit_field_remains_unsupported(field: str) -> None:
    source = fixture("schedule-default-field")
    rule0(source)["field"] = field
    assert_blocked(source, r"cadence is unsupported")


def test_omitted_legacy_cron_mode_does_not_select_modern_daily_defaults() -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["type"] = "n8n-nodes-base.cron"
    source["nodes"][0]["typeVersion"] = 1
    source["nodes"][0]["parameters"] = {"triggerTimes": {"item": [{}]}}
    assert_blocked(source, r"cadence is unsupported")


@pytest.mark.parametrize("fixture_name", DEFAULT_FIELD_FIXTURES)
def test_strict_cli_writes_exact_daily_output_or_disabled_negative_artifacts(fixture_name: str, tmp_path: Path) -> None:
    def no_change(s: dict[str, Any]) -> None:
        return None

    cases: list[tuple[str, Callable[[dict[str, Any]], None], str | None]] = [("daily default", no_change, None),
                                                                            *OMITTED_FIELD_FAILURES]
    for name, change, issue in cases:
        source = fixture(fixture_name)
        change(source)
        input_path, output = tmp_path / f"{name}.n8n.json", tmp_path / f"{name}.json"
        input_path.write_text(json.dumps(source), encoding="utf-8")
        cli = run_cli("convert", input_path, "-o", output, "--strict")
        assert cli.status == (2 if issue else 0), cli.stderr or cli.stdout
        template = json.loads(output.read_text(encoding="utf-8"))
        report = json.loads(Path(f"{output}.report.json").read_text(encoding="utf-8"))
        assert report["complete"] is (not issue)
        assert template["flows"][0]["valid"] is (not issue)
        if issue:
            assert report["nodes"][0]["status"] == "partial"
            assert re.search(issue, " ".join(report["nodes"][0]["issues"]))
            assert template["flows"][0]["trigger"]["type"] == "EMPTY"
            assert template["flows"][0]["trigger"]["settings"] == {}
            assert template["flows"][0]["trigger"]["nextAction"]["name"] == "migration_guard"
        else:
            assert template["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "43 10 * * *"


# Null/empty/unrecognized string/numeric/boolean/plain object/array fields share the source default for omission,
# including null clock fields and explicit zero overrides. Compare full output, not only the cron.
CLOCKS: list[dict[str, Any]] = [{}, {"triggerAtHour": None, "triggerAtMinute": None}, {"triggerAtHour": 0},
                                {"triggerAtMinute": 0}, {"triggerAtHour": 0, "triggerAtMinute": 0}]


@pytest.mark.parametrize("fixture_name", DEFAULT_FIELD_FIXTURES[1:])
def test_field_fixture_matches_omitted_and_explicit_daily_presets_immutably(fixture_name: str) -> None:
    for days_interval in [MISSING, None, 1]:
        for clock in CLOCKS:
            source = fixture(fixture_name)
            rule = rule0(source)
            if days_interval is not MISSING:
                rule["daysInterval"] = days_interval
            rule.update(clock)
            before = clone(source)
            result = convert(source)
            assert result["report"]["complete"] is True
            minute = coalesce(clock.get("triggerAtMinute"), 43)
            hour = coalesce(clock.get("triggerAtHour"), 10)
            assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == f"{minute} {hour} * * *"
            for field in [MISSING, "days"]:
                equivalent = clone(source)
                if field is MISSING:
                    del rule0(equivalent)["field"]
                else:
                    rule0(equivalent)["field"] = field
                assert result == convert(equivalent)
            assert result == convert(source), "deterministic"
            assert source == before, "source remains unchanged"


def _assert_daily_default(field: Any) -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field}]
    before = clone(source)
    daily = clone(source)
    rule0(daily)["field"] = "days"
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == "43 10 * * *"
    assert result == convert(daily)
    assert result == convert(source), "deterministic"
    assert source == before, "input remains unchanged"


# Exact membership matters: case/whitespace variants and object-prototype names
# are unrecognized strings in the pinned source, not recognized preset kinds.
@pytest.mark.parametrize("field", ["HOURS", "days ", " cronExpression", "__proto__", "constructor", "toString", "0"])
def test_unrecognized_string_selects_daily_defaults(field: str) -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field}]
    before = clone(source)
    daily = clone(source)
    rule0(daily)["field"] = "days"
    assert convert(source)["report"]["complete"] is True
    assert convert(source) == convert(daily)
    assert source == before


# Numeric JSON fields are never members of the pinned string field set.
NUMERIC_FIELDS = [0, -0.0, 1, -1, 0.5, -0.5, sys.float_info.max, 5e-324]


@pytest.mark.parametrize("field", NUMERIC_FIELDS, ids=["0", "-0", "1", "-1", "0.5", "-0.5", "MAX_VALUE", "MIN_VALUE"])
def test_numeric_field_selects_daily_defaults_immutably(field: float) -> None:
    _assert_daily_default(field)


@pytest.mark.parametrize("field", [float("nan"), float("inf"), float("-inf")], ids=["NaN", "Infinity", "-Infinity"])
def test_non_finite_numeric_field_is_invalid_json_data(field: float) -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field}]
    with pytest.raises(InputError, match="Non-finite JSON number"):
        convert(source)


# Plain JSON records default as a whole; nested field names/values are not coerced.
PLAIN_OBJECTS: list[dict[str, Any]] = [{}, {"field": "hours"}, {"nested": {"field": "seconds"}, "list": ["days"]},
                                       {"toString": "hours"}]


@pytest.mark.parametrize("field", PLAIN_OBJECTS, ids=[js(f) for f in PLAIN_OBJECTS])
def test_plain_object_field_selects_daily_defaults_immutably(field: dict[str, Any]) -> None:
    _assert_daily_default(field)


def test_null_prototype_json_record_field_selects_daily_defaults_without_changing_its_prototype() -> None:
    # Python has no prototypes: a JSON record (``Object.create(null)`` in Node) is a plain dict.
    source = fixture("schedule-default-field")
    field = {"field": "hours"}
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": field}]
    before = js(source)
    daily = clone(source)
    rule0(daily)["field"] = "days"
    result = convert(source)
    assert result["report"]["complete"] is True
    assert result == convert(daily)
    assert result == convert(source), "deterministic"
    assert js(source) == before
    assert type(field) is dict


class _InheritedRecord(dict):  # type: ignore[type-arg]
    """``Object.create({ field: 'days' })``: no own keys, an inherited ``field``."""

    field = "days"


NON_PLAIN: list[tuple[str, Callable[[], Any]]] = [
    ("[object Date]", lambda: datetime.fromtimestamp(0, tz=timezone.utc)),
    ("[object Map]", OrderedDict),
    ("[object Object]", _InheritedRecord),
]


@pytest.mark.parametrize("make", [m for _, m in NON_PLAIN], ids=[n for n, _ in NON_PLAIN])
def test_non_plain_library_object_field_remains_partial(make: Callable[[], Any]) -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": make()}]
    assert_blocked(source, r"cadence is unsupported")


# A JSON array never equals a source string kind. In particular, singleton
# arrays must not become hourly/seconds/cron/expression fields through coercion.
JSON_ARRAYS: list[list[Any]] = [[], ["hours"], ["seconds"], ["cronExpression"], ["=hours"], [{}], [["days"]],
                                [None, False, 0, {"field": "weeks"}]]


@pytest.mark.parametrize("field", JSON_ARRAYS, ids=[js(f) for f in JSON_ARRAYS])
def test_json_array_field_selects_daily_defaults_immutably(field: list[Any]) -> None:
    _assert_daily_default(field)


class LibraryArray(list):  # type: ignore[type-arg]
    pass


def test_non_plain_library_array_subclass_remains_partial() -> None:
    source = fixture("schedule-default-field")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [{"field": LibraryArray(["hours"])}]
    assert_blocked(source, r"cadence is unsupported")


@pytest.mark.parametrize("case", WEEKLY_CASES, ids=[c["name"] for c in WEEKLY_CASES])
def test_weekly_contract(case: dict[str, Any]) -> None:
    source = fixture("schedule-default-weeks")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [clone(case["rule"])]
    if "change" in case:
        case["change"](source)
    before = clone(source)
    if case.get("issue"):
        assert_blocked(source, case["issue"])
    else:
        result = convert(source)
        assert result["report"]["complete"] is True
        assert result["report"]["nodes"][0]["status"] == "converted"
        assert result["template"]["flows"][0]["valid"] is True
        assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == case["cron"]
        assert result == convert(source), "deterministic"
    assert source == before, "input remains unchanged"


def test_weekly_omitted_null_defaults_match_explicit_sunday_and_one_week_output() -> None:
    for weeks_interval in [MISSING, None, 1]:
        for trigger_at_day in [MISSING, None, [0]]:
            for clock in CLOCKS:
                source = fixture("schedule-default-weeks")
                rule = rule0(source)
                if weeks_interval is not MISSING:
                    rule["weeksInterval"] = weeks_interval
                if trigger_at_day is not MISSING:
                    rule["triggerAtDay"] = clone(trigger_at_day)
                rule.update(clock)
                before = clone(source)
                explicit = clone(source)
                rule0(explicit).update({
                    "weeksInterval": 1, "triggerAtDay": [0], "triggerAtHour": coalesce(clock.get("triggerAtHour"), 10),
                    "triggerAtMinute": coalesce(clock.get("triggerAtMinute"), 43),
                })
                result = convert(source)
                assert result["report"]["complete"] is True
                assert result == convert(explicit)
                assert source == before


def test_weekly_sparse_library_weekday_arrays_stay_partial_instead_of_emitting_an_empty_cron_field() -> None:
    # ``Array(1)`` has a hole; a hole reads as ``undefined``, never an integer.
    hole = object()
    source = fixture("schedule-default-weeks")
    rule0(source)["triggerAtDay"] = [hole]
    assert_blocked(source, r"weekday")


@pytest.mark.parametrize("case", MONTHLY_CASES, ids=[c["name"] for c in MONTHLY_CASES])
def test_monthly_contract(case: dict[str, Any]) -> None:
    source = fixture("schedule-default-months")
    source["nodes"][0]["parameters"]["rule"]["interval"] = [clone(case["rule"])]
    if "change" in case:
        case["change"](source)
    before = clone(source)
    if case.get("issue"):
        assert_blocked(source, case["issue"])
    else:
        result = convert(source)
        assert result["report"]["complete"] is True
        assert result["report"]["nodes"][0]["status"] == "converted"
        assert result["template"]["flows"][0]["valid"] is True
        assert result["template"]["flows"][0]["trigger"]["settings"]["input"]["cronExpression"] == case["cron"]
        assert result == convert(source), "deterministic"
        derived = source.get("id") == "synthetic-clock-268"
        explicit = clone(source)
        rule = case["rule"]
        rule0(explicit).update({
            "monthsInterval": coalesce(rule.get("monthsInterval"), 1),
            "triggerAtDayOfMonth": coalesce(rule.get("triggerAtDayOfMonth"), 15 if derived else 9),
            "triggerAtHour": coalesce(rule.get("triggerAtHour"), 0 if derived else 10),
            "triggerAtMinute": coalesce(rule.get("triggerAtMinute"), 0 if derived else 43),
        })
        assert result == convert(explicit), "nullish defaults match explicit values"
    assert source == before, "input remains unchanged"


# Independent calendar contract for captured executions of the pinned source
# recurrenceCheck. Month steps start at January, not the activation month.
@pytest.mark.parametrize("proof", SCHEDULE_ORACLE["monthlyRecurrenceProof"],
                         ids=[f"{p['size']} months, day {p['day']}" for p in SCHEDULE_ORACLE["monthlyRecurrenceProof"]])
def test_source_monthly_recurrence(proof: dict[str, Any]) -> None:
    expected: list[int] = []
    skipped: list[dict[str, int]] = []
    for year in range(2023, 2029):
        for month in range(1, 13):
            if (month - 1) % proof["size"] != 0:
                continue
            if calendar.monthrange(year, month)[1] < proof["day"]:
                skipped.append({"year": year, "month": month, "day": proof["day"]})
            else:
                expected.append(year * 12 + month - 1)
    assert proof["absoluteMonths"] == expected
    assert proof["skipped"] == skipped
    assert proof["allCandidatesAccepted"] is True
    assert proof["allInitialAndNullAccepted"] is True
    assert proof["allDuplicatesRejected"] is True
    assert proof["recurrence"] == {"activated": True, "index": 0, "intervalSize": proof["size"], "typeInterval": "months"}
