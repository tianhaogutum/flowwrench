"""Port of test/cli.test.js (plus a Python-only check for a missing Node.js)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from flowwrench.windmill_dryrun import cli as cli_module
from flowwrench.windmill_dryrun.cases import run_cases
from flowwrench.windmill_dryrun.errors import NODE_REQUIRED_MESSAGE, FormatError
from flowwrench.windmill_dryrun.fileio import read_data, write_output

from .helpers import clone, cli, fixture, read_json, read_text, write_json, write_text

SIMPLE = {"summary": "test", "value": {"modules": [{"id": "a", "value": {"type": "script", "path": "f/a", "input_transforms": {}}}]}}
ORDER = fixture("order-flow/")
CASES = [ORDER + f"cases/{name}.yaml" for name in ["cancelled", "empty", "paid", "pending"]]


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path


def write(tmp: Path, name: str, value: object) -> str:
    path = tmp / name
    return write_text(path, value) if isinstance(value, str) else write_json(path, value)


def base_files(tmp: Path) -> tuple[str, str]:
    return write(tmp, "flow.yaml", SIMPLE), write(tmp, "input.json", {})


@pytest.mark.needs_node
def test_cli_run_real_yaml_fixture_expected_arguments_results_coverage_json_trace(tmp: Path) -> None:
    output = tmp / "trace.json"
    r = cli("run", ORDER + "flow.yaml", "--input", ORDER + "input.json", "--mocks", ORDER + "mocks.yaml", "--trace", output, cwd=tmp)
    assert r.status == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["result"] == {"outcome": {"sent": True}}
    assert next(e for e in report["trace"] if e["module_id"] == "notify")["args"] == {"order_id": "demo-42", "reserved": 2, "channel": "email"}
    assert read_json(output) == report


@pytest.mark.needs_node
def test_cli_test_real_case_glob_inputs_aggregates_branches_modules_and_emits_junit(tmp: Path) -> None:
    xml, report = tmp / "results.xml", tmp / "suite.json"
    r = cli("test", *CASES, "--junit", xml, "--report", report, cwd=tmp)
    assert r.status == 0, r.stderr
    assert re.search(r"4 cases, 4 passed, 0 failed; modules 8/8 executed, branches 3/3", r.stdout)
    assert re.search(r'tests="4" failures="0"', read_text(xml))
    suite = read_json(report)
    assert suite["passed"] == 4
    assert len(suite["coverage"]) == 1


@pytest.mark.needs_node
def test_cli_run_exit_1_for_missing_mock_explicit_null_and_echo_policies_work(tmp: Path) -> None:
    flow, input_path = base_files(tmp)
    missing = cli("run", flow, "--input", input_path, cwd=tmp)
    assert missing.status == 1
    assert json.loads(missing.stdout)["error"]["name"] == "MissingMock"
    for policy in ["null", "echo-args"]:
        r = cli("run", flow, "--input", input_path, f"--unmocked={policy}", cwd=tmp)
        assert r.status == 0, r.stderr
        assert json.loads(r.stdout)["result"] == (None if policy == "null" else {})


@pytest.mark.needs_node
def test_cli_mock_consumption_gate_reports_exact_usage_and_exits_1_for_unused_responses(tmp: Path) -> None:
    base = fixture("mock-consumption/")
    args = ["run", base + "flow.yaml", "--mocks", base + "mocks.yaml", "--require-all-mocks-used=true", "--input"]
    exact = cli(*args, base + "input.json", cwd=tmp)
    assert exact.status == 0, exact.stderr
    report = json.loads(exact.stdout)
    assert report["mock_usage"]["send"] == {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}
    assert [e["mock"]["call"] for e in report["trace"] if e.get("mock")] == [1, 2]
    trace = tmp / "unused-trace.json"
    unused = cli(*args, base + "input-one.json", "--trace", trace, cwd=tmp)
    assert unused.status == 1, unused.stderr
    failed = json.loads(unused.stdout)
    assert failed["error"]["name"] == "MockUnused"
    assert failed["mock_usage"]["send"] == {"expected": 2, "calls": 1, "consumed": 1, "remaining": 1, "extra": 0}
    assert read_json(trace) == failed


@pytest.mark.needs_node
def test_case_mock_consumption_option_cli_overrides_expected_errors_and_junit_diagnostics_work(tmp: Path) -> None:
    base_files(tmp)
    fixture_cases = [fixture(f"mock-consumption/cases/{name}.yaml") for name in ["exact", "unused", "extra"]]
    examples = cli("test", *fixture_cases, cwd=tmp)
    assert examples.status == 0, examples.stdout + examples.stderr
    assert re.search(r"3 cases, 3 passed, 0 failed", examples.stdout)
    c = write(tmp, "unused-mock-case.yaml", {"flow": "flow.yaml", "mocks": {"a": {"sequence": [{"result": 7}, {"result": 8}]}},
                                             "options": {"requireAllMocksUsed": True}, "expect": {"status": "ok"}})
    xml = tmp / "unused-mocks.xml"
    strict = cli("test", c, "--junit", xml, cwd=tmp)
    assert strict.status == 1, strict.stderr
    assert re.search(r"MockUnused at 'a'.*consumed 1 of 2", strict.stdout)
    assert re.search(r"MockUnused.*consumed 1 of 2", read_text(xml))
    assert cli("test", c, "--require-all-mocks-used", "false", cwd=tmp).status == 0
    loose = write(tmp, "loose-mock-case.yaml", {"flow": "flow.yaml", "mocks": {"a": {"sequence": [{"result": 7}, {"result": 8}]}},
                                                "options": {"requireAllMocksUsed": False}, "expect": {"status": "ok"}})
    assert cli("test", loose, cwd=tmp).status == 0
    assert cli("test", loose, "--require-all-mocks-used", "true", cwd=tmp).status == 1
    malformed = write(tmp, "bad-mock-option.yaml", {"flow": "flow.yaml", "options": {"requireAllMocksUsed": "true"}, "expect": {"status": "ok"}})
    invalid = cli("test", malformed, cwd=tmp)
    assert invalid.status == 2
    assert re.search(r"requireAllMocksUsed.*boolean", invalid.stderr)


@pytest.mark.needs_node
def test_sequence_argument_contracts_fail_same_count_swaps_in_cli_junit_and_replay(tmp: Path) -> None:
    base = fixture("mock-arguments/")
    args = ["run", base + "flow.yaml", "--mocks", base + "mocks.yaml", "--input"]
    ordered = cli(*args, base + "input.json", cwd=tmp)
    assert ordered.status == 0, ordered.stderr
    assert json.loads(ordered.stdout)["result"] == [{"sent": "alice"}, {"sent": "bob"}]
    swapped = cli(*args, base + "input-swapped.json", cwd=tmp)
    assert swapped.status == 1, swapped.stderr
    r = json.loads(swapped.stdout)
    assert r["mock_usage"]["send"] == {"expected": 2, "calls": 2, "consumed": 2, "remaining": 0, "extra": 0}
    assert r["error"]["name"] == "MockArgsMismatch"
    assert r["error"]["call"] == 1
    assert r["error"]["expected_args"] == {"recipient": "alice"}
    assert r["error"]["actual_args"] == {"recipient": "bob"}
    assert r["mock_errors"][1]["call"] == 2
    examples = cli("test", base + "cases/ordered.yaml", base + "cases/swapped.yaml", cwd=tmp)
    assert examples.status == 0, examples.stdout + examples.stderr
    assert re.search(r"2 cases, 2 passed, 0 failed", examples.stdout)

    negative = write(tmp, "swapped-args.json", {"flow": base + "flow.yaml", "input": {"items": ["bob", "alice"]},
                                                "mocks": base + "mocks.yaml", "expect": {"status": "ok"}})
    xml, bundle = tmp / "swapped-args.xml", tmp / "swapped-args-bundle.json"
    failed = cli("test", negative, "--junit", xml, "--bundle", bundle, cwd=tmp)
    assert failed.status == 1, failed.stderr
    assert re.search(r"""MockArgsMismatch at 'send'.*call 1: expected {"recipient":"alice"}, got {"recipient":"bob"}""", failed.stdout)
    assert re.search(r"""call 2: expected {"recipient":"bob"}, got {"recipient":"alice"}""", failed.stdout)
    assert re.search(r"MockArgsMismatch.*call 1.*alice.*bob", read_text(xml))
    replay_report = tmp / "swapped-replay.json"
    replay = cli("replay", bundle, "--case", "1", "--report", replay_report, cwd=tmp)
    assert replay.status == 1, replay.stderr
    replayed = read_json(replay_report)
    assert replayed["replay"]["reproduced"] is True
    assert replayed["cases"][0]["report"] == r
    malformed = write(tmp, "bad-args-mock.json", {"send": {"sequence": [{"expect_args": None, "result": 1}]}})
    invalid = cli("run", base + "flow.yaml", "--input", base + "input.json", "--mocks", malformed, cwd=tmp)
    assert invalid.status == 2
    assert re.search(r"mock send\[0\]\.expect_args: expected object", invalid.stderr)


@pytest.mark.needs_node
def test_cli_malformed_yaml_json_wrong_openflow_wrapper_and_unknown_options_exit_2(tmp: Path) -> None:
    flow, input_path = base_files(tmp)
    yaml_path = write(tmp, "bad.yaml", "value: [broken")
    wrong = write(tmp, "wrong.yaml", {"value": {"modules": []}})
    bad_json = write(tmp, "bad.json", "{broken")
    for args in [
        ["run", yaml_path, "--input", input_path], ["run", wrong, "--input", input_path], ["run", flow, "--input", bad_json],
        ["run", flow, "--input", input_path, "--unmocked", "ignore"], ["run", flow, "--input", input_path, "--timeout-ms", "oops"],
        ["run", flow, "--input", input_path, "--mystery", "x"], ["test"], ["run", flow],
        ["run", flow, "--input", input_path, "--input", input_path], ["test", "--junit"],
        ["run", flow, "--input", input_path, "--require-all-mocks-used", "yes"],
        ["test", *CASES, "--require-all-mocks-used"], ["test", *CASES, "--require-all-mocks-used=1"],
    ]:
        r = cli(*args, cwd=tmp)
        assert r.status == 2, {"args": args, "stdout": r.stdout, "stderr": r.stderr}
        assert re.search("Invalid input", r.stderr)


@pytest.mark.needs_node
def test_cli_expression_timeout_unknown_result_and_while_exhaustion_are_exit_1_with_step_id(tmp: Path) -> None:
    _, input_path = base_files(tmp)
    timeout = clone(SIMPLE)
    timeout["value"]["modules"][0]["value"]["input_transforms"]["x"] = {"type": "javascript", "expr": "(()=>{while(true){}})()"}
    unknown = clone(SIMPLE)
    unknown["value"]["modules"][0]["value"]["input_transforms"]["x"] = {"type": "javascript", "expr": "results.missing"}
    repeat = {"summary": "loop", "value": {"modules": [{"id": "repeat", "value": {"type": "whileloopflow", "skip_failures": False,
                                                                                     "modules": [{"id": "body", "value": {"type": "identity"}}]}}]}}
    for name, f, module_id, pattern in [
        ("timeout", timeout, "a", "timed out"), ("unknown", unknown, "a", "Unknown result"), ("loop", repeat, "repeat", "max-iterations"),
    ]:
        r = cli("run", write(tmp, f"{name}.yaml", f), "--input", input_path, "--unmocked", "null", "--timeout-ms", "10", "--max-iterations", "2", cwd=tmp)
        assert r.status == 1, r.stderr
        report = json.loads(r.stdout)
        assert report["error"]["step_id"] == module_id
        assert re.search(pattern, report["error"]["message"])


@pytest.mark.needs_node
def test_assertion_mismatch_exits_1_and_has_a_useful_diff_in_junit(tmp: Path) -> None:
    base_files(tmp)
    c = write(tmp, "failure-case.yaml", {"name": 'A < B & "quoted"', "flow": "flow.yaml", "mocks": {"a": {"result": 7}}, "expect": {"result": 99}})
    output = tmp / "failure.xml"
    r = cli("test", c, "--junit", output, cwd=tmp)
    assert r.status == 1, r.stderr
    assert re.search("expected 99, got 7", r.stdout)
    xml = read_text(output)
    assert re.search('failures="1"', xml)
    assert re.search("A &lt; B &amp; &quot;quoted&quot;", xml)
    assert re.search("expected 99, got 7", xml)


@pytest.mark.needs_node
def test_cases_can_assert_expected_runtime_failure_unasserted_runtime_failure_fails(tmp: Path) -> None:
    base_files(tmp)
    c = write(tmp, "expected-error.yaml", {"flow": "flow.yaml", "expect": {"error": {"name": "MissingMock", "step_id": "a"}}})
    assert cli("test", c, cwd=tmp).status == 0
    unasserted = write(tmp, "unasserted.yaml", {"flow": "flow.yaml", "expect": {"args": {"a": [{}]}}})
    assert cli("test", unasserted, cwd=tmp).status == 1


@pytest.mark.needs_node
def test_unknown_assertion_keys_unknown_module_ids_and_empty_assertions_are_invalid(tmp: Path) -> None:
    base_files(tmp)
    for i, expect in enumerate([{}, {"typo": 1}, {"args": {"missing": []}}, {"path": ["missing"]}, {"args": {"a": {}}}]):
        path = write(tmp, f"invalid-case-{i}.yaml", {"flow": "flow.yaml", "expect": expect})
        assert cli("test", path, cwd=tmp).status == 2


def test_flow_case_paths_resolve_relative_to_case_coverage_namespaces_distinct_flows(tmp: Path) -> None:
    base_files(tmp)
    sub = tmp / "cases"
    sub.mkdir()
    path = sub / "case.yaml"
    path.write_text(json.dumps({"flow": "../flow.yaml", "mocks": {"a": {"result": 1}}, "expect": {"result": 1}}), encoding="utf-8")
    second = write(tmp, "other-flow.yaml", SIMPLE)
    c2 = write(tmp, "other-case.yaml", {"flow": "other-flow.yaml", "mocks": {"a": {"result": 2}}, "expect": {"result": 2}})
    suite = run_cases([str(path), c2])
    assert suite["passed"] == 2
    assert len(suite["coverage"]) == 2
    assert os.path.exists(second)


def test_output_confinement_rejects_traversal_symlink_parents_leaves_and_existing_files(tmp: Path) -> None:
    out = tmp / "output"
    out.mkdir()
    existing = out / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    (out / "escape").symlink_to(tmp)
    (out / "leaf").symlink_to(existing)
    with pytest.raises(FormatError, match="escapes"):
        write_output(str(out / ".." / "escaped.json"), "{}", str(out))
    with pytest.raises(FormatError, match="escapes"):
        write_output(str(out / "escape" / "escaped.json"), "{}", str(out))
    with pytest.raises(FormatError, match="cannot write"):
        write_output(str(out / "leaf"), "{}", str(out))
    with pytest.raises(FormatError, match="cannot write"):
        write_output(str(existing), "{}", str(out))
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (tmp / "escaped.json").exists()
    valid = out / "new.json"
    write_output(str(valid), "{}", str(out))
    assert valid.read_text(encoding="utf-8") == "{}"


@pytest.mark.needs_node
def test_cli_enforces_output_dir_and_never_overwrites_previous_traces(tmp: Path) -> None:
    flow, input_path = base_files(tmp)
    out = tmp / "cli-output"
    out.mkdir()
    inside = out / "new.json"
    args = ["run", flow, "--input", input_path, "--unmocked", "null", "--output-dir", out, "--trace"]
    assert cli(*args, tmp / "outside.json", cwd=tmp).status == 2
    assert cli(*args, inside, cwd=tmp).status == 0
    assert cli(*args, inside, cwd=tmp).status == 2


def test_yaml_aliases_duplicate_keys_custom_tags_multiple_documents_and_dangerous_keys_reject(tmp: Path) -> None:
    for i, source in enumerate([
        "a: &x [1]\nb: *x", "a: 1\na: 2", "a: !execute hello", "a: 1\n---\nb: 2",
        "__proto__: {polluted: true}", "constructor: bad", "a: .nan",
    ]):
        with pytest.raises(FormatError):
            read_data(write(tmp, f"yaml-unsafe-{i}.yaml", source))


def test_input_file_size_and_nesting_limits_reject_before_execution(tmp: Path) -> None:
    with pytest.raises(FormatError, match="1 MiB"):
        read_data(write(tmp, "large.json", " " * (1024 * 1024 + 1)))
    with pytest.raises(FormatError, match="nesting"):
        read_data(write(tmp, "nested.json", "[" * 70 + "0" + "]" * 70))


def test_help_version_work_errors_do_not_print_stack_traces(tmp: Path) -> None:
    assert re.search("No workflow code", cli("--help", cwd=tmp).stdout)
    assert cli("--version", cwd=tmp).stdout.strip() == "0.1.0"
    r = cli("unknown", cwd=tmp)
    assert r.status == 2
    assert " at " not in r.stderr
    assert "Traceback" not in r.stderr


def test_run_test_and_replay_exit_2_with_one_line_message_without_node(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Python-only: expression evaluation needs Node.js; refuse clearly without it.
    monkeypatch.setattr(cli_module, "find_node", lambda: None)
    flow, input_path = base_files(tmp)
    for args in [["run", flow, "--input", input_path], ["test", *CASES], ["replay", tmp / "missing.json", "--case", "1"]]:
        r = cli(*args, cwd=tmp)
        assert r.status == 2
        assert r.stdout == ""
        assert r.stderr == NODE_REQUIRED_MESSAGE + "\n"
    assert cli("--help", cwd=tmp).status == 0
