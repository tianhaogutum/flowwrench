"""Shared helpers for the ``flowwrench check`` tests (fixtures are synthetic)."""
import json
from pathlib import Path
import shutil
import sys

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flowwrench.check import cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEMO = Path(__file__).resolve().parents[2] / "examples" / "check"
# A fixed Monday keeps herd minutes and DST dates reproducible.
FROM = "2026-11-16"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Copy named fixture files into an empty directory and chdir into it."""
    def make(*names, into="."):
        target = tmp_path / into
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy(FIXTURES / name, target / name)
        return tmp_path
    monkeypatch.chdir(tmp_path)
    return make


@pytest.fixture
def check(capsys):
    """Run ``flowwrench check`` in-process; return (exit code, stdout, stderr)."""
    def run(*argv):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # argparse usage errors and --help
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


@pytest.fixture
def check_json(check):
    """Run with --format json and return (exit code, parsed document)."""
    def run(*argv):
        code, out, err = check(*argv, "--format", "json", "--from", FROM)
        assert err == "", err
        return code, json.loads(out)
    return run


def rule_ids(document, file=None):
    return sorted(f["ruleId"] for f in document["findings"] if file is None or f["file"] == file)
