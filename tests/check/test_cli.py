"""``flowwrench check`` command line: formats, exit codes, selection, the README demo."""
import json
import re

import pytest

from flowwrench import __version__, cli as dispatcher
from flowwrench.check.registry import CHECKS

from .conftest import DEMO, FROM, rule_ids

SUMMARY = re.compile(r"^\d+ files? · \d+ errors? · \d+ warnings? · \d+ info$", re.MULTILINE)


def test_help_uses_program_name(check):
    code, out, _ = check("--help")
    assert code == 0
    assert "usage: flowwrench check" in out


def test_dispatcher_routes_to_check(workdir, capsys):
    workdir("n8n-clean.json")
    assert dispatcher.main(["check", "--from", FROM]) == 0
    assert SUMMARY.search(capsys.readouterr().out)


def test_list_checks_names_every_check(check):
    code, out, _ = check("--list-checks")
    assert code == 0
    for item in CHECKS:
        assert re.search(rf"^  {re.escape(item.name)}\s", out, re.MULTILINE), item.name


def test_default_path_is_current_directory(workdir, check_json):
    workdir("ap-flow.json")
    code, document = check_json()
    assert code == 0
    assert document["paths"] == ["."]
    assert [f["path"] for f in document["files"]] == ["ap-flow.json"]


def test_text_report_groups_by_file_and_ends_with_summary(workdir, check):
    workdir("n8n-broken-ref.json", "nodered-flows.json")
    code, out, err = check("--from", FROM, "--color", "never")
    assert code == 1 and err == ""
    assert "\nn8n-broken-ref.json  n8n workflow\n" in out
    assert "\nnodered-flows.json  Node-RED flows\n" in out
    assert re.search(r"^  error  n8n-refs/R001 +Format: references missing node 'Lookup'$", out, re.MULTILINE)
    assert "\x1b[" not in out
    lines = out.rstrip("\n").splitlines()
    assert SUMMARY.match(lines[-2] if lines[-1].startswith("-v ") else lines[-1])


def test_verbose_shows_hints_and_pointers(workdir, check):
    workdir("n8n-broken-ref.json")
    _, out, _ = check("--from", FROM, "-v")
    assert "/nodes/1/parameters/values/string/0/value" in out
    assert "-> rename the reference" in out


def test_color_always_uses_ansi(workdir, check):
    workdir("n8n-broken-ref.json")
    _, out, _ = check("--from", FROM, "--color", "always")
    assert "\x1b[1;31m" in out


def test_exit_codes_follow_fail_on(workdir, check):
    workdir("windmill-parallel.flow.yaml")  # one warning, no error
    assert check("--from", FROM)[0] == 0
    assert check("--from", FROM, "--fail-on", "warning")[0] == 1
    workdir("n8n-broken-ref.json")  # adds an error
    assert check("--from", FROM)[0] == 1
    assert check("--from", FROM, "--fail-on", "never")[0] == 0


def test_info_only_never_fails(workdir, check):
    workdir("n8n-clean.json")  # n8n2ap readiness is info
    assert check("--from", FROM, "--fail-on", "warning")[0] == 0


@pytest.mark.parametrize("argv,message", [
    (["does-not-exist"], "no such file or directory"),
    (["--only", "nope"], "unknown check"),
    (["--skip", "parse,n8n-refs,n8n2ap,nodered,ap-promote,windmill,huginn,schedules"], "no check to run"),
    (["--default-timezone", "Mars/Olympus"], "timezone"),
])
def test_usage_and_input_errors_exit_2(workdir, check, argv, message):
    workdir("n8n-clean.json")
    code, out, err = check(*argv)
    assert code == 2 and out == ""
    assert message in err


@pytest.mark.parametrize("argv", [["--format", "xml"], ["--fail-on", "info"], ["--from", "16.11.2026"],
                                  ["--herd-threshold", "1"]])
def test_argparse_rejections_exit_2(check, argv):
    assert check(*argv)[0] == 2


def test_empty_directory_reports_nothing_found(tmp_path, monkeypatch, check):
    monkeypatch.chdir(tmp_path)
    code, out, _ = check()
    assert code == 0
    assert "No n8n, Node-RED, Activepieces, Windmill, Huginn or crontab exports found." in out
    assert "0 files · 0 errors · 0 warnings · 0 info" in out


