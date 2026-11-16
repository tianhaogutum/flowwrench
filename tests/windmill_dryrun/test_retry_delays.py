"""Port of test/retry-delays.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import Files, cli, js, read_json, read_text, sha

# Contract: exact complete ordered records, including zero-second events.
# Only array/object shape is validated; object-content differences fail assertions.
pytestmark = pytest.mark.needs_node

BAD = {"error": {"name": "Original", "message": "bad"}}


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], handler: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "Ordered retry assertions", "value": {"modules": modules, **({"failure_module": handler} if handler else {})}}


def delay(module_id: Any, attempt: Any, seconds: Any) -> dict[str, Any]:
    return {"module_id": module_id, "attempt": attempt, "retry_delay_seconds": seconds}


DELAYS = [delay("bad", 1, 2), delay("failure", 1, 3)]


def boundary() -> dict[str, Any]:
    return {
        "flow": flow([script("bad", {"retry": {"constant": {"attempts": 1, "seconds": 2}}})],
                     script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 3}}})),
        "mocks": {"bad": BAD, "failure": {"sequence": [BAD, {"result": "recovered"}]}},
        "expect": {"error": {"name": "Original"}, "failure_result": "recovered", "simulated_delay_seconds": 5, "retry_delays": DELAYS},
    }


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def test_retry_delays_asserts_the_confirmed_primary_and_recovered_handler_schedule(files: Files) -> None:
    value = boundary()
    suite = run_cases([files.case_file(value)])
    assert suite["passed"] == 1
    assert suite["cases"][0]["report"]["simulated_delay_seconds"] == 5
    assert suite["cases"][0]["report"] == run_flow(value["flow"], {}, value["mocks"])


def test_retry_delays_compares_complete_ordered_lists_fields_and_types_including_equal_totals(files: Files) -> None:
    value = boundary()
    reordered_keys = [{"retry_delay_seconds": d["retry_delay_seconds"], "attempt": d["attempt"], "module_id": d["module_id"]} for d in DELAYS]
    assert run_cases([files.case_file({**value, "expect": {**value["expect"], "retry_delays": reordered_keys}})])["passed"] == 1
    variants: list[Any] = [[], [DELAYS[0]], [*DELAYS, DELAYS[1]], list(reversed(DELAYS)),
                           [delay("bad", 1, 3), delay("failure", 1, 2)],
                           [{**DELAYS[0], "module_id": "unknown"}, DELAYS[1]],
                           [{**DELAYS[0], "module_id": 1}, DELAYS[1]],
                           [{**DELAYS[0], "extra": {"nested": None}}, DELAYS[1]], [{}, DELAYS[1]]]
    for field in ["attempt", "retry_delay_seconds"]:
        for v in ["1", None, True, 0, -1, 1.5, {}, []]:
            variants.append([{**DELAYS[0], field: v}, DELAYS[1]])
    for field in DELAYS[0]:
        incomplete = {key: item for key, item in DELAYS[0].items() if key != field}
        variants.append([incomplete, DELAYS[1]])
    suite = run_cases([files.case_file({**value, "expect": {**value["expect"], "retry_delays": retry_delays}}) for retry_delays in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"retry_delays: expected {js(variants[i])}, got {js(DELAYS)}"]
        assert c["report"] == suite["cases"][0]["report"]


def test_retry_delays_retains_constant_exponential_order_and_attempts_across_loops_and_handlers(files: Files) -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["a", "b"]}, "skip_failures": False,
                                       "modules": [script("send", {"retry": {"constant": {"attempts": 1, "seconds": 0},
                                                                             "exponential": {"attempts": 1, "multiplier": 2, "seconds": 3}}})]}},
              script("bad", {"retry": {"constant": {"attempts": 1, "seconds": 2}}})],
             script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 1}, "exponential": {"attempts": 1, "multiplier": 2, "seconds": 2}}}))
    expected = [delay("send", 1, 0), delay("send", 2, 18), delay("send", 1, 0), delay("send", 2, 18),
                delay("bad", 1, 2), delay("failure", 1, 1), delay("failure", 2, 8)]
    for exhausted in [False, True]:
        mocks = {"send": {"sequence": [BAD, BAD, {"result": "a"}, BAD, BAD, {"result": "b"}]}, "bad": BAD,
                 "failure": {"sequence": [BAD] if exhausted else [BAD, BAD, {"result": "handled"}]}}
        expect = {"error": {"name": "Original"}, "retry_delays": expected, "simulated_delay_seconds": 47,
                  **({"failure_error": {"name": "MockExhausted"}} if exhausted else {"failure_result": "handled"})}
        suite = run_cases([files.case_file({"flow": f, "mocks": mocks, "expect": expect})])
        assert suite["passed"] == 1
        report = suite["cases"][0]["report"]
        assert report == run_flow(f, {}, mocks)
        assert [[e["attempt"], e["mock"]["call"]] for e in report["trace"] if e["module_id"] == "send"] == [
            [1, 1], [2, 2], [3, 3], [1, 4], [2, 5], [3, 6],
        ]
        assert report["mock_usage"]["failure"] == ({"expected": 1, "calls": 3, "consumed": 1, "remaining": 0, "extra": 2} if exhausted
                                                   else {"expected": 3, "calls": 3, "consumed": 3, "remaining": 0, "extra": 0})
        names = [e["name"] for e in report["mock_errors"]] if "mock_errors" in report else None
        assert names == (["MockExhausted", "MockExhausted"] if exhausted else None)


def test_retry_delays_distinguishes_zero_second_events_from_no_retry_and_preserves_default_status(files: Files) -> None:
    scenarios: list[dict[str, Any]] = [
        {"flow": flow([]), "mocks": {}, "expect": {"retry_delays": []}},
        {"flow": flow([script("a", {"retry": {"constant": {"attempts": 1, "seconds": 0}}})]),
         "mocks": {"a": {"sequence": [BAD, {"result": 1}]}}, "expect": {"retry_delays": [delay("a", 1, 0)], "result": 1}},
        {"flow": flow([script("a", {"retry": {"constant": {"attempts": 1, "seconds": 60}, "retry_if": {"expr": "false"}}})]),
         "mocks": {"a": BAD}, "expect": {"status": "error", "retry_delays": []}},
        {"flow": flow([script("a")], script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 60}, "retry_if": {"expr": "false"}}})),
         "mocks": {"a": BAD, "failure": BAD}, "expect": {"error": {"name": "Original"}, "failure_error": {"name": "Original"}, "retry_delays": []}},
        {"flow": flow([script("a", {"skip_if": {"expr": "true"}, "retry": {"constant": {"attempts": 1, "seconds": 60}}})],
                      script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 60}}})),
         "mocks": {"a": BAD, "failure": BAD}, "expect": {"retry_delays": []}},
    ]
    for value in scenarios:
        suite = run_cases([files.case_file({**value, "expect": {**value["expect"], "simulated_delay_seconds": 0}})])
        assert suite["passed"] == 1
        assert suite["cases"][0]["report"] == run_flow(value["flow"], {}, value["mocks"])
    zero_mismatch = run_cases([files.case_file({**scenarios[1], "expect": {"retry_delays": []}})])
    assert zero_mismatch["cases"][0]["failures"] == [f"retry_delays: expected [], got {js([delay('a', 1, 0)])}"]
    value = boundary()
    suite = run_cases([files.case_file({**value, "expect": {"retry_delays": DELAYS}})])
    assert suite["cases"][0]["failures"] == ['status: expected "ok", got "error"']


def test_cli_retry_delays_rejects_only_malformed_array_object_shapes(files: Files, tmp_path: Path) -> None:
    value = boundary()
    for retry_delays in [None, {}, 1, False, "delays"]:
        result = cli("test", files.case_file({**value, "expect": {"retry_delays": retry_delays}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.retry_delays: expected array", result.stderr)
        assert result.stdout == ""
    for v in [None, [], 1, False, "delay"]:
        result = cli("test", files.case_file({**value, "expect": {"retry_delays": [DELAYS[0], v]}}), cwd=tmp_path)
        assert result.status == 2, result.stdout + result.stderr
        assert re.search(r"expect\.retry_delays\[1\]: expected object", result.stderr)
        assert result.stdout == ""


def test_cli_retry_delays_full_list_diagnostics_survive_json_escaped_junit_and_source_free_replay(files: Files, tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    value = boundary()
    cases = [value, {**value, "expect": {**value["expect"], "retry_delays": [delay("bad", 1, 3), delay("failure", 1, 2)]}},
             {"flow": flow([]), "mocks": {}, "expect": {"retry_delays": []}}]
    paths = [files.case_file({**c, "name": f"retry <&雪> {i}"}, source) for i, c in enumerate(cases)]
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stdout + result.stderr
    suite, saved, junit = read_json(report), read_json(bundle), read_text(xml)
    assert suite["passed"] == 2
    assert suite["failed"] == 1
    failure = f"retry_delays: expected {js(cases[1]['expect']['retry_delays'])}, got {js(DELAYS)}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    assert failure.replace('"', "&quot;") in junit
    assert "retry &lt;&amp;雪&gt;" in junit
    assert junit == saved["junit_xml"]
    shutil.rmtree(source)
    for case_id in [1, 2, 3]:
        out, xml_out = tmp_path / f"replayed-{case_id}.json", tmp_path / f"replayed-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", out, "--junit", xml_out, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == (1 if case_id == 2 else 0), replay.stdout + replay.stderr
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(out)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        if case_id == 2:
            assert failure.replace('"', "&quot;") in read_text(xml_out)
    for retry_delays in [{}, [None], [[]], [False]]:
        saved["cases"][0]["snapshot"]["expect"]["retry_delays"] = retry_delays
        saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
        invalid = cli("replay", files.write(saved), "--case", "1", cwd=tmp_path)
        assert invalid.status == 2, invalid.stderr
        assert re.search(r"expect\.retry_delays(?:\[0\])?: expected (?:array|object)", invalid.stderr)
