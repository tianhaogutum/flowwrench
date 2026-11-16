"""Port of test/mock-calls.test.js."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.runner import run_flow

from .helpers import cli, fixture, js, read_json, read_text, sha, write_json

# Contract: compare the entire ordered lookup list, including every record key.
# Only non-array/non-object assertion shapes are format errors (CLI exit 2).
# Wrong order/count/fields/types are assertion failures (exit 1), not coercions.
pytestmark = pytest.mark.needs_node


def call(module_id: Any, source: Any, number: Any) -> dict[str, Any]:
    return {"module_id": module_id, "source": source, "call": number}


CALLS = [call("send", "sequence", 1), call("send", "sequence", 2)]


def script(module_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": module_id, "value": {"type": "script", "path": "/never-run", "input_transforms": {}}, **(extra or {})}


def flow(modules: list[Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"summary": "synthetic lookup assertions", "value": {"modules": modules, **(extra or {})}}


class Writer:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.sequence = 0

    def write(self, name: str, value: Any) -> str:
        self.sequence += 1
        return write_json(self.tmp / f"{name}-{self.sequence}.json", value)

    def case(self, mock_calls: Any, overrides: dict[str, Any] | None = None) -> str:
        return self.write("case", {
            "name": f"calls <fixture & {self.sequence}>", "flow": fixture("mock-consumption/flow.yaml"),
            "mocks": fixture("mock-consumption/mocks.yaml"), "input": {"items": ["alice", "bob"]},
            "expect": {"mock_calls": mock_calls}, **(overrides or {}),
        })


@pytest.fixture
def files(tmp_path: Path) -> Writer:
    return Writer(tmp_path)


def test_exact_ordered_mock_calls_accepts_the_two_recipient_sequence_boundary(files: Writer) -> None:
    suite = run_cases([files.case(CALLS)])
    assert suite["passed"] == 1
    assert suite["cases"][0]["report"]["result"] == [{"sent": "alice"}, {"sent": "bob"}]
    assert [{"module_id": e["module_id"], **e["mock"]} for e in suite["cases"][0]["report"]["trace"] if e.get("mock")] == CALLS


def test_mock_calls_compares_full_ordered_lists_and_objects_without_coercing_fields(files: Writer) -> None:
    reordered_keys = [{"call": c["call"], "source": c["source"], "module_id": c["module_id"]} for c in CALLS]
    assert run_cases([files.case(reordered_keys)])["passed"] == 1
    variants: list[Any] = [[], [CALLS[0]], [*CALLS, CALLS[1]], list(reversed(CALLS)),
                           [{**CALLS[0], "module_id": "unknown"}, CALLS[1]],
                           [{**CALLS[0], "source": "external"}, CALLS[1]],
                           *[[{**CALLS[0], "call": value}, CALLS[1]] for value in ["1", None, True, 0, -1, 1.5, {}, []]],
                           [{**CALLS[0], "source": None}, CALLS[1]], [{**CALLS[0], "module_id": 1}, CALLS[1]],
                           [{**CALLS[0], "extra": {"nested": None}}, CALLS[1]], [{}, CALLS[1]]]
    for field in CALLS[0]:
        incomplete = {key: value for key, value in CALLS[0].items() if key != field}
        variants.append([incomplete, CALLS[1]])
    suite = run_cases([files.case(expected) for expected in variants])
    assert suite["failed"] == len(variants)
    for i, c in enumerate(suite["cases"]):
        assert c["failures"] == [f"mock_calls: expected {js(variants[i])}, got {js(CALLS)}"]
        assert c["report"] == suite["cases"][0]["report"]


def test_mock_calls_preserves_cross_module_ordering_retries_loop_calls_and_failure_handlers(files: Writer) -> None:
    f = flow([{"id": "each", "value": {"type": "forloopflow", "iterator": {"type": "static", "value": ["a", "b"]}, "skip_failures": False,
                                       "modules": [script("send", {"retry": {"constant": {"attempts": 1, "seconds": 0}}}), script("other")]}},
              script("bad"), script("never")], {"failure_module": script("failure")})
    mocks = {"send": {"sequence": [{"error": {"message": "retry"}}, {"result": 1}, {"result": 2}]},
             "other": {"result": 3}, "bad": {"error": {"name": "Original", "message": "bad"}},
             "never": {"sequence": [{"result": 4}]}, "failure": {"sequence": [{"result": None}]}}
    expected = [call("send", "sequence", 1), call("send", "sequence", 2), call("other", "external", 1),
                call("send", "sequence", 3), call("other", "external", 2), call("bad", "external", 1), call("failure", "sequence", 1)]
    path = files.write("ordered-flow", f)
    options = {"requireAllMocksUsed": True}
    suite = run_cases([files.case(None, {"flow": path, "mocks": mocks, "options": options, "expect": {
        "error": {"name": "Original", "message": "bad", "step_id": "bad"}, "failure_result": None, "mock_calls": expected,
    }})])
    assert suite["passed"] == 1
    # Assertions must leave reports, primary/collected errors and accounting intact.
    assert suite["cases"][0]["report"] == run_flow(f, {"items": ["alice", "bob"]}, mocks, options)
    assert [e["name"] for e in suite["cases"][0]["report"]["mock_errors"]] == ["MockUnused"]
    assert [e["attempt"] for e in suite["cases"][0]["report"]["trace"] if e["module_id"] == "send"] == [1, 2, 1]


def test_mock_calls_covers_inline_external_override_fallback_and_missing_provenance(files: Writer) -> None:
    f = flow([script("inline", {"mock": {"enabled": True, "return_value": 1}}),
              script("override", {"mock": {"enabled": True, "return_value": 2}}), script("absent")])
    path = files.write("sources-flow", f)
    mocks = {"override": {"result": 3}}
    for unmocked in ["null", "echo-args", "error"]:
        expected = [call("inline", "inline", 1), call("override", "external", 1),
                    call("absent", "missing" if unmocked == "error" else "fallback", 1)]
        suite = run_cases([files.case(None, {"flow": path, "mocks": mocks, "options": {"unmocked": unmocked},
                                             "expect": {"status": "error" if unmocked == "error" else "ok", "mock_calls": expected}})])
        assert suite["passed"] == 1
        assert suite["cases"][0]["report"]["mock_usage"] == {}
        if unmocked == "error":
            assert suite["cases"][0]["report"]["error"]["name"] == "MissingMock"
            no_opt_in = run_cases([files.case(expected, {"flow": path, "mocks": mocks})])
            assert no_opt_in["cases"][0]["failures"] == ['status: expected "ok", got "error"']


def test_mock_calls_empty_excludes_identity_containers_skipped_unselected_steps_and_failed_mappings(files: Writer) -> None:
    f = flow([{"id": "identity", "value": {"type": "identity"}},
              script("skipped", {"skip_if": {"expr": "true"}}),
              script("mapping", {"continue_on_error": True, "value": {**script("mapping")["value"],
                                                                       "input_transforms": {"x": {"type": "javascript", "expr": "flow_input.missing.value"}}}}),
              {"id": "branch", "value": {"type": "branchone", "branches": [{"expr": "false", "modules": [script("unselected")]}], "default": []}}])
    path = files.write("no-lookups", f)
    suite = run_cases([files.case([], {"flow": path, "mocks": {"skipped": {"sequence": [{"result": 1}]}}})])
    assert suite["passed"] == 1
    assert suite["cases"][0]["report"]["mock_usage"]["skipped"] == {"expected": 1, "calls": 0, "consumed": 0, "remaining": 1, "extra": 0}
    assert all(not e.get("mock") for e in suite["cases"][0]["report"]["trace"])


def test_mock_calls_includes_argument_mismatches_recovered_retries_and_exhausted_handler_lookups(files: Writer) -> None:
    f = flow([script("send", {"retry": {"constant": {"attempts": 1, "seconds": 0}}})], {
        "failure_module": script("failure", {"retry": {"constant": {"attempts": 1, "seconds": 0}}}),
    })
    mocks: dict[str, Any] = {"send": {"sequence": [{"expect_args": {"absent": 1}, "result": "never"}, {"result": 2}]},
                             "failure": {"sequence": [{"error": {"message": "handler retry"}}]}}
    expected = [call("send", "sequence", 1), call("send", "sequence", 2)]
    path = files.write("contract-flow", f)
    recovered = run_cases([files.case(None, {"flow": path, "mocks": mocks, "expect": {
        "error": {"name": "MockArgsMismatch"}, "mock_calls": expected, "results": {"send": 2},
    }})])
    assert recovered["passed"] == 1
    assert recovered["cases"][0]["report"] == run_flow(f, {"items": ["alice", "bob"]}, mocks)
    # A final accounting violation alone doesn't invoke the handler. Force a hard error.
    mocks["send"] = {"sequence": [{"error": {"name": "Original", "message": "bad"}}]}
    hard = run_cases([files.case(None, {"flow": path, "mocks": mocks, "expect": {
        "error": {"name": "MockExhausted"}, "mock_calls": [*expected, call("failure", "sequence", 1), call("failure", "sequence", 2)],
    }})])
    assert hard["passed"] == 1
    assert hard["cases"][0]["report"]["failure_error"]["name"] == "MockExhausted"
    assert [e["step_id"] for e in hard["cases"][0]["report"]["mock_errors"]] == ["send", "failure"]


def test_cli_mock_calls_full_list_diffs_survive_json_escaped_junit_and_source_free_replay(files: Writer, tmp_path: Path) -> None:
    source = tmp_path / "standalone"
    source.mkdir()
    for name in ["flow.yaml", "mocks.yaml"]:
        shutil.copyfile(fixture(f"mock-consumption/{name}"), source / name)
    overrides = {"flow": str(source / "flow.yaml"), "mocks": str(source / "mocks.yaml")}
    paths = []
    for i, expected in enumerate([CALLS, list(reversed(CALLS))]):
        path = source / f"case-{i}.json"
        write_json(path, {**read_json(files.case(expected, overrides)), "name": "calls <fixture & snow 雪>"})
        paths.append(path)
    report, xml, bundle = tmp_path / "suite.json", tmp_path / "suite.xml", tmp_path / "bundle.json"
    result = cli("test", *paths, "--report", report, "--junit", xml, "--bundle", bundle, "--output-dir", tmp_path, cwd=tmp_path)
    assert result.status == 1, result.stderr + result.stdout
    suite, saved = read_json(report), read_json(bundle)
    assert suite["passed"] == 1
    assert suite["failed"] == 1
    failure = f"mock_calls: expected {js(list(reversed(CALLS)))}, got {js(CALLS)}"
    assert suite["cases"][1]["failures"] == [failure]
    assert failure in result.stdout
    junit = read_text(xml)
    assert junit == saved["junit_xml"]
    assert "calls &lt;fixture &amp; snow 雪&gt;" in junit
    assert failure.replace('"', "&quot;") in junit
    assert [{"module_id": c["module_id"], "source": c["source"], "call": c["call"]} for c in saved["cases"][0]["mock_calls"]] == CALLS
    shutil.rmtree(source)
    for case_id in [1, 2]:
        replay_report, replay_xml = tmp_path / f"replay-{case_id}.json", tmp_path / f"replay-{case_id}.xml"
        replay = cli("replay", bundle, "--case", str(case_id), "--report", replay_report, "--junit", replay_xml, "--output-dir", tmp_path, cwd=tmp_path)
        assert replay.status == case_id - 1, replay.stderr + replay.stdout
        assert re.search(r"REPLAY case \d+: reproduced", replay.stdout)
        assert read_json(replay_report)["cases"][0] == saved["cases"][case_id - 1]["outcome"]
        if case_id == 2:
            assert failure.replace('"', "&quot;") in read_text(replay_xml)
    saved["cases"][0]["snapshot"]["expect"]["mock_calls"] = [None]
    saved["cases"][0]["hashes"]["expect"] = sha(saved["cases"][0]["snapshot"]["expect"])
    malformed = files.write("malformed-bundle", saved)
    replay = cli("replay", malformed, "--case", "1", cwd=tmp_path)
    assert replay.status == 2, replay.stderr
    assert re.search(r"mock_calls\[0\]: expected object", replay.stderr)


def test_cli_malformed_mock_calls_array_object_shapes_are_invalid_input(files: Writer, tmp_path: Path) -> None:
    for value in [None, {}, 1, False, "calls"]:
        result = cli("test", files.case(value), cwd=tmp_path)
        assert result.status == 2, result.stderr + result.stdout
        assert re.search(r"expect\.mock_calls: expected array", result.stderr)
        assert result.stdout == ""
    for value in [None, [], 1, False, "call"]:
        result = cli("test", files.case([CALLS[0], value]), cwd=tmp_path)
        assert result.status == 2, result.stderr + result.stdout
        assert re.search(r"expect\.mock_calls\[1\]: expected object", result.stderr)
        assert result.stdout == ""
