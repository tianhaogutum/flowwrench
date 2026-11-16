"""Each check calls its tool's API; a crashing check becomes an ``internal`` finding."""
import hashlib
import os
from pathlib import Path

import pytest

from flowwrench.check import CHECKS, CheckOptions, discover, run_checks, select
from flowwrench.check.model import ERROR, INFO, WARNING, Finding, at_or_above
from flowwrench.check.registry import DEBUG_ENV, Check

from .conftest import DEMO, FIXTURES, FROM

OPTIONS = CheckOptions(from_date=FROM)


def findings_for(*names, checks=None, options=OPTIONS):
    found = discover([str(FIXTURES / name) for name in names])
    return run_checks(found, checks, options)


def by_rule(findings):
    return {finding.rule_id: finding for finding in findings}


def test_n8n_refs_reports_a_missing_reference_as_error():
    findings = by_rule(findings_for("n8n-broken-ref.json", checks=select(["n8n-refs"])))
    finding = findings["n8n-refs/R001"]
    assert finding.severity == ERROR
    assert finding.node == "Format"
    assert finding.message == "references missing node 'Lookup'"
    assert finding.pointer == "/nodes/1/parameters/values/string/0/value"


def test_n8n_refs_clean_workflow_has_no_reference_findings():
    assert findings_for("n8n-clean.json", checks=select(["n8n-refs"])) == []


def test_n8n_bundle_pointers_name_the_workflow_index():
    findings = findings_for("n8n-bundle.json", checks=select(["n8n-refs"]))
    assert [(f.rule_id, f.pointer) for f in findings] == [
        ("n8n-refs/R001", "/1/nodes/1/parameters/values/string/0/value")]


def test_n8n2ap_readiness_is_info_only():
    findings = findings_for("n8n-clean.json", "n8n-bundle.json", checks=select(["n8n2ap"]))
    assert findings and {f.severity for f in findings} == {INFO}
    assert all(f.message.startswith("Activepieces migration:") for f in findings)


def test_nodered_integrity_finds_dangling_wire_and_missing_broker():
    findings = by_rule(findings_for("nodered-flows.json", checks=select(["nodered"])))
    assert set(findings) == {"nodered/dangling-wire", "nodered/missing-config"}
    wire = findings["nodered/dangling-wire"]
    assert (wire.severity, wire.node, wire.pointer) == (ERROR, "Transform", "/2/wires/0")
    assert "'fixture-missing'" in wire.message
    assert findings["nodered/missing-config"].pointer == "/3/broker"


def test_ap_promote_inventory_unpinned_and_sample_data():
    findings = by_rule(findings_for("ap-flow.json", checks=select(["ap-promote"])))
    assert set(findings) == {"ap-promote/connections", "ap-promote/unpinned", "ap-promote/sample-data"}
    assert findings["ap-promote/connections"].message.endswith("gmail-dev, slack-dev")
    unpinned = findings["ap-promote/unpinned"]
    assert (unpinned.severity, unpinned.node) == (WARNING, "trigger")
    assert "@activepieces/piece-gmail" in unpinned.message and "~0.9.0" in unpinned.message


def test_windmill_outside_subset_is_a_warning_and_valid_flow_is_clean():
    findings = findings_for("windmill-parallel.flow.yaml", "windmill-ok.flow.yaml", checks=select(["windmill"]))
    assert [(Path(f.file).name, f.rule_id, f.severity) for f in findings] == [
        ("windmill-parallel.flow.yaml", "windmill/openflow", WARNING)]
    assert "parallel branches" in findings[0].message


def test_huginn_orphan_agent_and_unknown_schedule():
    findings = findings_for("huginn-scenario.json", "huginn-unknown-schedule.json", checks=select(["huginn"]))
    pairs = {(Path(f.file).name, f.rule_id, f.severity, f.node) for f in findings}
    assert pairs == {
        ("huginn-scenario.json", "huginn/orphan-agent", WARNING, "Lonely"),
        ("huginn-unknown-schedule.json", "huginn/unknown-schedule", ERROR, "A"),
    }


def test_schedules_dst_gap_and_overlap_for_a_daily_job():
    findings = by_rule(findings_for("dst.crontab", checks=select(["schedules"])))
    skipped, doubled = findings["schedules/dst-skipped"], findings["schedules/dst-ambiguous"]
    assert skipped.severity == doubled.severity == WARNING
    assert skipped.line == doubled.line == 2
    assert "2027-03-28" in skipped.message and "02:30 Europe/Berlin" in skipped.message
    assert "2027-10-31" in doubled.message
    assert "schedules/no-timezone" not in findings  # CRON_TZ is set


