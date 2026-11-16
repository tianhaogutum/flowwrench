from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import WorkflowError, load_workflow, rename
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "fixtures/comparison/baseline.json"
CANDIDATE = ROOT / "fixtures/comparison/candidate.json"


class ComparisonTests(unittest.TestCase):
    def test_example_new_resolved_and_unchanged_with_original_locations(self):
        result = compare(load_workflow(BASELINE), load_workflow(CANDIDATE))
        self.assertTrue(result["complete"])
        self.assertEqual(result["renames"], [{"id": "consumer", "from": "Use", "to": "Renamed Use"}])
        self.assertEqual([e["candidate"]["reference"] for e in result["new"]], ["MissingNew"])
        self.assertEqual([e["baseline"]["reference"] for e in result["resolved"]], ["MissingOld"])
        self.assertEqual(len(result["unchanged"]), 1)
        entry = result["unchanged"][0]
        self.assertEqual(entry["identity"]["node"], {"kind": "id", "value": "consumer"})
        self.assertEqual(entry["identity"]["path"], "/parameters/fields/0/a~1~0")
        self.assertEqual(entry["identity"]["reference"], {"kind": "id", "value": "source"})
        self.assertEqual(entry["baseline"]["pointer"], "/nodes/2/parameters/fields/0/a~1~0")
        self.assertEqual(entry["candidate"]["pointer"], "/nodes/0/parameters/fields/0/a~1~0")

    def test_reference_and_consumer_rename_preserve_identity(self):
        baseline = workflow([node("A", disabled=True), node("Use", "={{ $items('A') }}")], [("A", "Use")])
        candidate = rename(rename(baseline, "A", "A / ~ renamed"), "Use", "Renamed Use")
        candidate["nodes"].reverse()
        candidate["nodes"][0]["parameters"]["text"] = "=prefix {{ $items(\"A / ~ renamed\") }}"
        original = deepcopy(candidate)
        result = compare(baseline, candidate)
        self.assertEqual(result["new"], [])
        self.assertEqual(result["resolved"], [])
        self.assertEqual(len(result["unchanged"]), 1)
        self.assertEqual(len(result["renames"]), 2)
        self.assertEqual(candidate, original)

    def test_stale_reference_after_rename_is_new(self):
        baseline = workflow([node("A", disabled=True), node("Use", "={{ $items('A') }}")], [("A", "Use")])
        candidate = rename(baseline, "A", "Renamed")
        candidate["nodes"][1]["parameters"]["text"] = baseline["nodes"][1]["parameters"]["text"]
        result = compare(baseline, candidate)
        self.assertEqual([e["candidate"]["rule"] for e in result["new"]], ["R001"])
        self.assertEqual([e["baseline"]["rule"] for e in result["resolved"]], ["R005"])

    def test_offsets_ignored_but_repeated_hazards_counted(self):
        baseline = workflow([node("Use", "={{ $items('Ghost') }}")])
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["parameters"]["text"] = "=prefix {{ $items(\"Ghost\") }} {{ $('Ghost').first() }}"
        result = compare(baseline, candidate)
        self.assertEqual(len(result["unchanged"]), 1)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(result["new"][0]["identity"]["occurrence"], 2)
        self.assertEqual(len(compare(candidate, baseline)["resolved"]), 1)

    def test_same_named_replacement_with_different_id_is_new(self):
        baseline = workflow([node("Use", "={{ $items('Ghost') }}")])
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["id"] = "replacement"
        result = compare(baseline, candidate)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(len(result["resolved"]), 1)
        self.assertEqual(result["renames"], [])

    def test_no_id_fallback_matches_names_but_does_not_guess_renames(self):
        baseline = workflow([node("Use", "={{ $items('Ghost') }}")])
        del baseline["nodes"][0]["id"]
        self.assertEqual(len(compare(baseline, baseline)["unchanged"]), 1)
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["name"] = "Renamed"
        result = compare(baseline, candidate)
        self.assertEqual(len(result["new"]), 1)
        self.assertEqual(len(result["resolved"]), 1)
        self.assertEqual(result["renames"], [])
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["id"] = "now-has-id"
        self.assertEqual(len(compare(baseline, candidate)["new"]), 1)

    def test_ambiguous_identity_refused(self):
        for nodes in ([node("A"), node("B", id="synthetic-A")],
                      [node("A"), node("A", id="other")],
                      [node("A", id="")], [node("A", id=[])], [node("A", id=None)]):
            data = workflow(nodes)
            with self.subTest(nodes=nodes), self.assertRaises(WorkflowError):
                compare(data, data)

    def test_node_and_path_are_both_part_of_identity(self):
        baseline = workflow([node("Use", "={{ $items('Ghost') }}"), node("Other")])
        for move_to_other in (False, True):
            candidate = deepcopy(baseline)
            value = candidate["nodes"][0]["parameters"].pop("text")
            candidate["nodes"][int(move_to_other)]["parameters"]["other"] = value
            result = compare(baseline, candidate)
            self.assertEqual(len(result["new"]), 1)
            self.assertEqual(len(result["resolved"]), 1)

    def test_connection_source_rename_uses_id_and_escaped_path(self):
        baseline = workflow([node("A/~")], [("A/~", "Missing")])
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["name"] = "New"
        candidate["connections"]["New"] = candidate["connections"].pop("A/~")
        result = compare(baseline, candidate)
        self.assertEqual(result["new"], [])
        self.assertEqual(len(result["unchanged"]), 1)
        self.assertEqual(result["unchanged"][0]["identity"]["path"], "/main/0/0/node")

    def test_unsupported_analysis_is_incomplete_even_when_unchanged(self):
        unsupported = workflow([node("Use", "={{ $(dynamic) }}")])
        result = compare(unsupported, unsupported)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["unchanged"]), 1)


class ComparisonCLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", "compare", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_example_report_and_new_gate(self):
        result = self.run_cli(BASELINE, CANDIDATE, "--format", "json", "--fail-on-new")
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["comparison"]["new"]), 1)
        self.assertEqual(self.run_cli(BASELINE, CANDIDATE).returncode, 0)
        text = self.run_cli(BASELINE, CANDIDATE).stdout
        self.assertIn("new: R001", text)
        self.assertIn("resolved: R001", text)
        self.assertIn("rename:", text)

    def test_existing_errors_and_resolved_errors_do_not_fail_new_gate(self):
        self.assertEqual(self.run_cli(BASELINE, BASELINE, "--fail-on-new").returncode, 0)
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            clean = Path(tmp) / "clean.json"
            data = load_workflow(BASELINE)
            data["nodes"][2]["parameters"] = {}
            clean.write_text(json.dumps(data))
            self.assertEqual(self.run_cli(BASELINE, clean, "--fail-on-new").returncode, 0)

    def test_new_warning_threshold(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            baseline = Path(tmp) / "baseline.json"
            data = load_workflow(ROOT / "fixtures/branch-ref.json")
            for n in data["nodes"]:
                n["parameters"] = {}
            baseline.write_text(json.dumps(data))
            candidate = ROOT / "fixtures/branch-ref.json"
            self.assertEqual(self.run_cli(baseline, candidate, "--fail-on-new").returncode, 0)
            self.assertEqual(self.run_cli(baseline, candidate, "--fail-on-new", "--fail-on", "warning").returncode, 1)

    def test_invalid_and_missing_inputs_return_structured_errors(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            invalid = Path(tmp) / "bad.json"
            invalid.write_text('{"credential": secret-source-text')
            for left, right in [(invalid, BASELINE), (BASELINE, invalid), (invalid, Path(tmp) / "missing.json")]:
                result = self.run_cli(left, right, "--format", "json")
                self.assertEqual(result.returncode, 2)
                data = json.loads(result.stdout)
                self.assertIsNone(data["comparison"])
                self.assertTrue(data["errors"])
                self.assertNotIn("secret-source-text", result.stdout + result.stderr)

    def test_ambiguous_ids_and_unsupported_analysis_never_pass(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp) / "input.json"
            for data in [workflow([node("A"), node("B", id="synthetic-A")]),
                         workflow([node("Use", "={{ $(dynamic) }}")])]:
                path.write_text(json.dumps(data))
                result = self.run_cli(path, path, "--format", "json", "--fail-on-new")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertTrue(json.loads(result.stdout)["errors"])


if __name__ == "__main__":
    unittest.main()
