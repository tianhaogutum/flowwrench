"""Case files, assertions, coverage and JUnit (port of ``lib/cases.js``)."""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .errors import FormatError
from .fileio import read_data, require_keys, require_object, resolve_path
from .jsvalues import (
    UNDEFINED,
    deep_equal,
    is_finite_number,
    is_number,
    is_safe_integer,
    js_items,
    js_string,
    stringify,
)
from .runner import OPTION_KEYS, execute_flow, resolve_options
from .validate import FlowModel, validate_flow

Entry = dict[str, Any]
Suite = dict[str, Any]

EXPECT_KEYS = (
    "status", "result", "results", "args", "path", "error", "failure_error", "mock_errors", "mock_usage",
    "mock_calls", "failure_result", "stopped", "simulated_delay_seconds", "retry_delays", "attempt_errors",
    "attempt_results", "branch_choices", "skipped_steps",
)
_LIST_ASSERTIONS = (
    "mock_errors", "mock_calls", "retry_delays", "attempt_errors", "attempt_results", "branch_choices", "skipped_steps",
)
_USAGE_FIELDS = ("expected", "calls", "consumed", "remaining", "extra")
_CASE_KEYS = ("name", "flow", "input", "mocks", "expect", "options")


def _known_module(model: FlowModel, module_id: Any) -> bool:
    return isinstance(module_id, str) and module_id in model.modules


def expect_shape(expected: Any, model: FlowModel, label: str) -> None:
    """Validate an ``expect`` mapping (format errors, never assertion failures)."""
    require_keys(expected, EXPECT_KEYS, label)
    if not expected:
        raise FormatError(f"{label}: at least one assertion required")
    if "status" in expected and not (isinstance(expected["status"], str) and expected["status"] in ("ok", "error")):
        raise FormatError(f"{label}.status: expected ok or error")
    if "stopped" in expected and not isinstance(expected["stopped"], bool):
        raise FormatError(f"{label}.stopped: expected boolean")
    if "simulated_delay_seconds" in expected:
        delay = expected["simulated_delay_seconds"]
        if not is_finite_number(delay) or delay < 0:
            raise FormatError(f"{label}.simulated_delay_seconds: expected nonnegative finite number")
    if "path" in expected and (
        not isinstance(expected["path"], list) or any(not _known_module(model, item) for item in expected["path"])
    ):
        raise FormatError(f"{label}.path: expected array of known module ids")
    for name in _LIST_ASSERTIONS:
        if name not in expected:
            continue
        if not isinstance(expected[name], list):
            raise FormatError(f"{label}.{name}: expected array")
        for index, record in enumerate(expected[name]):
            require_object(record, f"{label}.{name}[{index}]")
    if "mock_usage" in expected:
        usage_label = f"{label}.mock_usage"
        require_object(expected["mock_usage"], usage_label)
        if not expected["mock_usage"]:
            raise FormatError(f"{usage_label}: at least one module assertion required")
        for module_id, record in js_items(expected["mock_usage"]):
            if not _known_module(model, module_id):
                raise FormatError(f"{usage_label}: unknown module {module_id}")
            require_object(record, f"{usage_label}.{module_id}")
            # Missing/extra fields are exact-comparison failures, like mock_errors.
            for name in _USAGE_FIELDS:
                if name in record and (not is_safe_integer(record[name]) or record[name] < 0):
                    raise FormatError(f"{usage_label}.{module_id}.{name}: expected nonnegative safe integer")
    for name in ("error", "failure_error"):
        if name not in expected:
            continue
        error, error_label = expected[name], f"{label}.{name}"
        require_keys(error, ("name", "message", "step_id", "call", "expected_args", "actual_args"), error_label)
        if not error:
            raise FormatError(f"{error_label}: at least one assertion required")
        for name_field in ("name", "message", "step_id"):
            if name_field in error and not isinstance(error[name_field], str):
                raise FormatError(f"{error_label}.{name_field}: expected string")
        if "call" in error and (not is_safe_integer(error["call"]) or error["call"] < 1):
            raise FormatError(f"{error_label}.call: expected positive safe integer")
        for args_field in ("expected_args", "actual_args"):
            if args_field in error:
                require_object(error[args_field], f"{error_label}.{args_field}")
    for name in ("results", "args"):
        if name not in expected:
            continue
        require_object(expected[name], f"{label}.{name}")
        for module_id, value in js_items(expected[name]):
            if not _known_module(model, module_id):
                raise FormatError(f"{label}.{name}: unknown module {module_id}")
            if name == "args" and not isinstance(value, list):
                raise FormatError(f"{label}.args.{module_id}: expected an array of invocation arguments")


