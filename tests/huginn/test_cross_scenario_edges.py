"""Relocation-v2 contract: original scoped endpoints, validated final graph."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import EDGE_FIELDS
from flowwrench.huginn.formats import KitError, ReferenceValidationError, yaml_text
from flowwrench.huginn.relocation import combine
from .test_core import ROOT, graph_oracle
from .test_relocation import EXAMPLE, example


class CrossScenarioEdgeTests(unittest.TestCase):
    def setUp(self):
        self.left = example("left.json")
        self.right = example("right.json")
        self.manifest = example("manifest-v2.yaml")
        self.variables = example("vars.yaml")

    def combine(self):
        return combine(self.left, self.right, self.manifest, self.variables)

    def test_explicit_output_preserves_fields_and_adds_both_cross_scenario_edges(self):
        before = deepcopy((self.left, self.right, self.manifest, self.variables))
        result = self.combine()
        self.assertEqual(result, example("expected-v2.json"))
        _, _, links, controls = graph_oracle(result)
        self.assertEqual(links, [("copy_feed", "copy_sink"), ("feed", "sink"), ("sink", "copy_feed")])
        self.assertEqual(controls, [("control", "copy_feed"), ("control", "copy_sink"), ("control", "feed")])
        self.assertEqual(before, (self.left, self.right, self.manifest, self.variables))

    def test_endpoints_follow_original_guids_through_swaps_and_renames_once(self):
        self.manifest["guids"]["left"] = {"feed": "sink", "sink": "feed"}
        # Right's new feed GUID deliberately equals another right original GUID.
        self.manifest["guids"]["right"]["feed"] = "control"
        self.manifest["guids"]["right"]["control"] = "new_control"
        _, _, links, controls = graph_oracle(self.combine())
        self.assertIn(("feed", "control"), links)
        self.assertIn(("new_control", "sink"), controls)

    def test_empty_v2_edge_lists_match_v1_and_output_order_is_deterministic(self):
        self.manifest["links"] = []
        self.manifest["control_links"] = []
        self.assertEqual(self.combine(), example("expected.json"))
        for kind, fields in EDGE_FIELDS.items():
            self.manifest[kind] = [dict(zip(fields, ({"input": "left", "guid": "sink"},
                                                    {"input": "right", "guid": "feed"}))),
                                   dict(zip(fields, ({"input": "right", "guid": "sink"},
                                                    {"input": "left", "guid": "sink"})))]
        first = self.combine()
        for kind in EDGE_FIELDS:
            self.manifest[kind].reverse()
        self.assertEqual(first, self.combine())

    def test_each_endpoint_requires_guid_in_named_original_input(self):
        for kind, fields in EDGE_FIELDS.items():
            for field in fields:
                for endpoint in ({"input": "left", "guid": "control"},
                                 {"input": "right", "guid": "copy_feed"},
                                 {"input": "right", "guid": "SECRET_ABSENT"}):
                    with self.subTest(kind=kind, field=field, endpoint=endpoint):
                        self.manifest = example("manifest-v2.yaml")
                        self.manifest[kind][0][field] = endpoint
                        with self.assertRaises(ReferenceValidationError) as raised:
                            self.combine()
                        self.assertEqual(raised.exception.pointer, f"/{kind}/0/{field}/guid")
                        self.assertIn("unknown original guid", raised.exception.reason)
                        self.assertNotIn("SECRET_ABSENT", str(raised.exception))

    def test_edge_shapes_and_endpoints_are_concrete_and_strict(self):
        for kind, fields in EDGE_FIELDS.items():
            for bad in (None, {}, "${HSK:EDGES}", [None], [{}],
                        [{**self.manifest[kind][0], "extra": 0}]):
                with self.subTest(kind=kind, bad=bad), self.assertRaises(KitError):
                    manifest = {**self.manifest, kind: bad}
                    combine(self.left, self.right, manifest, self.variables)
            for field in fields:
                for bad in (None, [], "feed", 0, {}, {"input": "left"},
                            {"input": "left", "guid": "feed", "extra": 1},
                            {"input": [], "guid": "feed"}, {"input": True, "guid": "feed"},
                            {"input": "middle", "guid": "feed"},
                            {"input": "${HSK:SIDE}", "guid": "feed"},
                            {"input": "left", "guid": "${HSK:GUID}"},
                            {"input": "left", "guid": 1}, {"input": "left", "guid": "bad/guid"}):
                    with self.subTest(kind=kind, field=field, bad=bad), self.assertRaises(KitError):
                        manifest = deepcopy(self.manifest)
                        manifest[kind][0][field] = bad
                        combine(self.left, self.right, manifest,
                                {**self.variables, "SIDE": "left", "GUID": "feed", "EDGES": []})

    def test_duplicates_among_added_edges_and_against_inherited_edges_rejected(self):
        for kind, fields in EDGE_FIELDS.items():
            self.manifest = example("manifest-v2.yaml")
            self.manifest[kind].append(deepcopy(self.manifest[kind][0]))
            with self.subTest(kind=kind, case="added"), self.assertRaisesRegex(KitError, "duplicate edge"):
                self.combine()
            # This same-input edge resolves to an inherited right-side edge.
            originals = ("feed", "sink") if kind == "links" else ("control", "feed")
            self.manifest[kind] = [dict(zip(fields, ({"input": "right", "guid": g} for g in originals)))]
            with self.subTest(kind=kind, case="inherited"), self.assertRaisesRegex(KitError, "duplicate edge"):
                self.combine()

    def test_same_pair_in_different_edge_kinds_is_valid_and_self_loops_are_retained(self):
        endpoints = ({"input": "left", "guid": "sink"}, {"input": "right", "guid": "feed"})
        for kind, fields in EDGE_FIELDS.items():
            self.manifest[kind] = [dict(zip(fields, endpoints)),
                                   dict(zip(fields, [endpoints[0]] * 2))]
        _, _, links, controls = graph_oracle(self.combine())
        for edges in (links, controls):
            self.assertIn(("sink", "copy_feed"), edges)
            self.assertIn(("sink", "sink"), edges)

    def test_v1_cannot_silently_accept_edges_and_v2_requires_both_arrays(self):
        for version in (1, True, 2.0, 3, "2", "${HSK:VERSION}"):
            with self.subTest(version=version), self.assertRaises(KitError):
                combine(self.left, self.right, {**self.manifest, "relocation_version": version}, self.variables)
        for missing in ("links", "control_links"):
            manifest = {k: v for k, v in self.manifest.items() if k != missing}
            with self.subTest(missing=missing), self.assertRaises(KitError):
                combine(self.left, self.right, manifest, self.variables)

    def test_combined_link_budget_includes_new_edges_of_each_kind(self):
        # 10,000 unique inherited edges + one added cross-input edge.
        for kind, fields in EDGE_FIELDS.items():
            self.setUp()
            template = self.left["agents"][0]
            self.left["agents"] = [{**template, "guid": f"a{i}"} for i in range(100)]
            self.left["links"] = []
            self.left["control_links"] = []
            self.left[kind] = [dict(zip(fields, (a, b))) for a in range(100) for b in range(100)]
            self.manifest["links"] = []
            self.manifest["control_links"] = []
            self.manifest[kind] = [dict(zip(fields, ({"input": "left", "guid": "a0"},
                                                    {"input": "right", "guid": "feed"})))]
            # Remove right's inherited edges to hit the boundary exactly.
            self.right["links"] = []
            self.right["control_links"] = []
            with self.subTest(kind=kind), self.assertRaisesRegex(KitError, "10000 links"):
                self.combine()


class CrossScenarioCliTests(unittest.TestCase):
    def test_v2_example_and_rejections_with_dry_run_do_not_mutate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manifest.yaml"
            output = root / "output.json"
            args = [sys.executable, "-m", "flowwrench.huginn", "combine",
                    str(EXAMPLE / "left.json"), str(EXAMPLE / "right.json"),
                    "--manifest", str(source), "--vars", str(EXAMPLE / "vars.yaml"), "-o", str(output)]
            source.write_text(yaml_text(example("manifest-v2.yaml")))
            before = {p: p.read_bytes() for p in EXAMPLE.iterdir()}
            result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), example("expected-v2.json"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            output.unlink()
            result = subprocess.run([*args, "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"valid": True, "agents": 5, "links": 3, "control_links": 3})
            self.assertFalse(output.exists())
            for kind, fields in EDGE_FIELDS.items():
                for case in ("dangling", "duplicate"):
                    manifest = example("manifest-v2.yaml")
                    if case == "dangling":
                        manifest[kind][0][fields[1]]["guid"] = "SECRET_ABSENT"
                        pointer = f"/{kind}/0/{fields[1]}/guid"
                    else:
                        manifest[kind].append(deepcopy(manifest[kind][0]))
                        pointer = f"/{kind}/1"
                    source.write_text(yaml_text(manifest))
                    manifest_before = source.read_bytes()
                    for extra in ([], ["--dry-run"]):
                        with self.subTest(kind=kind, case=case, extra=extra):
                            result = subprocess.run([*args, *extra], cwd=ROOT, capture_output=True, text=True, timeout=10)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertIn(f"{source}#{pointer}: ", result.stderr)
                            self.assertNotIn("SECRET_ABSENT", result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertFalse(output.exists())
                            self.assertEqual(source.read_bytes(), manifest_before)
            self.assertEqual(before, {p: p.read_bytes() for p in EXAMPLE.iterdir()})
