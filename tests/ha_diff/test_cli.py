"""`flowwrench ha-diff` command surface and the bundled examples."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import flowwrench
from flowwrench.ha_diff.cli import main

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "ha-diff"


def test_help_uses_new_program_name(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: flowwrench ha-diff ")
    for flag in ("--left-pointer", "--right-pointer", "--format", "--output", "--exact",
                 "--ordinal-repeats", "--ignore-field", "--show-equal", "--check"):
        assert flag in out


def test_version_is_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{flowwrench.__version__}\n"


def test_errors_name_the_new_command(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json"), str(tmp_path / "missing.json")]) == 2
    assert capsys.readouterr().err.startswith("flowwrench ha-diff: ")


def test_motion_example_reports_the_blocked_condition(capsys):
    code = main([str(EXAMPLES / "motion-success.json"), str(EXAMPLES / "motion-blocked.json"),
                 "--format", "json", "--check"])
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "ha-trace-diff/v1"
    assert report["status"] == "different"


def test_module_entry_point_routes_through_dispatcher():
    result = subprocess.run([sys.executable, "-m", "flowwrench", "ha-diff", "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: flowwrench ha-diff ")
