"""Content-based detection of exported workflow files."""
from pathlib import Path

import pytest

from flowwrench.check.discover import DiscoveryError, classify, discover

from .conftest import FIXTURES


@pytest.mark.parametrize("name,kind", [
    ("n8n-broken-ref.json", "n8n"),
    ("n8n-clean.json", "n8n"),
    ("n8n-bundle.json", "n8n-bundle"),
    ("nodered-flows.json", "node-red"),
    ("ap-flow.json", "activepieces"),
    ("huginn-scenario.json", "huginn"),
    ("windmill-ok.flow.yaml", "windmill-flow"),
    ("windmill-parallel.flow.yaml", "windmill-flow"),
    ("windmill-nightly.schedule.yaml", "windmill-schedule"),
    ("dst.crontab", "crontab"),
])
def test_fixture_kinds(name, kind):
    found = discover([str(FIXTURES / name)])
    assert [source.kind for source in found.sources] == [kind]
    assert found.findings == []


def test_directory_walk_detects_by_content_and_skips_other_json():
    found = discover([str(FIXTURES)])
    names = {Path(source.display).name for source in found.sources}
    assert "package.json" not in names
    assert "tsconfig.json" not in names
    assert {"n8n-broken-ref.json", "nodered-flows.json", "ap-flow.json", "dst.crontab"} <= names
    # Only the file whose name announces a workflow export is reported as unparseable.
    assert [(Path(f.file).name, f.rule_id, f.severity) for f in found.findings] == [
        ("broken-flows.json", "parse/invalid-json", "error")]
    assert found.scanned == len(list(FIXTURES.iterdir()))


def test_display_paths_are_relative_to_the_given_root(workdir):
    workdir("n8n-clean.json", into="exports/n8n")
    found = discover(["exports"])
    assert [source.display for source in found.sources] == ["exports/n8n/n8n-clean.json"]
    found = discover(["."])
    assert [source.display for source in found.sources] == ["exports/n8n/n8n-clean.json"]


def test_hidden_and_dependency_directories_are_skipped(workdir):
    root = workdir("n8n-clean.json", into=".git")
    workdir("n8n-clean.json", into="node_modules/pkg")
    workdir("ap-flow.json", into="real")
    found = discover([str(root)])
    assert [Path(source.display).name for source in found.sources] == ["ap-flow.json"]


def test_same_file_given_twice_is_checked_once(workdir):
    workdir("ap-flow.json")
    found = discover(["ap-flow.json", ".", "./ap-flow.json"])
    assert len(found.sources) == 1


def test_explicit_non_workflow_file_gets_an_info_notice():
    found = discover([str(FIXTURES / "package.json")])
    assert found.sources == []
    assert [(f.rule_id, f.severity) for f in found.findings] == [("parse/unrecognised", "info")]


def test_explicit_unparseable_file_is_reported_whatever_its_name():
    found = discover([str(FIXTURES / "tsconfig.json")])
    assert [f.rule_id for f in found.findings] == ["parse/invalid-json"]


def test_missing_path_is_a_usage_error():
    with pytest.raises(DiscoveryError, match="no such file"):
        discover(["does/not/exist"])


def test_huginn_kit_directory_is_one_source(tmp_path):
    kit = tmp_path / "weather-kit"
    (kit / "agents").mkdir(parents=True)
    (kit / "scenario.yaml").write_text(
        "kit_version: 1\nscenario:\n  schema_version: 1\n  name: Kit\n  guid: fixture-kit\n"
        "links: []\ncontrol_links: []\n")
    (kit / "agents" / "a.yaml").write_text(
        "guid: fixture-a\nname: A\ntype: Agents::ManualEventAgent\noptions: {}\ndisabled: false\n")
    found = discover([str(tmp_path)])
    assert [(source.kind, Path(source.display).name) for source in found.sources] == [
        ("huginn-kit", "weather-kit")]


def test_windmill_inline_tags_do_not_hide_a_flow(tmp_path):
    flow = tmp_path / "f" / "x.flow"
    flow.mkdir(parents=True)
    (flow / "flow.yaml").write_text(
        "summary: Inline\nvalue:\n  modules:\n    - id: a\n      value:\n        type: rawscript\n"
        "        content: !inline a.inline_script.ts\n        language: bun\n        input_transforms: {}\n")
    found = discover([str(tmp_path)])
    assert [source.kind for source in found.sources] == ["windmill-flow"]


@pytest.mark.parametrize("data", [
    {"name": "x"}, [], [1, 2], {"nodes": [], "connections": []}, {"agents": []},
    {"trigger": {"type": "SOMETHING"}}, [{"id": "a", "type": "x"}],
])
def test_classify_rejects_lookalikes(data):
    assert classify(data) is None