def test_only_and_skip(workdir, check_json):
    workdir("n8n-broken-ref.json", "nodered-flows.json", "ap-flow.json")
    _, document = check_json("--only", "nodered", "--only", "ap-promote")
    assert document["checks"] == ["nodered", "ap-promote"]
    assert {f["check"] for f in document["findings"]} == {"nodered", "ap-promote"}
    _, document = check_json("--skip", "n8n2ap,schedules,ap-promote")
    assert {f["check"] for f in document["findings"]} == {"n8n-refs", "nodered"}


def test_json_document_shape(workdir, check_json):
    workdir("n8n-broken-ref.json", "broken-flows.json")
    code, document = check_json()
    assert code == 1
    assert document["format"] == "flowwrench.check/v1"
    assert document["tool"] == {"name": "flowwrench", "version": __version__}
    assert document["failOn"] == "error" and document["failed"] is True
    assert document["summary"]["files"] == 1  # the unparseable file is a finding, not a source
    assert document["summary"]["error"] == 2
    assert rule_ids(document, "broken-flows.json") == ["parse/invalid-json"]
    finding = next(f for f in document["findings"] if f["ruleId"] == "n8n-refs/R001")
    assert set(finding) == {"check", "rule", "ruleId", "severity", "message", "file", "pointer", "line",
                            "node", "hint"}


def test_sarif_document(workdir, check):
    workdir("n8n-broken-ref.json", "dst.crontab", into="exports")
    code, out, _ = check("exports", "--format", "sarif", "--from", FROM)
    assert code == 1
    sarif = json.loads(out)
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    run = sarif["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == "flowwrench" and driver["version"] == __version__
    rules = [rule["id"] for rule in driver["rules"]]
    assert len(rules) == len(set(rules))
    for result in run["results"]:
        assert rules[result["ruleIndex"]] == result["ruleId"]
        assert result["level"] in ("error", "warning", "note")
        location = result["locations"][0]["physicalLocation"]
        assert location["artifactLocation"]["uri"].startswith("exports/")
        assert location["region"]["startLine"] >= 1
    r001 = next(r for r in run["results"] if r["ruleId"] == "n8n-refs/R001")
    assert r001["level"] == "error"
    assert r001["locations"][0]["logicalLocations"][0]["fullyQualifiedName"].startswith("/nodes/1/")
    dst = next(r for r in run["results"] if r["ruleId"] == "schedules/dst-skipped")
    assert dst["locations"][0]["physicalLocation"]["region"]["startLine"] == 2
    assert run["invocations"][0]["executionSuccessful"] is True


def test_readme_demo_output(monkeypatch, check):
    """The README demo (assets/check-demo.svg) is drawn from this exact run."""
    monkeypatch.chdir(DEMO)
    code, out, err = check("--from", FROM, "--color", "never")
    assert code == 1 and err == ""
    assert "(7 workflow exports: 2 n8n, 1 Node-RED, 1 Activepieces, 1 Windmill, 1 Huginn, 1 cron; " \
           "7 files inspected)" in out
    assert "7 files · 3 errors · 9 warnings · 7 info" in out
    assert "✓ 1 file with no findings" in out
    for expected in [
        r"^  error  n8n-refs/R001 +Post summary: references missing node 'Enrich'$",
        r"^  warn   n8n-refs/R003 +Notify sales: references 'Enrich lead', which does not run on every path",
        r"^  error  nodered/dangling-wire +Parse reading: wire to node 'debug-old' that does not exist$",
        r"^  error  nodered/missing-config +Publish: uses config node 'broker-home'",
        r"^  warn   ap-promote/unpinned +trigger: @activepieces/piece-gmail is not pinned",
        r"^  warn   huginn/orphan-agent +Unused formatter: agent is not linked",
        r"^  warn   schedules/dst-skipped +02:30 Europe/Berlin does not exist on 2027-03-28 .*  line 4$",
        r"^  warn   schedules/dst-ambiguous +02:30 Europe/Berlin happens twice on 2027-10-31 .*  line 4$",
        r"^  warn   schedules/herd +4 schedules start at once \(Mon 00:00 UTC\) across 4 files: "
        r"Order sync / Every night$",
    ]:
        assert re.search(expected, out, re.MULTILINE), expected
