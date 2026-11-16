"""Opt-in unused binding checks use original HSK references, never supplied values."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, load_scenario, render, split_files
from flowwrench.huginn.formats import KitError, load_data, write_directory, yaml_text
from .test_core import FIXTURES, ROOT, raw


class UnusedVariableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.counter = 0

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "flowwrench.huginn", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def template(self, kind, options, **fields):
        self.counter += 1
        base = self.path / str(self.counter)
        base.mkdir()
        doc = raw()
        if kind == "json":
            doc["agents"][0].update(options=options, **fields)
            source = base / "template.json"
            source.write_text(json.dumps(doc))
        else:
            source = base / "kit"
            write_directory(source, split_files(from_export(doc)))
            agent_file = next(p for p in (source / "agents").glob("*.yaml")
                              if load_data(p)["guid"] == doc["agents"][0]["guid"])
            agent = load_data(agent_file)
            agent.update(options=options, **fields)
            agent_file.write_text(yaml_text(agent))
        return source

    def vars_file(self, bindings, kind="yaml"):
        path = self.path / f"vars.{kind}"
        path.write_text(json.dumps(bindings) if kind == "json" else yaml_text(bindings))
        return path

    def assert_refusal(self, result, reason):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "error: " + reason + "\n")
        self.assertNotIn("SENSITIVE_SENTINEL", result.stderr)

    def test_misspelled_extra_binding_fixture(self):
        variables = FIXTURES / "vars.unused.example.yaml"
        default = self.cli("render", FIXTURES / "template", "--vars", variables)
        self.assertEqual(default.returncode, 0, default.stderr)
        output = self.path / "never-created.json"
        result = self.cli("render", FIXTURES / "template", "--vars", variables,
                          "--reject-unused-vars", "-o", output)
        self.assert_refusal(result, "unused variables: LOCATON")
        self.assertFalse(output.exists())

    def test_nested_repeated_typed_references_in_json_and_kits(self):
        options = {"a/b": {"~key": ["${HSK:_one.dot-1}", "x=${HSK:_one.dot-1}",
                                         "${HSK:LIST}", "${HSK:NULL}", "${HSK:MAP}"]},
                   "literal": "{{ location }} {% credential weather_key %} ${OTHER:VALUE}"}
        bindings = {"_one.dot-1": 3, "LIST": [1, False], "NULL": None,
                    "MAP": {"x": 2}, "BOOL": True}
        for kind in ("json", "yaml"):
            for vars_kind in ("json", "yaml"):
                with self.subTest(kind=kind, vars_kind=vars_kind):
                    source = self.template(kind, options, disabled="${HSK:BOOL}")
                    variables = self.vars_file(bindings, vars_kind)
                    default = self.cli("render", source, "--vars", variables)
                    strict = self.cli("render", source, "--vars", variables, "--reject-unused-vars")
                    self.assertEqual(default.returncode, 0, default.stderr)
                    self.assertEqual(strict.returncode, 0, strict.stderr)
                    self.assertEqual(strict.stdout, default.stdout)
                    agent = next(a for a in json.loads(strict.stdout)["agents"]
                                 if a["guid"] == raw()["agents"][0]["guid"])
                    self.assertIs(agent["disabled"], True)
                    self.assertEqual(agent["options"]["a/b"]["~key"],
                                     [3, "x=3", [1, False], None, {"x": 2}])
                    self.assertEqual(agent["options"]["literal"], options["literal"])
                    inventory = self.cli("extract-vars", source)
                    self.assertEqual(json.loads(inventory.stdout),
                                     {"variables": sorted(bindings), "credentials": ["weather_key"]})

    def test_whole_field_mapping_and_empty_bindings(self):
        for kind in ("json", "yaml"):
            for options, bindings in (("${HSK:OPTIONS}", {"OPTIONS": {"nested": [True, 3]}}),
                                      ({"literal": "{{ location }}"}, {})):
                with self.subTest(kind=kind, options=options):
                    source = self.template(kind, options)
                    variables = self.vars_file(bindings)
                    output = self.path / f"{self.counter}.json"
                    dry_run = self.cli("render", source, "--vars", variables,
                                       "--reject-unused-vars", "-o", output, "--dry-run")
                    self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
                    self.assertIs(json.loads(dry_run.stdout)["valid"], True)
                    self.assertFalse(output.exists())
                    result = self.cli("render", source, "--vars", variables,
                                      "--reject-unused-vars", "-o", output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(output.is_file())
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    expected = bindings.get("OPTIONS", options)
                    agent = next(a for a in json.loads(output.read_text())["agents"]
                                 if a["guid"] == raw()["agents"][0]["guid"])
                    self.assertEqual(agent["options"], expected)

    def test_no_hsk_references_does_not_count_liquid_or_credentials(self):
        for kind in ("json", "yaml"):
            with self.subTest(kind=kind):
                source = self.template(kind, {"literal": "{{ location }} {% credential weather_key %} ${OTHER:VALUE}"})
                variables = self.vars_file({"weather_key": "SENSITIVE_SENTINEL",
                                            "location": {"nested": "${HSK:location}"}, "OTHER": None})
                self.assert_refusal(self.cli("render", source, "--vars", variables, "--reject-unused-vars"),
                                    "unused variables: OTHER, location, weather_key")

    def test_rejection_precedes_any_output_operation_and_dry_run(self):
        variables = FIXTURES / "vars.unused.example.yaml"
        for existing in (False, True):
            for dry_run in (False, True):
                with self.subTest(existing=existing, dry_run=dry_run):
                    output = self.path / f"{existing}-{dry_run}.json"
                    if existing:
                        output.write_bytes(b"PRESERVE_EXISTING_BYTES\n")
                    before = {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}
                    args = ["render", FIXTURES / "template", "--vars", variables,
                            "--reject-unused-vars", "-o", output]
                    if dry_run:
                        args += ["--dry-run"]
                    self.assert_refusal(self.cli(*args), "unused variables: LOCATON")
                    self.assertEqual(before, {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()})
                    # --force is deliberately unsupported, including on strict failures.
                    forced = self.cli(*args, "--force")
                    self.assertEqual(forced.returncode, 2)
                    self.assertEqual(forced.stdout, "")
                    self.assertIn("unrecognized arguments: --force", forced.stderr)
                    self.assertEqual(before, {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()})

    def test_missing_names_still_aggregate_before_unused(self):
        source = self.template("json", {"x": "${HSK:Z} ${HSK:A}"})
        variables = self.vars_file({"UNUSED": "SENSITIVE_SENTINEL ${HSK:bad space}"})
        self.assert_refusal(self.cli("render", source, "--vars", variables, "--reject-unused-vars"),
                            "missing variables: A, Z")

    def test_original_invalid_placeholders_and_keys_keep_diagnostics(self):
        for kind in ("json", "yaml"):
            for options in ({"x": "${HSK:bad space}"}, {"${HSK:KEY}": "SENSITIVE_SENTINEL"}):
                with self.subTest(kind=kind, options=options):
                    source = self.template(kind, options)
                    variables = self.vars_file({"UNUSED": "SENSITIVE_SENTINEL"})
                    args = ["render", source, "--vars", variables]
                    default = self.cli(*args)
                    strict = self.cli(*args, "--reject-unused-vars")
                    self.assertEqual(default.returncode, 2)
                    self.assertEqual(strict.returncode, 2)
                    self.assertEqual(strict.stderr, default.stderr)
                    self.assertEqual(strict.stdout, "")
                    self.assertIn(str(source.absolute()), strict.stderr)
                    self.assertNotIn("SENSITIVE_SENTINEL", strict.stderr)

    def test_only_original_references_count_and_single_pass_is_preserved(self):
        doc = from_export(raw())
        doc["agents"][0]["options"] = {"x": "${HSK:A}"}
        with self.assertRaisesRegex(KitError, "^unused variables: B$"):
            render(doc, {"A": "${HSK:B}", "B": "SENSITIVE_SENTINEL"}, reject_unused_vars=True)
        with self.assertRaisesRegex(KitError, "single-pass"):
            render(doc, {"A": "${HSK:A}"}, reject_unused_vars=True)
        for value in ("${HSK:bad space}", {"${HSK:KEY}": "SENSITIVE_SENTINEL"}):
            with self.subTest(value=value):
                with self.assertRaises(KitError) as default:
                    render(doc, {"A": value})
                with self.assertRaises(KitError) as strict:
                    render(doc, {"A": value}, reject_unused_vars=True)
                self.assertEqual(str(strict.exception), str(default.exception))
                self.assertNotIn("SENSITIVE_SENTINEL", str(strict.exception))
        doc["description"] = "${HSK:METADATA}"
        bindings = {"A": 3, "METADATA": "test"}
        before = deepcopy(doc), deepcopy(bindings)
        strict = render(doc, bindings, reject_unused_vars=True)
        self.assertEqual(strict, render(doc, bindings))
        self.assertEqual((doc, bindings), before)

    def test_invalid_binding_names_are_not_echoed_or_reported_as_valid_unused(self):
        source = self.template("json", {})
        variables = self.vars_file({"bad name SENSITIVE_SENTINEL": "SENSITIVE_SENTINEL", "VALID": 1})
        self.assertEqual(self.cli("render", source, "--vars", variables).returncode, 0)
        self.assert_refusal(self.cli("render", source, "--vars", variables, "--reject-unused-vars"),
                            "variable names must match [A-Za-z_][A-Za-z0-9_.-]*")
        variables.write_text('1: SENSITIVE_SENTINEL\n')
        result = self.cli("render", source, "--vars", variables, "--reject-unused-vars")
        self.assert_refusal(result, "mapping keys must be strings")


if __name__ == "__main__":
    unittest.main()
