"""Port of test/cli.test.js."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from flowwrench.nodered_merge.flows import merge_flows, read_flow

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return str(FIXTURES / f"{name}.json")


def parse(file: str | os.PathLike[str]) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


def write(directory: Path, name: str, value: Any) -> str:
    file = directory / name
    file.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return str(file)


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    return next((n for n in flow if n["id"] == node_id), None)


def test_real_format_fixture_e2e_preserves_edits_layout_added_node_and_wires(run_cli, sandbox: Path) -> None:
    out = sandbox / "merged.json"
    result = run_cli(["merge", fixture("base"), fixture("ours"), fixture("theirs"), "-o", out])
    assert result.returncode == 0, result.stderr
    flow = parse(out)
    assert find(flow, "function-a")["func"] == find(read_flow(fixture("ours")), "function-a")["func"]
    assert find(flow, "function-b")["func"] == find(read_flow(fixture("theirs")), "function-b")["func"]
    assert find(flow, "function-a")["x"] == 320
    assert find(flow, "function-a")["wires"] == [["debug-a", "function-b"]]
    assert find(flow, "debug-a")
    assert parse(f"{out}.conflicts.json")["clean"] is True


def test_cli_diff_text_and_json_classify_fixtures_including_named_wire_additions(run_cli) -> None:
    result = run_cli(["diff", fixture("base"), fixture("theirs"), "--ignore-layout", "--format", "json"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["added"][0]["id"] == "debug-a"
    text = run_cli(["diff", fixture("base"), fixture("theirs")])
    assert re.search(r"port 0 \+ Inspect prepared value \[debug-a\]", text.stdout)


def test_cli_conflicts_exit_1_retain_valid_json_and_explicit_sidecar_values_even_with_prefer(run_cli, sandbox: Path) -> None:
    flow = read_flow(fixture("base"))
    ours, theirs = copy.deepcopy(flow), copy.deepcopy(flow)
    find(ours, "function-a")["func"] = "ours"
    find(theirs, "function-a")["func"] = "theirs"
    o, th = write(sandbox, "ours.json", ours), write(sandbox, "theirs.json", theirs)
    out, report = sandbox / "merged.json", sandbox / "report.json"
    result = run_cli(["merge", fixture("base"), o, th, "-o", out, "--conflicts-file", report, "--prefer", "theirs"])
    assert result.returncode == 1, result.stderr
    assert find(parse(out), "function-a")["func"] == "theirs"
    assert parse(report)["conflicts"][0]["base"]["value"] == find(flow, "function-a")["func"]
    assert parse(report)["conflicts"][0]["ours"]["value"] == "ours"
    assert parse(report)["conflicts"][0]["theirs"]["value"] == "theirs"
    assert not re.search(r"<<<<<<<|=======|>>>>>>>", out.read_text(encoding="utf-8"))


BAD_INPUTS = [
    ("invalid JSON", "[broken SECRET_INPUT"),
    ("non-array", "{}"),
    ("duplicate IDs", '[{"id":"x","type":"inject"},{"id":"x","type":"debug"}]'),
    ("invalid wires", '[{"id":"x","type":"inject","wires":["bad"]}]'),
]


@pytest.mark.parametrize(("name", "content"), BAD_INPUTS, ids=[name for name, _ in BAD_INPUTS])
def test_cli_bad_input_exits_2_with_both_output_files_untouched(run_cli, sandbox: Path, name: str, content: str) -> None:
    bad, out, report = sandbox / "bad.json", sandbox / "out.json", sandbox / "report.json"
    bad.write_text(content, encoding="utf-8")
    out.write_text("KEEP OUT", encoding="utf-8")
    report.write_text("KEEP REPORT", encoding="utf-8")
    result = run_cli(["merge", fixture("base"), fixture("ours"), bad, "-o", out, "--conflicts-file", report])
    assert result.returncode == 2
    assert out.read_text(encoding="utf-8") == "KEEP OUT"
    assert report.read_text(encoding="utf-8") == "KEEP REPORT"
    assert "SECRET_INPUT" not in result.stderr


def test_missing_input_invalid_options_output_directory_errors_fail_without_writing(run_cli, sandbox: Path) -> None:
    out = sandbox / "out.json"
    for args in [
        ["merge", fixture("base"), fixture("ours"), sandbox / "missing", "-o", out],
        ["merge", fixture("base"), fixture("ours"), fixture("theirs"), "-o", out, "--prefer", "unknown"],
        ["diff", fixture("base"), fixture("theirs"), "--format", "yaml"],
        ["diff", fixture("base"), fixture("theirs"), "--unknown"],
        ["normalize", fixture("base"), "-o"],
        ["merge", fixture("base"), fixture("ours"), fixture("theirs"), "-o", out, "--conflicts-file", sandbox / "missing" / "report.json"],
    ]:
        result = run_cli(args)
        assert result.returncode == 2, f"{' '.join(map(str, args))}\n{result.stderr}"
        assert "out.json" not in os.listdir(sandbox)


def test_reports_cannot_alias_output_or_any_input_including_symlinks_and_hard_links(run_cli, sandbox: Path) -> None:
    source = write(sandbox, "input.json", [])
    out = sandbox / "out.json"
    hard, symbolic = sandbox / "hard.json", sandbox / "symbolic.json"
    os.link(source, hard)
    os.symlink(source, symbolic)
    for report in [source, hard, symbolic, out]:
        result = run_cli(["merge", source, source, source, "-o", out, "--conflicts-file", report])
        assert result.returncode == 2, result.stderr
        assert Path(source).read_text(encoding="utf-8") == "[]"
        assert "out.json" not in os.listdir(sandbox)


def test_symlink_outputs_are_rejected_normalize_supports_atomic_in_place_writes_and_preserves_mode(run_cli, sandbox: Path) -> None:
    file = Path(write(sandbox, "flow.json", read_flow(fixture("base"))))
    alias = sandbox / "link.json"
    os.symlink(file, alias)
    assert run_cli(["normalize", file, "-o", alias]).returncode == 2
    file.chmod(0o664)
    mode = file.stat().st_mode & 0o777
    result = run_cli(["normalize", file, "-o", file])
    assert result.returncode == 0, result.stderr
    assert file.stat().st_mode & 0o777 == mode
    assert run_cli(["normalize", file]).stdout == file.read_text(encoding="utf-8")
    assert run_cli(["textconv", file]).stdout == file.read_text(encoding="utf-8")


def test_cli_explicit_config_reference_option_checks_missing_config_ids(run_cli, sandbox: Path) -> None:
    flow = [{"id": "a", "type": "mqtt in", "broker": "missing", "x": 1, "y": 1, "wires": []}]
    source, out = write(sandbox, "input.json", flow), sandbox / "out.json"
    result = run_cli(["merge", source, source, source, "-o", out, "--config-ref", "mqtt in.broker"])
    assert result.returncode == 1, result.stderr
    assert parse(f"{out}.conflicts.json")["integrity"][0]["kind"] == "missing-config"


def test_install_dry_run_is_read_only_apply_is_idempotent_and_preserves_other_attributes(run_cli, sandbox: Path, init_repo, git_ok) -> None:
    init_repo(sandbox)
    attrs = sandbox / ".gitattributes"
    attrs.write_text("*.txt text\n", encoding="utf-8")
    config = (sandbox / ".git/config").read_text(encoding="utf-8")
    dry = run_cli(["install", "--repo", sandbox])
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["applied"] is False
    assert attrs.read_text(encoding="utf-8") == "*.txt text\n"
    assert (sandbox / ".git/config").read_text(encoding="utf-8") == config
    assert json.loads(dry.stdout)["attributes"] == "flows.json merge=nrflow-merge diff=nrflow-merge"
    for _ in range(2):
        assert run_cli(["install", "--repo", sandbox, "--apply"]).returncode == 0
    assert len(attrs.read_text(encoding="utf-8").split("# BEGIN nrflow-merge")) == 2
    assert re.match(r"\*\.txt text", attrs.read_text(encoding="utf-8"))
    assert re.search("git-driver", git_ok(sandbox, "config", "--get", "merge.nrflow-merge.driver"))
    assert git_ok(sandbox, "check-attr", "merge", "--", "flows.json") == "flows.json: merge: nrflow-merge"
    assert git_ok(sandbox, "check-attr", "merge", "--", "flows_cred.json") == "flows_cred.json: merge: unspecified"


def test_install_rejects_unsafe_patterns_malformed_blocks_and_symlink_attributes_before_mutation(run_cli, sandbox: Path, init_repo) -> None:
    init_repo(sandbox)
    original = (sandbox / ".git/config").read_text(encoding="utf-8")
    for pattern in ["../outside", "/absolute", "flows.json\n*.json", "$(touch injected)", "-x"]:
        assert run_cli(["install", "--repo", sandbox, "--pattern", pattern, "--apply"]).returncode == 2
    (sandbox / ".gitattributes").write_text("# BEGIN nrflow-merge\n", encoding="utf-8")
    assert run_cli(["install", "--repo", sandbox, "--apply"]).returncode == 2
    (sandbox / ".gitattributes").unlink()
    os.symlink(sandbox / "target", sandbox / ".gitattributes")
    assert run_cli(["install", "--repo", sandbox, "--apply"]).returncode == 2
    assert (sandbox / ".git/config").read_text(encoding="utf-8") == original
    assert "injected" not in os.listdir(sandbox)


@pytest.mark.parametrize(
    "conflicting",
    [False, True],
    ids=["semantic merge and textconv succeed", "conflict returns unmerged index with JSON + report"],
)
def test_real_git_e2e(run_cli, sandbox: Path, init_repo, git, git_ok, make_shim, conflicting: bool) -> None:
    init_repo(sandbox)
    # The driver is "flowwrench nodered-merge ..."; put the command in a directory
    # with spaces and apostrophes on PATH to exercise Git's shell invocation.
    executable, env = make_shim(sandbox / "tool space's")
    install = run_cli(["install", "--repo", sandbox, "--apply"], env=env, executable=executable)
    assert install.returncode == 0, install.stderr
    start, ours, theirs = read_flow(fixture("base")), read_flow(fixture("ours")), read_flow(fixture("theirs"))
    if conflicting:
        find(theirs, "function-a")["func"] = "return null;"
    flow_file = Path(write(sandbox, "flows.json", start))
    git_ok(sandbox, "add", "flows.json", ".gitattributes")
    git_ok(sandbox, "commit", "-m", "base")
    git_ok(sandbox, "checkout", "-b", "theirs")
    flow_file.write_text(json.dumps(theirs, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    git_ok(sandbox, "add", "flows.json")
    git_ok(sandbox, "commit", "-m", "theirs")
    git_ok(sandbox, "checkout", "main")
    flow_file.write_text(json.dumps(ours, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    git_ok(sandbox, "add", "flows.json")
    git_ok(sandbox, "commit", "-m", "ours")
    result = git(sandbox, "merge", "--no-edit", "theirs", env=env)
    assert result.returncode == (1 if conflicting else 0), f"{result.stdout}\n{result.stderr}"
    actual, expected = parse(flow_file), merge_flows(start, ours, theirs)["flows"]
    assert actual == expected
    report_dir = sandbox / ".git/nrflow-merge-conflicts"
    reports = os.listdir(report_dir)
    assert len(reports) == 1
    assert parse(report_dir / reports[0])["clean"] is (not conflicting)
    if conflicting:
        assert re.search(r"UU flows.json", git_ok(sandbox, "status", "--porcelain"))
        assert not re.search(r"<<<<<<<|>>>>>>>", flow_file.read_text(encoding="utf-8"))
    else:
        assert git_ok(sandbox, "ls-files", "-u") == ""
        diff = git(sandbox, "diff", "--textconv", "HEAD~1", "HEAD", "--", "flows.json", env=env)
        assert diff.returncode == 0, diff.stderr
        assert re.search(r'"id": "debug-a"', diff.stdout)


def test_oversized_sparse_file_and_directory_input_fail_without_being_read_as_flows(run_cli, sandbox: Path) -> None:
    huge = sandbox / "huge.json"
    # Sparse file exercises the size guard without allocating a large fixture.
    with huge.open("wb") as handle:
        handle.truncate(33554433)
    assert run_cli(["normalize", huge]).returncode == 2
    assert run_cli(["normalize", sandbox]).returncode == 2


def test_installed_flowwrench_command_runs_real_fixture_behavior(run_cli, sandbox: Path, init_repo, git_ok, make_shim) -> None:
    """Port of the offline npm tarball test.

    Python packaging is outside this subpackage, so instead of ``npm pack`` and
    ``npm install`` this drives the ``flowwrench nodered-merge`` command (the test
    shim standing in for the console script) through the same workflow.
    """
    executable, env = make_shim(sandbox / "installed-bin")

    def cli(args: list[Any]) -> Any:
        return run_cli(args, executable=executable, env=env)

    version = cli(["--version"])
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "0.1.0"
    out = sandbox / "packaged-merge.json"
    merged = cli(["merge", fixture("base"), fixture("ours"), fixture("theirs"), "-o", out])
    assert merged.returncode == 0, merged.stderr
    assert len(parse(out)) == 7
    assert parse(f"{out}.conflicts.json")["clean"] is True
    # Installed CLI: named config/wire summaries and layout filtering use packaged fixtures.
    summary_inputs = [fixture(f"summary/{role}") for role in ("base", "ours", "theirs")]
    summarized = cli(["merge", *summary_inputs, "-o", out, "--ignore-layout"])
    assert summarized.returncode == 0, summarized.stderr
    summary = parse(f"{out}.conflicts.json")["summary"]
    assert summary["merged"]["layoutOnly"] == []
    reading = find(summary["merged"]["changed"], "a")
    assert [p["property"] for p in reading["properties"]] == ["/broker", "/wires"]
    assert reading["configReferences"]["added"][0]["name"] == "Backup [backup]"
    assert reading["wireChanges"][0]["added"][0]["name"] == "Archive [c]"
    assert find(parse(out), "a")["x"] == 150
    explicit_before = write(sandbox, "explicit-before.json", [{"id": "n", "type": "custom", "server": ""}])
    explicit_after = write(sandbox, "explicit-after.json", [{"id": "n", "type": "custom", "server": "never-present"}])
    explicit = cli(["diff", explicit_before, explicit_after, "--config-ref", "custom.server"])
    assert explicit.returncode == 0, explicit.stderr
    assert re.search(r"config /server \+ \(missing\) \[never-present\] \(invalid: missing-target\)", explicit.stdout)
    # Exercise the installed command's complete decision workflow.
    conflicting = read_flow(fixture("theirs"))
    find(conflicting, "function-a")["func"] = "return null;"
    theirs = Path(write(sandbox, "packaged-theirs.json", conflicting))
    manifest = sandbox / "decisions.json"
    inputs = [fixture("base"), fixture("ours"), theirs]
    planned = cli(["merge", *inputs, "-o", out, "--manifest", manifest])
    assert planned.returncode == 1, planned.stderr
    decisions = parse(manifest)
    assert len(decisions["conflicts"]) == 1
    decisions["conflicts"][0]["decision"] = {"choice": "custom", "present": True, "value": "msg.reviewed = true; return msg;"}
    manifest.write_text(json.dumps(decisions), encoding="utf-8")
    resolved = cli(["resolve", *inputs, "--manifest", manifest, "-o", out])
    assert resolved.returncode == 0, resolved.stderr
    assert find(parse(out), "function-a")["func"] == "msg.reviewed = true; return msg;"
    assert find(parse(out), "function-a")["x"] == 320
    assert parse(f"{out}.conflicts.json")["clean"] is True
    before = out.read_text(encoding="utf-8")
    theirs.write_text(f"{theirs.read_text(encoding='utf-8')}\n", encoding="utf-8")
    stale = cli(["resolve", *inputs, "--manifest", manifest, "-o", out])
    assert stale.returncode == 2
    assert re.search("stale", stale.stderr)
    assert out.read_text(encoding="utf-8") == before
    # The installed command can recover a manifest from retained Git index objects alone.
    repo = sandbox / "index-repo"
    repo.mkdir()
    init_repo(repo)
    stages = []
    for i, role in enumerate(("base", "ours", "theirs")):
        blob = git_ok(repo, "hash-object", "-w", "--stdin", input=Path(fixture(f"resolution/{role}")).read_bytes())
        stages.append(f"100644 {blob} {i + 1}\tflows.json\n")
    git_ok(repo, "update-index", "--index-info", input="".join(stages))
    bundle = sandbox / "installed-index-review"
    captured = cli(["git-manifest", "flows.json", "--repo", repo, "--snapshot-dir", bundle])
    assert captured.returncode == 1, captured.stderr
    receipt = json.loads(captured.stdout)
    manifest_from_index = parse(receipt["manifest"])
    assert Path(receipt["inputs"][0]).read_bytes() == Path(fixture("resolution/base")).read_bytes()
    assert len(manifest_from_index["conflicts"]) == 1
    manifest_from_index["conflicts"][0]["decision"] = {"choice": "theirs"}
    Path(receipt["manifest"]).write_text(json.dumps(manifest_from_index), encoding="utf-8")
    recovered = cli(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", out])
    assert recovered.returncode == 0, recovered.stderr
    assert parse(out)[0]["func"] == "msg.payload = 1; return msg;"
    assert parse(out)[0]["x"] == 150
    assert parse(out)[0]["y"] == 200
    assert parse(f"{out}.conflicts.json")["clean"] is True


def test_invalid_utf8_is_rejected_and_function_code_is_never_executed(run_cli, sandbox: Path) -> None:
    invalid = sandbox / "invalid.json"
    invalid.write_bytes(b'[{"id":"n","type":"test","name":"' + bytes([0xFF]) + b'"}]')
    assert run_cli(["normalize", invalid]).returncode == 2
    sentinel = sandbox / "executed"
    flow = [{
        "id": "a", "type": "function", "x": 10, "y": 10, "wires": [],
        "func": f"require('node:fs').writeFileSync({json.dumps(str(sentinel))}, 'bad'); return msg;",
    }]
    source, out = write(sandbox, "code.json", flow), sandbox / "merged.json"
    assert run_cli(["merge", source, source, source, "-o", out]).returncode == 0
    assert "executed" not in os.listdir(sandbox)
    assert parse(out)[0]["func"] == flow[0]["func"]