def iterate_cases(paths: list[str], cli_options: dict[str, Any] | None = None) -> Iterator[Entry]:
    """Load cases lazily so a completed case can be checkpointed before the next."""
    cli_options = {} if cli_options is None else cli_options
    if not paths:
        raise FormatError("test requires at least one case path")
    for path in paths:
        case = read_data(path)
        require_keys(case, _CASE_KEYS, f"case {path}")
        flow_ref = case.get("flow", UNDEFINED)
        if not isinstance(flow_ref, str) or not flow_ref:
            raise FormatError(f"case {path}: flow must be a relative file path")
        if "name" in case and not isinstance(case["name"], str):
            raise FormatError(f"case {path}: name must be a string")
        base = posixpath.dirname(resolve_path(path))
        flow_path = resolve_path(base, flow_ref)
        flow = read_data(flow_path)
        model = validate_flow(flow)
        expect_shape(case.get("expect", UNDEFINED), model, f"case {path}.expect")
        input_ref = case.get("input")
        flow_input = read_data(resolve_path(base, input_ref), json=True) if isinstance(input_ref, str) else (
            {} if input_ref is None else input_ref
        )
        mocks_ref = case.get("mocks")
        mocks = read_data(resolve_path(base, mocks_ref)) if isinstance(mocks_ref, str) else (
            {} if mocks_ref is None else mocks_ref
        )
        if "options" in case:
            require_keys(case["options"], OPTION_KEYS, f"case {path}.options")
        name = case.get("name")
        yield {
            "name": path if name is None else name,
            "path": path,
            "flowPath": os.path.realpath(flow_path, strict=True),
            "snapshot": {
                "flow": flow,
                "input": flow_input,
                "mocks": mocks,
                "expect": case["expect"],
                "options": resolve_options({**case.get("options", {}), **cli_options}),
            },
        }


def load_cases(paths: list[str], cli_options: dict[str, Any] | None = None) -> list[Entry]:
    return list(iterate_cases(paths, cli_options))


def _project(trace: list[dict[str, Any]], owns: Callable[[dict[str, Any]], bool], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{name: event[name] for name in fields} for event in trace if owns(event)]


