import copy
import json
import os
from html.parser import HTMLParser

from flowwrench.ha_diff import compare_traces, parse_trace
from flowwrench.ha_diff.cli import main
from flowwrench.ha_diff.render import render_html, render_text


class DocumentInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        for key in ("href", "src"):
            if key in attributes:
                self.links.append(attributes[key])


def test_html_is_script_free_escaped_and_has_working_internal_anchors(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["error"] = (
        '</pre><script>alert("x")</script><img src="https://invalid/">'
    )
    report = compare_traces(parse_trace(trace), parse_trace(right))
    output = render_html(report)
    inspector = DocumentInspector()
    inspector.feed(output)
    assert "script" not in inspector.tags and "img" not in inspector.tags
    assert len(inspector.ids) == len(set(inspector.ids))
    assert all(link.startswith("#") and link[1:] in inspector.ids for link in inspector.links)
    assert "&lt;script&gt;" in output
    assert "Content-Security-Policy" in output
    assert "action~10/0/error" in output
    assert 'class="step first"' in output


def test_text_escapes_terminal_control_characters_in_field_names(trace):
    right = copy.deepcopy(trace)
    right["trace"]["action/0"][0]["changed_variables"]["\x1b[31m\nforged row"] = "value"
    report = compare_traces(parse_trace(trace), parse_trace(right))
    output = render_text(report)
    assert "\x1b" not in output
    assert "\\u001b" in output and "\\nforged row" in output


def test_html_partial_and_equal_reports_are_explicit(trace):
    trace["trace"] = {}
    report = compare_traces(parse_trace(trace), parse_trace(trace))
    output = render_html(report)
    assert "Inconclusive" in output
    assert "Partial evidence" in output
    assert "No step-level difference" in output


def test_atomic_output_refuses_a_hardlink_to_an_input(testkit, tmp_path, capsys):
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(testkit))
    alias = tmp_path / "alias.json"
    os.link(source, alias)
    before = source.read_bytes()
    assert main([str(source), str(source), "--output", str(alias)]) == 2
    assert source.read_bytes() == before
    assert "overwrite" in capsys.readouterr().err


def test_failed_atomic_replace_preserves_existing_report_and_cleans_temp(
    testkit, tmp_path, monkeypatch, capsys
):
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(testkit))
    destination = tmp_path / "report.html"
    destination.write_text("existing report")

    def fail_replace(_source, _destination):
        raise OSError("simulated full disk")

    monkeypatch.setattr("flowwrench.ha_diff.cli.os.replace", fail_replace)
    assert main([str(source), str(source), "--format", "html", "--output", str(destination)]) == 2
    assert destination.read_text() == "existing report"
    assert not list(tmp_path.glob(".ha-trace-diff-*.tmp"))
    assert "simulated full disk" in capsys.readouterr().err


def test_successful_output_has_private_permissions(testkit, tmp_path):
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(testkit))
    destination = tmp_path / "report.html"
    assert main([str(source), str(source), "--format", "html", "--output", str(destination)]) == 0
    assert destination.read_text().startswith("<!doctype html>")
    assert destination.stat().st_mode & 0o777 == 0o600


def test_first_variable_and_first_control_difference_are_distinct(trace):
    left = copy.deepcopy(trace)
    left["trace"]["trigger/0"][0]["changed_variables"]["lux"] = 12
    left["trace"]["condition/0"] = [
        {
            "path": "condition/0",
            "timestamp": "2026-10-07T04:57:43.847300+00:00",
            "result": {"result": True},
        }
    ]
    right = copy.deepcopy(left)
    right["trace"]["trigger/0"][0]["changed_variables"]["lux"] = 125
    right["trace"]["condition/0"][0]["result"]["result"] = False
    report = compare_traces(parse_trace(left), parse_trace(right))
    assert report["first_divergence"] == "trigger/0#0"
    assert report["first_control_divergence"] == "condition/0#0"


def test_html_lists_uncertain_first_candidates_as_internal_evidence_links(trace):
    left = copy.deepcopy(trace)
    path = "action/0/repeat/sequence/0"
    left["trace"] = {
        path: [
            {
                "path": path,
                "timestamp": "2026-10-07T04:57:43.847400+00:00",
                "result": {"result": True},
            }
        ]
    }
    right = copy.deepcopy(left)
    right["trace"][path][0]["result"]["result"] = False
    report = compare_traces(parse_trace(left), parse_trace(right))
    assert report["first_divergence"] is None
    output = render_html(report)
    inspector = DocumentInspector()
    inspector.feed(output)
    assert "Recorded candidates:" in output
    assert all(link.startswith("#") and link[1:] in inspector.ids for link in inspector.links)
