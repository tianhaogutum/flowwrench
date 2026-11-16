"""Port of test/git-manifest.test.js."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROLES = ("base", "ours", "theirs")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VERSIONS = [(FIXTURES / "resolution" / f"{role}.json").read_bytes() for role in ROLES]
ADDITIONS = [(FIXTURES / "add-add" / f"{role}.json").read_bytes() for role in ("ours", "theirs")]


def parse(file: str | os.PathLike[str]) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


def write_compact(file: str | os.PathLike[str], value: Any) -> None:
    Path(file).write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == node_id)


@pytest.fixture
def s(run_cli, sandbox: Path, init_repo) -> SimpleNamespace:
    repo = sandbox / "repo"
    repo.mkdir()
    init_repo(repo)

    def run(args: Sequence[Any], cwd: Path | None = None) -> Any:
        return run_cli(args, cwd=cwd or repo)

    return SimpleNamespace(dir=sandbox, repo=repo, bundle=sandbox / "review", run=run)


@pytest.fixture
def index(git_ok) -> Callable[..., list[str | None]]:
    def stage(
        s: SimpleNamespace,
        *,
        file: str = "flows.json",
        data: Sequence[bytes | None] = VERSIONS,
        modes: Sequence[str] = ("100644", "100644", "100644"),
    ) -> list[str | None]:
        entries = ""
        ids: list[str | None] = []
        for i, blob in enumerate(data):
            if blob is None:
                ids.append(None)
                continue
            object_id = git_ok(s.repo, "hash-object", "-w", "--stdin", input=blob)
            entries += f"{modes[i]} {object_id} {i + 1}\t{file}\0"
            ids.append(object_id)
        git_ok(s.repo, "update-index", "-z", "--index-info", input=entries)
        return ids

    return stage


def capture(s: SimpleNamespace, extra: Sequence[str] = ()) -> Any:
    return s.run(["git-manifest", "flows.json", "--snapshot-dir", s.bundle, *extra])


def unchanged_failure(s: SimpleNamespace, args: Sequence[Any], message: str) -> None:
    before = (s.repo / ".git/index").read_bytes()
    working = s.repo / "flows.json"
    working.write_text("PRIVATE WORKTREE CONTENT", encoding="utf-8")
    r = s.run(args)
    assert r.returncode == 2, r.stderr
    assert re.search(message, r.stderr)
    assert r.stdout == ""
    assert (s.repo / ".git/index").read_bytes() == before
    assert working.read_text(encoding="utf-8") == "PRIVATE WORKTREE CONTENT"
    assert not s.bundle.exists()
    assert not re.search(r"PRIVATE WORKTREE CONTENT|PRIVATE BLOB CONTENT", r.stderr)


def commit_branches(s: SimpleNamespace, git_ok, data: Sequence[bytes], *, with_base: bool = True) -> None:
    """Commit base (optional), a ``theirs`` branch and ``main`` (ours) versions of flows.json."""
    flow = s.repo / "flows.json"
    if with_base:
        flow.write_bytes(data[0])
        git_ok(s.repo, "add", ".")
    git_ok(s.repo, "commit", "--allow-empty", "-m", "base")
    git_ok(s.repo, "checkout", "-b", "theirs")
    flow.write_bytes(data[2])
    git_ok(s.repo, "add", ".")
    git_ok(s.repo, "commit", "-m", "theirs")
    git_ok(s.repo, "checkout", "main")
    flow.write_bytes(data[1])
    git_ok(s.repo, "add", ".")
    git_ok(s.repo, "commit", "-m", "ours")


def test_real_add_add_conflict_requires_opt_in_records_an_absent_base_and_resolves_independent_additions(s, git, git_ok) -> None:
    flow = s.repo / "flows.json"
    commit_branches(s, git_ok, [b"", ADDITIONS[0], ADDITIONS[1]], with_base=False)
    merge = git(s.repo, "merge", "--no-edit", "theirs")
    assert merge.returncode == 1, merge.stderr
    stages = [int(line.split("\t")[0][-1]) for line in git_ok(s.repo, "ls-files", "-u").split("\n")]
    assert stages == [2, 3]
    before_index, before_flow = (s.repo / ".git/index").read_bytes(), flow.read_bytes()
    refused = capture(s)
    assert refused.returncode == 2
    assert re.search("requires unmerged stages", refused.stderr)
    assert refused.stdout == ""
    assert not s.bundle.exists()
    assert (s.repo / ".git/index").read_bytes() == before_index
    assert flow.read_bytes() == before_flow

    r = capture(s, ["--allow-missing-base"])
    assert r.returncode == 1, r.stderr
    receipt = json.loads(r.stdout)
    report, decisions = parse(receipt["report"]), parse(receipt["manifest"])
    assert receipt["gitIndex"]["stages"]["base"] is None
    assert receipt["gitIndex"]["syntheticBase"] == {"reason": "missing-stage-1", "value": []}
    assert report["gitIndex"] == receipt["gitIndex"]
    assert Path(receipt["inputs"][0]).read_text(encoding="utf-8") == "[]\n"
    for i, role in enumerate(("ours", "theirs")):
        assert Path(receipt["inputs"][i + 1]).read_bytes() == ADDITIONS[i]
        assert receipt["gitIndex"]["stages"][role] == {
            "stage": i + 2, "mode": "100644", "blob": git_ok(s.repo, "rev-parse", f":{i + 2}:flows.json")
        }
    for i, role in enumerate(ROLES):
        assert decisions["inputHashes"][role] == sha256(Path(receipt["inputs"][i]).read_bytes())
    assert s.bundle.stat().st_mode & 0o777 == 0o700
    for file in os.listdir(s.bundle):
        assert (s.bundle / file).stat().st_mode & 0o777 == 0o600
    assert len(decisions["conflicts"]) == 1
    assert decisions["conflicts"][0]["kind"] == "add/add"
    assert decisions["conflicts"][0]["nodeId"] == "shared"
    assert decisions["conflicts"][0]["base"] == {"present": False}
    assert decisions["conflicts"][0]["decision"] is None
    args = ["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", flow]
    assert s.run(args).returncode == 2, "unreviewed decisions must be rejected"
    assert flow.read_bytes() == before_flow
    decisions["conflicts"][0]["decision"] = {"choice": "theirs"}
    write_compact(receipt["manifest"], decisions)
    kept = [(file, (s.bundle / file).read_bytes()) for file in os.listdir(s.bundle)]
    retry = capture(s, ["--allow-missing-base"])
    assert retry.returncode == 2
    assert re.search("must not exist", retry.stderr)
    for file, data in kept:
        assert (s.bundle / file).read_bytes() == data
    assert (s.repo / ".git/index").read_bytes() == before_index
    assert flow.read_bytes() == before_flow
    resolved = s.run(args)
    assert resolved.returncode == 0, resolved.stderr
    actual = parse(flow)
    assert sorted(n["id"] for n in actual) == ["flow", "ours-only", "shared", "theirs-only"]
    assert find(actual, "shared")["func"] == "msg.payload = 1; return msg;"
    for i, node_id in enumerate(("ours-only", "theirs-only")):
        assert find(actual, node_id) == find(json.loads(ADDITIONS[i]), node_id)
    assert parse(f"{flow}.conflicts.json")["clean"] is True
    assert (s.repo / ".git/index").read_bytes() == before_index


def test_opt_in_distinguishes_synthetic_empty_base_from_a_real_stage_1_empty_array_blob(s, index) -> None:
    empty = b"[]\n"
    ids = index(s, data=[empty, empty, empty])
    r = capture(s, ["--allow-missing-base"])
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt["gitIndex"]["stages"]["base"] == {"stage": 1, "mode": "100644", "blob": ids[0]}
    assert "syntheticBase" not in receipt["gitIndex"]
    assert parse(receipt["report"])["gitIndex"] == receipt["gitIndex"]
    assert Path(receipt["inputs"][0]).read_bytes() == empty


def test_opt_in_can_capture_a_clean_add_add_preview_with_regular_executable_blobs(s, index) -> None:
    index(
        s,
        data=[None, b'[{"id":"a","type":"config"}]', b'[{"id":"b","type":"config"}]'],
        modes=["100644", "100755", "100755"],
    )
    r = capture(s, ["--allow-missing-base"])
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt["clean"] is True
    assert receipt["gitIndex"]["stages"]["base"] is None
    assert parse(receipt["manifest"])["conflicts"] == []
    assert [n["id"] for n in parse(receipt["output"])] == ["a", "b"]


def test_real_driver_conflict_capture_exact_index_snapshots_after_temporary_inputs_disappear_then_resolve(s, git, git_ok) -> None:
    flow = s.repo / "flows.json"
    assert s.run(["install", "--apply"]).returncode == 0
    commit_branches(s, git_ok, VERSIONS)
    merge = git(s.repo, "merge", "--no-edit", "theirs")
    assert merge.returncode == 1, merge.stderr
    report_dir = s.repo / ".git/nrflow-merge-conflicts"
    old_report = parse(report_dir / os.listdir(report_dir)[0])
    assert all(not Path(file).exists() for file in old_report["inputs"]), "Git removed all driver input files"
    before_index, before_flow = (s.repo / ".git/index").read_bytes(), flow.read_bytes()
    r = capture(s, ["--prefer", "theirs", "--config-ref", "function.server", "--ignore-layout"])
    assert r.returncode == 1, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt["inputs"] == [str(s.bundle / f"{role}.json") for role in ROLES]
    assert receipt["manifest"] == str(s.bundle / "decisions.json")
    assert receipt["output"] == str(s.bundle / "merged.json")
    assert receipt["report"] == f"{receipt['output']}.conflicts.json"
    assert receipt["gitIndex"]["repo"] == str(s.repo)
    assert receipt["gitIndex"]["path"] == "flows.json"
    for i, file in enumerate(receipt["inputs"]):
        assert Path(file).read_bytes() == VERSIONS[i]
        assert Path(file).stat().st_mode & 0o777 == 0o600
        assert receipt["gitIndex"]["stages"][ROLES[i]] == {
            "stage": i + 1, "mode": "100644", "blob": git_ok(s.repo, "rev-parse", f":{i + 1}:flows.json")
        }
    assert s.bundle.stat().st_mode & 0o777 == 0o700
    assert sorted(os.listdir(s.bundle)) == [
        "base.json", "decisions.json", "merged.json", "merged.json.conflicts.json", "ours.json", "theirs.json"
    ]
    for file in os.listdir(s.bundle):
        assert (s.bundle / file).stat().st_mode & 0o777 == 0o600
    decisions, report = parse(receipt["manifest"]), parse(receipt["report"])
    assert report["gitIndex"] == receipt["gitIndex"]
    assert report["inputs"] == receipt["inputs"]
    assert report["summary"]["ignoreLayout"] is True
    assert decisions["options"] == {"prefer": "theirs", "configRefs": ["function.server"]}
    assert decisions["inputHashes"] == {role: sha256(VERSIONS[i]) for i, role in enumerate(ROLES)}
    assert decisions["conflicts"][0]["decision"] is None
    assert parse(receipt["output"])[0]["func"] == "msg.payload = 1; return msg;"
    assert (s.repo / ".git/index").read_bytes() == before_index
    assert flow.read_bytes() == before_flow
    for c in decisions["conflicts"]:
        c["decision"] = {"choice": "ours"}
    write_compact(receipt["manifest"], decisions)
    # Captured snapshots remain sufficient after the unmerged index entries disappear.
    git_ok(s.repo, "add", "flows.json")
    assert git_ok(s.repo, "ls-files", "-u") == ""
    resolved = s.run(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", flow], s.dir)
    assert resolved.returncode == 0, resolved.stderr
    assert parse(flow)[0]["func"] == "return null;"
    assert parse(flow)[0]["x"] == 150
    assert parse(flow)[0]["y"] == 200
    assert parse(f"{flow}.conflicts.json")["clean"] is True
    before = flow.read_bytes()
    Path(receipt["inputs"][1]).write_bytes(VERSIONS[1] + b"\n")
    stale = s.run(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", flow])
    assert stale.returncode == 2
    assert re.search("stale", stale.stderr)
    assert flow.read_bytes() == before


def test_literal_special_path_from_a_subdirectory_and_repo_captures_only_that_path_including_executable_blobs(s, index) -> None:
    file = "sub/:flow [*]\t雪\n.json"
    (s.repo / "sub").mkdir()
    ids = index(s, file=file, data=[VERSIONS[0]] * 3, modes=["100755"] * 3)
    index(s)  # A second unmerged path must not be included.
    r = s.run(["git-manifest", file[4:], "--repo", s.repo / "sub", "--snapshot-dir", "review"], s.dir)
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert receipt["gitIndex"]["path"] == file
    assert receipt["gitIndex"]["stages"]["base"]["blob"] == ids[0]
    assert receipt["gitIndex"]["stages"]["base"]["mode"] == "100755"
    assert parse(receipt["manifest"])["conflicts"] == []
    assert Path(receipt["inputs"][0]).read_bytes() == VERSIONS[0]


@pytest.mark.parametrize("missing", [0, 1, 2, "all"])
def test_missing_stage_fails_before_creating_a_bundle(s, index, git_ok, missing: int | str) -> None:
    index(s, data=[None if missing == "all" or i == missing else b for i, b in enumerate(VERSIONS)])
    if missing == "all":
        (s.repo / "flows.json").write_bytes(VERSIONS[0])
        git_ok(s.repo, "add", "flows.json")
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle], r"requires unmerged stages 1.*2.*3")


PRESENT = [[1, 2], [1, 3], [1], [2], [3], []]


@pytest.mark.parametrize("present", PRESENT, ids=["/".join(map(str, p)) or "0 only" for p in PRESENT])
def test_opt_in_still_rejects_stages(s, index, git_ok, present: list[int]) -> None:
    index(s, data=[b if i + 1 in present else None for i, b in enumerate(VERSIONS)])
    if not present:
        (s.repo / "flows.json").write_bytes(VERSIONS[0])
        git_ok(s.repo, "add", "flows.json")
    unchanged_failure(
        s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle, "--allow-missing-base"], r"requires unmerged stages"
    )


INVALID_BLOBS = [
    ("JSON", b"{PRIVATE BLOB CONTENT", r"cannot read or parse JSON"),
    ("UTF-8", bytes([0xFF]), r"cannot read or parse JSON"),
    ("schema", b"{}", r"expected a flat array"),
    ("oversize", b" " * (32 * 1024 * 1024 + 1), r"byte limit"),
]


@pytest.mark.parametrize("side", [1, 2], ids=["stage 2", "stage 3"])
@pytest.mark.parametrize(("name", "data", "message"), INVALID_BLOBS, ids=[name for name, _, _ in INVALID_BLOBS])
def test_opt_in_rejects_invalid_blob(s, index, name: str, data: bytes, message: str, side: int) -> None:
    blobs: list[bytes | None] = [None, VERSIONS[1], VERSIONS[2]]
    blobs[side] = data
    index(s, data=blobs)
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle, "--allow-missing-base"], message)


def _seed_commit(s: SimpleNamespace, git_ok) -> str:
    (s.repo / "seed").write_text("local", encoding="utf-8")
    git_ok(s.repo, "add", "seed")
    git_ok(s.repo, "commit", "-m", "seed")
    return git_ok(s.repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("side", [2, 3])
@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_opt_in_rejects_nonregular_mode_in_stage(s, git_ok, mode: str, side: int) -> None:
    commit = _seed_commit(s, git_ok)
    blob = git_ok(s.repo, "hash-object", "-w", "--stdin", input=VERSIONS[1])
    records = "".join(
        f"{mode if stage == side else '100644'} {commit if stage == side and mode == '160000' else blob} {stage}\tflows.json\n"
        for stage in (2, 3)
    )
    git_ok(s.repo, "update-index", "--index-info", input=records)
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle, "--allow-missing-base"], r"regular blob")


@pytest.mark.parametrize(("name", "data", "message"), INVALID_BLOBS, ids=[name for name, _, _ in INVALID_BLOBS])
def test_invalid_stage_leaves_working_flow_and_index_untouched(s, index, name: str, data: bytes, message: str) -> None:
    index(s, data=[VERSIONS[0], data, VERSIONS[2]])
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle], message)


def test_symlink_index_entries_are_rejected_rather_than_followed(s, index) -> None:
    index(s, modes=["100644", "120000", "100644"])
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle], r"regular blob")


def test_gitlink_entries_are_rejected_as_unsupported_stage_modes(s, git_ok) -> None:
    commit = _seed_commit(s, git_ok)
    records = "".join(f"160000 {commit} {stage}\tflows.json\n" for stage in (1, 2, 3))
    git_ok(s.repo, "update-index", "--index-info", input=records)
    unchanged_failure(s, ["git-manifest", "flows.json", "--snapshot-dir", s.bundle], r"regular blob")


def test_integrity_only_failure_creates_a_review_bundle_and_resolution_still_rejects_the_broken_graph(s, index) -> None:
    broken = b'[{"id":"a","type":"function","x":0,"z":"missing","wires":[]}]'
    index(s, data=[broken, broken, broken])
    r = capture(s)
    assert r.returncode == 1, r.stderr
    receipt = json.loads(r.stdout)
    assert parse(receipt["manifest"])["conflicts"] == []
    assert parse(receipt["report"])["clean"] is False
    assert len(parse(receipt["report"])["integrity"]) > 0
    before = Path(receipt["output"]).read_bytes()
    resolve = s.run(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", receipt["output"]])
    assert resolve.returncode == 2
    assert re.search("integrity", resolve.stderr)
    assert Path(receipt["output"]).read_bytes() == before


def test_a_missing_working_file_cannot_be_replaced_by_the_snapshot_directory(s, index) -> None:
    index(s)
    flow = s.repo / "flows.json"
    assert not flow.exists()
    r = s.run(["git-manifest", "flows.json", "--snapshot-dir", flow])
    assert r.returncode == 2
    assert re.search("differ from the working flow", r.stderr)
    assert not flow.exists()


def test_existing_destinations_and_symlinks_preserve_reviewed_decisions_missing_parent_leaves_no_bundle(s, index) -> None:
    index(s)
    assert capture(s).returncode == 1
    keep = s.bundle / "decisions.json"
    keep.write_text("REVIEWED DECISIONS", encoding="utf-8")
    retry = capture(s)
    assert retry.returncode == 2
    assert re.search("must not exist", retry.stderr)
    assert keep.read_text(encoding="utf-8") == "REVIEWED DECISIONS"
    alias = s.dir / "alias"
    os.symlink(s.bundle, alias)
    assert s.run(["git-manifest", "flows.json", "--snapshot-dir", alias]).returncode == 2
    assert keep.read_text(encoding="utf-8") == "REVIEWED DECISIONS"
    missing = s.dir / "missing" / "bundle"
    assert s.run(["git-manifest", "flows.json", "--snapshot-dir", missing]).returncode == 2
    assert not (s.dir / "missing").exists()


def test_invalid_paths_and_options_fail_without_creating_outputs(s, index) -> None:
    index(s)
    for args, message in [
        (["../outside.json", "--snapshot-dir", s.bundle], r"inside.*worktree"),
        ([".", "--snapshot-dir", s.bundle], r"requires unmerged stages"),
        (["flows.json"], r"requires --snapshot-dir"),
        (["flows.json", "--snapshot-dir", s.bundle, "--prefer", "invalid"], r"prefer must"),
        (["flows.json", "--snapshot-dir", s.bundle, "--config-ref", "invalid"], r"TYPE.PROPERTY"),
    ]:
        unchanged_failure(s, ["git-manifest", *args], message)
    r = s.run(["git-manifest", "flows.json", "--repo", "/dev/null", "--snapshot-dir", s.bundle], s.dir)
    assert r.returncode == 2
    assert re.search("git rev-parse failed", r.stderr)
    assert not s.bundle.exists()


@pytest.mark.parametrize("base_kind", ["three-stage", "real-empty", "absent"])
def test_in_bundle_resolution_retains_capture_provenance_without_original_repository(s, git, git_ok, base_kind: str) -> None:
    data = VERSIONS if base_kind == "three-stage" else [b"[]\n", *ADDITIONS]
    commit_branches(s, git_ok, data, with_base=base_kind != "absent")
    merge = git(s.repo, "merge", "--no-edit", "theirs")
    assert merge.returncode == 1, merge.stderr
    captured = capture(s, ["--allow-missing-base"] if base_kind == "absent" else [])
    assert captured.returncode == 1, captured.stderr
    receipt = json.loads(captured.stdout)
    manifest = parse(receipt["manifest"])
    for c in manifest["conflicts"]:
        c["decision"] = {"choice": "theirs"}
    write_compact(receipt["manifest"], manifest)
    manifest_bytes = Path(receipt["manifest"]).read_bytes()
    git_ok(s.repo, "add", "flows.json")
    assert git_ok(s.repo, "ls-files", "-u") == ""
    shutil.rmtree(s.repo)  # No live index, objects, worktree or repo remains.
    args = ["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", receipt["output"]]
    resolved = s.run(args, s.dir)
    assert resolved.returncode == 0, resolved.stderr
    report, actual = parse(receipt["report"]), parse(receipt["output"])
    assert report["clean"] is True
    assert report["integrity"] == []
    assert all(c["resolved"] and c["selected"] == "theirs" for c in report["conflicts"])
    if base_kind == "three-stage":
        assert find(actual, "a")["func"] == "msg.payload = 1; return msg;"
        assert find(actual, "a")["x"] == 150
        assert find(actual, "a")["y"] == 200
    else:
        assert sorted(n["id"] for n in actual) == ["flow", "ours-only", "shared", "theirs-only"]
        assert find(actual, "shared")["func"] == "msg.payload = 1; return msg;"
        for i, node_id in enumerate(("ours-only", "theirs-only")):
            assert find(actual, node_id) == find(json.loads(data[i + 1]), node_id)
        if base_kind == "absent":
            assert receipt["gitIndex"]["stages"]["base"] is None
            assert receipt["gitIndex"]["syntheticBase"] == {"reason": "missing-stage-1", "value": []}
        else:
            assert receipt["gitIndex"]["stages"]["base"]["stage"] == 1
            assert "syntheticBase" not in receipt["gitIndex"]
    # Assert the overwritten report first: this is the user-visible loss on the old CLI.
    assert report["gitIndex"] == receipt["gitIndex"]
    assert manifest["gitIndex"] == receipt["gitIndex"]
    assert Path(receipt["manifest"]).read_bytes() == manifest_bytes
    outputs = [Path(receipt["output"]), Path(receipt["report"])]
    before = [f.read_bytes() for f in outputs]
    Path(receipt["inputs"][1]).write_bytes(data[1] + b"\n")
    stale = s.run(args, s.dir)
    assert stale.returncode == 2
    assert re.search("stale", stale.stderr)
    for i, f in enumerate(outputs):
        assert f.read_bytes() == before[i]


def test_literal_capture_path_provenance_also_survives_resolution_with_a_custom_report(s, index) -> None:
    file = ":flow [*]\\\t雪\n.json"
    index(s, file=file, data=[VERSIONS[0]] * 3, modes=["100755"] * 3)
    captured = s.run(["git-manifest", file, "--snapshot-dir", s.bundle])
    assert captured.returncode == 0, captured.stderr
    receipt = json.loads(captured.stdout)
    custom = s.dir / "custom-report.json"
    result = s.run(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", receipt["output"], "--conflicts-file", custom])
    assert result.returncode == 0, result.stderr
    assert parse(custom)["gitIndex"] == receipt["gitIndex"]


def test_synthetic_provenance_rejects_rehashed_noncanonical_base_bytes_before_replacing_bundle_outputs(s, index) -> None:
    index(s, data=[None, VERSIONS[1], VERSIONS[2]])
    captured = capture(s, ["--allow-missing-base"])
    assert captured.returncode == 1, captured.stderr
    receipt = json.loads(captured.stdout)
    manifest = parse(receipt["manifest"])
    for c in manifest["conflicts"]:
        c["decision"] = {"choice": "ours"}
    Path(receipt["inputs"][0]).write_text("[]", encoding="utf-8")
    manifest["inputHashes"]["base"] = sha256(b"[]")
    write_compact(receipt["manifest"], manifest)
    files = [Path(receipt["output"]), Path(receipt["report"]), Path(receipt["manifest"])]
    before = [f.read_bytes() for f in files]
    result = s.run(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", receipt["output"]])
    assert result.returncode == 2
    assert re.search("gitIndex", result.stderr)
    for i, f in enumerate(files):
        assert f.read_bytes() == before[i]
