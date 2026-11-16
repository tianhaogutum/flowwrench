"""Port of test/api-v2.test.js."""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROLES = ("base", "ours", "theirs")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def parse(file: str | os.PathLike[str]) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


def write(file: str | os.PathLike[str], value: Any) -> None:
    Path(file).write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def fixtures() -> list[Any]:
    return [parse(FIXTURES / "api-v2" / f"{role}.json") for role in ROLES]


def descriptor(document: Any) -> dict[str, Any]:
    has_rev = isinstance(document, dict) and "rev" in document
    return {
        "format": "array" if isinstance(document, list) else "v2",
        "rev": {"present": True, "value": document["rev"]} if has_rev else {"present": False},
    }


def check_provenance(report: dict[str, Any], inputs: list[Any]) -> None:
    assert report["revisionProvenance"] == {
        **{role: descriptor(inputs[i]) for i, role in enumerate(ROLES)},
        "output": {**descriptor(inputs[1]), "source": "ours"},
        "serverCurrentRevisionVerified": False,
    }


def find(flow: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    return next(n for n in flow if n["id"] == node_id)


def check_merged(document: Any) -> None:
    flow = document if isinstance(document, list) else document["flows"]
    assert find(flow, "a")["name"] == "Renamed Alpha"
    assert find(flow, "b")["x"] == 360


@pytest.fixture
def setup(run_cli, sandbox: Path) -> Callable[..., SimpleNamespace]:
    counter = itertools.count()

    def make(versions: list[Any] | None = None) -> SimpleNamespace:
        versions = versions if versions is not None else fixtures()
        directory = sandbox / f"api-v2-{next(counter)}"
        directory.mkdir()
        files = []
        for i, role in enumerate(ROLES):
            file = directory / f"{role}.json"
            write(file, versions[i])
            files.append(str(file))
        out = directory / "out.json"

        def merge(*args: Any) -> Any:
            return run_cli(["merge", *files, "-o", out, *args])

        return SimpleNamespace(
            dir=directory, files=files, out=out, report=Path(f"{out}.conflicts.json"),
            manifest=directory / "decisions.json", merge=merge,
        )

    return make


def test_v2_r0_r1_r2_merge_combines_node_edits_and_reports_ours_revision_without_a_content_conflict(setup) -> None:
    inputs = fixtures()
    s = setup(inputs)
    r = s.merge()
    assert r.returncode == 0, r.stderr
    actual, report = parse(s.out), parse(s.report)
    assert sorted(actual) == ["flows", "rev"]
    assert actual["rev"] == "r1"
    check_merged(actual)
    check_provenance(report, inputs)
    assert report["conflicts"] == []
    assert report["integrity"] == []
    assert find(report["summary"]["ours"]["changed"], "a")["properties"][0]["property"] == "/name"
    assert report["summary"]["theirs"]["layoutOnly"][0]["id"] == "b"
    for i, file in enumerate(s.files):
        assert parse(file) == inputs[i]


@pytest.mark.parametrize("mask", range(8))
def test_mixed_shapes_ours_alone_determines_output_shape(setup, mask: int) -> None:
    inputs = [doc if mask & (1 << i) else doc["flows"] for i, doc in enumerate(fixtures())]
    s = setup(inputs)
    r = s.merge()
    assert r.returncode == 0, r.stderr
    actual = parse(s.out)
    assert isinstance(actual, list) == isinstance(inputs[1], list)
    check_merged(actual)
    if not isinstance(actual, list):
        assert actual["rev"] == "r1"
    check_provenance(parse(s.report), inputs)


REVISIONS = [None, ' r1\n\t雪 " \\ ', "r0", "r2", "0"]


@pytest.mark.parametrize(
    "revision", REVISIONS, ids=["undefined" if r is None else json.dumps(r, ensure_ascii=False) for r in REVISIONS]
)
def test_ours_revision_is_opaque_and_preserves_presence(setup, revision: str | None) -> None:
    inputs = fixtures()
    del inputs[1]["rev"]
    if revision is not None:
        inputs[1]["rev"] = revision
    s = setup(inputs)
    r = s.merge("--prefer", "theirs")
    assert r.returncode == 0, r.stderr
    assert ("rev" in parse(s.out)) == (revision is not None)
    assert parse(s.out).get("rev") == revision
    check_provenance(parse(s.report), inputs)


def test_all_revisions_absent_and_empty_flow_documents_remain_valid(setup) -> None:
    inputs = [{"flows": []}, {"flows": []}, {"flows": []}]
    s = setup(inputs)
    assert s.merge().returncode == 0
    assert parse(s.out) == {"flows": []}
    check_provenance(parse(s.report), inputs)


@pytest.mark.parametrize("prefer", ["ours", "theirs"])
def test_node_conflicts_remain_exit_1_with_fallback_while_rev_stays_ours(setup, prefer: str) -> None:
    inputs = fixtures()
    inputs[2]["flows"][1]["name"] = "Their Alpha"
    s = setup(inputs)
    r = s.merge("--prefer", prefer)
    assert r.returncode == 1, r.stderr
    assert parse(s.out)["rev"] == "r1"
    assert find(parse(s.out)["flows"], "a")["name"] == ("Renamed Alpha" if prefer == "ours" else "Their Alpha")
    assert find(parse(s.out)["flows"], "b")["x"] == 360
    assert [c["property"] for c in parse(s.report)["conflicts"]] == ["/name"]
    check_provenance(parse(s.report), inputs)


def test_resolve_retains_ours_revision_even_with_theirs_decision_and_hashes_whole_wrappers(setup, run_cli) -> None:
    inputs = fixtures()
    inputs[2]["flows"][1]["name"] = "Their Alpha"
    s = setup(inputs)
    assert s.merge("--manifest", s.manifest).returncode == 1
    manifest = parse(s.manifest)
    manifest["conflicts"][0]["decision"] = {"choice": "theirs"}
    write(s.manifest, manifest)

    def resolve() -> Any:
        return run_cli(["resolve", *s.files, "--manifest", s.manifest, "-o", s.out])

    r = resolve()
    assert r.returncode == 0, r.stderr
    actual = parse(s.out)
    assert actual["rev"] == "r1"
    assert find(actual["flows"], "a")["name"] == "Their Alpha"
    check_provenance(parse(s.report), inputs)
    files = [s.out, s.report, s.manifest]
    before = [f.read_bytes() for f in files]
    inputs[1]["rev"] = "another-revision"
    write(s.files[1], inputs[1])
    stale = resolve()
    assert stale.returncode == 2
    assert re.search("stale", stale.stderr)
    for i, f in enumerate(files):
        assert f.read_bytes() == before[i]


def test_wrapper_graph_integrity_still_prunes_invalid_wires_and_reports_actual_result(setup) -> None:
    inputs = fixtures()
    inputs[2]["flows"][1]["wires"] = [["missing"]]
    s = setup(inputs)
    r = s.merge()
    assert r.returncode == 1, r.stderr
    assert parse(s.out)["rev"] == "r1"
    assert find(parse(s.out)["flows"], "a")["wires"] == [[]]
    report = parse(s.report)
    assert report["integrity"][0]["kind"] == "dangling-wire"
    assert find(report["summary"]["theirs"]["changed"], "a")["wireChanges"][0]["added"][0]["id"] == "missing"
    assert find(report["summary"]["merged"]["changed"], "a")["wireChanges"][0]["added"] == []


BAD_INPUTS: list[tuple[str, Any, str]] = [
    ("empty rev", {"rev": "", "flows": []}, r"rev.*nonempty string"),
    *[
        (f"non-string rev {json.dumps(rev, separators=(',', ':'))}", {"rev": rev, "flows": []}, r"rev.*nonempty string")
        for rev in [None, 0, True, [], {}]
    ],
    ("credentials", {"rev": "r1", "flows": [], "credentials": {"secret": "PRIVATE"}}, r"unsupported top-level fields"),
    ("empty credentials", {"flows": [], "credentials": None}, r"unsupported top-level fields"),
    ("unknown field", {"flows": [], "extra": "PRIVATE"}, r"unsupported top-level fields"),
    ("prototype field", json.loads('{"flows":[],"__proto__":{}}'), r"unsupported top-level fields"),
    ("single flow", {"id": "flow", "label": "Flow", "nodes": [], "configs": []}, r"expected.*flows"),
    ("single flow with flows", {"flows": [], "nodes": []}, r"unsupported top-level fields"),
    ("missing flows", {"rev": "r1"}, r"expected.*flows"),
    ("non-array flows", {"rev": "r1", "flows": {}}, r"flows.*flat array"),
    ("duplicate nodes", {"flows": [{"id": "a", "type": "debug"}, {"id": "a", "type": "debug"}]}, r"duplicate node"),
    ("nested subflow", {"flows": [{"id": "s", "type": "subflow", "nodes": [], "configs": []}]}, r"nested subflow"),
]


@pytest.mark.parametrize(("name", "bad", "error"), BAD_INPUTS, ids=[name for name, _, _ in BAD_INPUTS])
def test_strict_adapter_rejects_input_in_each_role_without_any_writes(setup, name: str, bad: Any, error: str) -> None:
    s = setup()
    preserved = [s.out, s.report, s.manifest]
    for f in preserved:
        f.write_text("KEEP", encoding="utf-8")
    for role in range(3):
        inputs = fixtures()
        inputs[role] = bad
        for i, f in enumerate(s.files):
            write(f, inputs[i])
        r = s.merge("--manifest", s.manifest)
        assert r.returncode == 2, r.stderr
        assert re.search(error, r.stderr)
        assert "PRIVATE" not in r.stderr
        for f in preserved:
            assert f.read_text(encoding="utf-8") == "KEEP"
        assert sorted(os.listdir(s.dir)) == [
            "base.json", "decisions.json", "ours.json", "out.json", "out.json.conflicts.json", "theirs.json"
        ]


def test_non_merge_commands_reject_wrappers_without_silently_stripping_revision(setup, run_cli) -> None:
    s = setup()
    s.out.write_text("KEEP", encoding="utf-8")
    for args in [["diff", *s.files[:2]], ["normalize", s.files[0], "-o", s.out], ["textconv", s.files[0]]]:
        r = run_cli(args)
        assert r.returncode == 2
        assert re.search("flat array", r.stderr)
        assert r.stdout == ""
        assert s.out.read_text(encoding="utf-8") == "KEEP"


@pytest.mark.parametrize("conflict", [False, True], ids=["conflict=false", "conflict=true"])
def test_wrapper_git_driver_preserves_ours_revision(setup, run_cli, git_ok, conflict: bool) -> None:
    inputs = fixtures()
    if conflict:
        inputs[2]["flows"][1]["name"] = "Their Alpha"
    s = setup(inputs)
    git_ok(s.dir, "init", "-b", "main")
    git_ok(s.dir, "config", "user.name", "Offline Test")
    git_ok(s.dir, "config", "user.email", "test@example.invalid")
    git_ok(s.dir, "config", "commit.gpgsign", "false")
    git_ok(s.dir, "config", "core.hooksPath", "/dev/null")
    r = run_cli(["git-driver", *s.files], cwd=s.dir)
    assert r.returncode == (1 if conflict else 0), r.stderr
    assert parse(s.files[1])["rev"] == "r1"
    if not conflict:
        check_merged(parse(s.files[1]))
    reports = s.dir / ".git/nrflow-merge-conflicts"
    check_provenance(parse(reports / os.listdir(reports)[0]), inputs)


def test_index_wrapper_snapshots_preview_and_resolve_with_whole_document_hashes_and_revision_provenance(setup, run_cli, git_ok) -> None:
    inputs = fixtures()
    inputs[2]["flows"][1]["name"] = "Their Alpha"
    s = setup(inputs)
    git_ok(s.dir, "init", "-b", "main")
    records = [
        f"100644 {git_ok(s.dir, 'hash-object', '-w', '--stdin', input=Path(f).read_bytes())} {i + 1}\tflows.json\n"
        for i, f in enumerate(s.files)
    ]
    git_ok(s.dir, "update-index", "--index-info", input="".join(records))
    index = (s.dir / ".git/index").read_bytes()
    bundle = s.dir / "bundle"
    captured = run_cli(["git-manifest", "flows.json", "--repo", s.dir, "--snapshot-dir", bundle])
    assert captured.returncode == 1, captured.stderr
    receipt = json.loads(captured.stdout)
    check_provenance(receipt, inputs)
    check_provenance(parse(receipt["report"]), inputs)
    for i, f in enumerate(receipt["inputs"]):
        assert Path(f).read_bytes() == Path(s.files[i]).read_bytes()
    manifest = parse(receipt["manifest"])
    manifest["conflicts"][0]["decision"] = {"choice": "theirs"}
    write(receipt["manifest"], manifest)
    r = run_cli(["resolve", *receipt["inputs"], "--manifest", receipt["manifest"], "-o", s.out])
    assert r.returncode == 0, r.stderr
    assert parse(s.out)["rev"] == "r1"
    check_provenance(parse(s.report), inputs)
    assert (s.dir / ".git/index").read_bytes() == index
    assert not (s.dir / "flows.json").exists()


def test_wrapper_consumes_one_json_depth_level_and_keeps_existing_numeric_schema_guards(setup) -> None:
    value: Any = 1
    for _ in range(62):
        value = {"child": value}
    flow = [{"id": "n", "type": "config", "value": value}]
    s = setup([flow, flow, flow])
    # Array depth=0, node=1, leaf=64: valid as array, too deep in a wrapper.
    assert s.merge().returncode == 0
    files = [s.out, s.report]
    before = [f.read_bytes() for f in files]
    write(s.files[0], {"rev": "r0", "flows": flow})
    deep = s.merge()
    assert deep.returncode == 2
    assert re.search("maximum depth", deep.stderr)
    Path(s.files[0]).write_text('{"rev":"r0","flows":[{"id":"n","type":"config","value":1e400}]}', encoding="utf-8")
    numeric = s.merge()
    assert numeric.returncode == 2
    assert re.search("non-finite", numeric.stderr)
    for i, f in enumerate(files):
        assert f.read_bytes() == before[i]
