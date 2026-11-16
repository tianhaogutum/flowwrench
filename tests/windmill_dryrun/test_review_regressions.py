"""Port of test/review-regressions.test.js."""

from __future__ import annotations

import re
from pathlib import Path

from flowwrench.windmill_dryrun.cases import junit, run_cases

from .helpers import clone, js, write_json


def test_case_assertions_preserve_null_results_and_distinguish_absent_fields(tmp_path: Path) -> None:
    # No JavaScript expressions are involved, so Node.js is not needed here.
    flow = {"summary": "null regression", "value": {"modules": [{"id": "a", "value": {"type": "script", "path": "f/a", "input_transforms": {}}}]}}
    write_json(tmp_path / "flow.json", flow)
    cases = [
        {"mocks": {"a": {"result": None}}, "expect": {"status": "ok", "result": None, "results": {"a": None}}, "pass": True},
        {"mocks": {"a": {"result": 0}}, "expect": {"result": None}, "pass": False},
        {"expect": {"status": "error", "result": None}, "pass": False},
        {"mocks": {"a": {"result": None}}, "expect": {"failure_result": None}, "pass": False},
        {"mocks": {"a": {"result": None}}, "expect": {"stopped": False}, "pass": True},
    ]
    failure_flow = clone(flow)
    failure_flow["value"]["failure_module"] = {"id": "failure", "value": {"type": "script", "path": "f/failure", "input_transforms": {}}}
    write_json(tmp_path / "failure.json", failure_flow)
    cases.append({"flow": "failure.json", "mocks": {"failure": {"result": None}}, "expect": {"status": "error", "failure_result": None}, "pass": True})
    for i, case in enumerate(cases):
        path = tmp_path / f"case-{i}.json"
        data = {key: value for key, value in case.items() if key != "pass"}
        write_json(path, {"flow": "flow.json", **data})
        result = run_cases([str(path)])
        assert result["failed"] == (0 if case["pass"] else 1), js(result["cases"][0])
        assert re.search(f'failures="{0 if case["pass"] else 1}"', junit(result))
