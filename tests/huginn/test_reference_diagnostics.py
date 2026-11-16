"""Source pointers refer to the input arrays, before graph canonicalization."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, load_scenario, split_files, to_export, validate
from flowwrench.huginn.formats import ReferenceValidationError, load_data, write_directory, yaml_text
from .test_core import FIXTURES, ROOT, raw


ENDPOINTS = (("links", "source"), ("links", "receiver"),
             ("control_links", "controller"), ("control_links", "control_target"))


class ReferenceDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "flowwrench.huginn", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def assert_refusal(self, result, source, pointer, output):
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn(f"{source.absolute()}#{pointer}: ", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("SECRET_SENTINEL", result.stderr)
        self.assertFalse(output.exists())

    def test_split_identifies_each_bad_endpoint_in_original_json_array(self):
        for kind, field in ENDPOINTS:
            for value in (-1, 99, True, 0.0, "SECRET_SENTINEL", None, [], {}):
                with self.subTest(kind=kind, field=field, value=value):
                    doc = raw("control")
                    # A valid edge precedes the bad one. Its original position,
                    # not a canonical sorted position, must appear in the error.
                    doc[kind].append(deepcopy(doc[kind][0]))
                    index = len(doc[kind]) - 1
                    doc[kind][index][field] = value
                    source = self.path / "bad.json"
                    source.write_text(json.dumps(doc))
                    output = self.path / "never-created"
                    before = source.read_bytes()
                    result = self.cli("split", source, "-o", output)
                    self.assert_refusal(result, source, f"/{kind}/{index}/{field}", output)
                    self.assertEqual(source.read_bytes(), before)

    def test_join_and_render_identify_guid_endpoints_in_edited_split_kit(self):
        kit = self.path / "kit"
        write_directory(kit, split_files(from_export(raw("control"))))
        source = kit / "scenario.yaml"
        original = load_data(source)
        variables = self.path / "vars.yaml"
        variables.write_text("{}\n")
        for kind, field in ENDPOINTS:
            for value in ("SECRET_SENTINEL", True, [], {}):
                doc = deepcopy(original)
                # Reversing the original order must not change pointer meaning.
                doc[kind].reverse()
                index = len(doc[kind]) - 1
                doc[kind][index][field] = value
                source.write_text(yaml_text(doc))
                before = {p: p.read_bytes() for p in kit.rglob("*") if p.is_file()}
                for command in ("join", "render"):
                    for dry_run in (False, True):
                        with self.subTest(kind=kind, field=field, value=value,
                                          command=command, dry_run=dry_run):
                            output = self.path / "never-created.json"
                            args = [command, kit, "-o", output]
                            if command == "render":
                                args += ["--vars", variables]
                            if dry_run:
                                args += ["--dry-run"]
                            self.assert_refusal(self.cli(*args), source,
                                                f"/{kind}/{index}/{field}", output)
                            self.assertEqual(before, {p: p.read_bytes() for p in kit.rglob("*") if p.is_file()})

    def test_deleting_an_agent_locates_its_remaining_receiver_reference(self):
        kit = self.path / "kit"
        write_directory(kit, split_files(from_export(raw("control"))))
        source = kit / "scenario.yaml"
        receiver = load_data(source)["links"][0]["receiver"]
        for path in (kit / "agents").iterdir():
            if load_data(path)["guid"] == receiver:
                path.unlink()
        output = self.path / "never-created.json"
        self.assert_refusal(self.cli("join", kit, "-o", output), source, "/links/0/receiver", output)

    def test_render_from_json_reports_original_json_source(self):
        source = self.path / "bad.json"
        doc = raw("control")
        doc["control_links"][1]["control_target"] = 99
        source.write_text(json.dumps(doc))
        variables = self.path / "vars.yaml"
        variables.write_text("{}\n")
        output = self.path / "never-created.json"
        self.assert_refusal(self.cli("render", source, "--vars", variables, "-o", output),
                            source, "/control_links/1/control_target", output)

    def test_combine_identifies_which_source_export_is_invalid(self):
        manifest = FIXTURES / "relocation/manifest.yaml"
        variables = FIXTURES / "relocation/vars.yaml"
        for side in ("left", "right"):
            with self.subTest(side=side):
                sources = {name: FIXTURES / f"relocation/{name}.json" for name in ("left", "right")}
                doc = load_data(sources[side])
                doc["links"][0]["receiver"] = 99
                source = self.path / f"{side}.json"
                source.write_text(json.dumps(doc))
                sources[side] = source
                output = self.path / "never-created.json"
                result = self.cli("combine", sources["left"], sources["right"],
                                  "--manifest", manifest, "--vars", variables, "-o", output)
                self.assert_refusal(result, source, "/links/0/receiver", output)

    def test_shape_and_duplicate_diagnostics_locate_the_array_or_edge(self):
        for kind, _ in (ENDPOINTS[0], ENDPOINTS[2]):
            doc = raw("control")
            cases = ((None, f"/{kind}"), ([None], f"/{kind}/0"),
                     ([{**doc[kind][0], "unexpected": "SECRET_SENTINEL"}], f"/{kind}/0"),
                     ([doc[kind][0], doc[kind][0]], f"/{kind}/1"))
            for links, pointer in cases:
                with self.subTest(kind=kind, pointer=pointer, links=links):
                    doc[kind] = links
                    source = self.path / "bad.json"
                    source.write_text(json.dumps(doc))
                    output = self.path / "never-created"
                    self.assert_refusal(self.cli("split", source, "-o", output), source, pointer, output)

    def test_shared_readers_reject_bad_references_with_the_same_source_pointer(self):
        source = self.path / "bad.json"
        doc = raw("control")
        doc["links"][0]["receiver"] = 99
        source.write_text(json.dumps(doc))
        for args in (("lint", source), ("diff", FIXTURES / "control.json", source),
                     ("extract-vars", source)):
            with self.subTest(command=args[0]):
                self.assert_refusal(self.cli(*args), source, "/links/0/receiver", self.path / "absent")

    def test_library_errors_expose_structured_location_without_inventing_a_file(self):
        source = self.path / "input.json"
        doc = raw("control")
        doc["links"][0]["receiver"] = 99
        with self.assertRaises(ReferenceValidationError) as raised:
            from_export(doc, source=source)
        self.assertEqual(raised.exception.source, str(source))
        self.assertEqual(raised.exception.pointer, "/links/0/receiver")
        self.assertEqual(raised.exception.reason, "dangling or non-integer link index")
        doc = from_export(raw("control"))
        doc["control_links"][1]["controller"] = "absent"
        with self.assertRaises(ReferenceValidationError) as raised:
            validate(doc)
        self.assertIsNone(raised.exception.source)
        self.assertEqual(raised.exception.pointer, "/control_links/1/controller")
        self.assertEqual(str(raised.exception), "/control_links/1/controller: dangling guid")

    def test_valid_roundtrip_render_and_opaque_arrays_are_unchanged(self):
        doc = raw("control")
        # These are extension data, not upstream structural graph endpoints.
        doc["agents"][0]["options"].update(receivers=["outside"], propagation=[99])
        source = self.path / "valid.json"
        source.write_text(json.dumps(doc))
        kit = self.path / "kit"
        self.assertEqual(self.cli("split", source, "-o", kit).returncode, 0)
        expected = to_export(from_export(doc))
        self.assertEqual(to_export(load_scenario(kit)), expected)
        variables = self.path / "vars.yaml"
        variables.write_text("{}\n")
        for command in ("join", "render"):
            output = self.path / f"{command}.json"
            args = [command, kit, "-o", output]
            if command == "render":
                args += ["--vars", variables]
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), expected)
