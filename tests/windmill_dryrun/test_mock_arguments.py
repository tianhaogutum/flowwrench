"""Port of test/mock-arguments.test.js."""

from __future__ import annotations

from typing import Any

import pytest

from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.fileio import read_data
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import clone, fixture, js

# Contract: expect_args is an optional exact JSON argument object on a sequence
# response. A mismatch consumes the slot, precedes its outcome, and remains a
# run failure through recovery. Call numbers are one-based across all attempts.
pytestmark = pytest.mark.needs_node

F = read_data(fixture("mock-arguments/flow.yaml"))
MOCKS = read_data(fixture("mock-arguments/mocks.yaml"))
USAGE = {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}


def single(args: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "static argument contract", "value": {"modules": [{"id": "send", "value": {
        "type": "script", "path": "/never-execute",
        "input_transforms": {key: {"type": "static", "value": value} for key, value in args.items()},
    }}]}}


def match_error(call: int, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": "MockArgsMismatch", "step_id": "send", "call": call, "expected_args": expected, "actual_args": actual,
            "message": f"Mock arguments mismatch for 'send' at call {call}: expected {js(expected)}, got {js(actual)}"}


def test_ordered_recipients_pass_swapped_recipients_fail_despite_consuming_exactly_two_responses() -> None:
    before = clone(MOCKS)
    for _ in range(2):
        ordered = run_flow(F, {"items": ["alice", "bob"]}, MOCKS, {"requireAllMocksUsed": True})
        assert ordered["status"] == "ok"
        assert ordered["result"] == [{"sent": "alice"}, {"sent": "bob"}]
        assert ordered["mock_usage"]["send"] == USAGE
        swapped = run_flow(F, {"items": ["bob", "alice"]}, MOCKS, {"requireAllMocksUsed": True})
        assert swapped["status"] == "error"
        assert swapped["mock_usage"]["send"] == USAGE
        errors = [match_error(1, {"recipient": "alice"}, {"recipient": "bob"}),
                  match_error(2, {"recipient": "bob"}, {"recipient": "alice"})]
        assert swapped["error"] == errors[0]
        assert swapped["mock_errors"] == errors
        events = [e for e in swapped["trace"] if e.get("mock")]
        assert [e["error"] for e in events] == errors
        assert [e["mock"]["call"] for e in events] == [1, 2]
        assert all(e.get("continued") and "result" not in e for e in events)
    assert MOCKS == before
    second = run_flow(F, {"items": ["alice", "alice"]}, MOCKS)
    assert second["error"] == match_error(2, {"recipient": "bob"}, {"recipient": "alice"})


def test_expected_arguments_compare_complete_nested_json_with_unordered_object_keys_and_ordered_arrays() -> None:
    args = {"recipient": "alice", "meta": {"tags": ["one", "two"], "enabled": False, "count": 0, "extra": None}}
    expected = {"meta": {"extra": None, "count": 0, "enabled": False, "tags": ["one", "two"]}, "recipient": "alice"}
    mock = {"send": {"sequence": [{"expect_args": expected, "result": None}]}}
    assert run_flow(single(args), {}, mock)["status"] == "ok"
    for wrong in [
        {**args, "unknown": True}, {"recipient": "alice"},
        {**args, "meta": {**args["meta"], "tags": ["two", "one"]}},
        {**args, "meta": {**args["meta"], "count": "0"}},
    ]:
        r = run_flow(single(wrong), {}, mock)
        assert r["error"] == match_error(1, expected, wrong)
    assert run_flow(single({}), {}, {"send": {"sequence": [{"expect_args": {}, "echo_args": True}]}})["status"] == "ok"
    assert run_flow(single({"recipient": None}), {}, {"send": {"sequence": [{"expect_args": {}, "result": 1}]}})["error"]["name"] == "MockArgsMismatch"


