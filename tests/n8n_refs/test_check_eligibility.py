"""Per-input SARIF eligibility is explanatory, independent of check gates."""
from copy import deepcopy
import json
import subprocess
import sys
import unittest

from . import test_check_fingerprints as fingerprints
from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import WorkflowError
from .helpers import node, workflow

NAMESPACE, ROOT = fingerprints.NAMESPACE, fingerprints.ROOT


class CheckEligibilityTests(unittest.TestCase):
    setUp = fingerprints.CheckFingerprintTests.setUp
    write = fingerprints.CheckFingerprintTests.write
    run_cli = fingerprints.CheckFingerprintTests.run_cli
    check = fingerprints.CheckFingerprintTests.check
    assert_original_results = fingerprints.CheckFingerprintTests.assert_original_results

    def metadata(self, run, expected):
        self.assertEqual(run["properties"]["fingerprintEligibility"], {
            "namespace": NAMESPACE,
            "artifacts": [{"uri": path.as_uri(), "eligible": not reasons, "reasonCodes": reasons}
                          for path, reasons in expected],
        })

    def test_eligible_hazard_clean_idless_and_dangling_connection(self):
        variants = [workflow([node("Use", "={{ $items('Ghost') }}", id="consumer-雪")]),
                    workflow([node("Use")]), workflow([node("A")], [("A", "Missing")])]
        fallback = deepcopy(variants[0])
        del fallback["nodes"][0]["id"]
        variants.append(fallback)
        for index, data in enumerate(variants):
            path = self.write(f"eligible # 雪 {index}.json", data)
            run = self.check(path, expected=0 if index == 1 else 1)
            self.metadata(run, [(path, [])])
            self.assert_original_results(run, [(path, data)])
            self.assertTrue(all(NAMESPACE in r["partialFingerprints"] for r in run["results"]))
            if index == 0:
                self.assertEqual(run["results"][0]["partialFingerprints"][NAMESPACE],
                                 "9845c1b8467773d8422d5941a0c32a2552ec324bd4acc4d5a3656303c8788365")

    def test_identity_reason_codes_share_compare_rejections(self):
        for field, value, reason in (("name", "Use", "duplicate-node-name"),
                                     ("id", "synthetic-Use", "duplicate-node-id"),
                                     *(("id", v, "invalid-node-id") for v in (None, "", " \t", 7, False, [], {}))):
            with self.subTest(field=field, value=value):
                data = workflow([node("Use", "={{ $items('Ghost') }}"), node("Other")])
                data["nodes"][1][field] = value
                path = self.write("blocked.json", data)
                run = self.check(path)
                self.metadata(run, [(path, [reason])])
                self.assert_original_results(run, [(path, data)])
                self.assertTrue(all("partialFingerprints" not in r for r in run["results"]))
                with self.assertRaises(WorkflowError):
                    compare(data, data)

    def test_all_blockers_are_distinct_ordered_and_not_values(self):
        data = workflow([node("Use", "={{ $items('Ghost') }} {{ $(dynamic) }}"),
                         node("Use", id=None), node("Other", id="synthetic-Use"),
                         node("Last", id=[])])
        path = self.write("combined.json", data)
        run = self.check(path)
        self.metadata(run, [(path, ["duplicate-node-name", "invalid-node-id",
                                    "duplicate-node-id", "incomplete-analysis"])])
        self.assert_original_results(run, [(path, data)])
        self.assertTrue(all("partialFingerprints" not in r for r in run["results"]))

    def test_mixed_input_order_load_errors_repeated_paths_and_r007_isolation(self):
        good_data = workflow([node("Use", "={{ $items('Ghost') }}")])
        good = self.write("good.json", good_data)
        unsupported_data = workflow([node("Use", "={{ $(dynamic) }}")])
        unsupported = self.write("r007.json", unsupported_data)
        missing = self.directory / "missing.json"
        malformed = self.directory / "bad.json"
        malformed.write_text('{"secret": secret-source-text')
        structure = self.write("structure.json", [])
        paths = [missing, good, malformed, unsupported, good, structure]
        run = self.check(*paths, expected=2)
        self.metadata(run, [(p, ["load-error"] if p in (missing, malformed, structure)
                             else ["incomplete-analysis"] if p == unsupported else []) for p in paths])
        self.assert_original_results(run, [(good, good_data), (unsupported, unsupported_data),
                                          (good, good_data)], successful=False)
        self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), 3)
        self.assertNotIn("secret-source-text", json.dumps(run))
        for result in run["results"]:
            self.assertEqual("partialFingerprints" in result, result["ruleId"] != "R007")

    def test_clean_invalid_id_is_omitted_but_preserves_text_json_and_zero_gate(self):
        path = self.write("clean-bad-id.json", workflow([node("Use", id=None)]))
        run = self.check(path, expected=0)
        self.metadata(run, [(path, ["invalid-node-id"])])
        self.assertEqual(run["results"], [])
        code, document = self.run_cli("check", path, "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(document, {"version": "0.1.0", "reports": [{"file": str(path), "findings": []}], "errors": []})
        text = subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", "check", str(path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual((text.returncode, text.stdout, text.stderr), (0, f"{path}: OK\n", ""))

    def test_exact_id_matching_and_side_metadata_agree(self):
        data = workflow([node("A", id="id"), node("B", id=" id ")])
        path = self.write("exact.json", data)
        self.metadata(self.check(path, expected=0), [(path, [])])
        code, document = self.run_cli("compare", path, path, "--format", "sarif")
        self.assertEqual(code, 0)
        metadata = document["runs"][0]["properties"]["fingerprintEligibility"]
        self.assertTrue(metadata["eligible"])
        for side in ("baseline", "candidate"):
            self.assertEqual(metadata["sides"][side],
                             {"uri": path.as_uri(), "eligible": True, "reasonCodes": []})


if __name__ == "__main__":
    unittest.main()
