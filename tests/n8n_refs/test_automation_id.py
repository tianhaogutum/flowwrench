"""Explicit SARIF stream identity must not affect analysis or gate behavior."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_refs.cli import main
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
OPTION = "--sarif-automation-id"


class AutomationIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.paths = {}
        for name, data in {
            "clean": workflow([node("Use")]),
            "hazard": workflow([node("Use", "={{ $items('Ghost') }}")]),
            "incomplete": workflow([node("Use", "={{ $items('Ghost') }} {{ $(dynamic) }}")]),
            "identity": workflow([node("A", id=None)]),
            "structure": [],
        }.items():
            path = self.directory / (name + " # 雪.json")
            path.write_text(json.dumps(data))
            self.paths[name] = str(path)
        self.paths["missing"] = str(self.directory / "missing.json")
        bad = self.directory / "malformed.json"
        bad.write_text('{"secret": secret-source-text')
        self.paths["malformed"] = str(bad)

    def cli(self, args):
        return subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", *args],
                              cwd=ROOT, capture_output=True, timeout=15)

    def sarif_pair(self, args, expected, automation_id="CI/Refs.check-1_0/"):
        default = self.cli([*args, "--format", "sarif"])
        explicit = self.cli([*args, "--format", "sarif", OPTION, automation_id])
        self.assertEqual((default.returncode, explicit.returncode), (expected, expected), explicit.stderr)
        self.assertEqual((default.stderr, explicit.stderr), (b"", b""))
        plain, grouped = json.loads(default.stdout), json.loads(explicit.stdout)
        self.assertNotIn("automationDetails", plain["runs"][0])
        self.assertEqual(grouped["runs"][0].pop("automationDetails"), {"id": automation_id})
        # Compare serialized bytes after deleting only the requested field. This
        # freezes fingerprints, eligibility, locations, notices and ordering.
        self.assertEqual((json.dumps(grouped, ensure_ascii=True, indent=2) + "\n").encode(), default.stdout)
        return plain["runs"][0]

    def test_check_complete_incomplete_error_and_mixed_invocations(self):
        p = self.paths
        for names, extra, code in (
            (("clean",), (), 0), (("hazard",), (), 1), (("identity",), (), 0),
            (("incomplete",), (), 1), (("missing",), (), 2),
            (("malformed",), (), 2), (("structure",), (), 2),
            (("missing", "hazard", "incomplete", "hazard"), (), 2),
        ):
            with self.subTest(names=names):
                self.sarif_pair(["check", *(p[n] for n in names), *extra], code)
        warning = str(ROOT / "fixtures/branch-ref.json")
        self.sarif_pair(["check", warning], 0)
        self.sarif_pair(["check", warning, "--fail-on", "warning"], 1)

    def test_compare_complete_incomplete_and_failed_invocations(self):
        p = self.paths
        for left, right, extra, code in (
            ("clean", "hazard", (), 0), ("clean", "hazard", ("--fail-on-new",), 1),
            ("hazard", "hazard", ("--fail-on-new",), 0),
            ("hazard", "clean", ("--fail-on-new",), 0),
            ("clean", "clean", (), 0), ("clean", "incomplete", (), 2),
            ("incomplete", "hazard", (), 2), ("identity", "hazard", (), 2),
            ("missing", "hazard", (), 2), ("hazard", "malformed", (), 2),
            ("structure", "missing", (), 2),
        ):
            with self.subTest(left=left, right=right, extra=extra):
                self.sarif_pair(["compare", p[left], p[right], *extra], code)
        self.sarif_pair(["compare", str(ROOT / "fixtures/comparison/baseline.json"),
                         str(ROOT / "fixtures/comparison/candidate.json"), "--fail-on-new"], 1)

    def test_two_streams_same_findings_and_exact_accepted_syntax(self):
        for command in (["check", self.paths["hazard"]],
                        ["compare", self.paths["clean"], self.paths["hazard"], "--fail-on-new"]):
            for identity in ("A", "0", "CI/check-1_0.2/", "CI/compare/", "x" * 128):
                with self.subTest(command=command[0], identity=identity):
                    self.sarif_pair(command, 1, identity)

    def test_invalid_empty_or_missing_value_is_argument_error_before_reads(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        for command in (["check", self.paths["missing"]],
                        ["compare", self.paths["missing"], self.paths["missing"]]):
            for identity in ("", " ", " a", "a ", "a b", "a\t", "a\n", "a\r", "a\x1b",
                             "a\\b", "a:b", "a#b", "a%b", "雪", ".a", "_a", "/a", "-a", "x" * 129):
                with self.subTest(command=command[0], identity=identity):
                    out, err = StringIO(), StringIO()
                    with patch("flowwrench.n8n_refs.cli.load_workflow") as load, redirect_stdout(out), redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*command, "--format", "sarif", OPTION + "=" + identity])
                    self.assertEqual(error.exception.code, 2)
                    self.assertEqual(out.getvalue(), "")
                    self.assertIn(OPTION, err.getvalue())
                    load.assert_not_called()
            result = self.cli([*command, "--format", "sarif", OPTION])
            self.assertEqual((result.returncode, result.stdout), (2, b""))
            self.assertIn(b"expected one argument", result.stderr)

    def test_option_is_sarif_only_and_rename_does_not_accept_it(self):
        for command in (["check", self.paths["clean"]],
                        ["compare", self.paths["clean"], self.paths["clean"]]):
            for format_args in ([], ["--format", "text"], ["--format", "json"]):
                with self.subTest(command=command[0], format_args=format_args):
                    result = self.cli([*command, *format_args, OPTION, "CI/refs/"])
                    self.assertEqual((result.returncode, result.stdout), (2, b""))
                    self.assertIn(b"requires --format sarif", result.stderr)
        before = Path(self.paths["clean"]).read_bytes()
        result = self.cli(["rename", self.paths["clean"], "--from", "Use", "--to", "Changed",
                           "--in-place", OPTION, "CI/refs/"])
        self.assertEqual((result.returncode, result.stdout), (2, b""))
        self.assertEqual(Path(self.paths["clean"]).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
