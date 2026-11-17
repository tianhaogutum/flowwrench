import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from flowwrench.ha_diff import compare_traces, parse_trace

SCHEMA = json.loads((Path(__file__).parents[2] / "docs" / "ha-diff" / "report.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)


@pytest.mark.parametrize("envelope", ["raw", "download", "websocket", "saved"])
def test_all_supported_envelopes_produce_contract_valid_reports(trace, envelope):
    other = copy.deepcopy(trace)
    other["trace"]["action/0"][0]["error"] = "changed"

    def wrap(item):
        return {
            "raw": item,
            "download": {"trace": item},
            "websocket": {"type": "result", "success": True, "result": item},
            "saved": {"extended_dict": item, "short_dict": {}},
        }[envelope]

    report = compare_traces(parse_trace(wrap(trace)), parse_trace(wrap(other)))
    VALIDATOR.validate(report)


def test_partial_and_anchored_report_variants_match_contract(trace, monkeypatch):
    empty = copy.deepcopy(trace)
    empty["trace"] = {}
    VALIDATOR.validate(compare_traces(parse_trace(empty), parse_trace(empty)))
    path = "action/0/repeat/sequence/0"
    trace["trace"] = {
        path: [
            {
                "path": path,
                "timestamp": "2026-10-07T04:57:43.847400+00:00",
                "changed_variables": {"repeat": {"index": 3}, "state": "on"},
            }
        ]
    }
    other = copy.deepcopy(trace)
    other["trace"][path][0]["changed_variables"]["state"] = "off"
    report = compare_traces(parse_trace(trace), parse_trace(other))
    VALIDATOR.validate(report)
    assert report["steps"][0]["occurrence"] is None
    import flowwrench.ha_diff.compare as comparison

    monkeypatch.setattr(comparison, "MAX_TOTAL_CHANGES", 1)
    other["config"] = {"changed": "configuration"}
    VALIDATOR.validate(compare_traces(parse_trace(trace), parse_trace(other)))
