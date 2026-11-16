import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.cli import atomic_write, fingerprint
from flowwrench.n8n_refs.core import WorkflowError, analyze, load_workflow
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent


class CLITests(unittest.TestCase):
    def run_cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", *map(str, args)],
                              cwd=cwd, capture_output=True, text=True, timeout=15)

    def test_clean_and_warning_thresholds(self):
        result = self.run_cli("check", "fixtures/clean.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertEqual(self.run_cli("check", "fixtures/branch-ref.json").returncode, 0)
        result = self.run_cli("check", "fixtures/branch-ref.json", "--fail-on", "warning")
        self.assertEqual(result.returncode, 1)
        self.assertIn("R003 warning", result.stdout)
        self.assertIn("/nodes/5/parameters/text", result.stdout)

    def test_errors_json_and_multiple_files(self):
        result = self.run_cli("check", "fixtures/clean.json", "fixtures/rules/r001-positive.json", "--format", "json")
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["reports"]), 2)
        self.assertEqual(data["reports"][1]["findings"][0]["rule"], "R001")

    def test_sarif_structure(self):
        result = self.run_cli("check", "fixtures/branch-ref.json", "fixtures/merge-pairing.json", "--format", "sarif")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data["version"], "2.1.0")
        run = data["runs"][0]
        self.assertEqual(run["tool"]["driver"]["name"], "n8n-refcheck")
        self.assertEqual(len(run["tool"]["driver"]["rules"]), 7)
        self.assertEqual({r["ruleId"] for r in run["results"]}, {"R003", "R004"})
        for r in run["results"]:
            self.assertEqual(run["tool"]["driver"]["rules"][r["ruleIndex"]]["id"], r["ruleId"])
            loc = r["locations"][0]
            self.assertTrue(loc["physicalLocation"]["artifactLocation"]["uri"].startswith("file:///"))
            self.assertEqual(loc["logicalLocations"][0]["fullyQualifiedName"], r["properties"]["jsonPointer"])
        self.assertTrue(run["invocations"][0]["executionSuccessful"])

    def test_invalid_json_not_workflow_and_missing_file_exit2(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp) / "bad.json"
            for source in ['{"secret":super-secret-value', '{}', '[]']:
                path.write_text(source)
                result = self.run_cli("check", path, "--format", "json")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(json.loads(result.stdout)["errors"]), 1)
                self.assertNotIn("super-secret-value", result.stdout + result.stderr)
            result = self.run_cli("check", Path(tmp)/"missing", "--format", "sarif")
            self.assertEqual(result.returncode, 2)
            run = json.loads(result.stdout)["runs"][0]
            self.assertFalse(run["invocations"][0]["executionSuccessful"])
            self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), 1)

    def test_cli_parser_errors(self):
        for args in [[], ["check"], ["check", "x", "--format", "csv"], ["rename", "x", "--from", "A"], ["rename", "x", "--from", "A", "--to", "B", "-o", "x", "--in-place"]]:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args).returncode, 2)

    def test_rename_end_to_end_and_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp)/"renamed.json"
            result = self.run_cli("rename", "fixtures/rename.json", "--from", "Fetch / A", "--to", "Renamed ' 雪 😀", "-o", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.run_cli("check", path, "--fail-on", "warning").returncode, 0)
            changed = load_workflow(path)
            self.assertIn("Renamed ' 雪 😀", changed["pinData"])
            result = self.run_cli("rename", path, "--from", "Renamed ' 雪 😀", "--to", "Fetch / A", "--in-place")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(analyze(load_workflow(path)), [])

    def test_no_write_on_collision_unsupported_and_existing_output(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source, output = Path(tmp)/"input.json", Path(tmp)/"output.json"
            original = (ROOT/"fixtures/clean.json").read_bytes()
            source.write_bytes(original)
            result = self.run_cli("rename", source, "--from", "Fetch", "--to", "Notify", "--in-place")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(output.exists())
            result = self.run_cli("rename", "fixtures/rules/r007-positive.json", "--from", "A", "--to", "New", "-o", output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            output.write_text("keep me")
            result = self.run_cli("rename", source, "--from", "Fetch", "--to", "New", "-o", output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_text(), "keep me")
            result = self.run_cli("rename", source, "--from", "Fetch", "--to", "New", "-o", source)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob(".n8n-refcheck-*")), [])

    def test_dry_run_and_stdout_never_modify_files(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source, output = Path(tmp)/"input.json", Path(tmp)/"output.json"
            original = (ROOT/"fixtures/clean.json").read_bytes()
            source.write_bytes(original)
            for flags in [[], ["--in-place", "--dry-run"], ["-o", str(output), "--dry-run"]]:
                result = self.run_cli("rename", source, "--from", "Fetch", "--to", "New", *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["nodes"][1]["name"], "New")
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(output.exists())

    def test_symlink_outputs_and_inplace_refused(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source, link = Path(tmp)/"source.json", Path(tmp)/"link.json"
            original = (ROOT/"fixtures/clean.json").read_bytes()
            source.write_bytes(original)
            link.symlink_to(source)
            for args in [(link, "--in-place"), (source, "-o", link)]:
                result = self.run_cli("rename", args[0], "--from", "Fetch", "--to", "New", *args[1:])
                self.assertEqual(result.returncode, 2)
                self.assertTrue(link.is_symlink())
                self.assertEqual(source.read_bytes(), original)

    def test_mode_preserved_and_names_never_used_as_output_paths(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source = Path(tmp)/"source.json"
            source.write_bytes((ROOT/"fixtures/clean.json").read_bytes())
            source.chmod(0o640)
            result = self.run_cli("rename", source, "--from", "Fetch", "--to", "../../escape", "--in-place")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)
            self.assertEqual(load_workflow(source)["nodes"][1]["name"], "../../escape")
            self.assertEqual(list(Path(tmp).iterdir()), [source])

    def test_stale_fingerprint_prevents_replace(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp)/"file.json"
            path.write_text("before")
            expected = fingerprint(path)
            path.write_text("modified concurrently")
            with self.assertRaises(WorkflowError):
                atomic_write(path, "replacement", expected)
            self.assertEqual(path.read_text(), "modified concurrently")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_user_code_and_expressions_are_not_executed(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            sentinel = Path(tmp)/"must-not-exist"
            payload = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('executed')"
            data = workflow([node("A", kind="code"), node("B", "={{ " + payload + " }}")], [("A", "B")])
            data["nodes"][0]["parameters"]["pythonCode"] = payload
            path = Path(tmp)/"input.json"
            path.write_text(json.dumps(data))
            self.assertEqual(self.run_cli("check", path).returncode, 0)
            self.assertEqual(self.run_cli("rename", path, "--from", "A", "--to", "New", "--in-place", "--dry-run").returncode, 0)
            self.assertFalse(sentinel.exists())


class FixtureTests(unittest.TestCase):
    def test_each_rule_positive_and_negative_real_format(self):
        for number in range(1, 8):
            rule = f"R{number:03}"
            for positive in (True, False):
                path = ROOT/"fixtures"/"rules"/f"{rule.lower()}-{'positive' if positive else 'negative'}.json"
                with self.subTest(path=path):
                    found = {f.rule for f in analyze(load_workflow(path))}
                    self.assertEqual(found, {rule} if positive else set())
