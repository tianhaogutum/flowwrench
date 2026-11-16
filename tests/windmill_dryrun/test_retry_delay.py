"""Port of test/retry-delay.test.js."""

from __future__ import annotations

import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_case_entries, run_cases
from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.jsvalues import UNDEFINED
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, cli, read_json, read_text, sha, write_text

# Contract: exact finite nonnegative numeric total; no status opt-in, coercion,
# tolerance, sleeping, script execution or changes to the underlying report.
pytestmark = pytest.mark.needs_node

BAD = {"error": {"name": "Original", "message": "bad"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Retry delay assertions", "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def handler_exhaustion() -> dict[str, Any]:
    return {
        "flow": flow([script("bad")], script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 3}}})),
        "mocks": {"bad": BAD, "failure": {"sequence": [{"error": {"message": "handler retry"}}]}},
        "expect": {"error": {"name": "Original"}, "failure_error": {"name": "MockExhausted"}, "simulated_delay_seconds": 3},
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def test_simulated_delay_seconds_asserts_the_confirmed_exhausted_handler_retry_total(files: Files) -> None:
    value = handler_exhaustion()
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1
    report = suite["cases"][0]["report"]
    assert report["simulated_delay_seconds"] == 3
    assert report["error"] == {"name": "Original", "message": "bad", "step_id": "bad"}
    assert report["failure_error"] == {"name": "MockExhausted", "message": "Mock sequence exhausted for 'failure' at call 2", "step_id": "failure"}
    assert report["mock_errors"] == [report["failure_error"]]
    assert report["mock_usage"]["failure"] == {"expected": 1, "calls": 2, "consumed": 1, "remaining": 0, "extra": 1}
    assert [e["mock"] for e in report["trace"]] == [{"source": "external", "call": 1}, {"source": "sequence", "call": 1}, {"source": "sequence", "call": 2}]
    assert report == run_flow(value["flow"], {}, value["mocks"])


def test_delay_totals_accumulate_constant_and_exponential_retries_across_primary_and_recovered_handler(files: Files) -> None:
    value = {
        "flow": flow([
            script("first", {"retry": {"constant": {"attempts": 1, "seconds": 4}}}),
            script("bad", {"retry": {"constant": {"attempts": 1, "seconds": 2}, "exponential": {"attempts": 1, "multiplier": 2, "seconds": 3}}}),
        ], script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 1}, "exponential": {"attempts": 1, "multiplier": 2, "seconds": 2}}})),
        "mocks": {
            "first": {"sequence": [BAD, {"result": "recovered primary"}]},
            "bad": {"sequence": [BAD, BAD, BAD]},
            "failure": {"sequence": [BAD, BAD, {"result": "recovered handler"}]},
        },
        "expect": {"error": {"name": "Original"}, "failure_result": "recovered handler", "simulated_delay_seconds": 33},
    }
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1
    report = suite["cases"][0]["report"]
    # Exponential n includes earlier constant retries: 2*3**2 and 2*2**2.
    assert [e["retry_delay_seconds"] for e in report["trace"] if "retry_delay_seconds" in e] == [4, 2, 18, 1, 8]
    assert "failure_error" not in report
    assert "mock_errors" not in report
    assert report["error"] == {"name": "Original", "message": "bad", "step_id": "bad"}
    for module_id, count in [("first", 2), ("bad", 3), ("failure", 3)]:
        assert report["mock_usage"][module_id] == {"expected": count, "calls": count, "consumed": count, "remaining": 0, "extra": 0}
    assert report == run_flow(value["flow"], {}, value["mocks"])


def test_zero_covers_no_retry_zero_second_retry_suppression_and_unused_handler_recovered_runs_stay_ok(files: Files) -> None:
    scenarios: list[dict[str, Any]] = [
        {"flow": flow([script("a")]), "mocks": {"a": {"result": 1}}},
        {"flow": flow([script("a", {"retry": {"constant": {"attempts": 1, "seconds": 0}}})]),
         "mocks": {"a": {"sequence": [BAD, {"result": 1}]}}},
        {"flow": flow([script("a", {"retry": {"exponential": {"attempts": 1, "multiplier": 1, "seconds": 60}, "retry_if": {"expr": "false"}}})]),
         "mocks": {"a": BAD}, "expect": {"status": "error"}},
        {"flow": flow([script("a")], script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 60}, "retry_if": {"expr": "false"}}})),
         "mocks": {"a": BAD, "failure": BAD}, "expect": {"error": {"name": "Original"}, "failure_error": {"name": "Original"}}},
        {"flow": flow([script("a")], script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 60}}})),
         "mocks": {"a": {"result": 1}, "failure": BAD}},
    ]
    for value in scenarios:
        suite = run_cases([files.case_file({**value, "expect": {**value.get("expect", {}), "simulated_delay_seconds": 0}})])
        assert suite["passed"] == 1
        assert suite["cases"][0]["report"] == run_flow(value["flow"], {}, value["mocks"])
        assert suite["cases"][0]["report"]["simulated_delay_seconds"] == 0
    f = flow([script("a", {"retry": {"exponential": {"attempts": 1, "multiplier": 1, "seconds": 3}}})])
    recovered = run_cases([files.case_file({"flow": f, "mocks": {"a": {"sequence": [BAD, {"result": 1}]}},
                                            "expect": {"result": 1, "simulated_delay_seconds": n}}) for n in [3, 1.5]])
    assert recovered["passed"] == 1
    assert recovered["failed"] == 1
    assert recovered["cases"][1]["failures"] == ["simulated_delay_seconds: expected 1.5, got 3"]
    assert recovered["cases"][0]["report"]["status"] == "ok"


