"""Port of test/resolution.test.js."""

from __future__ import annotations

import hashlib
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


def parse(file: str | os.PathLike[str]) -> Any:
    return json.loads(Path(file).read_text(encoding="utf-8"))


def write(file: str | os.PathLike[str], value: Any) -> None:
    Path(file).write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def node(node_id: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": "function", "x": 0, "y": 0, "wires": [], **(props or {})}


def default_versions() -> list[list[dict[str, Any]]]:
    return [
        [node("a", {"func": "base", "name": "base"})],
        [node("a", {"func": "ours", "name": "ours"})],
        [node("a", {"func": "theirs", "name": "base", "y": 20})],
    ]


@pytest.fixture
def setup(run_cli, sandbox: Path) -> Callable[..., SimpleNamespace]:
    counter = itertools.count()

    def make(versions: list[list[dict[str, Any]]] | None = None) -> SimpleNamespace:
        versions = versions if versions is not None else default_versions()
        directory = sandbox / f"resolution-{next(counter)}"
        directory.mkdir()
        files = []
        for i, name in enumerate(ROLES):
            file = directory / f"{name}.json"
            write(file, versions[i])
            files.append(str(file))
        out = directory / "out.json"
        report = Path(f"{out}.conflicts.json")
        manifest = directory / "decisions.json"
        generated = run_cli(["merge", *files, "-o", out, "--manifest", manifest])
        assert generated.returncode == 1, generated.stderr
        document = parse(manifest)

        def resolve() -> Any:
            return run_cli(["resolve", *files, "--manifest", manifest, "-o", out])

        return SimpleNamespace(dir=directory, files=files, out=out, report=report, manifest=manifest, document=document, resolve=resolve)

    return make


def by_property(document: dict[str, Any], prop: str) -> dict[str, Any]:
    return next(c for c in document["conflicts"] if c["property"] == prop)


def test_manifest_hashes_exact_inputs_and_ids_survive_ordering_formatting_and_fallback_changes(setup, run_cli) -> None:
    s = setup([
        [node("b"), node("a", {"func": "base"})],
        [node("b"), node("a", {"func": "ours"})],
        [node("b"), node("a", {"func": "theirs"})],
    ])
    assert s.document["version"] == 1
    assert s.document["kind"] == "nrflow-merge-resolution"
    assert s.document["inputHashes"] == {
        ROLES[i]: hashlib.sha256(Path(file).read_bytes()).hexdigest() for i, file in enumerate(s.files)
    }
    conflict = s.document["conflicts"][0]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", conflict["id"])
    assert conflict["decision"] is None
    assert conflict["property"] == "/func"
    for file in s.files:
        Path(file).write_text(json.dumps(list(reversed(parse(file))), indent=2, ensure_ascii=False), encoding="utf-8")
    result = run_cli(["merge", *s.files, "-o", s.out, "--manifest", s.manifest, "--prefer", "theirs"])
    assert result.returncode == 1, result.stderr
    assert parse(s.manifest)["conflicts"][0]["id"] == conflict["id"]
    assert parse(s.report)["conflicts"][0]["id"] == conflict["id"]


def test_mixed_decisions_apply_together_and_one_unresolved_conflict_prevents_all_writes(setup) -> None:
    def make(func: str, name: str, options: list[int]) -> list[dict[str, Any]]:
        return [node("a", {"func": func, "name": name, "options": options})]

    s = setup([make("base", "base", [1]), make("ours", "ours", [2]), make("theirs", "theirs", [3])])
    by_property(s.document, "/func")["decision"] = {"choice": "ours"}
    by_property(s.document, "/name")["decision"] = {"choice": "theirs"}
    write(s.manifest, s.document)
    before, report_before = s.out.read_text(encoding="utf-8"), s.report.read_text(encoding="utf-8")
    assert s.resolve().returncode == 2
    assert s.out.read_text(encoding="utf-8") == before
    assert s.report.read_text(encoding="utf-8") == report_before
    by_property(s.document, "/options")["decision"] = {"choice": "custom", "present": True, "value": [4, 5]}
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert parse(s.out) == make("ours", "theirs", [4, 5])


@pytest.mark.parametrize("choice", ["ours", "theirs", "custom"])
def test_resolve_explicit_choice_preserves_independent_edits(setup, choice: str) -> None:
    s = setup()
    s.document["conflicts"][0]["decision"] = (
        {"choice": choice, "present": True, "value": "reviewed"} if choice == "custom" else {"choice": choice}
    )
    write(s.manifest, s.document)
    result = s.resolve()
    assert result.returncode == 0, result.stderr
    assert parse(s.out)[0]["func"] == ("reviewed" if choice == "custom" else choice)
    assert parse(s.out)[0]["name"] == "ours"
    assert parse(s.out)[0]["y"] == 20
    report = parse(s.report)
    assert report["clean"] is True
    assert report["conflicts"][0]["resolved"] is True
    assert report["conflicts"][0]["selected"] == choice


def _mutate_unresolved(s: SimpleNamespace) -> None:
    pass


def _mutate_stale(s: SimpleNamespace) -> None:
    Path(s.files[1]).write_text(f"{Path(s.files[1]).read_text(encoding='utf-8')}\n", encoding="utf-8")


def _mutate_document(change: Callable[[dict[str, Any]], None]) -> Callable[[SimpleNamespace], None]:
    def mutate(s: SimpleNamespace) -> None:
        change(s.document)
        write(s.manifest, s.document)

    return mutate


def _set_conflicts_empty(document: dict[str, Any]) -> None:
    document["conflicts"] = []


def _set_unknown_id(document: dict[str, Any]) -> None:
    document["conflicts"][0]["id"] = f"sha256:{'0' * 64}"


def _duplicate(document: dict[str, Any]) -> None:
    document["conflicts"].append(document["conflicts"][0])


def _decision(value: Any) -> Callable[[dict[str, Any]], None]:
    def change(document: dict[str, Any]) -> None:
        document["conflicts"][0]["decision"] = value

    return change


def _invalid_json(s: SimpleNamespace) -> None:
    s.manifest.write_text("{ private invalid input", encoding="utf-8")


REJECTIONS = [
    ("unresolved", _mutate_unresolved),
    ("stale whitespace", _mutate_stale),
    ("missing conflict", _mutate_document(_set_conflicts_empty)),
    ("unknown ID", _mutate_document(_set_unknown_id)),
    ("duplicate ID", _mutate_document(_duplicate)),
    ("invalid choice", _mutate_document(_decision({"choice": "automatic"}))),
    ("ambiguous custom absence", _mutate_document(_decision({"choice": "custom", "present": False, "value": None}))),
    ("invalid JSON", _invalid_json),
]


@pytest.mark.parametrize(("name", "mutate"), REJECTIONS, ids=[name for name, _ in REJECTIONS])
def test_resolve_rejects_without_replacing_either_output(setup, name: str, mutate: Callable[[SimpleNamespace], None]) -> None:
    s = setup()
    mutate(s)
    s.out.write_text("KEEP OUTPUT", encoding="utf-8")
    s.report.write_text("KEEP REPORT", encoding="utf-8")
    result = s.resolve()
    assert result.returncode == 2, result.stderr
    assert s.out.read_text(encoding="utf-8") == "KEEP OUTPUT"
    assert s.report.read_text(encoding="utf-8") == "KEEP REPORT"
    assert not any(entry.endswith(".tmp") for entry in os.listdir(s.dir))
    assert "private invalid input" not in result.stderr


def test_custom_property_deletion_differs_from_null_escaped_and_prototype_keys_stay_data(setup) -> None:
    def make(value: int) -> list[dict[str, Any]]:
        return [node("a", json.loads(f'{{"options":{{"a/b~c":{value},"__proto__":{value}}}}}'))]

    s = setup([make(1), make(2), make(3)])
    for conflict in s.document["conflicts"]:
        conflict["decision"] = (
            {"choice": "custom", "present": True, "value": None}
            if conflict["property"].endswith("__proto__")
            else {"choice": "custom", "present": False}
        )
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert parse(s.out)[0]["options"] == json.loads('{"__proto__":null}')
    assert "polluted" not in {}


def test_whole_node_add_add_and_modify_delete_decisions_allow_custom_replacement_or_deletion(setup) -> None:
    s = setup([
        [node("a")],
        [node("a", {"name": "changed"}), node("b", {"name": "ours"})],
        [node("b", {"name": "theirs"})],
    ])
    for c in s.document["conflicts"]:
        c["decision"] = (
            {"choice": "theirs"}
            if c["nodeId"] == "a"
            else {"choice": "custom", "present": True, "value": node("b", {"name": "reviewed"})}
        )
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert parse(s.out) == [node("b", {"name": "reviewed"})]
    next(c for c in s.document["conflicts"] if c["nodeId"] == "b")["decision"]["value"]["id"] = "other"
    write(s.manifest, s.document)
    before = s.out.read_text(encoding="utf-8")
    assert s.resolve().returncode == 2
    assert s.out.read_text(encoding="utf-8") == before


def test_custom_invalid_schema_and_unresolved_graph_integrity_never_commit_output(setup) -> None:
    s = setup()
    s.document["conflicts"][0]["decision"] = {"choice": "custom", "present": True, "value": {"nested": "allowed for func"}}
    # Unknown node fields are allowed; graph references still need to be sound.
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    g = setup([
        [node("a", {"z": "missing", "wires": [[]]})],
        [node("a", {"z": "missing", "wires": []})],
        [node("a", {"z": "missing", "wires": [["a"]]})],
    ])
    g.document["conflicts"][0]["decision"] = {"choice": "custom", "present": True, "value": "invalid wires"}
    write(g.manifest, g.document)
    before, report_before = g.out.read_text(encoding="utf-8"), g.report.read_text(encoding="utf-8")
    assert g.resolve().returncode == 2
    assert g.out.read_text(encoding="utf-8") == before
    g.document["conflicts"][0]["decision"] = {"choice": "ours"}
    write(g.manifest, g.document)
    result = g.resolve()
    assert result.returncode == 2, result.stderr
    assert re.search("integrity", result.stderr)
    assert g.out.read_text(encoding="utf-8") == before
    assert g.report.read_text(encoding="utf-8") == report_before


def test_manifest_generation_and_resolution_reject_input_output_manifest_hard_link_aliases(setup, run_cli) -> None:
    s = setup()
    s.document["conflicts"][0]["decision"] = {"choice": "theirs"}
    write(s.manifest, s.document)
    alias = s.dir / "alias.json"
    os.link(s.manifest, alias)
    before = s.manifest.read_text(encoding="utf-8")
    for output in [s.manifest, alias, s.files[1]]:
        assert run_cli(["resolve", *s.files, "--manifest", s.manifest, "-o", output]).returncode == 2
    assert run_cli(["merge", *s.files, "--manifest", s.files[0], "-o", s.out]).returncode == 2
    assert s.manifest.read_text(encoding="utf-8") == before


def test_schema_conflicts_accept_complete_node_decisions_and_new_schema_conflicts_are_refused(setup) -> None:
    s = setup([
        [{"id": "s", "type": "custom", "in": []}],
        [{"id": "s", "type": "subflow", "in": []}],
        [{"id": "s", "type": "custom", "in": {"customOption": True}}],
    ])
    assert s.document["conflicts"][0]["kind"] == "schema"
    s.document["conflicts"][0]["decision"] = {"choice": "theirs"}
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert parse(s.out) == parse(s.files[2])
    w = setup([[node("a", {"wires": [[]]})], [node("a", {"wires": []})], [node("a", {"wires": [["a"]]})]])
    w.document["conflicts"][0]["decision"] = {"choice": "custom", "present": True, "value": "invalid wires"}
    write(w.manifest, w.document)
    before = w.out.read_text(encoding="utf-8")
    assert w.resolve().returncode == 2
    assert w.out.read_text(encoding="utf-8") == before


def test_a_resolved_wire_port_conflict_can_remove_the_original_integrity_issue(setup) -> None:
    s = setup([[node("a", {"wires": [[]]})], [node("a", {"wires": [["missing"]]})], [node("a", {"wires": []})]])
    assert parse(s.report)["integrity"][0]["kind"] == "dangling-wire"
    s.document["conflicts"][0]["decision"] = {"choice": "theirs"}
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert parse(s.out)[0]["wires"] == []
    assert parse(s.report)["integrity"] == []


def provenance(length: int = 40) -> dict[str, Any]:
    """These are editable capture claims, deliberately not hashes of the input blobs."""
    return {
        "repo": "/nonexistent/capture-repo",
        "path": "sub/:flow [*]\\\t雪\n.json",
        "stages": {
            role: {"stage": i + 1, "mode": "100755" if i == 0 else "100644", "blob": str(i + 1) * length}
            for i, role in enumerate(ROLES)
        },
    }


@pytest.mark.parametrize("length", [40, 64])
def test_resolve_retains_informational_hex_provenance_with_no_git_verification(setup, length: int) -> None:
    s = setup()
    s.document["gitIndex"] = provenance(length)
    for c in s.document["conflicts"]:
        c["decision"] = {"choice": "theirs"}
    write(s.manifest, s.document)
    before = s.manifest.read_bytes()
    result = s.resolve()
    assert result.returncode == 0, result.stderr
    assert parse(s.report)["gitIndex"] == s.document["gitIndex"]
    assert s.manifest.read_bytes() == before


def test_generic_version_1_manifest_and_report_have_no_git_index(setup) -> None:
    s = setup()
    assert "gitIndex" not in s.document
    for c in s.document["conflicts"]:
        c["decision"] = {"choice": "ours"}
    write(s.manifest, s.document)
    assert s.resolve().returncode == 0
    assert "gitIndex" not in parse(s.report)


def _gi(change: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    """Apply *change* to ``manifest['gitIndex']``."""

    def mutate(manifest: dict[str, Any]) -> None:
        change(manifest["gitIndex"])

    return mutate


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return _gi(lambda gi: gi.__setitem__(key, value))


def _delete(key: str) -> Callable[[dict[str, Any]], None]:
    return _gi(lambda gi: gi.pop(key))


def _stage(role: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return _gi(lambda gi: gi["stages"][role].__setitem__(key, value))


def _all_blobs(gi: dict[str, Any]) -> None:
    for stage in gi["stages"].values():
        stage["blob"] = "a" * 40 + "\n"


def _marker(value: Any) -> Callable[[dict[str, Any]], None]:
    def change(gi: dict[str, Any]) -> None:
        gi["stages"]["base"] = None
        gi["syntheticBase"] = value

    return _gi(change)


def _whole(value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(manifest: dict[str, Any]) -> None:
        manifest["gitIndex"] = value

    return mutate


MALFORMED_GIT_INDEX: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("null", _whole(None)),
    ("array", _whole([])),
    ("missing repo", _delete("repo")),
    ("relative repo", _set("repo", "relative")),
    ("empty repo", _set("repo", "")),
    ("NUL repo", _set("repo", "/private\0repo")),
    ("unknown key", _set("verified", True)),
    ("missing path", _delete("path")),
    ("absolute path", _set("path", "/flow.json")),
    ("empty path", _set("path", "")),
    ("dot path", _set("path", "sub/./flow.json")),
    ("parent path", _set("path", "../flow.json")),
    ("empty path segment", _set("path", "sub//flow.json")),
    ("NUL path", _set("path", "flow\0.json")),
    ("nonstring path", _set("path", 1)),
    ("missing stages", _delete("stages")),
    ("array stages", _set("stages", [])),
    ("missing base", _gi(lambda gi: gi["stages"].pop("base"))),
    ("missing ours", _gi(lambda gi: gi["stages"].pop("ours"))),
    ("null theirs", _gi(lambda gi: gi["stages"].__setitem__("theirs", None))),
    ("extra stage", _gi(lambda gi: gi["stages"].__setitem__("other", gi["stages"]["ours"]))),
    ("wrong stage role", _stage("ours", "stage", 1)),
    ("string stage", _stage("base", "stage", "1")),
    ("nonregular mode", _stage("base", "mode", "120000")),
    ("numeric mode", _stage("base", "mode", 100644)),
    ("missing blob", _gi(lambda gi: gi["stages"]["base"].pop("blob"))),
    ("short blob", _stage("base", "blob", "a" * 39)),
    ("intermediate blob", _stage("base", "blob", "a" * 41)),
    ("newline in all blobs", _gi(_all_blobs)),
    ("uppercase blob", _stage("base", "blob", "A" * 40)),
    ("mixed blob lengths", _stage("base", "blob", "a" * 64)),
    ("extra stage field", _stage("base", "extra", True)),
    ("null base without marker", _gi(lambda gi: gi["stages"].__setitem__("base", None))),
    ("marker with real base", _set("syntheticBase", {"reason": "missing-stage-1", "value": []})),
    ("null marker", _marker(None)),
    ("wrong marker reason", _marker({"reason": "empty-base", "value": []})),
    ("nonempty marker", _marker({"reason": "missing-stage-1", "value": [1]})),
    ("missing marker value", _marker({"reason": "missing-stage-1"})),
    ("extra marker field", _marker({"reason": "missing-stage-1", "value": [], "verified": True})),
    ("synthetic hash mismatch", _marker({"reason": "missing-stage-1", "value": []})),
]


@pytest.mark.parametrize(("name", "mutate"), MALFORMED_GIT_INDEX, ids=[name for name, _ in MALFORMED_GIT_INDEX])
def test_resolve_rejects_malformed_git_index_without_replacing_outputs(setup, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    s = setup()
    s.document["gitIndex"] = provenance()
    for c in s.document["conflicts"]:
        c["decision"] = {"choice": "theirs"}
    mutate(s.document)
    write(s.manifest, s.document)
    files = [s.out, s.report, s.manifest]
    before = [f.read_bytes() for f in files]
    result = s.resolve()
    assert result.returncode == 2, result.stderr
    assert re.search("gitIndex", result.stderr)
    for i, f in enumerate(files):
        assert f.read_bytes() == before[i]
