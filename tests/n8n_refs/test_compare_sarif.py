"""Compare SARIF contract: side-correct provenance, states and fail-closed gates."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.core import load_workflow
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "fixtures/comparison/baseline.json"
CANDIDATE = ROOT / "fixtures/comparison/candidate.json"


def resolve_pointer(data, pointer):
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


class ComparisonSARIFTests(unittest.TestCase):
    def run_cli(self, baseline, candidate, *args):
        result = subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", "compare", str(baseline),
                                 str(candidate), "--format", "sarif", *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stderr, "")
        document = json.loads(result.stdout)
        self.assertEqual(document["version"], "2.1.0")
        return result.returncode, document["runs"][0]

    def assert_location(self, location, path, pointer):
        self.assertEqual(location["physicalLocation"], {"artifactLocation": {"uri": path.absolute().as_uri()}})
        self.assertEqual(location["logicalLocations"][0]["fullyQualifiedName"], pointer)
        # The analyzer has JSON Pointers/decoded-string offsets, not raw JSON
        # line/column or byte positions. Never reuse those offsets as regions.
        self.assertNotIn("region", location["physicalLocation"])

    def test_states_and_both_original_locations_after_reorder_and_rename(self):
        code, run = self.run_cli(BASELINE, CANDIDATE, "--fail-on-new")
        self.assertEqual(code, 1)
        self.assertTrue(run["invocations"][0]["executionSuccessful"])
        metadata = run["properties"]["comparison"]
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["baseline"], BASELINE.as_uri())
        self.assertEqual(metadata["candidate"], CANDIDATE.as_uri())
        self.assertEqual(metadata["renames"], [{"id": "consumer", "from": "Use", "to": "Renamed Use"}])
        self.assertEqual(len(run["results"]), 3)
        by_state = {entry["baselineState"]: entry for entry in run["results"]}
        self.assertEqual(set(by_state), {"new", "unchanged", "absent"})
        for state, reference in (("new", "MissingNew"), ("unchanged", "Disabled"), ("absent", "MissingOld")):
            entry = by_state[state]
            side = "baseline" if state == "absent" else "candidate"
            path = BASELINE if state == "absent" else CANDIDATE
            index = 2 if state == "absent" else 0
            pointer = f"/nodes/{index}/parameters/fields/0/a~1~0"
            self.assertEqual(entry["properties"]["comparisonSide"], side)
            self.assertEqual(entry["properties"]["jsonPointer"], pointer)
            self.assertEqual(entry["properties"]["reference"], reference)
            self.assertEqual(run["tool"]["driver"]["rules"][entry["ruleIndex"]]["id"], entry["ruleId"])
            self.assert_location(entry["locations"][0], path, pointer)
            value = resolve_pointer(load_workflow(path), pointer)
            offset = entry["properties"]["offset"]
            self.assertTrue(value[offset:].startswith("'" + reference + "'"))
            if state != "unchanged":
                self.assertNotIn("relatedLocations", entry)
        unchanged = by_state["unchanged"]
        old = unchanged["relatedLocations"][0]
        self.assert_location(old, BASELINE, "/nodes/2/parameters/fields/0/a~1~0")
        self.assertEqual(old["properties"]["jsonPointer"], "/nodes/2/parameters/fields/0/a~1~0")
        self.assertEqual(old["properties"]["node"], "Use")
        self.assertEqual(unchanged["properties"]["node"], "Renamed Use")
        self.assertNotEqual(old["properties"]["offset"], unchanged["properties"]["offset"])
        self.assertTrue(resolve_pointer(load_workflow(BASELINE), old["properties"]["jsonPointer"])[
            old["properties"]["offset"]:].startswith("'Disabled'"))

    def test_report_only_existing_and_resolved_errors_do_not_fail_gate(self):
        self.assertEqual(self.run_cli(BASELINE, CANDIDATE)[0], 0)
        code, run = self.run_cli(BASELINE, BASELINE, "--fail-on-new", "--fail-on", "warning")
        self.assertEqual(code, 0)
        self.assertEqual([e["baselineState"] for e in run["results"]], ["unchanged", "unchanged"])
        code, run = self.run_cli(BASELINE, ROOT / "fixtures/clean.json", "--fail-on-new")
        self.assertEqual(code, 0)
        self.assertEqual([e["baselineState"] for e in run["results"]], ["absent", "absent"])
        self.assertTrue(run["invocations"][0]["executionSuccessful"])

    def test_warning_threshold_and_empty_comparison(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            baseline = Path(tmp) / "baseline.json"
            data = load_workflow(ROOT / "fixtures/branch-ref.json")
            for n in data["nodes"]:
                n["parameters"] = {}
            baseline.write_text(json.dumps(data))
            candidate = ROOT / "fixtures/branch-ref.json"
            self.assertEqual(self.run_cli(baseline, candidate, "--fail-on-new")[0], 0)
            code, run = self.run_cli(baseline, candidate, "--fail-on-new", "--fail-on", "warning")
            self.assertEqual(code, 1)
            self.assertEqual([(e["baselineState"], e["level"]) for e in run["results"]], [("new", "warning")])
            code, run = self.run_cli(baseline, baseline, "--fail-on-new")
            self.assertEqual(code, 0)
            self.assertEqual(run["results"], [])

    def test_bad_input_and_ambiguous_identity_return_sarif_failure(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            bad = Path(tmp) / "bad.json"
            cases = ['{"secret": secret-source-text', '{}', '[]',
                     json.dumps(workflow([node("A"), node("B", id="synthetic-A")])),
                     json.dumps(workflow([node("A"), node("A", id="other")]))]
            for source in cases:
                bad.write_text(source)
                for left, right in ((bad, BASELINE), (BASELINE, bad)):
                    with self.subTest(source=source, left=left):
                        code, run = self.run_cli(left, right, "--fail-on-new")
                        self.assertEqual(code, 2)
                        self.assertEqual(run["results"], [])
                        invocation = run["invocations"][0]
                        self.assertFalse(invocation["executionSuccessful"])
                        self.assertTrue(invocation["toolExecutionNotifications"])
                        self.assertNotIn("secret-source-text", json.dumps(run))
            code, run = self.run_cli(Path(tmp) / "missing.json", bad)
            self.assertEqual(code, 2)
            self.assertIn("baseline", run["invocations"][0]["toolExecutionNotifications"][0]["message"]["text"])

    def test_incomplete_analysis_never_claims_new_unchanged_or_absent(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            unsupported = Path(tmp) / "unsupported.json"
            unsupported.write_text(json.dumps(workflow([node("Use", "={{ $(dynamic) }}")])))
            for left, right in ((BASELINE, unsupported), (unsupported, BASELINE), (unsupported, unsupported)):
                with self.subTest(left=left, right=right):
                    code, run = self.run_cli(left, right, "--fail-on-new")
                    self.assertEqual(code, 2)
                    self.assertFalse(run["properties"]["comparison"]["complete"])
                    self.assertFalse(run["invocations"][0]["executionSuccessful"])
                    self.assertTrue(run["results"])
                    self.assertTrue(any(e["ruleId"] == "R007" for e in run["results"]))
                    self.assertTrue(all("baselineState" not in e for e in run["results"]))

    def test_decoded_unicode_and_multiline_offsets_are_not_json_lines(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            baseline, candidate = Path(tmp) / "baseline.json", Path(tmp) / "candidate.json"
            source = "const label = '雪';\nreturn $items('Ghost');"
            data = workflow([node("Use", kind="code", parameters={"jsCode": source})])
            baseline.write_text(json.dumps(data, ensure_ascii=True, indent=2))
            data["nodes"][0]["parameters"]["jsCode"] = "// prefix\n" + source
            candidate.write_text(json.dumps(data, ensure_ascii=False))
            code, run = self.run_cli(baseline, candidate, "--fail-on-new")
            self.assertEqual(code, 0)
            entry, = run["results"]
            self.assertEqual(entry["baselineState"], "unchanged")
            self.assertEqual(entry["properties"]["offset"], source.index("'Ghost'") + len("// prefix\n"))
            old, = entry["relatedLocations"]
            self.assertEqual(old["properties"]["offset"], source.index("'Ghost'"))
            self.assert_location(entry["locations"][0], candidate, "/nodes/0/parameters/jsCode")
            self.assert_location(old, baseline, "/nodes/0/parameters/jsCode")

    def test_connection_pointer_null_offset_and_escaped_file_uris(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            baseline, candidate = Path(tmp) / "before # 雪.json", Path(tmp) / "after %.json"
            data = workflow([node("A/~")], [("A/~", "Missing")])
            baseline.write_text(json.dumps(data, ensure_ascii=True, indent=2))
            changed = deepcopy(data)
            changed["nodes"][0]["name"] = "New"
            changed["connections"]["New"] = changed["connections"].pop("A/~")
            # Compact JSON and escaped Unicode must not manufacture line numbers.
            candidate.write_text(json.dumps(changed))
            code, run = self.run_cli(baseline.relative_to(ROOT), candidate.relative_to(ROOT), "--fail-on-new")
            self.assertEqual(code, 0)
            entry, = run["results"]
            self.assertEqual(entry["baselineState"], "unchanged")
            self.assertEqual(entry["ruleId"], "R006")
            self.assertIsNone(entry["properties"]["offset"])
            self.assert_location(entry["locations"][0], candidate, "/connections/New/main/0/0/node")
            old = entry["relatedLocations"][0]
            self.assert_location(old, baseline, "/connections/A~1~0/main/0/0/node")
            self.assertEqual(resolve_pointer(data, old["properties"]["jsonPointer"]), "Missing")
            self.assertIn("%23%20%E9%9B%AA", old["physicalLocation"]["artifactLocation"]["uri"])


if __name__ == "__main__":
    unittest.main()
