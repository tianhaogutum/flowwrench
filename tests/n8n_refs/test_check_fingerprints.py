"""Standalone SARIF fingerprints agree with comparison or are omitted per file."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.cli import _sarif_result, comparison_sarif
from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import analyze, rename
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
NAMESPACE = "n8n-refcheck/comparisonIdentity/v1"


class CheckFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def write(self, name, data):
        path = self.directory / name
        path.write_text(json.dumps(data, ensure_ascii=True, indent=2))
        return path

    def run_cli(self, *args):
        process = subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", *map(str, args)],
                                 cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(process.stderr, "")
        return process.returncode, json.loads(process.stdout)

    def check(self, *paths, extra=(), expected=1):
        code, document = self.run_cli("check", *paths, "--format", "sarif", *extra)
        self.assertEqual(code, expected)
        return document["runs"][0]

    def fingerprints(self, entries):
        values = []
        for entry in entries:
            self.assertEqual(set(entry.get("partialFingerprints", {})), {NAMESPACE})
            value = entry["partialFingerprints"][NAMESPACE]
            self.assertRegex(value, r"^[0-9a-f]{64}$")
            values.append(value)
        return values

    def assert_original_results(self, run, paths_and_data, successful=True):
        expected = [_sarif_result(path, finding.to_dict())
                    for path, data in paths_and_data for finding in analyze(data)]
        actual = deepcopy(run["results"])
        for entry in actual:
            entry.pop("partialFingerprints", None)
        self.assertEqual(actual, expected)
        self.assertEqual(run["invocations"][0]["executionSuccessful"], successful)

    def test_golden_vector_agrees_with_new_absent_unchanged_comparison(self):
        data = workflow([node("Use", "={{ $items('Ghost') }}", id="consumer-雪")])
        path = self.write("input # 雪.json", data)
        run = self.check(path)
        self.assert_original_results(run, [(path, data)])
        fingerprints = self.fingerprints(run["results"])
        self.assertEqual(fingerprints, ["9845c1b8467773d8422d5941a0c32a2552ec324bd4acc4d5a3656303c8788365"])
        clean = workflow([node("Use", id="consumer-雪")])
        for left, right in ((clean, data), (data, clean), (data, data)):
            comparison = comparison_sarif("before.json", "after.json", compare(left, right), [])
            self.assertEqual(self.fingerprints(comparison["runs"][0]["results"]), fingerprints)

    def test_safe_rename_reorder_quotes_offsets_and_occurrence_counts(self):
        data = workflow([node("A", disabled=True), node("Use", "={{ $items('A') }} {{ $items('A') }}")],
                        [("A", "Use")])
        changed = rename(rename(data, "A", "A / ~ 雪"), "Use", "Renamed Use")
        changed["nodes"].reverse()
        changed["nodes"][0]["parameters"]["text"] = (
            '=prefix 雪 {{ $items("A / ~ 雪") }} {{ $items(`A / ~ 雪`) }}')
        before, after = self.write("before.json", data), self.write("after %.json", changed)
        old, new = self.check(before), self.check(after)
        stable = self.fingerprints(old["results"])
        self.assertEqual(len(set(stable)), 2)
        self.assertEqual(self.fingerprints(new["results"]), stable)
        self.assertNotEqual(old["results"][0]["properties"]["offset"], new["results"][0]["properties"]["offset"])
        self.assertNotEqual(old["results"][0]["properties"]["jsonPointer"], new["results"][0]["properties"]["jsonPointer"])
        self.assert_original_results(new, [(after, changed)])
        comparison = comparison_sarif(before, after, compare(data, changed), [])
        self.assertEqual(self.fingerprints(comparison["runs"][0]["results"]), stable)
        changed["nodes"][0]["parameters"]["text"] += ' {{ $items("A / ~ 雪") }}'
        added = self.write("added.json", changed)
        increased = self.fingerprints(self.check(added)["results"])
        self.assertEqual(increased[:2], stable)
        self.assertEqual(len(set(increased)), 3)
        for left, right in ((data, changed), (changed, data)):
            compared = comparison_sarif(before, added, compare(left, right), [])
            self.assertEqual(set(self.fingerprints(compared["runs"][0]["results"])), set(increased))

    def test_exact_name_fallback_and_dangling_connection_identities(self):
        data = workflow([node("A/~"), node("Use", "={{ $items('A/~') }}", disabled=True)],
                        [("A/~", "Missing"), ("Missing source", "Use")])
        for item in data["nodes"]:
            del item["id"]
        data["nodes"][1]["parameters"]["other/~"] = "={{ $items('Ghost') }}"
        path = self.write("fallback.json", data)
        run = self.check(path)
        self.assert_original_results(run, [(path, data)])
        compared = comparison_sarif(path, path, compare(data, data), [])["runs"][0]["results"]
        self.assertEqual(self.fingerprints(run["results"]), self.fingerprints(compared))
        self.assertTrue(any(e["ruleId"] == "R006" for e in run["results"]))
        self.assertTrue(any(e["properties"]["offset"] is None for e in run["results"]))
        changed = deepcopy(data)
        changed["nodes"][1]["id"] = "Use"  # Same value, different identity kind.
        other = self.write("id.json", changed)
        self.assertNotEqual(self.fingerprints(self.check(other)["results"]), self.fingerprints(run["results"]))

    def test_ambiguous_identity_omits_all_fingerprints_without_new_diagnostics(self):
        original = workflow([node("Use", "={{ $items('Ghost') }}"), node("A"), node("B")])
        variants = []
        for field, value in (("name", "A"), ("id", "synthetic-A"), ("id", ""), ("id", " \t"),
                             ("id", None), ("id", 7), ("id", []), ("id", {})):
            data = deepcopy(original)
            data["nodes"][2][field] = value
            variants.append((field, value, data))
        for field, value, data in variants:
            with self.subTest(field=field, value=value):
                path = self.write("ambiguous.json", data)
                run = self.check(path)
                self.assert_original_results(run, [(path, data)])
                self.assertTrue(run["results"])
                self.assertTrue(all("partialFingerprints" not in e for e in run["results"]))
                code, document = self.run_cli("check", path, "--format", "json")
                self.assertEqual(code, 1)
                self.assertEqual(document["errors"], [])
                self.assertEqual(document["reports"], [{"file": str(path), "findings": [f.to_dict() for f in analyze(data)]}])

    def test_multifile_incomplete_and_load_error_do_not_suppress_other_workflows(self):
        good = workflow([node("Use", "={{ $items('Ghost') }}")])
        unsupported = deepcopy(good)
        unsupported["nodes"][0]["parameters"]["dynamic"] = "={{ $(dynamic) }}"
        duplicate = deepcopy(good)
        duplicate["nodes"].append(node("Other", id="synthetic-Use"))
        data = [good, unsupported, duplicate]
        paths = [self.write(name, value) for name, value in zip(("good.json", "r007.json", "duplicate.json"), data)]
        for extra in ([], [self.directory / "missing.json"]):
            with self.subTest(extra=extra):
                run = self.check(*paths, *extra, expected=2 if extra else 1)
                self.assert_original_results(run, list(zip(paths, data)), successful=not extra)
                eligible = [e for e in run["results"] if e["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == paths[0].as_uri()]
                self.assertEqual(len(self.fingerprints(eligible)), 1)
                omitted = [e for e in run["results"] if e not in eligible]
                self.assertTrue(any(e["ruleId"] == "R007" for e in omitted))
                self.assertTrue(any(e["ruleId"] == "R001" for e in omitted))
                self.assertTrue(all("partialFingerprints" not in e for e in omitted))
                self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), int(bool(extra)))

    def test_warning_gates_and_clean_results_are_preserved(self):
        path = ROOT / "fixtures/branch-ref.json"
        for extra, expected in (((), 0), (("--fail-on", "warning"), 1)):
            run = self.check(path, extra=extra, expected=expected)
            self.assertEqual(len(self.fingerprints(run["results"])), 1)
        clean = self.write("clean.json", workflow([node("Use")]))
        self.assertEqual(self.check(clean, expected=0)["results"], [])
        # check has never validated IDs; identity ambiguity alone must not fail.
        malformed = self.write("clean-bad-id.json", workflow([node("Use", id=None)]))
        self.assertEqual(self.check(malformed, expected=0)["results"], [])


if __name__ == "__main__":
    unittest.main()
