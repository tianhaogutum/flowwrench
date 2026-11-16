"""Intrinsic side eligibility versus pair-wide fingerprint/state omission."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowwrench.n8n_refs.cli import comparison_sarif
from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import WorkflowError
from .helpers import TEST_TMP, node, workflow
from . import test_compare_sarif

NAMESPACE = "n8n-refcheck/comparisonIdentity/v1"


class ComparisonEligibilityTests(unittest.TestCase):
    run_cli = test_compare_sarif.ComparisonSARIFTests.run_cli

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.clean = workflow([node("Use", id="consumer-雪")])
        self.hazard = workflow([node("Use", "={{ $items('Ghost') }}", id="consumer-雪")])
        self.unsupported = workflow([node("Use", "={{ $(dynamic) }}")])

    def write(self, name, data):
        path = self.directory / name
        path.write_text(json.dumps(data, ensure_ascii=False))
        return path

    def metadata(self, run, left, right, old_reasons=(), new_reasons=(), formed=True):
        self.assertEqual(run["properties"]["fingerprintEligibility"], {
            "namespace": NAMESPACE, "eligible": formed and not old_reasons and not new_reasons,
            "sides": {
                side: {"uri": path.as_uri(), "eligible": not reasons, "reasonCodes": list(reasons)}
                for side, path, reasons in (("baseline", left, old_reasons), ("candidate", right, new_reasons))
            },
        })
        if old_reasons or new_reasons or not formed:
            self.assertFalse(run["properties"]["comparison"]["complete"])
            for entry in run["results"]:
                self.assertNotIn("partialFingerprints", entry)
                self.assertNotIn("baselineState", entry)

    def test_complete_clean_idless_r006_and_frozen_fingerprints(self):
        clean = self.write("clean.json", self.clean)
        hazard = self.write("hazard # 雪.json", self.hazard)
        for left, right, state in ((clean, hazard, "new"), (hazard, clean, "absent"),
                                   (hazard, hazard, "unchanged")):
            code, run = self.run_cli(left, right)
            self.assertEqual(code, 0)
            self.metadata(run, left, right)
            entry, = run["results"]
            self.assertEqual(entry["baselineState"], state)
            self.assertEqual(entry["partialFingerprints"], {NAMESPACE:
                             "9845c1b8467773d8422d5941a0c32a2552ec324bd4acc4d5a3656303c8788365"})
        variants = [self.clean, workflow([node("A")], [("A", "Missing")]), deepcopy(self.hazard),
                    workflow([node("A", id="id"), node("B", id=" id ")])]
        del variants[2]["nodes"][0]["id"]
        for data in variants:
            path = self.write("same.json", data)
            code, run = self.run_cli(path, path, "--fail-on-new")
            self.assertEqual(code, 0)
            self.metadata(run, path, path)
        self.assertEqual(self.run_cli(clean, hazard, "--fail-on-new")[0], 1)

    def test_r007_on_one_or_both_sides_blocks_pair_not_healthy_peer(self):
        good = self.write("good.json", self.hazard)
        bad = self.write("r007.json", self.unsupported)
        for left, right in ((good, bad), (bad, good), (bad, bad)):
            code, run = self.run_cli(left, right, "--fail-on-new")
            self.assertEqual(code, 2)
            self.metadata(run, left, right, ["incomplete-analysis"] if left == bad else [],
                          ["incomplete-analysis"] if right == bad else [])
            self.assertTrue(run["results"])
            self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), 1)

    def test_each_identity_reason_both_sides_and_compare_first_error_preserved(self):
        good = self.write("good.json", self.hazard)
        for field, value, reason, message in (
            ("name", "Use", "duplicate-node-name", "unique node names"),
            ("id", "consumer-雪", "duplicate-node-id", "unique node IDs"),
            *(("id", value, "invalid-node-id", "nonempty string node IDs when present")
              for value in (None, "", " \t", False, 7, [], {})),
        ):
            data = deepcopy(self.hazard)
            data["nodes"].append(node("Other"))
            data["nodes"][1][field] = value
            bad = self.write("blocked.json", data)
            for left, right in ((bad, good), (good, bad), (bad, bad)):
                with self.subTest(field=field, value=value, left=left, right=right):
                    code, run = self.run_cli(left, right)
                    self.assertEqual(code, 2)
                    self.metadata(run, left, right, [reason] if left == bad else [],
                                  [reason] if right == bad else [], formed=False)
                    self.assertEqual(run["results"], [])
                    side = "baseline" if left == bad else "candidate"
                    text = run["invocations"][0]["toolExecutionNotifications"][0]["message"]["text"]
                    self.assertEqual(text, f"{side}: comparison requires {message}")
                    with self.assertRaises(WorkflowError):
                        compare(data, self.hazard)

    def test_all_reasons_collected_even_when_comparison_cannot_form(self):
        combined = workflow([node("Use", "={{ $(dynamic) }}", id=None),
                             node("Use", id="duplicate"), node("Last", id="duplicate"),
                             node("End", id=[])])
        bad = self.write("combined.json", combined)
        r007 = self.write("r007.json", self.unsupported)
        code, run = self.run_cli(bad, r007)
        self.assertEqual(code, 2)
        self.metadata(run, bad, r007, ["duplicate-node-name", "invalid-node-id", "duplicate-node-id",
                                      "incomplete-analysis"], ["incomplete-analysis"], formed=False)
        self.assertEqual(run["results"], [])
        # Metadata assessment adds no new error notifications.
        self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), 1)

    def test_load_errors_do_not_hide_healthy_or_blocked_peer(self):
        good = self.write("good.json", self.hazard)
        r007 = self.write("r007.json", self.unsupported)
        malformed = self.directory / "bad.json"
        malformed.write_text('{"secret": secret-source-text')
        failures = [self.directory / "missing.json", malformed, self.write("structure.json", []),
                    self.directory]  # read error as well as ENOENT
        for bad in failures:
            for peer, reasons in ((good, []), (r007, ["incomplete-analysis"]), (bad, ["load-error"])):
                for left, right in ((bad, peer), (peer, bad)):
                    code, run = self.run_cli(left, right, "--fail-on-new")
                    self.assertEqual(code, 2)
                    self.metadata(run, left, right, ["load-error"] if left == bad else reasons,
                                  ["load-error"] if right == bad else reasons, formed=False)
                    self.assertEqual(run["results"], [])
                    self.assertNotIn("secret-source-text", json.dumps(run))
        invalid = self.write("invalid.json", workflow([node("Use", id=None)]))
        code, run = self.run_cli(invalid, malformed)
        self.assertEqual(code, 2)
        self.metadata(run, invalid, malformed, ["invalid-node-id"], ["load-error"], formed=False)

    def test_low_level_renderer_without_context_preserves_existing_metadata(self):
        result = compare(self.clean, self.hazard)
        run = comparison_sarif("old.json", "new.json", result, [])["runs"][0]
        self.assertEqual(set(run["properties"]), {"comparison"})


if __name__ == "__main__":
    unittest.main()
