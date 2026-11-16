"""CLI kit inputs are join-equivalent exports; relocations stay input-scoped.

Concrete fixtures are split from relocation/{left,right}.json and deliberately
renamed. Mixed inputs equal expected-v2.json; two kits equal that same explicit
export minus exported_at. JSON timestamps and v1 behavior remain unchanged.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, split_files
from flowwrench.huginn.formats import load_data, yaml_text
from .test_core import ROOT, graph_oracle
from .test_relocation import EXAMPLE, example


class CombineKitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kits = {}
        for side in ("left", "right"):
            kit = self.root / side
            (kit / "agents").mkdir(parents=True)
            for name, content in split_files(from_export(example(f"{side}.json"))).items():
                (kit / name).write_text(content)
            # File order differs from GUID order; file labels are not identities.
            files = sorted((kit / "agents").iterdir())
            for i, path in enumerate(files):
                path.rename(path.parent / f"{len(files)-i}-renamed.yaml")
            self.kits[side] = kit
        self.manifest = self.root / "manifest.yaml"
        self.manifest.write_text(yaml_text(example("manifest-v2.yaml")))
        self.output = self.root / "combined.json"

    def snapshot(self):
        return {str(p): p.read_bytes() for base in (self.root, EXAMPLE)
                for p in base.rglob("*") if p.is_file()}

    def cli(self, left, right, *, dry=False, expected=0, variables=None):
        args = [sys.executable, "-m", "flowwrench.huginn", "combine", str(left), str(right),
                "--manifest", str(self.manifest), "--vars", str(variables or EXAMPLE / "vars.yaml"),
                "-o", str(self.output)]
        if dry:
            args.append("--dry-run")
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def assert_rejected(self, left, right, message):
        before = self.snapshot()
        for dry in (False, True):
            result = self.cli(left, right, dry=dry, expected=2)
            self.assertEqual(result.stdout, "")
            self.assertIn(message, result.stderr)
            self.assertNotIn("SECRET_VALUE", result.stderr)
            self.assertFalse(self.output.exists())
            self.assertEqual(self.snapshot(), before)

    def agent_file(self, side, guid):
        return next(p for p in (self.kits[side] / "agents").iterdir()
                    if load_data(p)["guid"] == guid)

    def test_mixed_inputs_match_explicit_v2_output_and_are_private_immutable(self):
        for left, right, both_kits in (
                (self.kits["left"], EXAMPLE / "right.json", False),
                (EXAMPLE / "left.json", self.kits["right"], False),
                (self.kits["left"], self.kits["right"], True)):
            with self.subTest(left=left, right=right):
                before = self.snapshot()
                self.cli(left, right)
                expected = example("expected-v2.json")
                if both_kits:
                    expected.pop("exported_at")
                self.assertEqual(json.loads(self.output.read_text()), expected)
                self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
                output_bytes = self.output.read_bytes()
                self.cli(left, right, expected=2)
                self.assertEqual(self.output.read_bytes(), output_bytes)
                self.output.unlink()
                self.assertEqual(self.snapshot(), before)
                result = self.cli(left, right, dry=True)
                self.assertEqual(json.loads(result.stdout),
                                 {"valid": True, "agents": 5, "links": 3, "control_links": 3})
                self.assertFalse(self.output.exists())
                self.assertEqual(self.snapshot(), before)

    def test_swaps_and_overlapping_original_guids_resolve_once_for_all_input_pairs(self):
        manifest = example("manifest-v2.yaml")
        manifest["guids"]["left"] = {"feed": "sink", "sink": "feed"}
        manifest["guids"]["right"].update({"feed": "control", "control": "new_control"})
        self.manifest.write_text(yaml_text(manifest))
        # Explicit expected agents/indices, independent of the relocation engine.
        expected = example("expected-v2.json")
        by_guid = {a["guid"]: a for a in expected["agents"]}
        order = [("copy_feed", "control"), ("copy_sink", "copy_sink"),
                 ("sink", "feed"), ("control", "new_control"), ("feed", "sink")]
        expected["agents"] = [{**by_guid[old], "guid": new} for old, new in order]
        expected["links"] = [{"source": 0, "receiver": 1}, {"source": 2, "receiver": 0},
                             {"source": 4, "receiver": 2}]
        expected["control_links"] = [{"controller": 3, "control_target": i} for i in (0, 1, 4)]
        for use_left in (False, True):
            for use_right in (False, True):
                with self.subTest(left_kit=use_left, right_kit=use_right):
                    target = deepcopy(expected)
                    if use_left and use_right:
                        target.pop("exported_at")
                    self.cli(self.kits["left"] if use_left else EXAMPLE / "left.json",
                             self.kits["right"] if use_right else EXAMPLE / "right.json")
                    actual = json.loads(self.output.read_text())
                    self.assertEqual(actual, target)
                    _, _, events, controls = graph_oracle(actual)
                    self.assertIn(("feed", "control"), events)
                    self.assertIn(("new_control", "sink"), controls)
                    self.output.unlink()

    def test_v1_and_timestamp_normalization_match_equivalent_json_exports(self):
        self.manifest.write_text(yaml_text(example("manifest.yaml")))
        expected = example("expected.json")
        expected.pop("exported_at")
        self.cli(self.kits["left"], self.kits["right"])
        self.assertEqual(json.loads(self.output.read_text()), expected)
        self.output.unlink()
        # Equivalent exports have no timestamps; kit assembly must match exactly.
        paths = []
        for side in ("left", "right"):
            raw = example(f"{side}.json")
            raw.pop("exported_at")
            path = self.root / f"{side}.json"
            path.write_text(json.dumps(raw))
            paths.append(path)
        self.cli(*paths)
        self.assertEqual(json.loads(self.output.read_text()), expected)
        self.output.unlink()
        manifest = example("manifest-v2.yaml")
        manifest["scenario"]["exported_at"] = "explicit timestamp"
        self.manifest.write_text(yaml_text(manifest))
        self.cli(self.kits["left"], self.kits["right"])
        self.assertEqual(json.loads(self.output.read_text())["exported_at"], "explicit timestamp")

    def test_kit_placeholders_report_original_files_and_escaped_paths_without_rendering(self):
        for side in ("left", "right"):
            path = self.agent_file(side, "feed")
            original = path.read_text()
            for field, value, pointer in (
                    ("disabled", "${HSK:VALUE}", "/disabled"),
                    ("schedule", "${HSK:VALUE}", "/schedule"),
                    ("options", "${HSK:VALUE}", "/options"),
                    ("options", {"a/b": {"~key": ["${HSK:VALUE}"]}}, "/options/a~1b/~0key/0"),
                    ("options", {"url": "${HSK:bad syntax}"}, "/options/url"),
                    ("options", {"${HSK:KEY}": "SECRET_VALUE"}, "/options")):
                with self.subTest(side=side, field=field, value=value):
                    doc = load_data(path)
                    doc[field] = value
                    path.write_text(yaml_text(doc))
                    self.assert_rejected(self.kits["left"], self.kits["right"], f"{path}#{pointer}: ")
                    path.write_text(original)
            scenario = self.kits[side] / "scenario.yaml"
            doc = load_data(scenario)
            doc["scenario"]["extension"] = {"a/b": "${HSK:VALUE}"}
            scenario.write_text(yaml_text(doc))
            self.assert_rejected(self.kits["left"], self.kits["right"],
                                 f"{scenario}#/scenario/extension/a~1b: ")
            del doc["scenario"]["extension"]
            scenario.write_text(yaml_text(doc))

    def test_supplied_manifest_vars_never_resolve_kit_fields_but_json_stays_literal(self):
        path = self.agent_file("left", "feed")
        doc = load_data(path)
        doc["options"]["url"] = "${HSK:PREFIX}"
        path.write_text(yaml_text(doc))
        self.assert_rejected(self.kits["left"], EXAMPLE / "right.json", f"{path}#/options/url: ")
        raw = example("left.json")
        raw["agents"][0]["options"]["url"] = "${HSK:PREFIX}"
        source = self.root / "left.json"
        source.write_text(json.dumps(raw))
        self.cli(source, self.kits["right"])
        agents = {a["guid"]: a for a in json.loads(self.output.read_text())["agents"]}
        self.assertEqual(agents["feed"]["options"]["url"], "${HSK:PREFIX}")

    def test_dangling_kit_endpoints_and_invalid_agent_fields_retain_original_locations(self):
        for side, kind, field in (("left", "links", "receiver"),
                                  ("right", "control_links", "control_target")):
            source = self.kits[side] / "scenario.yaml"
            original = source.read_text()
            doc = load_data(source)
            doc[kind][0][field] = "SECRET_VALUE"
            source.write_text(yaml_text(doc))
            self.assert_rejected(self.kits["left"], self.kits["right"], f"{source}#/{kind}/0/{field}: ")
            source.write_text(original)
        source = self.agent_file("right", "feed")
        doc = load_data(source)
        doc["disabled"] = 1
        source.write_text(yaml_text(doc))
        self.assert_rejected(self.kits["left"], self.kits["right"], f"{source}#/disabled: must be a boolean")

    def test_duplicate_relocations_edges_and_metadata_conflicts_fail_before_output(self):
        for case in ("identities", "within-input", "event-added", "control-inherited", "metadata"):
            manifest = example("manifest-v2.yaml")
            if case == "identities":
                manifest["guids"]["right"] = {}
                # Isolate the identity collision from coincident added edges.
                manifest["links"] = []
                manifest["control_links"] = []
                message = "duplicate agent guid"
            elif case == "within-input":
                manifest["guids"]["right"]["feed"] = "control"
                message = "duplicate agent guid"
            elif case == "event-added":
                manifest["links"].append(deepcopy(manifest["links"][0]))
                message = f"{self.manifest}#/links/1: duplicate edge after relocation"
            elif case == "control-inherited":
                manifest["control_links"][0]["control_target"] = {"input": "right", "guid": "feed"}
                message = f"{self.manifest}#/control_links/0: duplicate edge after relocation"
            else:
                manifest["scenario"] = {}
                message = "scenario metadata conflicts: guid, name"
            self.manifest.write_text(yaml_text(manifest))
            with self.subTest(case=case):
                self.assert_rejected(self.kits["left"], self.kits["right"], message)

    def test_kit_symlinks_and_reserved_timestamp_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.kits["left"], target_is_directory=True)
        self.assert_rejected(alias, self.kits["right"], "symlink paths are not supported")
        source = self.kits["left"] / "scenario.yaml"
        doc = load_data(source)
        doc["scenario"]["exported_at"] = "not allowed in kit metadata"
        source.write_text(yaml_text(doc))
        self.assert_rejected(self.kits["left"], self.kits["right"], "scenario metadata contains reserved fields")
