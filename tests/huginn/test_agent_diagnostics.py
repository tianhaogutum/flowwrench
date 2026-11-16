"""Rendered field errors retain original files/indices through GUID sorting."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import extract, from_export, load_scenario, render, split_files, to_export
from flowwrench.huginn.formats import KitError, load_data, write_directory, yaml_text
from .test_core import ROOT, raw


class AgentDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.kit = self.path / "kit"
        write_directory(self.kit, split_files(from_export(raw())))
        # Keep filename order opposite to GUID order, with filenames unrelated to
        # generated slugs. The first filename belongs to the last canonical agent.
        files = sorted((self.kit / "agents").glob("*.yaml"))
        agents = sorted([load_data(p) for p in files], key=lambda a: a["guid"], reverse=True)
        for p in files:
            p.unlink()
        for index, agent in enumerate(agents):
            (self.kit / f"agents/{index}-renamed.yaml").write_text(yaml_text(agent))
        self.source = self.kit / "agents/0-renamed.yaml"
        self.agent = agents[0]
        self.variables = self.path / "vars.yaml"

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "flowwrench.huginn", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}

    def check_refusal(self, source, pointer, reason=None):
        before = self.snapshot()
        output = self.path / "never-created.json"
        for dry_run in (False, True):
            args = ["render", self.kit, "--vars", self.variables, "-o", output]
            if dry_run:
                args += ["--dry-run"]
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertIn(f"{source.absolute()}#{pointer}: ", result.stderr)
            if reason is not None:
                self.assertEqual(result.stderr, f"error: {source.absolute()}#{pointer}: {reason}\n")
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("SECRET_SENTINEL", result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(self.snapshot(), before)

    def placeholder_template(self, kind, value, variables, *, whole_options=False):
        agent = deepcopy(self.agent)
        agent["options"] = value if whole_options else {"a/b": {"~key": [value]}}
        if kind == "yaml":
            self.kit = self.path / "kit"
            source = self.kit / "agents/0-renamed.yaml"
            source.write_text(yaml_text(agent))
            prefix = ""
        else:
            doc = raw()
            doc["agents"].sort(key=lambda a: a["guid"], reverse=True)
            doc["agents"][0] = agent
            doc["links"] = []
            doc["control_links"] = []
            source = self.path / "template.json"
            source.write_text(json.dumps(doc))
            self.kit = source
            prefix = "/agents/0"
        self.variables.write_text(yaml_text(variables))
        return source, prefix + ("/options" if whole_options else "/options/a~1b/~0key/0")

    def test_original_malformed_agent_placeholders_are_located(self):
        for kind in ("yaml", "json"):
            for value in ("${HSK:BAD", "${HSK:bad space}", "${HSK:}",
                          "${HSK:URL} and ${HSK:bad space}"):
                with self.subTest(kind=kind, value=value):
                    source, pointer = self.placeholder_template(kind, value, {"URL": "SECRET_SENTINEL"})
                    self.check_refusal(source, pointer, "malformed placeholder; use ${HSK:NAME}")

    def check_inventory_refusal(self, source, pointer, reason):
        before = self.snapshot()
        result = self.cli("extract-vars", self.kit)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertEqual(result.stderr, f"error: {source.absolute()}#{pointer}: {reason}\n")
        self.assertEqual(result.stdout, "")
        self.assertNotIn("SECRET_SENTINEL", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_inventory_malformed_values_locate_original_string(self):
        for kind in ("yaml", "json"):
            for token in ("${HSK:BAD", "${HSK:bad space}", "${HSK:}",
                          "${HSK:Z} and ${HSK:bad space}"):
                with self.subTest(kind=kind, token=token):
                    source, pointer = self.placeholder_template(kind, {
                        "a_inventory": "${HSK:Z} ${HSK:A} {% credential first %}",
                        "leaf/value~": ["${HSK:A}", "SECRET_SENTINEL " + token],
                    }, {})
                    self.check_inventory_refusal(source, pointer + "/leaf~1value~0/1",
                                                 "malformed placeholder; use ${HSK:NAME}")

    def test_inventory_reserved_keys_locate_containing_mapping(self):
        for kind in ("yaml", "json"):
            for key in ("SECRET_SENTINEL/${HSK:KEY}~", "SECRET_SENTINEL ${HSK:bad space}",
                        "SECRET_SENTINEL ${HSK:"):
                with self.subTest(kind=kind, key=key):
                    source, pointer = self.placeholder_template(kind, [
                        "${HSK:A} {% credential first %}", {key: "SECRET_SENTINEL"},
                    ], {})
                    self.check_inventory_refusal(source, pointer + "/1",
                                                 "placeholders in mapping keys are not supported")

    def test_inventory_library_origins_root_keys_and_immutability(self):
        from flowwrench.huginn.formats import AgentValidationError
        for kind in ("yaml", "json"):
            for is_key in (False, True):
                with self.subTest(kind=kind, is_key=is_key):
                    source, pointer = self.placeholder_template(kind, "SECRET_SENTINEL ${HSK:}", {})
                    if is_key:
                        if kind == "yaml":
                            agent = load_data(source)
                            agent["options"] = {}
                            agent["SECRET_SENTINEL ${HSK:KEY}"] = "SECRET_SENTINEL"
                            source.write_text(yaml_text(agent))
                            pointer = ""
                        else:
                            doc = load_data(source)
                            doc["agents"][0]["options"] = {}
                            doc["agents"][0]["SECRET_SENTINEL ${HSK:KEY}"] = "SECRET_SENTINEL"
                            source.write_text(json.dumps(doc))
                            pointer = "/agents/0"
                    reason = ("placeholders in mapping keys are not supported" if is_key else
                              "malformed placeholder; use ${HSK:NAME}")
                    self.check_inventory_refusal(source, pointer, reason)
                    origins = {}
                    doc = load_scenario(self.kit, allow_templates=True, check_schedules=False,
                                        agent_origins=origins)
                    before = deepcopy((doc, origins))
                    tail = "" if is_key else "/options/a~1b/~0key/0"
                    for sidecar, expected_source, expected_pointer in (
                        (origins, str(source.absolute()), pointer),
                        (None, None, "/agents/2" + tail),
                    ):
                        with self.subTest(sidecar=sidecar), self.assertRaises(AgentValidationError) as raised:
                            extract(doc, agent_origins=sidecar)
                        self.assertEqual(raised.exception.source, expected_source)
                        self.assertEqual(raised.exception.pointer, expected_pointer)
                        self.assertEqual(raised.exception.reason, reason)
                        self.assertNotIn("SECRET_SENTINEL", str(raised.exception))
                        self.assertEqual((doc, origins), before)

    def test_inventory_success_stays_sorted_deduplicated_and_literal(self):
        result = self.cli("extract-vars", ROOT / "fixtures/template")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "credentials": ["weather_key"],
            "variables": ["FEED_URL", "LOCATION", "RETRIES", "SCHEDULE"],
        })
        for kind in ("yaml", "json"):
            with self.subTest(kind=kind):
                self.placeholder_template(kind, {
                    "first": "${HSK:Z} ${HSK:A} {% credential z_key %} {% credential 'alpha' %}",
                    "second": '${HSK:A} {%- credential "alpha" -%} {{ value }} ${OTHER:VALUE}',
                }, {})
                before = self.snapshot()
                result = self.cli("extract-vars", self.kit)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout), {
                    "credentials": ["alpha", "weather_key", "z_key"], "variables": ["A", "Z"],
                })
                self.assertEqual(self.snapshot(), before)

    def test_introduced_scalar_placeholders_are_located_without_expansion(self):
        for kind in ("yaml", "json"):
            for template in ("${HSK:URL}", "prefix ${HSK:URL}/suffix"):
                for token, reason in (("${HSK:NEXT}", "variable values may not introduce placeholders (rendering is single-pass)"),
                                      ("${HSK:bad space}", "malformed placeholder; use ${HSK:NAME}")):
                    with self.subTest(kind=kind, template=template, token=token):
                        source, pointer = self.placeholder_template(
                            kind, template, {"URL": "SECRET_SENTINEL " + token, "NEXT": "resolved"})
                        self.check_refusal(source, pointer, reason)

    def test_introduced_nested_values_point_to_whole_field_template_site(self):
        for kind in ("yaml", "json"):
            for value, reason in (
                ({"SECRET_SENTINEL/inserted": {"~nested": ["${HSK:NEXT}"]}},
                 "variable values may not introduce placeholders (rendering is single-pass)"),
                ({"SECRET_SENTINEL/inserted": [False, {"x": "${HSK:bad space}"}]},
                 "malformed placeholder; use ${HSK:NAME}"),
                ({"SECRET_SENTINEL ${HSK:KEY}": 1}, "placeholders in mapping keys are not supported"),
                ([{"SECRET_SENTINEL": ["${HSK:NEXT}"]}],
                 "variable values may not introduce placeholders (rendering is single-pass)"),
            ):
                with self.subTest(kind=kind, value=value):
                    source, pointer = self.placeholder_template(
                        kind, "${HSK:URL}", {"URL": value, "NEXT": "resolved"}, whole_options=True)
                    self.check_refusal(source, pointer, reason)

    def test_placeholder_assembled_across_embedded_variables_points_to_template(self):
        for kind in ("yaml", "json"):
            for tail, reason in (("K:NEXT}", "variable values may not introduce placeholders (rendering is single-pass)"),
                                 ("K:bad space}", "malformed placeholder; use ${HSK:NAME}")):
                with self.subTest(kind=kind, tail=tail):
                    source, pointer = self.placeholder_template(
                        kind, "${HSK:A}${HSK:B}", {"A": "SECRET_SENTINEL ${HS", "B": tail, "NEXT": "resolved"})
                    self.check_refusal(source, pointer, reason)

    def test_placeholder_library_error_retains_origin_and_does_not_mutate_inputs(self):
        from flowwrench.huginn.formats import AgentValidationError
        self.placeholder_template("yaml", "${HSK:URL}", {})
        origins = {}
        doc = load_scenario(self.kit, check_schedules=False, allow_templates=True, agent_origins=origins)
        variables = {"URL": {"SECRET_SENTINEL": ["${HSK:NEXT}"]}, "NEXT": "resolved"}
        before = deepcopy((doc, variables, origins))
        for sidecar, source, pointer in ((origins, str(self.source), "/options/a~1b/~0key/0"),
                                         (None, None, "/agents/2/options/a~1b/~0key/0")):
            with self.subTest(sidecar=sidecar), self.assertRaises(AgentValidationError) as raised:
                render(deepcopy(doc), variables, agent_origins=sidecar)
            self.assertEqual(raised.exception.source, source)
            self.assertEqual(raised.exception.pointer, pointer)
            self.assertEqual(raised.exception.reason, "variable values may not introduce placeholders (rendering is single-pass)")
            self.assertEqual((doc, variables, origins), before)

    def test_original_reserved_keys_locate_containing_mapping_without_disclosing_key(self):
        for kind in ("yaml", "json"):
            for whole_options in (False, True):
                for key in ("SECRET_SENTINEL/${HSK:KEY}~", "SECRET_SENTINEL ${HSK:bad space}",
                            "SECRET_SENTINEL ${HSK:"):
                    with self.subTest(kind=kind, whole_options=whole_options, key=key):
                        source, pointer = self.placeholder_template(
                            kind, {key: "SECRET_SENTINEL"}, {}, whole_options=whole_options)
                        self.check_refusal(source, pointer, "placeholders in mapping keys are not supported")

    def test_original_reserved_agent_root_key_retains_structured_origin(self):
        from flowwrench.huginn.formats import AgentValidationError
        agent = deepcopy(self.agent)
        agent["SECRET_SENTINEL ${HSK:KEY}"] = "SECRET_SENTINEL"
        self.source.write_text(yaml_text(agent))
        origins = {}
        doc = load_scenario(self.kit, allow_templates=True, agent_origins=origins)
        before = deepcopy((doc, origins))
        for sidecar, source, pointer in ((origins, str(self.source), ""),
                                        (None, None, "/agents/2")):
            with self.subTest(sidecar=sidecar), self.assertRaises(AgentValidationError) as raised:
                render(doc, {}, agent_origins=sidecar)
            self.assertEqual(raised.exception.source, source)
            self.assertEqual(raised.exception.pointer, pointer)
            self.assertEqual(raised.exception.reason, "placeholders in mapping keys are not supported")
            self.assertNotIn("SECRET_SENTINEL", str(raised.exception))
            self.assertEqual((doc, origins), before)

    def test_non_agent_reserved_keys_and_low_level_scanner_keep_generic_errors(self):
        from flowwrench.huginn.core import required_variables
        reason = "placeholders in mapping keys are not supported"
        doc = from_export(raw())
        doc["extension"] = {"SECRET_SENTINEL ${HSK:KEY}": "SECRET_SENTINEL"}
        for operation in (render, extract):
            with self.subTest(operation=operation), self.assertRaises(KitError) as raised:
                operation(doc, {}) if operation is render else operation(doc)
            self.assertEqual(str(raised.exception), reason)
        doc.pop("extension")
        doc["agents"][0]["options"] = {"SECRET_SENTINEL ${HSK:KEY}": "SECRET_SENTINEL"}
        with self.assertRaises(KitError) as raised:
            required_variables(doc)
        self.assertEqual(str(raised.exception), reason)

    def test_reserved_keys_missing_aggregation_and_unused_values_keep_contracts(self):
        doc = from_export(raw())
        doc["agents"][0]["options"] = {"x": "${HSK:Z} ${HSK:A}"}
        with self.assertRaises(KitError) as raised:
            render(doc, {"UNUSED": "${HSK:bad space}"})
        self.assertEqual(str(raised.exception), "missing variables: A, Z")
        doc["agents"][0]["options"] = {"${HSK:KEY}": "SECRET_SENTINEL"}
        with self.assertRaises(KitError) as raised:
            render(doc, {"KEY": "x"})
        self.assertEqual(str(raised.exception), "/agents/0/options: placeholders in mapping keys are not supported")
        literal = "{{ value }} {% credential key %} ${OTHER:VALUE}"
        doc["agents"][0]["options"] = {"x": "${HSK:URL}"}
        result = render(doc, {"URL": literal, "UNUSED": "${HSK:bad space}"})
        self.assertEqual(result["agents"][0]["options"], {"x": literal})

    def test_rendered_disabled_uses_original_yaml_filename_after_sorting(self):
        agent = deepcopy(self.agent)
        agent["disabled"] = "${HSK:VALUE}"
        self.source.write_text(yaml_text(agent))
        self.variables.write_text("VALUE: 1\n")
        self.check_refusal(self.source, "/disabled")

    def test_all_rendered_field_types_and_unknown_schedule_are_located(self):
        cases = {"name": [None, "", "  ", []], "type": [1, ""],
                 "options": [False, [], None], "disabled": [1, None, "SECRET_SENTINEL"],
                 "propagate_immediately": [0, [], "SECRET_SENTINEL"],
                 "keep_events_for": [True, -1, 0.5, "SECRET_SENTINEL"],
                 "schedule": [True, {}, "SECRET_SENTINEL"]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    agent = deepcopy(self.agent)
                    agent[field] = "${HSK:VALUE}"
                    self.source.write_text(yaml_text(agent))
                    self.variables.write_text(yaml_text({"VALUE": value}))
                    self.check_refusal(self.source, f"/{field}")

    def test_nested_embedded_collection_pointer_escapes_keys_and_array_indices(self):
        agent = deepcopy(self.agent)
        agent["options"] = {"a/b": {"~key": ["prefix ${HSK:VALUE}"]}}
        self.source.write_text(yaml_text(agent))
        for value in (["SECRET_SENTINEL"], {"secret": "SECRET_SENTINEL"}):
            with self.subTest(value=value):
                self.variables.write_text(yaml_text({"VALUE": value}))
                self.check_refusal(self.source, "/options/a~1b/~0key/0")

    def test_json_render_keeps_original_array_index(self):
        doc = raw()
        doc["agents"].sort(key=lambda a: a["guid"], reverse=True)
        doc["links"] = []
        doc["control_links"] = []
        doc["agents"][0]["disabled"] = "${HSK:VALUE}"
        source = self.path / "template.json"
        source.write_text(json.dumps(doc))
        self.kit = source
        self.variables.write_text("VALUE: 1\n")
        self.check_refusal(source, "/agents/0/disabled")

    def test_duplicate_guid_cannot_overwrite_an_earlier_invalid_field_origin(self):
        from flowwrench.huginn.formats import AgentValidationError
        agent = deepcopy(self.agent)
        agent["disabled"] = 1
        self.source.write_text(yaml_text(agent))
        other_source = self.kit / "agents/1-renamed.yaml"
        duplicate = load_data(other_source)
        duplicate["guid"] = agent["guid"]
        other_source.write_text(yaml_text(duplicate))
        with self.assertRaises(AgentValidationError) as raised:
            load_scenario(self.kit)
        self.assertEqual(raised.exception.source, str(self.source))
        self.assertEqual(raised.exception.pointer, "/disabled")
        doc = raw()
        doc["agents"][0]["disabled"] = 1
        doc["agents"][1]["guid"] = doc["agents"][0]["guid"]
        with self.assertRaises(AgentValidationError) as raised:
            from_export(doc, source=self.path / "duplicates.json")
        self.assertEqual(raised.exception.pointer, "/agents/0/disabled")

    def test_library_origins_survive_canonicalization_and_copy_without_data_keys(self):
        from flowwrench.huginn.formats import AgentValidationError
        agent = deepcopy(self.agent)
        agent["options"] = "${HSK:VALUE}"
        self.source.write_text(yaml_text(agent))
        origins = {}
        doc = load_scenario(self.kit, check_schedules=False, allow_templates=True,
                            agent_origins=origins)
        self.assertEqual(doc["agents"][-1]["guid"], self.agent["guid"])
        before = deepcopy(doc)
        with self.assertRaises(AgentValidationError) as raised:
            render(deepcopy(doc), {"VALUE": False}, agent_origins=origins)
        error = raised.exception
        self.assertIsInstance(error, KitError)
        self.assertEqual(error.source, str(self.source))
        self.assertEqual(error.pointer, "/options")
        self.assertEqual(error.reason, "must be a mapping")
        self.assertEqual(doc, before)
        self.assertFalse(set(origins) & set(doc))
        with self.assertRaises(AgentValidationError) as raised:
            render(doc, {"VALUE": False})
        self.assertIsNone(raised.exception.source)
        self.assertEqual(raised.exception.pointer, "/agents/2/options")

    def test_valid_typed_output_is_unchanged_and_origins_do_not_leak(self):
        agent = deepcopy(self.agent)
        expected = {"disabled": True, "propagate_immediately": False,
                    "keep_events_for": 0, "options": {"a/b": {"~key": [1, None, "{{ value }}"]}},
                    "name": "Renamed", "type": "Agents::EventFormattingAgent", "schedule": None}
        for field in expected:
            agent[field] = "${HSK:" + field + "}"
        self.source.write_text(yaml_text(agent))
        self.variables.write_text(yaml_text(expected))
        origins = {}
        doc = load_scenario(self.kit, check_schedules=False, allow_templates=True,
                            agent_origins=origins)
        result = to_export(render(doc, expected, agent_origins=origins))
        concrete = deepcopy(self.agent)
        concrete.update(expected)
        oracle = from_export(raw())
        oracle["agents"][-1] = concrete
        self.assertEqual(result, to_export(oracle))
        output = self.path / "valid.json"
        response = self.cli("render", self.kit, "--vars", self.variables, "-o", output)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
