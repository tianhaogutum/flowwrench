"""CLI integration and hostile-input regression tests, entirely offline."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.formats import (
    KitError, MAX_FILE_BYTES, load_data, write_directory, write_file, yaml_text,
)
from flowwrench.huginn.core import from_export, load_scenario, split_files
from .test_core import FIXTURES, ROOT, graph_oracle, raw


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def data(self, text, *, json_only=False):
        p = self.path / "input"
        p.write_text(text)
        return load_data(p, json_only=json_only)

    def test_duplicate_yaml_and_json_keys(self):
        for text, json_only in (("x: 1\nx: 2\n", False), ('{"x": 1, "x": 2}', True)):
            with self.subTest(json_only=json_only), self.assertRaisesRegex(KitError, "duplicate"):
                self.data(text, json_only=json_only)

    def test_non_string_yaml_keys_and_merge_keys(self):
        for text in ("1: hello", "[a, b]: hello", "x: {<<: {x: 1}}"):
            with self.subTest(text=text), self.assertRaises(KitError):
                self.data(text)

    def test_aliases_including_recursive_alias_rejected(self):
        for text in ("a: &a [*a]", "a: &a value\nb: *a"):
            with self.subTest(text=text), self.assertRaisesRegex(KitError, "aliases"):
                self.data(text)

    def test_python_yaml_tags_cannot_execute(self):
        sentinel = self.path / "executed"
        with self.assertRaisesRegex(KitError, "invalid YAML"):
            self.data(f'!!python/object/apply:os.system ["touch {sentinel}"]')
        self.assertFalse(sentinel.exists())

    def test_yaml_json_scalar_typing(self):
        result = self.data("a: yes\nb: on\nc: 2026-01-01\nd: 012\ne: true\nf: 1e2\ng: null\n")
        self.assertEqual(result, dict(a="yes", b="on", c="2026-01-01", d="012", e=True, f=100.0, g=None))

    def test_non_finite_numbers_rejected(self):
        for text, json_only in (('{"x": NaN}', True), ('{"x": 1e999}', True), ("x: !!float .nan", False)):
            with self.subTest(text=text), self.assertRaises(KitError):
                self.data(text, json_only=json_only)

    def test_depth_limit_yaml_and_json(self):
        text = "[" * 70 + "0" + "]" * 70
        for json_only in (True, False):
            with self.subTest(json_only=json_only), self.assertRaisesRegex(KitError, "depth"):
                self.data(text, json_only=json_only)

    def test_node_limit(self):
        with self.assertRaisesRegex(KitError, "node"):
            self.data("[" + ",".join(["0"] * 100001) + "]", json_only=True)

    def test_file_size_limit(self):
        p = self.path / "large.json"
        with p.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(KitError, "5 MiB"):
            load_data(p, json_only=True)

    def test_kit_total_size_limit_before_loading_agents(self):
        kit = self.path / "kit"
        write_directory(kit, split_files(from_export(raw())))
        # Sparse files: bounded disk use; loader rejects total before parsing.
        for i in range(5):
            with (kit / "agents" / f"large{i}.yaml").open("wb") as stream:
                stream.truncate(MAX_FILE_BYTES)
        with self.assertRaisesRegex(KitError, "20 MiB"):
            load_scenario(kit)

    def test_invalid_utf8_and_missing_file(self):
        p = self.path / "input"
        p.write_bytes(b"\xff")
        with self.assertRaisesRegex(KitError, "UTF-8"):
            load_data(p)
        with self.assertRaisesRegex(KitError, "regular file"):
            load_data(self.path / "missing")

    def test_input_symlink_and_symlink_ancestor_rejected(self):
        (self.path / "target").write_text("x: 1")
        (self.path / "link").symlink_to(self.path / "target")
        (self.path / "parent-link").symlink_to(self.path, target_is_directory=True)
        for p in (self.path / "link", self.path / "parent-link/target"):
            with self.subTest(path=p), self.assertRaisesRegex(KitError, "symlink"):
                load_data(p)

    def test_agent_and_manifest_symlinks_rejected(self):
        for relative in ("scenario.yaml", "agents/fake.yaml"):
            with self.subTest(relative=relative):
                kit = self.path / ("kit-" + relative.replace("/", "-"))
                write_directory(kit, split_files(from_export(raw())))
                target = self.path / "external.yaml"
                target.write_text("external: untouched")
                link = kit / relative
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                with self.assertRaisesRegex(KitError, "symlink"):
                    load_scenario(kit)
                self.assertEqual(target.read_text(), "external: untouched")

    def test_output_never_overwrites_and_files_private(self):
        p = self.path / "output"
        write_file(p, "first")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(KitError, "overwrite"):
            write_file(p, "second")
        self.assertEqual(p.read_text(), "first")
        kit = self.path / "kit"
        write_directory(kit, split_files(from_export(raw())))
        with self.assertRaisesRegex(KitError, "overwrite"):
            write_directory(kit, {})

    def test_output_symlink_cannot_write_outside_target(self):
        target = self.path / "outside"
        target.mkdir()
        (target / "safe").write_text("keep")
        (self.path / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(KitError, "symlink"):
            write_file(self.path / "link/new", "data")
        with self.assertRaisesRegex(KitError, "symlink"):
            write_directory(self.path / "link/kit", {})
        self.assertEqual(list(target.iterdir()), [target / "safe"])

    def test_unsafe_generated_filename_and_cleanup(self):
        with self.assertRaisesRegex(KitError, "unsafe"):
            write_directory(self.path / "kit", {"../escaped": "value"})
        self.assertEqual(list(self.path.iterdir()), [])

    def test_missing_parent_does_not_create_directories(self):
        with self.assertRaisesRegex(KitError, "parent directory"):
            write_file(self.path / "missing/out.json", "data")
        self.assertFalse((self.path / "missing").exists())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "flowwrench.huginn", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return result

    def test_complete_split_join_diff_weather_workflow(self):
        kit = self.path / "weather"
        result = self.cli("split", FIXTURES / "weather.json", "-o", kit)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(list((kit / "agents").glob("*.yaml"))), 3)
        joined = self.path / "weather.json"
        self.cli("join", kit, "-o", joined)
        self.assertEqual(graph_oracle(raw()), graph_oracle(json.loads(joined.read_text())))
        result = self.cli("diff", FIXTURES / "weather.json", joined, "--exit-code")
        self.assertFalse(json.loads(result.stdout)["different"])
        self.cli("lint", kit)

    def test_complete_control_link_workflow(self):
        kit = self.path / "control"
        self.cli("split", FIXTURES / "control.json", "-o", kit)
        joined = self.path / "control.json"
        self.cli("join", kit, "-o", joined)
        self.assertEqual(graph_oracle(raw("control")), graph_oracle(json.loads(joined.read_text())))

    def test_extract_and_render_end_to_end(self):
        inventory = json.loads(self.cli("extract-vars", FIXTURES / "template").stdout)
        self.assertEqual(inventory["credentials"], ["weather_key"])
        result = self.cli("render", FIXTURES / "template", "--vars", FIXTURES / "vars.example.yaml")
        doc = json.loads(result.stdout)
        self.assertEqual(doc["agents"][0]["options"]["headers"]["X-Location"], "Berlin")
        self.assertEqual(doc["agents"][0]["schedule"], "every_2h")
        out = self.path / "rendered.json"
        self.cli("render", FIXTURES / "template", "--vars", FIXTURES / "vars.example.yaml", "-o", out)
        self.assertEqual(json.loads(out.read_text()), doc)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_missing_all_variables_exit_2_and_no_output(self):
        vars_path = self.path / "empty.yaml"
        vars_path.write_text("{}")
        output = self.path / "output.json"
        result = self.cli("render", FIXTURES / "template", "--vars", vars_path, "-o", output, expected=2)
        self.assertIn("FEED_URL, LOCATION, RETRIES, SCHEDULE", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(output.exists())

    def test_invalid_yaml_exit_2_without_secret_snippet(self):
        vars_path = self.path / "broken.yaml"
        vars_path.write_text("PASSWORD: [SECRET_SENTINEL")
        result = self.cli("render", FIXTURES / "template", "--vars", vars_path, expected=2)
        self.assertIn("invalid YAML", result.stderr)
        self.assertNotIn("SECRET_SENTINEL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unsupported_schema_duplicate_guid_dangling_index_exit_2(self):
        for change, message in (("schema", "unsupported schema_version"),
                                ("duplicate", "duplicate agent guid"), ("index", "link index")):
            with self.subTest(change=change):
                doc = raw()
                if change == "schema":
                    doc["schema_version"] = 42
                elif change == "duplicate":
                    doc["agents"][1]["guid"] = doc["agents"][0]["guid"]
                else:
                    doc["links"][0]["source"] = -1
                source = self.path / "bad.json"
                source.write_text(json.dumps(doc))
                output = self.path / "output"
                result = self.cli("split", source, "-o", output, expected=2)
                self.assertIn(message, result.stderr)
                self.assertFalse(output.exists())

    def test_join_duplicate_guid_dangling_guid_schedule_exit_2(self):
        for change, message in (("duplicate", "duplicate agent guid"),
                                ("dangling", "dangling guid"), ("schedule", "unknown schedule")):
            with self.subTest(change=change):
                kit = self.path / change
                self.cli("split", FIXTURES / "weather.json", "-o", kit)
                if change == "dangling":
                    p = kit / "scenario.yaml"
                    d = load_data(p)
                    d["links"][0]["source"] = "absent"
                else:
                    files = sorted((kit / "agents").glob("*.yaml"))
                    p = files[0]
                    d = load_data(p)
                    if change == "duplicate":
                        d["guid"] = load_data(files[1])["guid"]
                    else:
                        d["schedule"] = "every_3m"
                p.write_text(yaml_text(d))
                output = self.path / f"{change}.json"
                result = self.cli("join", kit, "-o", output, expected=2)
                self.assertIn(message, result.stderr)
                self.assertFalse(output.exists())

    def test_diff_exit_code_optional_and_option_path(self):
        doc = raw()
        doc["agents"][0]["options"]["subject"] = "CHANGED_SECRET"
        p = self.path / "changed.json"
        p.write_text(json.dumps(doc))
        result = self.cli("diff", FIXTURES / "weather.json", p, "--exit-code", expected=1)
        self.assertIn("/options/subject", result.stdout)
        self.assertNotIn("CHANGED_SECRET", result.stdout)
        self.cli("diff", FIXTURES / "weather.json", p)
        self.cli("diff", FIXTURES / "weather.json", self.path / "missing", expected=2)

    def test_lint_exit_1_for_warning_and_unknown_schedule(self):
        result = self.cli("lint", FIXTURES / "unicode.json", expected=1)
        self.assertEqual(json.loads(result.stdout)["issues"][0]["code"], "orphan-agent")
        doc = raw()
        doc["agents"][0]["schedule"] = "every_3m"
        p = self.path / "bad-schedule.json"
        p.write_text(json.dumps(doc))
        result = self.cli("lint", p, expected=1)
        self.assertEqual(json.loads(result.stdout)["issues"][0]["code"], "unknown-schedule")

    def test_dry_run_split_join_render_have_no_side_effects(self):
        output = self.path / "new-output"
        self.cli("split", FIXTURES / "weather.json", "-o", output, "--dry-run")
        self.assertFalse(output.exists())
        self.cli("join", FIXTURES / "template", "-o", output, "--dry-run", expected=2)
        self.assertFalse(output.exists())
        self.cli("split", FIXTURES / "weather.json", "-o", self.path / "kit")
        self.cli("join", self.path / "kit", "-o", output, "--dry-run")
        self.assertFalse(output.exists())
        before = {str(p.relative_to(FIXTURES)): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()}
        result = self.cli("render", FIXTURES / "template", "--vars", FIXTURES / "vars.example.yaml", "-o", output, "--dry-run")
        self.assertEqual(json.loads(result.stdout), {"valid": True, "agents": 3, "links": 2, "control_links": 0})
        self.assertNotIn("Berlin", result.stdout)
        self.assertFalse(output.exists())
        after = {str(p.relative_to(FIXTURES)): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_join_requires_render_when_option_placeholder_remains(self):
        kit = self.path / "kit"
        self.cli("split", FIXTURES / "weather.json", "-o", kit)
        p = next((kit / "agents").glob("weather*"))
        doc = load_data(p)
        doc["options"]["url"] = "${HSK:URL}"
        p.write_text(yaml_text(doc))
        result = self.cli("join", kit, "-o", self.path / "joined.json", expected=2)
        self.assertIn("unresolved variables", result.stderr)

    def test_output_overwrite_and_symlink_exit_2(self):
        kit = self.path / "kit"
        self.cli("split", FIXTURES / "weather.json", "-o", kit)
        self.cli("split", FIXTURES / "weather.json", "-o", kit, expected=2)
        p = self.path / "link"
        p.symlink_to(kit, target_is_directory=True)
        self.cli("join", p, "-o", self.path / "out.json", expected=2)

    def test_typed_fields_load_for_render_only(self):
        kit = self.path / "typed"
        shutil.copytree(FIXTURES / "template", kit)
        p = next((kit / "agents").glob("weather*"))
        doc = load_data(p)
        doc["disabled"] = "${HSK:DISABLED}"
        p.write_text(yaml_text(doc))
        variables = load_data(FIXTURES / "vars.example.yaml")
        variables["DISABLED"] = True
        varfile = self.path / "vars.yaml"
        varfile.write_text(yaml_text(variables))
        self.assertIn("DISABLED", self.cli("extract-vars", kit).stdout)
        result = self.cli("render", kit, "--vars", varfile)
        self.assertTrue(json.loads(result.stdout)["agents"][0]["disabled"])

    def test_cli_help_version_and_argparse_errors(self):
        self.assertIn("split", self.cli("--help").stdout)
        self.assertEqual(self.cli("--version").stdout.strip(), "0.1.0")
        self.cli("join", expected=2)
        self.cli("unknown-command", expected=2)


class OutputBoundaryTests(unittest.TestCase):
    def test_json_that_would_exceed_yaml_nodes_rejected_before_split_write(self):
        doc = raw()
        doc["agents"][0]["options"] = {f"key{i}": 0 for i in range(50000)}
        doc = from_export(doc)  # Valid under the JSON-value budget.
        with self.assertRaisesRegex(KitError, "generated YAML exceeds depth/node"):
            split_files(doc)

    def test_surrogates_rejected_as_values_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for text in ('{"x": "\\ud800"}', '{"\\ud800": "x"}'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaisesRegex(KitError, "surrogates"):
                    load_data(path, json_only=True)
