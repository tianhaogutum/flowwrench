"""Synthetic schema-v1 combination contract; no live Huginn instance needed."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.formats import KitError, MAX_FILE_BYTES, load_data, yaml_text
from .test_core import FIXTURES, ROOT, graph_oracle

EXAMPLE = FIXTURES / "relocation"


def example(name):
    return load_data(EXAMPLE / name)


class CombineCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "flowwrench.huginn", "combine",
                                 EXAMPLE / "left.json", EXAMPLE / "right.json", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return result

    def test_parameterized_manifest_matches_explicit_expected_export(self):
        before = {p: p.read_bytes() for p in EXAMPLE.iterdir()}
        output = self.path / "combined.json"
        self.cli("--manifest", EXAMPLE / "manifest.yaml", "--vars", EXAMPLE / "vars.yaml", "-o", output)
        result = json.loads(output.read_text())
        self.assertEqual(result, example("expected.json"))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, {p: p.read_bytes() for p in EXAMPLE.iterdir()})

    def test_stdout_and_dry_run(self):
        args = ("--manifest", EXAMPLE / "manifest.yaml", "--vars", EXAMPLE / "vars.yaml")
        self.assertEqual(json.loads(self.cli(*args).stdout), example("expected.json"))
        output = self.path / "missing-parent" / "output.json"
        result = self.cli(*args, "-o", output, "--dry-run")
        self.assertEqual(json.loads(result.stdout), {"valid": True, "agents": 5, "links": 2, "control_links": 2})
        self.assertFalse(output.parent.exists())

    def test_failures_leave_no_output(self):
        for kind, message in (("collision", "duplicate agent guid"), ("conflict", "scenario metadata conflicts"),
                              ("missing", "missing variables: ENV, PREFIX"), ("source", "unknown source guid")):
            with self.subTest(kind=kind):
                manifest = example("manifest.yaml")
                if kind == "collision":
                    manifest["guids"]["right"] = {}
                elif kind == "conflict":
                    manifest["scenario"] = {}
                elif kind == "source":
                    manifest["guids"]["right"]["absent"] = "new_guid"
                path = self.path / "manifest.yaml"
                path.write_text(yaml_text(manifest))
                output = self.path / "output.json"
                args = ["--manifest", path, "-o", output]
                if kind != "missing":
                    args += ["--vars", EXAMPLE / "vars.yaml"]
                result = self.cli(*args, expected=2)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(output.exists())

    def test_overwrite_and_symlink_rejected(self):
        args = ("--manifest", EXAMPLE / "manifest.yaml", "--vars", EXAMPLE / "vars.yaml")
        output = self.path / "combined.json"
        output.write_text("keep")
        self.cli(*args, "-o", output, expected=2)
        self.assertEqual(output.read_text(), "keep")
        path = self.path / "manifest-link.yaml"
        path.symlink_to(EXAMPLE / "manifest.yaml")
        self.cli("--manifest", path, "--vars", EXAMPLE / "vars.yaml", expected=2)

    def test_combined_output_size_rejected_before_writing(self):
        inputs = []
        for side in ("left", "right"):
            doc = example(f"{side}.json")
            doc[f"{side}_padding"] = "x" * (MAX_FILE_BYTES // 2)
            path = self.path / f"{side}.json"
            path.write_text(json.dumps(doc))
            self.assertLess(path.stat().st_size, MAX_FILE_BYTES)
            inputs.append(str(path))
        output = self.path / "too-large.json"
        for extra in ([], ["--dry-run"]):
            result = subprocess.run(
                [sys.executable, "-m", "flowwrench.huginn", "combine", *inputs,
                 "--manifest", str(EXAMPLE / "manifest.yaml"), "--vars", str(EXAMPLE / "vars.yaml"),
                 "-o", str(output), *extra], cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("generated JSON exceeds 5 MiB", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(output.exists())

    def test_concrete_manifest_needs_no_vars_file(self):
        manifest = example("manifest.yaml")
        manifest["guids"]["right"] = {"feed": "copy_feed", "sink": "copy_sink"}
        manifest["scenario"] = {"guid": "combined_demo", "name": "Combined demo"}
        path = self.path / "manifest.yaml"
        path.write_text(yaml_text(manifest))
        self.assertEqual(json.loads(self.cli("--manifest", path).stdout), example("expected.json"))


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.left = example("left.json")
        self.right = example("right.json")
        self.manifest = example("manifest.yaml")
        self.variables = example("vars.yaml")

    def combine(self):
        from flowwrench.huginn.relocation import combine
        return combine(self.left, self.right, self.manifest, self.variables)

    def test_preserves_every_field_except_explicit_relocations_and_overrides(self):
        before = deepcopy((self.left, self.right, self.manifest, self.variables))
        result = self.combine()
        self.assertEqual(result, example("expected.json"))
        # Independent index-to-GUID oracle plus full original agent dictionaries.
        _, agents, links, controls = graph_oracle(result)
        self.assertEqual(links, [("copy_feed", "copy_sink"), ("feed", "sink")])
        self.assertEqual(controls, [("control", "copy_feed"), ("control", "copy_sink")])
        for side, relocation in ((self.left, {}), (self.right, {"feed": "copy_feed", "sink": "copy_sink"})):
            for agent in side["agents"]:
                expected = deepcopy(agent)
                expected["guid"] = relocation.get(agent["guid"], agent["guid"])
                self.assertEqual(agents[expected["guid"]], expected)
        self.assertEqual((self.left, self.right, self.manifest, self.variables), before)

    def test_left_mapping_is_simultaneous_not_transitive(self):
        self.manifest["guids"]["left"] = {"feed": "sink", "sink": "feed"}
        result = self.combine()
        _, agents, links, _ = graph_oracle(result)
        self.assertEqual(agents["sink"]["name"], "Left source")
        self.assertEqual(agents["feed"]["name"], "Left sink")
        self.assertIn(("sink", "feed"), links)

    def test_duplicate_final_ids_within_one_input_rejected(self):
        self.manifest["guids"]["right"]["feed"] = "control"
        with self.assertRaisesRegex(KitError, "duplicate agent guid"):
            self.combine()

    def test_invalid_original_identity_cannot_be_repaired_by_mapping(self):
        self.right["agents"][1]["guid"] = "control"
        with self.assertRaisesRegex(KitError, "duplicate agent guid"):
            self.combine()

    def test_unmapped_reference_like_strings_are_literal_data(self):
        self.right["agents"][1]["options"]["opaque"] = "${HSK:OPAQUE} feed {{ feed }}"
        result = self.combine()
        agent = next(a for a in result["agents"] if a["guid"] == "copy_feed")
        self.assertEqual(agent["options"]["opaque"], "${HSK:OPAQUE} feed {{ feed }}")

    def test_metadata_conflicts_are_type_sensitive_and_values_are_not_disclosed(self):
        for left, right in ((True, 1), ({"nested": [True]}, {"nested": [1]}), ("SECRET_LEFT", "SECRET_RIGHT")):
            self.left["custom"] = left
            self.right["custom"] = right
            with self.subTest(left=left), self.assertRaisesRegex(KitError, "scenario metadata conflicts: custom") as raised:
                self.combine()
            self.assertNotIn("SECRET_", str(raised.exception))
        self.manifest["scenario"]["custom"] = None
        self.assertIsNone(self.combine()["custom"])

    def test_timestamp_conflict_requires_explicit_override(self):
        self.right["exported_at"] = "different"
        with self.assertRaisesRegex(KitError, "scenario metadata conflicts: exported_at"):
            self.combine()
        self.manifest["scenario"]["exported_at"] = "chosen"
        self.assertEqual(self.combine()["exported_at"], "chosen")

    def test_invalid_manifest_shapes_versions_and_reserved_overrides(self):
        cases = [None, {}, {**self.manifest, "extra": 1},
                 {**self.manifest, "relocation_version": True},
                 {**self.manifest, "relocation_version": 2},
                 {**self.manifest, "guids": {"right": {}}},
                 {**self.manifest, "guids": {"left": {}, "right": []}},
                 {**self.manifest, "scenario": []}]
        cases += [{**self.manifest, "scenario": {key: []}}
                  for key in ("agents", "links", "control_links", "schema_version")]
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(KitError):
                self.manifest = manifest
                self.combine()

    def test_invalid_target_guids_and_unknown_sources(self):
        for target in (True, None, [], "bad/guid", "", "x" * 129):
            with self.subTest(target=target), self.assertRaises(KitError):
                self.manifest["guids"]["right"]["feed"] = target
                self.combine()
        self.manifest = example("manifest.yaml")
        self.manifest["guids"]["left"]["absent"] = "new_guid"
        with self.assertRaisesRegex(KitError, "unknown source guid"):
            self.combine()

    def test_parameters_are_single_pass_and_mapping_keys_are_concrete(self):
        self.variables["PREFIX"] = "${HSK:OTHER}"
        with self.assertRaisesRegex(KitError, "single-pass"):
            self.combine()
        self.variables = example("vars.yaml")
        self.manifest["guids"]["left"]["${HSK:KEY}"] = "new_guid"
        with self.assertRaisesRegex(KitError, "mapping keys"):
            self.combine()

    def test_combined_agent_and_node_budgets(self):
        for side in (self.left, self.right):
            template = side["agents"][0]
            side["agents"] = [{**template, "guid": f"{side['guid']}_{i}"} for i in range(501)]
            side["links"] = []
            side["control_links"] = []
        self.manifest["guids"] = {"left": {}, "right": {}}
        with self.assertRaisesRegex(KitError, "1000 agents"):
            self.combine()
        self.setUp()
        self.left["large"] = [0] * 50000
        self.right["large_right"] = [0] * 50000
        with self.assertRaisesRegex(KitError, "node limits"):
            self.combine()

    def test_combined_link_budget_for_both_kinds(self):
        for kind, fields in (("links", ("source", "receiver")),
                             ("control_links", ("controller", "control_target"))):
            self.setUp()
            for side in (self.left, self.right):
                template = side["agents"][0]
                side["agents"] = [{**template, "guid": f"{side['guid']}_{i}"} for i in range(72)]
                side["links"] = []
                side["control_links"] = []
                side[kind] = [dict(zip(fields, (a, b))) for a in range(72) for b in range(72)]
            self.manifest["guids"] = {"left": {}, "right": {}}
            with self.subTest(kind=kind), self.assertRaisesRegex(KitError, "10000 links"):
                self.combine()