def test_guards_apply_before_errors_and_echo_args_including_retries_recovery_cannot_erase_a_mismatch() -> None:
    f = single({"recipient": "alice"})
    f["value"]["modules"][0]["retry"] = {"constant": {"attempts": 1, "seconds": 0}}
    expected = {"recipient": "alice"}
    ok = run_flow(f, {}, {"send": {"sequence": [
        {"expect_args": expected, "error": {"name": "Temporary", "message": "retry me"}},
        {"expect_args": expected, "echo_args": True},
    ]}})
    assert ok["status"] == "ok"
    assert ok["result"] == expected
    assert [e["mock"]["call"] for e in ok["trace"]] == [1, 2]
    recovered = run_flow(f, {}, {"send": {"sequence": [
        {"expect_args": {"recipient": "bob"}, "error": {"name": "Temporary", "message": "not reached"}},
        {"expect_args": expected, "echo_args": True},
    ]}})
    assert recovered["trace"][1]["status"] == "success"
    assert recovered["status"] == "error"
    assert recovered["error"] == match_error(1, {"recipient": "bob"}, expected)
    assert recovered["mock_usage"]["send"] == USAGE


def test_skipped_failures_and_failure_handlers_retain_argument_mismatches_and_original_runtime_errors() -> None:
    f = clone(F)
    del f["value"]["modules"][0]["value"]["modules"][0]["continue_on_error"]
    f["value"]["modules"][0]["value"]["skip_failures"] = True
    skipped = run_flow(f, {"items": ["bob", "alice"]}, MOCKS)
    assert skipped["status"] == "error"
    assert len(skipped["mock_errors"]) == 2
    assert skipped["mock_usage"]["send"] == USAGE
    failed = single({})
    failed["value"]["failure_module"] = {"id": "failure", "value": {"type": "script", "path": "/never-execute", "input_transforms": {}}}
    handler = run_flow(failed, {}, {"send": {"error": {"name": "Original", "message": "bad"}},
                                    "failure": {"sequence": [{"expect_args": {"recipient": "alice"}, "result": 7}]}})
    assert handler["error"]["name"] == "Original"
    assert handler["failure_error"]["name"] == "MockArgsMismatch"
    assert handler["failure_error"]["call"] == 1
    assert handler["mock_errors"] == [handler["failure_error"]]


def test_legacy_repeating_and_sequence_scalar_outcomes_remain_unguarded() -> None:
    for result in [None, False, 0, "alice"]:
        repeated = run_flow(F, {"items": ["bob", "alice"]}, {"send": {"result": result}})
        assert repeated["status"] == "ok"
        assert repeated["result"] == [result, result]
    mixed = run_flow(F, {"items": ["alice", "bob"]}, {"send": {"sequence": [
        {"expect_args": {"recipient": "alice"}, "result": 0}, {"result": False},
    ]}})
    assert mixed["status"] == "ok"
    assert mixed["result"] == [0, False]


def test_malformed_argument_guards_are_format_errors_a_guarded_response_still_needs_exactly_one_outcome() -> None:
    for expect_args in [None, [], "alice", False, 0]:
        with pytest.raises(FormatError, match=r"mock send\[0\]\.expect_args: expected object"):
            run_flow(single({}), {}, {"send": {"sequence": [{"expect_args": expect_args, "result": 1}]}})
    for response in [{"expect_args": {}}, {"expect_args": {}, "result": 1, "echo_args": True}]:
        with pytest.raises(FormatError, match="exactly one of result, error, echo_args"):
            run_flow(single({}), {}, {"send": {"sequence": [response]}})
    with pytest.raises(FormatError, match="unsupported field 'expect_args'"):
        run_flow(single({}), {}, {"send": {"expect_args": {}, "result": 1}})
    with pytest.raises(FormatError, match="unsupported field 'expected_args'"):
        run_flow(single({}), {}, {"send": {"sequence": [{"expected_args": {}, "result": 1}]}})