def test_exact_mismatches_report_expected_actual_and_do_not_change_expected_status_or_reports(files: Files) -> None:
    value = handler_exhaustion()
    totals = [0, 2.9999999999999996, 3.0000000000000004, sys.float_info.max]
    shown = ["0", "2.9999999999999996", "3.0000000000000004", "1.7976931348623157e+308"]
    expectations = [value["expect"], {"simulated_delay_seconds": 3}, {"status": "error", "simulated_delay_seconds": 3},
                    *[{**value["expect"], "simulated_delay_seconds": n} for n in totals]]
    suite = run_cases([files.case_file({**value, "expect": expect}) for expect in expectations])
    assert suite["passed"] == 2
    assert suite["cases"][1]["failures"] == ['status: expected "ok", got "error"',
                                             "MockExhausted at 'failure': Mock sequence exhausted for 'failure' at call 2"]
    for i, _ in enumerate(expectations):
        if i >= 3:
            assert suite["cases"][i]["failures"] == [f"simulated_delay_seconds: expected {shown[i - 3]}, got 3"]
        assert suite["cases"][i]["report"] == suite["cases"][0]["report"]
    zero = {"flow": flow([]), "mocks": {}, "expect": {"simulated_delay_seconds": -0.0}}
    assert run_cases([files.case_file(zero)])["passed"] == 1
    # -0 is lost by JSON serialization; exercise it directly as well.
    entry = {"name": "signed zero", "path": "metadata", "flowPath": "metadata", "snapshot": {**zero, "input": {}, "options": {}}}
    assert run_case_entries([entry])["passed"] == 1


def test_cli_rejects_malformed_and_non_finite_delay_expectations_before_execution(files: Files, tmp_path: Path) -> None:
    value = {"flow": flow([script("a")]), "mocks": {"a": {"result": None}}}
    for n in [None, True, False, "0", "", [], {}, {"seconds": 3}, -1, -0.001]:
        result = cli("test", files.case_file({**value, "expect": {"simulated_delay_seconds": n}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.simulated_delay_seconds: expected nonnegative finite number", result.stderr)
        assert result.stdout == ""
    for literal in [".nan", ".inf", "-.inf", "1e999"]:
        files.sequence += 1
        path = tmp_path / f"nonfinite-{files.sequence}.yaml"
        flow_path = files.write(value["flow"])
        write_text(path, f'flow: "{flow_path}"\nexpect:\n  simulated_delay_seconds: {literal}\n')
        result = cli("test", path, cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search("only finite JSON values are supported", result.stderr)
        assert result.stdout == ""
    # UNDEFINED stands in for JavaScript's explicit `undefined` value.
    for n in [math.nan, math.inf, -math.inf, UNDEFINED]:
        with pytest.raises(FormatError, match=r"expect\.simulated_delay_seconds: expected nonnegative finite number"):
            run_case_entries([{"name": "invalid", "path": "metadata", "flowPath": "metadata",
                               "snapshot": {**value, "input": {}, "options": {}, "expect": {"simulated_delay_seconds": n}}}])


def test_cli_delay_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = handler_exhaustion()
    cases = [value, {**value, "expect": {**value["expect"], "simulated_delay_seconds": 0}},
             {"flow": flow([script("a")]), "mocks": {"a": {"result": None}}, "expect": {"simulated_delay_seconds": 0}}]
    paths = [files.case_file({**c, "name": f"delay <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 1
    assert suite["cases"][1]["failures"] == ["simulated_delay_seconds: expected 0, got 3"]
    assert suite["cases"][1]["failures"][0] in result.stdout
    assert suite["cases"][1]["failures"][0] in junit
    assert "delay &lt;&amp;雪&gt;" in junit
    assert junit == saved["junit_xml"]
    shutil.rmtree(source)
    for case_id in [1, 2, 3]:
        out, junit_out = tmp_path / f"replayed-{case_id}.json", tmp_path / f"replayed-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", out, "--junit", junit_out, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (1 if case_id == 2 else 0), replay.stdout + replay.stderr
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(out)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        if case_id == 2:
            assert suite["cases"][1]["failures"][0] in read_text(junit_out)
    saved["cases"][0]["snapshot"]["expect"]["simulated_delay_seconds"] = -1
    saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
    invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
    assert invalid.status == 2, invalid.stderr
    assert re.search(r"expect\.simulated_delay_seconds: expected nonnegative finite number", invalid.stderr)