def test_schedules_herd_across_files_and_platforms():
    findings = findings_for("herd.crontab", "windmill-nightly.schedule.yaml", checks=select(["schedules"]))
    herds = [f for f in findings if f.rule == "herd"]
    assert [Path(f.file).name for f in herds] == ["herd.crontab", "windmill-nightly.schedule.yaml"]
    assert all("4 schedules start at once (Mon 00:00 UTC) across 2 files" in f.message for f in herds)


def test_herd_threshold_is_configurable():
    high = CheckOptions(from_date=FROM, herd_threshold=5)
    findings = findings_for("herd.crontab", checks=select(["schedules"]), options=high)
    assert not [f for f in findings if f.rule == "herd"]


def test_schedule_notes_for_intervals_and_missing_timezones():
    findings = by_rule(findings_for("nodered-flows.json", "huginn-scenario.json", checks=select(["schedules"])))
    assert findings["schedules/not-forecast"].severity == INFO
    assert "relative repeat" in findings["schedules/not-forecast"].message
    assert findings["schedules/no-timezone"].message == "1 schedule has no timezone in the export; assumed UTC"


def test_crashing_check_becomes_an_internal_finding_and_others_still_run(monkeypatch):
    monkeypatch.delenv(DEBUG_ENV, raising=False)

    def boom(source, options):
        raise KeyError("secret-value-from-input")
        yield  # pragma: no cover - makes this a generator like the real checks

    crashing = Check("boom", "-", "always crashes", frozenset({"huginn"}), boom)
    findings = findings_for("huginn-scenario.json", checks=[crashing, *select(["huginn"])])
    internal = [f for f in findings if f.rule == "internal"]
    assert len(internal) == 1
    assert internal[0].severity == ERROR and internal[0].check == "boom"
    assert "KeyError" in internal[0].message
    assert "secret-value-from-input" not in internal[0].message  # exception text may quote inputs
    assert "huginn/orphan-agent" in {f.rule_id for f in findings}


def test_crash_in_cross_file_check_is_isolated(monkeypatch):
    monkeypatch.delenv(DEBUG_ENV, raising=False)

    def boom(sources, options):
        raise RuntimeError("x")

    crashing = Check("cross", "-", "always crashes", frozenset({"crontab"}), boom, cross_file=True)
    findings = findings_for("dst.crontab", checks=[crashing])
    assert [(f.rule_id, f.file) for f in findings] == [("cross/internal", None)]


def test_debug_environment_variable_reraises(monkeypatch):
    monkeypatch.setenv(DEBUG_ENV, "1")

    def boom(source, options):
        raise RuntimeError("debug me")

    crashing = Check("boom", "-", "always crashes", frozenset({"huginn"}), boom)
    with pytest.raises(RuntimeError, match="debug me"):
        findings_for("huginn-scenario.json", checks=[crashing])


def test_tool_error_types_are_findings_not_crashes(tmp_path):
    # Detected as n8n by shape, but rejected by n8n-refs validation (node without a type).
    (tmp_path / "workflow.json").write_text('{"nodes": [{"name": "A"}], "connections": {}}')
    findings = run_checks(discover([str(tmp_path)]), select(["n8n-refs", "n8n2ap"]), OPTIONS)
    assert "n8n-refs/invalid-workflow" in {f.rule_id for f in findings}
    assert not [f for f in findings if f.rule == "internal"]


def test_checks_never_write(tmp_path):
    import shutil
    shutil.copytree(FIXTURES, tmp_path / "fixtures")
    shutil.copytree(DEMO, tmp_path / "demo")

    def snapshot():
        return {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    before = snapshot()
    run_checks(discover([str(tmp_path)]), None, OPTIONS)
    assert snapshot() == before


def test_select_validates_names():
    assert [c.name for c in select()] == [c.name for c in CHECKS]
    assert [c.name for c in select(skip=["schedules", "n8n2ap"])] == [
        c.name for c in CHECKS if c.name not in ("schedules", "n8n2ap")]
    with pytest.raises(ValueError, match="unknown check"):
        select(["nope"])


def test_at_or_above():
    warning = Finding("x", "y", WARNING, "m", "f")
    assert at_or_above([warning], "warning")
    assert not at_or_above([warning], "error")
    assert not at_or_above([warning], None)
    with pytest.raises(ValueError):
        Finding("x", "y", "fatal", "m")


def test_every_rule_emitted_is_registered():
    found = discover([str(FIXTURES), str(DEMO)])
    registered = {f"{c.name}/{rule}" for c in CHECKS for rule in c.rules}
    emitted = {f.rule_id for f in run_checks(found, None, OPTIONS)}
    assert emitted <= registered, emitted - registered
    assert os.environ.get(DEBUG_ENV) != "1"