def _failures(report: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def compare(label: str, actual: Any, wanted: Any) -> None:
        if not deep_equal(actual, wanted):
            shown = stringify(actual)
            failures.append(f"{label}: expected {stringify(wanted)}, got {'missing' if shown is None else shown}")

    trace = report["trace"]
    # Unasserted errors must fail; an error expectation opts into a failed run.
    expected_status = expected.get("status")
    if expected_status is None:
        expected_status = "error" if "error" in expected else "ok"
    compare("status", report["status"], expected_status)
    if expected_status == "ok" and report["status"] == "error":
        for error in report.get("mock_errors", []):
            failures.append(f"{js_string(error['name'])} at '{js_string(error['step_id'])}': {js_string(error['message'])}")
    for name in ("result", "failure_result", "stopped"):
        if name in expected:
            actual = report.get(name, False) if name == "stopped" else report.get(name, UNDEFINED)
            compare(name, actual, expected[name])
    # Numeric equality is exact (no tolerance/coercion); either signed zero is 0.
    if "simulated_delay_seconds" in expected:
        wanted = expected["simulated_delay_seconds"]
        actual = report["simulated_delay_seconds"]
        if not (is_number(wanted) and actual == wanted):
            compare("simulated_delay_seconds", actual, wanted)
    for module_id, value in js_items(expected.get("results", {})):
        compare(f"results.{module_id}", report["results"].get(module_id, UNDEFINED), value)
    for module_id, value in js_items(expected.get("args", {})):
        actual_args = [event["args"] for event in trace if event["module_id"] == module_id and "args" in event]
        compare(f"args.{module_id}", actual_args, value)
    if "path" in expected:
        compare("path", report["executed"], expected["path"])
    for name in ("error", "failure_error"):
        recorded = report.get(name, UNDEFINED)
        for field_name, value in js_items(expected.get(name, {})):
            actual = recorded.get(field_name, UNDEFINED) if isinstance(recorded, dict) else UNDEFINED
            compare(f"{name}.{field_name}", actual, value)
    if "mock_errors" in expected:
        compare("mock_errors", report.get("mock_errors", []), expected["mock_errors"])
    if "mock_calls" in expected:
        calls = [{"module_id": event["module_id"], **event["mock"]} for event in trace if event.get("mock")]
        compare("mock_calls", calls, expected["mock_calls"])
    if "retry_delays" in expected:
        compare("retry_delays", _project(trace, lambda event: "retry_delay_seconds" in event,
                                         ("module_id", "attempt", "retry_delay_seconds")), expected["retry_delays"])
    if "attempt_errors" in expected:
        compare("attempt_errors", _project(trace, lambda event: "error" in event,
                                           ("module_id", "attempt", "error")), expected["attempt_errors"])
    if "attempt_results" in expected:
        compare("attempt_results", _project(trace, lambda event: "result" in event,
                                            ("module_id", "attempt", "result")), expected["attempt_results"])
    if "branch_choices" in expected:
        compare("branch_choices", _project(trace, lambda event: "branch" in event,
                                           ("module_id", "attempt", "branch")), expected["branch_choices"])
    if "skipped_steps" in expected:
        compare("skipped_steps", _project(trace, lambda event: event.get("status") == "skipped",
                                          ("module_id", "attempt", "path")), expected["skipped_steps"])
    for module_id, value in js_items(expected.get("mock_usage", {})):
        compare(f"mock_usage.{module_id}", report["mock_usage"].get(module_id, UNDEFINED), value)
    return failures


def run_case_entries(
    entries: Iterable[Entry],
    on_case: Callable[[Entry, Suite], None] | None = None,
    on_invalid_case: Callable[[FormatError], None] | None = None,
) -> Suite:
    """Run loaded (or replayed) cases; call ``on_case`` after each completed case.

    ``on_invalid_case`` receives a FormatError raised while loading, validating
    or starting a case; errors raised by ``on_case`` itself are never reported
    as invalid requested cases.
    """
    outcomes: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, Any]] = {}

    def suite() -> Suite:
        return {
            "cases": outcomes,
            "passed": sum(1 for outcome in outcomes if outcome["passed"]),
            "failed": sum(1 for outcome in outcomes if not outcome["passed"]),
            "coverage": [
                {
                    "flow": record["flow"],
                    "modules": {"executed": sorted(record["modules"]), "total": record["module_total"]},
                    "branches": {"executed": sorted(record["branches"]), "total": record["branch_total"]},
                }
                for record in coverage.values()
            ],
        }

    iterator = iter(entries)
    while True:
        try:
            try:
                entry = next(iterator)
            except StopIteration:
                break
            snapshot = entry["snapshot"]
            flow = snapshot.get("flow", UNDEFINED)
            expected = snapshot.get("expect", UNDEFINED)
            model = validate_flow(flow)
            expect_shape(expected, model, f"case {js_string(entry.get('path', UNDEFINED))}.expect")
            report = execute_flow(
                flow,
                snapshot.get("input", UNDEFINED),
                snapshot.get("mocks", UNDEFINED),
                snapshot.get("options", UNDEFINED),
            )
        except FormatError as error:
            if on_invalid_case is not None:
                on_invalid_case(error)
            raise
        failures = _failures(report, expected)
        outcomes.append({
            "name": entry["name"], "path": entry["path"], "passed": not failures, "failures": failures, "report": report,
        })
        key = entry["flowPath"]
        record = coverage.get(key)
        if record is None:
            record = {
                "flow": key, "modules": set(), "branches": set(),
                "module_total": len(model.modules), "branch_total": len(model.branches),
            }
        record["modules"].update(report["coverage"]["modules"]["executed"])
        record["branches"].update(report["coverage"]["branches"]["executed"])
        coverage[key] = record
        if on_case is not None:
            on_case(entry, suite())
    return suite()


def run_cases(paths: list[str], cli_options: dict[str, Any] | None = None) -> Suite:
    return run_case_entries(load_cases(paths, cli_options))


_XML_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_ESCAPES = {"<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;"}
_XML_SPECIAL = re.compile("[<>&\"']")


def _xml(text: Any) -> str:
    return _XML_SPECIAL.sub(lambda match: _XML_ESCAPES[match.group()], _XML_CONTROL.sub("", js_string(text)))


def junit(suite: Suite) -> str:
    """JUnit XML for a suite (names and messages escaped for CI)."""
    cases = "\n".join(
        f'  <testcase name="{_xml(case["name"])}">'
        + ("" if case["passed"] else f'<failure message="assertions failed">{_xml(chr(10).join(case["failures"]))}</failure>')
        + "</testcase>"
        for case in suite["cases"]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="openflow-dryrun" tests="{len(suite["cases"])}" failures="{js_string(suite["failed"])}">\n'
        f"{cases}\n</testsuite>\n"
    )
