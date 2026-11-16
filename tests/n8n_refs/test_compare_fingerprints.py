"""Versioned comparison fingerprints: stability, separation and fail-closed output."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.cli import comparison_sarif, sarif
from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import analyze, rename
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
NAMESPACE = "n8n-refcheck/comparisonIdentity/v1"


def results(baseline, candidate):
    comparison = compare(baseline, candidate)
    errors = [] if comparison["complete"] else [{"message": "incomplete"}]
    return comparison_sarif("before.json", "after.json", comparison, errors)["runs"][0]["results"]


class ComparisonFingerprintTests(unittest.TestCase):
    def fingerprint(self, entry):
        fingerprints = entry.get("partialFingerprints", {})
        self.assertEqual(set(fingerprints), {NAMESPACE})
        self.assertRegex(fingerprints[NAMESPACE], r"^[0-9a-f]{64}$")
        return fingerprints[NAMESPACE]

    def test_v1_golden_vector_and_new_absent_unchanged_agree(self):
        hazard = workflow([node("Use", "={{ $items('Ghost') }}", id="consumer-雪")])
        clean = workflow([node("Use", id="consumer-雪")])
        new, = results(clean, hazard)
        absent, = results(hazard, clean)
        unchanged, = results(hazard, hazard)
        self.assertEqual([e["baselineState"] for e in (new, absent, unchanged)],
                         ["new", "absent", "unchanged"])
        self.assertEqual(new["properties"]["identity"], {
            "rule": "R001", "scope": "nodes", "node": {"kind": "id", "value": "consumer-雪"},
            "path": "/parameters/text", "reference": {"kind": "missing", "value": "Ghost"},
            "occurrence": 1,
        })
        # Frozen v1 vector: compact sorted-key ASCII-escaped JSON, UTF-8, SHA-256.
        expected = "9845c1b8467773d8422d5941a0c32a2552ec324bd4acc4d5a3656303c8788365"
        for entry in (new, absent, unchanged):
            self.assertEqual(self.fingerprint(entry), expected)

    def test_safe_renames_reorder_quotes_offsets_and_repeated_hazards(self):
        baseline = workflow([node("A", disabled=True),
                             node("Use", "={{ $items('A') }} {{ $items('A') }}")], [("A", "Use")])
        candidate = rename(rename(baseline, "A", "A / ~ 雪"), "Use", "Renamed Use")
        candidate["nodes"].reverse()
        candidate["nodes"][0]["parameters"]["text"] = (
            '=prefix 雪 {{ $items("A / ~ 雪") }} {{ $items(`A / ~ 雪`) }}')
        old, new, matched = results(baseline, baseline), results(candidate, candidate), results(baseline, candidate)
        self.assertEqual(len(matched), 2)
        self.assertTrue(all(e["baselineState"] == "unchanged" for e in matched))
        self.assertNotEqual(old[0]["properties"]["offset"], new[0]["properties"]["offset"])
        self.assertNotEqual(old[0]["properties"]["jsonPointer"], new[0]["properties"]["jsonPointer"])
        fingerprints = [self.fingerprint(e) for e in old]
        self.assertEqual(fingerprints, [self.fingerprint(e) for e in new])
        self.assertEqual(fingerprints, [self.fingerprint(e) for e in matched])
        self.assertEqual(len(set(fingerprints)), 2)
        candidate["nodes"][0]["parameters"]["text"] += ' {{ $items("A / ~ 雪") }}'
        added = results(baseline, candidate)
        fresh, = [e for e in added if e["baselineState"] == "new"]
        self.assertEqual(fresh["properties"]["identity"]["occurrence"], 3)
        self.assertNotIn(self.fingerprint(fresh), fingerprints)
        removed = results(candidate, baseline)
        absent, = [e for e in removed if e["baselineState"] == "absent"]
        self.assertEqual(self.fingerprint(fresh), self.fingerprint(absent))

    def test_rule_path_node_reference_and_identity_kind_are_distinct(self):
        original = workflow([node("A"), node("B"), node("Use", "={{ $items('A') }}")])
        original["nodes"][2]["parameters"]["other/~"] = "={{ $items('A') }}"
        variants = [original]
        # R002 -> R005 at the same node/path/reference/occurrence.
        disabled = deepcopy(original)
        disabled["nodes"][0]["disabled"] = True
        disabled["connections"] = {"A": {"main": [[{"node": "Use", "type": "main", "index": 0}]]}}
        variants.append(disabled)
        for field, value in (("id", "replacement"), ("id", None)):
            changed = deepcopy(original)
            if value is None:
                del changed["nodes"][2][field]
            else:
                changed["nodes"][2][field] = value
            variants.append(changed)
        target_changed = deepcopy(original)
        target_changed["nodes"][2]["parameters"] = {"text": "={{ $items('B') }}"}
        variants.append(target_changed)
        # An ID and an exact-name fallback with identical values must differ.
        id_matches_name = deepcopy(variants[3])
        id_matches_name["nodes"][2]["id"] = "Use"
        variants.append(id_matches_name)
        known = {}
        for data in variants:
            for entry in results(data, data):
                identity = json.dumps(entry["properties"]["identity"], sort_keys=True)
                fingerprint = self.fingerprint(entry)
                self.assertNotIn(fingerprint, known, (identity, known.get(fingerprint)))
                known[fingerprint] = identity
        self.assertEqual(len(known), 11)

    def test_connection_identity_survives_source_rename_and_has_no_offset(self):
        baseline = workflow([node("A/~")], [("A/~", "Missing")])
        candidate = deepcopy(baseline)
        candidate["nodes"][0]["name"] = "New"
        candidate["connections"]["New"] = candidate["connections"].pop("A/~")
        old, = results(baseline, baseline)
        new, = results(baseline, candidate)
        self.assertEqual(new["properties"]["identity"]["scope"], "connections")
        self.assertIsNone(new["properties"]["offset"])
        self.assertEqual(self.fingerprint(old), self.fingerprint(new))
        # Dangling source produces an identity with a null reference as well.
        candidate["connections"]["Missing source"] = candidate["connections"].pop("New")
        entries = results(candidate, candidate)
        self.assertTrue(any(e["properties"]["identity"]["reference"] is None for e in entries))
        for entry in entries:
            self.fingerprint(entry)

    def test_incomplete_and_context_free_results_do_not_claim_fingerprints(self):
        hazard = workflow([node("Use", "={{ $items('Ghost') }}")])
        unsupported = deepcopy(hazard)
        unsupported["nodes"][0]["parameters"]["dynamic"] = "={{ $(dynamic) }}"
        for left, right in ((hazard, unsupported), (unsupported, hazard), (unsupported, unsupported)):
            entries = results(left, right)
            self.assertTrue(entries)
            for entry in entries:
                self.assertNotIn("baselineState", entry)
                self.assertNotIn("partialFingerprints", entry)
        # A findings-only renderer call lacks workflow identity validation.
        checked = sarif([{"file": "input.json", "findings": [f.to_dict() for f in analyze(hazard)]}], [])
        self.assertNotIn("partialFingerprints", checked["runs"][0]["results"][0])

    def test_cli_is_deterministic_across_processes_and_file_names(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            paths = [Path(tmp) / name for name in ("before # 雪.json", "after %.json", "elsewhere.json")]
            hazard = workflow([node("Use", "={{ $items('Ghost') }}", id="consumer-雪")])
            paths[0].write_text(json.dumps(workflow([node("Use", id="consumer-雪")])))
            paths[1].write_text(json.dumps(hazard, ensure_ascii=True, indent=2))
            paths[2].write_text(json.dumps(hazard, ensure_ascii=False))
            fingerprints = []
            for left, right in ((paths[0], paths[1]), (paths[0], paths[2]), (paths[1], paths[0])):
                command = [sys.executable, "-m", "flowwrench.n8n_refs", "compare", str(left), str(right),
                           "--format", "sarif"]
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stderr, "")
                entry, = json.loads(completed.stdout)["runs"][0]["results"]
                fingerprints.append(self.fingerprint(entry))
            self.assertEqual(len(set(fingerprints)), 1)


if __name__ == "__main__":
    unittest.main()
