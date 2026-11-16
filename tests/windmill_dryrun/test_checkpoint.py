"""Port of test/checkpoint.test.js."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from flowwrench.windmill_dryrun import fileio
from flowwrench.windmill_dryrun.bundle import encode_bundle
from flowwrench.windmill_dryrun.cases import iterate_cases, load_cases, run_case_entries
from flowwrench.windmill_dryrun.errors import FormatError
from flowwrench.windmill_dryrun.fileio import checkpoint_outputs

from .helpers import cli, read_json, read_text, subprocess_command, subprocess_env, write_json

PREFIX = "Invalid input: "


@dataclass
class Fixture:
    dir: Path
    flow: str
    passing: str
    failing: str
    bundle: str


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    flow = write_json(tmp_path / "flow.json", {"summary": "checkpoint", "value": {"modules": [
        {"id": "work", "value": {"type": "script", "path": "/never-loaded", "input_transforms": {}}},
    ]}})
    passing = write_json(tmp_path / "pass.json", {"name": "passing", "flow": "flow.json", "mocks": {"work": {"result": 7}}, "expect": {"result": 7}})
    failing = write_json(tmp_path / "fail.json", {"name": "failing", "flow": "flow.json", "mocks": {"work": {"result": 7}}, "expect": {"result": 99}})
    return Fixture(tmp_path, flow, passing, failing, str(tmp_path / "bundle.json"))


def terminal_message(stderr: str) -> str:
    """``stderr.slice('Invalid input: '.length, -1)``."""
    return stderr[len(PREFIX):-1]


def temporaries(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if name.startswith(".openflow-dryrun-")]


# Contract: loading/executing a later invalid case exits 2, but every earlier
# completed outcome (including failures) is already durably named and replayable.
@pytest.mark.needs_node
def test_later_malformed_cases_retain_completed_prefix_hashes_xml_and_standalone_replay(fx: Fixture) -> None:
    malformed = fx.dir / "bad.yaml"
    malformed.write_text("flow: [broken", encoding="utf-8")
    report, xml = fx.dir / "suite.json", fx.dir / "chosen.xml"
    entries = load_cases([fx.passing, fx.failing])
    original = json.loads(encode_bundle(entries, run_case_entries(entries), fx.bundle, str(xml), 3))
    result = cli("test", fx.passing, fx.failing, malformed, "--bundle", fx.bundle, "--junit", xml, "--report", report, cwd=fx.dir)
    assert result.status == 2, result.stderr
    assert re.search("Invalid input", result.stderr)
    assert os.path.exists(fx.bundle), "completed cases must be saved before loading bad.yaml"
    saved = read_json(fx.bundle)
    assert saved["checkpoint"] == {"total": 3, "completed": 2, "complete": False}
    assert saved["terminal_error"] == {"case": 3, "path": str(malformed), "message": terminal_message(result.stderr)}
    assert [[c["id"], c["outcome"]["passed"]] for c in saved["cases"]] == [[1, True], [2, False]]
    assert saved["cases"] == original["cases"], "terminal metadata must not change hashes, ids or diagnostics"
    assert saved["cases"][1]["junit"]["href"] == "chosen.xml"
    assert saved["cases"][1]["junit"]["testcase_index"] == 1
    assert saved["junit_xml"] == read_text(xml)
    assert re.search(r'tests="2" failures="1"', saved["junit_xml"])
    assert not report.exists(), "--report remains final-only"
    assert os.stat(fx.bundle).st_mode & 0o777 == 0o600
    for path in [fx.flow, fx.passing, fx.failing, malformed, xml]:
        os.remove(path)
    for case_id, exit_code in [(1, 0), (2, 1)]:
        replay = cli("replay", fx.bundle, "--case", str(case_id), cwd=fx.dir)
        assert replay.status == exit_code, replay.stderr
        assert re.search("reproduced", replay.stdout)
    assert cli("replay", fx.bundle, "--case", "3", cwd=fx.dir).status == 2


@pytest.mark.needs_node
def test_later_input_mock_flow_and_option_format_errors_do_not_erase_an_earlier_outcome(fx: Fixture) -> None:
    invalid = [
        {"flow": "flow.json", "input": [], "expect": {"status": "ok"}},
        {"flow": "flow.json", "mocks": {"work": {"sequence": []}}, "expect": {"status": "ok"}},
        {"flow": "missing.json", "expect": {"status": "ok"}},
        {"flow": "flow.json", "options": {"maxSteps": 0}, "expect": {"status": "ok"}},
    ]
    for i, value in enumerate(invalid):
        bad = write_json(fx.dir / f"invalid-{i}.json", value)
        bundle = str(fx.dir / f"saved-{i}.json")
        result = cli("test", fx.passing, bad, "--bundle", bundle, cwd=fx.dir)
        assert result.status == 2, result.stderr
        assert os.path.exists(bundle), f"missing prefix for invalid case {i}"
        assert read_json(bundle)["checkpoint"] == {"total": 2, "completed": 1, "complete": False}
        assert read_json(bundle)["cases"][0]["outcome"]["passed"] is True
        assert read_json(bundle)["terminal_error"] == {"case": 2, "path": bad, "message": terminal_message(result.stderr)}


@pytest.mark.needs_node
def test_completed_suites_mark_complete_and_old_v1_bundles_still_replay(fx: Fixture) -> None:
    report = fx.dir / "suite.json"
    result = cli("test", fx.passing, fx.failing, fx.passing, "--bundle", fx.bundle, "--report", report, cwd=fx.dir)
    assert result.status == 1, result.stderr
    saved = read_json(fx.bundle)
    assert saved["checkpoint"] == {"total": 3, "completed": 3, "complete": True}
    assert "terminal_error" not in saved
    assert [c["outcome"] for c in saved["cases"]] == read_json(report)["cases"]
    assert saved["junit_xml"] == read_text(fx.bundle + ".junit.xml")
    del saved["checkpoint"]
    legacy = write_json(fx.dir / "legacy.json", saved)
    assert cli("replay", legacy, "--case", "1", cwd=fx.dir).status == 0
    for i, checkpoint in enumerate([
        {"total": 2, "completed": 3, "complete": True},
        {"total": 3, "completed": 2, "complete": False},
        {"total": 4, "completed": 3, "complete": True},
        {"total": 3.5, "completed": 3, "complete": False},
        None,
    ]):
        saved["checkpoint"] = checkpoint
        corrupt = write_json(fx.dir / f"bad-checkpoint-{i}.json", saved)
        replay = cli("replay", corrupt, "--case", "1", cwd=fx.dir)
        assert replay.status == 2, replay.stderr
        assert re.search("checkpoint", replay.stderr)


@pytest.mark.needs_node
def test_invalid_first_case_creates_no_artifacts_preexisting_aliasing_and_escaping_outputs_reject(fx: Fixture) -> None:
    bad = write_json(fx.dir / "bad.json", {"flow": "flow.json", "expect": {}})
    assert cli("test", bad, "--bundle", fx.bundle, cwd=fx.dir).status == 2
    assert not os.path.exists(fx.bundle)
    assert not os.path.exists(fx.bundle + ".junit.xml")
    existing = fx.dir / "keep.xml"
    existing.write_text("keep", encoding="utf-8")
    configs = [
        ["--junit", existing], ["--report", existing], ["--junit", fx.bundle], ["--report", fx.bundle],
        ["--report", fx.bundle + ".junit.xml"], ["--junit", fx.dir / "absent" / "out.xml"],
    ]
    for args in configs:
        assert cli("test", fx.passing, "--bundle", fx.bundle, *args, cwd=fx.dir).status == 2
        assert not os.path.exists(fx.bundle), f"unexpected bundle for {args}"
        assert not os.path.exists(fx.bundle + ".junit.xml")
        assert existing.read_text(encoding="utf-8") == "keep"
    out = fx.dir / "out"
    out.mkdir()
    (out / "escape").symlink_to(fx.dir)
    (out / "leaf").symlink_to(existing)
    (out / "alias").symlink_to(out)
    for path in [out / "escape" / "escaped.json", out / "leaf"]:
        assert cli("test", fx.passing, "--bundle", path, "--output-dir", out, cwd=fx.dir).status == 2
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (fx.dir / "escaped.json").exists()
    assert cli("test", fx.passing, "--bundle", out / "same.json", "--junit", out / "alias" / "same.json", "--output-dir", out, cwd=fx.dir).status == 2
    assert not (out / "same.json").exists()


def test_atomic_writer_preserves_its_last_file_on_write_failure_and_refuses_files_installed_by_others(fx: Fixture) -> None:
    other = str(fx.dir / "raced.json")
    output = checkpoint_outputs([fx.bundle, other], str(fx.dir))
    first, second = json.dumps({"completed": 1}), json.dumps({"completed": 2})
    output(fx.bundle, first)
    # File-write failure after opening a temporary file cannot truncate the target.
    with pytest.raises(FormatError, match="cannot write"):
        output(fx.bundle, {})  # type: ignore[arg-type]
    assert read_text(fx.bundle) == first
    output(fx.bundle, second)
    assert read_text(fx.bundle) == second
    assert os.stat(fx.bundle).st_mode & 0o777 == 0o600
    Path(other).write_text("someone else", encoding="utf-8")
    with pytest.raises(FormatError, match="cannot write"):
        output(other, "overwrite")
    assert read_text(other) == "someone else"
    replacement = fx.dir / "replacement.json"
    replacement.write_text("replacement", encoding="utf-8")
    os.remove(fx.bundle)
    os.symlink(replacement, fx.bundle)
    with pytest.raises(FormatError, match="checkpoint output was replaced"):
        output(fx.bundle, "overwrite")
    assert replacement.read_text(encoding="utf-8") == "replacement"
    assert temporaries(fx.dir) == []


@pytest.mark.needs_node
def test_an_oversized_next_encoding_leaves_the_previous_checkpoint_replayable(fx: Fixture) -> None:
    # Every source file is below 1 MiB. Repeating a large completed case makes the
    # next aggregate exceed 64 MiB without relaxing the source or report budgets.
    large = write_json(fx.dir / "large.json", {"flow": "flow.json", "input": {"text": "x" * 900000},
                                               "mocks": {"work": {"result": None}}, "expect": {"result": None}})
    entries = load_cases([fx.passing, large])
    suite = run_case_entries(entries)
    output = checkpoint_outputs([fx.bundle], str(fx.dir))
    first = encode_bundle([entries[0]], {**suite, "cases": [suite["cases"][0]], "passed": 1, "failed": 0},
                          fx.bundle, fx.bundle + ".junit.xml", 100)
    output(fx.bundle, first)
    many = [entries[0], *[entries[1]] * 80]
    outcomes = [suite["cases"][0], *[suite["cases"][1]] * 80]
    with pytest.raises(FormatError, match="64 MiB"):
        output(fx.bundle, encode_bundle(many, {**suite, "cases": outcomes, "passed": 81, "failed": 0},
                                        fx.bundle, fx.bundle + ".junit.xml", 100))
    assert read_text(fx.bundle) == first
    assert cli("replay", fx.bundle, "--case", "1", cwd=fx.dir).status == 0


# A live observation is essential: a post-suite write alone cannot pass this.
@pytest.mark.needs_node
def test_sigterm_retains_the_last_parseable_completed_case_checkpoint(fx: Fixture) -> None:
    write_json(fx.dir / "slow-flow.json", {"summary": "bounded slow expression", "value": {"modules": [
        {"id": "slow", "value": {"type": "script", "path": "/never-loaded", "input_transforms": {
            "x": {"type": "javascript", "expr": "(()=>{while(true){}})()"},
        }}},
    ]}})
    slow = write_json(fx.dir / "slow.json", {"flow": "slow-flow.json", "options": {"timeoutMs": 2000}, "expect": {"status": "ok"}})
    child = subprocess.Popen(
        subprocess_command("test", fx.passing, slow, "--bundle", fx.bundle),
        cwd=fx.dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=subprocess_env(),
    )
    try:
        deadline = time.monotonic() + 10
        while not os.path.exists(fx.bundle) and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert os.path.exists(fx.bundle), "must expose a checkpoint during execution"
        saved = read_text(fx.bundle)
        child.send_signal(signal.SIGTERM)
        code = child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert code == -signal.SIGTERM
    assert json.loads(saved)["checkpoint"] == {"total": 2, "completed": 1, "complete": False}
    assert "terminal_error" not in json.loads(saved)
    assert read_text(fx.bundle) == saved
    assert cli("replay", fx.bundle, "--case", "1", cwd=fx.dir).status == 0
    assert "bundle.json" in os.listdir(fx.dir)


@pytest.mark.needs_node
def test_later_missing_case_records_the_original_relative_argument_and_stops_before_remaining_cases(fx: Fixture) -> None:
    result = cli("test", "pass.json", "fail.json", "missing-case.yaml", "pass.json", "--bundle", fx.bundle, cwd=fx.dir)
    assert result.status == 2, result.stderr
    assert re.search("ENOENT", result.stderr)
    saved = read_json(fx.bundle)
    assert saved["checkpoint"] == {"total": 4, "completed": 2, "complete": False}
    assert saved["terminal_error"] == {"case": 3, "path": "missing-case.yaml", "message": terminal_message(result.stderr)}
    entries = load_cases([str(fx.dir / "pass.json"), str(fx.dir / "fail.json")])
    # Baseline uses identical relative identity metadata to compare every saved byte.
    entries[0]["path"] = "pass.json"
    entries[1]["path"] = "fail.json"
    original = json.loads(encode_bundle(entries, run_case_entries(entries), fx.bundle, fx.bundle + ".junit.xml", 4))
    assert saved["cases"] == original["cases"]
    for path in [fx.flow, fx.passing, fx.failing, fx.bundle + ".junit.xml"]:
        os.remove(path)
    for case_id, exit_code in [(1, 0), (2, 1)]:
        replay = cli("replay", fx.bundle, "--case", str(case_id), cwd=fx.dir)
        assert replay.status == exit_code, replay.stderr
        assert re.search("reproduced", replay.stdout)


@pytest.mark.needs_node
def test_terminal_metadata_validates_shape_and_checkpoint_consistency_while_older_partial_bundles_replay(fx: Fixture) -> None:
    assert cli("test", fx.passing, "--bundle", fx.bundle, cwd=fx.dir).status == 0
    original = read_json(fx.bundle)
    original["checkpoint"] = {"total": 2, "completed": 1, "complete": False}
    terminal = {"case": 2, "path": "missing.yaml", "message": "missing.yaml: no such file"}

    def save_replay(name: str, value: Any) -> Any:
        return cli("replay", write_json(fx.dir / f"{name}.json", value), "--case", "1", cwd=fx.dir)

    assert save_replay("in-progress", original).status == 0
    original["terminal_error"] = terminal
    assert save_replay("terminal", original).status == 0

    def without(field: str) -> dict[str, Any]:  # JSON.stringify drops undefined fields
        return {key: value for key, value in terminal.items() if key != field}

    invalid: list[Any] = [None, [], {}, "error"]
    invalid += [without("case")] + [{**terminal, "case": value} for value in [0, 1, 3, 1.5, 2**53, "2", None]]
    invalid += [without("path")] + [{**terminal, "path": value} for value in ["", None, 2, [], {}]]
    invalid += [without("message")] + [{**terminal, "message": value} for value in ["", None, 2, [], {}]]
    invalid += [{**terminal, "unknown": True}]
    for i, error in enumerate(invalid):
        replay = save_replay(f"invalid-terminal-{i}", {**original, "terminal_error": error})
        assert replay.status == 2, replay.stderr
        assert re.search("terminal_error", replay.stderr)
        assert replay.stdout == ""
    without_checkpoint = {key: value for key, value in original.items() if key != "checkpoint"}
    for i, value in enumerate([without_checkpoint, {**original, "checkpoint": {"total": 1, "completed": 1, "complete": True}}]):
        replay = save_replay(f"invalid-terminal-checkpoint-{i}", value)
        assert replay.status == 2, replay.stderr
        assert re.search("terminal_error", replay.stderr)
    entries = load_cases([fx.passing])
    with pytest.raises(FormatError, match="terminal_error"):
        encode_bundle(entries, run_case_entries(entries), fx.bundle, fx.bundle + ".junit.xml", 1, terminal)


@pytest.mark.needs_node
def test_terminal_write_failure_preserves_the_previous_checkpoint_and_original_invalid_input_diagnostic(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = fileio._write_fd

    def failing_write(fd: int, content: str) -> None:
        if isinstance(content, str) and '"terminal_error":' in content:
            raise OSError("injected terminal write failure")
        original_write(fd, content)

    monkeypatch.setattr(fileio, "_write_fd", failing_write)
    result = cli("test", fx.passing, fx.failing, "missing.yaml", "--bundle", fx.bundle, cwd=fx.dir)
    monkeypatch.undo()
    assert result.status == 2, result.stderr
    assert re.search(r"Cannot save terminal diagnostics:.*injected terminal write failure", result.stderr)
    assert re.search(r"Invalid input: missing.yaml:.*ENOENT", result.stderr)
    saved = read_json(fx.bundle)
    entries = load_cases([fx.passing, fx.failing])
    original = encode_bundle(entries, run_case_entries(entries), fx.bundle, fx.bundle + ".junit.xml", 3)
    assert read_text(fx.bundle) == original, "failed terminal write must retain the exact preceding checkpoint"
    assert "terminal_error" not in saved
    assert temporaries(fx.dir) == []
    assert cli("replay", fx.bundle, "--case", "2", cwd=fx.dir).status == 1


def test_checkpoint_callback_errors_are_never_treated_as_invalid_requested_cases(fx: Fixture) -> None:
    terminal_called = []
    error = FormatError("injected checkpoint encoding/IO error")

    def on_case(entry: Any, suite: Any) -> None:
        raise error

    with pytest.raises(FormatError) as caught:
        run_case_entries(iterate_cases([fx.passing, "missing.yaml"]), on_case, terminal_called.append)
    assert caught.value is error
    assert terminal_called == []
