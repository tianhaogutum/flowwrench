from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowwrench.huginn.core import (
    SCHEDULES, canonical, cycle_components, extract, from_export, lint,
    load_scenario, render, semantic_diff, split_files, to_export, validate,
)
from flowwrench.huginn.formats import KitError, load_data, write_directory, yaml_text

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def raw(name="weather"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def graph_oracle(doc):
    """Independent comparison: index lookup against original agent order."""
    metadata = {k: v for k, v in doc.items()
                if k not in ("agents", "links", "control_links", "exported_at")}
    ids = [a["guid"] for a in doc["agents"]]
    links = sorted((ids[e["source"]], ids[e["receiver"]]) for e in doc.get("links", []))
    controls = sorted((ids[e["controller"]], ids[e["control_target"]])
                      for e in doc.get("control_links", []))
    return metadata, {a["guid"]: a for a in doc["agents"]}, links, controls


class RoundTripTests(unittest.TestCase):
    def test_four_fixture_roundtrips_with_independent_index_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("weather", "control", "unicode", "empty"):
                with self.subTest(name=name):
                    original = raw(name)
                    path = Path(tmp) / name
                    write_directory(path, split_files(from_export(original)))
                    result = to_export(load_scenario(path))
                    self.assertEqual(graph_oracle(original), graph_oracle(result))
                    self.assertNotIn("exported_at", result)
                    self.assertNotIn("exported_at", (path / "scenario.yaml").read_text())

    def test_control_links_map_correct_direction_after_sorting(self):
        doc = from_export(raw("control"))
        result = to_export(doc)
        self.assertEqual(result["links"], [{"source": 0, "receiver": 1}])
        self.assertEqual(result["control_links"], [
            {"controller": 2, "control_target": 0},
            {"controller": 2, "control_target": 1},
        ])

    def test_split_is_byte_identical_after_agent_and_link_reordering(self):
        original = raw()
        reordered = deepcopy(original)
        reordered["exported_at"] = "different export time"
        reordered["agents"].reverse()
        n = len(original["agents"])
        reordered["links"] = [dict(source=n-1-e["source"], receiver=n-1-e["receiver"])
                              for e in reversed(original["links"])]
        self.assertEqual(split_files(from_export(original)), split_files(from_export(reordered)))

    def test_split_join_split_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = split_files(from_export(raw("unicode")))
            path = Path(tmp) / "kit"
            write_directory(path, files)
            self.assertEqual(files, split_files(from_export(to_export(load_scenario(path)))))

    def test_filenames_do_not_escape_output(self):
        files = split_files(from_export(raw("unicode")))
        self.assertEqual(set(files), {"scenario.yaml", "agents/agent--11111111.yaml"})

    def test_equal_name_and_guid_prefix_collision_extended(self):
        doc = raw()
        doc["agents"][0]["name"] = doc["agents"][1]["name"] = "same"
        doc["agents"][0]["guid"] = "deadbeef1"
        doc["agents"][1]["guid"] = "deadbeef2"
        files = split_files(from_export(doc))
        self.assertIn("agents/same--deadbeef1.yaml", files)
        self.assertIn("agents/same--deadbeef2.yaml", files)

    def test_one_guid_is_prefix_of_another(self):
        doc = raw()
        doc["agents"][0]["name"] = doc["agents"][1]["name"] = "same"
        doc["agents"][0]["guid"] = "a"
        doc["agents"][1]["guid"] = "aa"
        files = split_files(from_export(doc))
        self.assertIn("agents/same--a.yaml", files)
        self.assertIn("agents/same--aa.yaml", files)

    def test_agent_filenames_are_not_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kit"
            doc = from_export(raw())
            write_directory(path, split_files(doc))
            agent = next((path / "agents").iterdir())
            agent.rename(agent.with_name("renamed.yaml"))
            self.assertEqual(load_scenario(path), doc)

    def test_does_not_mutate_input(self):
        original = raw()
        saved = deepcopy(original)
        doc = from_export(original)
        to_export(doc)
        split_files(doc)
        self.assertEqual(original, saved)


class ValidationTests(unittest.TestCase):
    def test_unsupported_schema_versions(self):
        for version in (None, 0, 2, "1", True):
            with self.subTest(version=version):
                doc = raw()
                doc["schema_version"] = version
                with self.assertRaisesRegex(KitError, "unsupported schema_version"):
                    from_export(doc)

    def test_duplicate_guid(self):
        doc = raw()
        doc["agents"][1]["guid"] = doc["agents"][0]["guid"]
        with self.assertRaisesRegex(KitError, "duplicate agent guid"):
            from_export(doc)

    def test_dangling_indices_both_link_kinds(self):
        for kind, field in (("links", "source"), ("control_links", "control_target")):
            for index in (-1, 3, True, "0", None, 0.0):
                with self.subTest(kind=kind, index=index):
                    doc = raw("control")
                    doc[kind][0][field] = index
                    with self.assertRaisesRegex(KitError, "link index"):
                        from_export(doc)

    def test_dangling_guid_both_link_kinds(self):
        for kind, field in (("links", "receiver"), ("control_links", "controller")):
            with self.subTest(kind=kind):
                doc = from_export(raw("control"))
                doc[kind][0][field] = "absent"
                with self.assertRaisesRegex(KitError, "dangling guid"):
                    to_export(doc)

    def test_duplicate_edges_not_silently_collapsed(self):
        doc = raw()
        doc["links"].append(deepcopy(doc["links"][0]))
        with self.assertRaisesRegex(KitError, "duplicate edge"):
            from_export(doc)

    def test_unknown_link_attributes_rejected(self):
        doc = raw()
        doc["links"][0]["extra"] = True
        with self.assertRaisesRegex(KitError, "exactly"):
            from_export(doc)

    def test_invalid_schedules_and_null_unscheduled(self):
        doc = raw()
        for schedule in ("every_3m", "12am", "", []):
            doc["agents"][0]["schedule"] = schedule
            with self.subTest(schedule=schedule), self.assertRaises(KitError):
                from_export(doc)
        doc["agents"][0]["schedule"] = None
        from_export(doc)

    def test_all_known_schedule_values(self):
        doc = raw()
        self.assertEqual(len(SCHEDULES), 37)
        for schedule in SCHEDULES:
            doc["agents"][0]["schedule"] = schedule
            from_export(doc)

    def test_invalid_agent_shapes(self):
        for key, value in (("options", []), ("disabled", "false"), ("type", ""),
                           ("name", None), ("keep_events_for", -1), ("keep_events_for", True),
                           ("propagate_immediately", 1), ("guid", "../../unsafe")):
            with self.subTest(key=key, value=value):
                doc = raw()
                doc["agents"][0][key] = value
                with self.assertRaises(KitError):
                    from_export(doc)

    def test_bad_manifest_fields_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kit"
            write_directory(path, split_files(from_export(raw())))
            manifest = load_data(path / "scenario.yaml")
            manifest["kit_version"] = 2
            (path / "scenario.yaml").write_text(yaml_text(manifest))
            with self.assertRaisesRegex(KitError, "kit_version"):
                load_scenario(path)
            manifest["kit_version"] = 1
            manifest["scenario"]["agents"] = []
            (path / "scenario.yaml").write_text(yaml_text(manifest))
            with self.assertRaisesRegex(KitError, "reserved"):
                load_scenario(path)

    def test_extra_non_yaml_agent_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kit"
            write_directory(path, split_files(from_export(raw())))
            (path / "agents/ignored.txt").write_text("not ignored")
            with self.assertRaisesRegex(KitError, "only regular .yaml"):
                load_scenario(path)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.doc = load_scenario(FIXTURES / "template", check_schedules=False, allow_templates=True)
        self.vars = load_data(FIXTURES / "vars.example.yaml")

    def test_inventory(self):
        self.assertEqual(extract(self.doc), {
            "variables": ["FEED_URL", "LOCATION", "RETRIES", "SCHEDULE"],
            "credentials": ["weather_key"],
        })

    def test_render_preserves_liquid_and_types(self):
        result = render(self.doc, self.vars)
        feed = result["agents"][0]
        self.assertEqual(feed["schedule"], "every_2h")
        self.assertEqual(feed["options"]["expected_update_period_in_days"], 3)
        self.assertEqual(feed["options"]["headers"]["X-Location"], "Berlin")
        self.assertEqual(feed["options"]["headers"]["X-API-Key"], "{% credential weather_key %}")
        self.assertEqual(result["agents"][1]["options"]["instructions"]["message"],
                         "{{ location }}: {{ temperature }} C")

    def test_missing_variables_listed_together_sorted(self):
        with self.assertRaisesRegex(KitError, "missing variables: FEED_URL, LOCATION, RETRIES, SCHEDULE"):
            render(self.doc, {})

    def test_typed_whole_values_and_embedded_scalars(self):
        doc = from_export(raw())
        doc["agents"][0]["options"] = {
            "list": "${HSK:LIST}", "mapping": "${HSK:MAP}", "null": "${HSK:NULL}",
            "string": "${HSK:TEXT}", "embedded": "${HSK:BOOL}/${HSK:NULL}/${HSK:NUM}",
        }
        result = render(doc, dict(LIST=[1, False], MAP={"x": 2}, NULL=None, TEXT="3", BOOL=True, NUM=3))
        options = result["agents"][0]["options"]
        self.assertEqual(options, dict(list=[1, False], mapping={"x": 2}, null=None, string="3", embedded="true/null/3"))

    def test_typed_agent_fields(self):
        doc = deepcopy(self.doc)
        doc["agents"][0]["disabled"] = "${HSK:DISABLED}"
        doc["agents"][0]["keep_events_for"] = "${HSK:SECONDS}"
        result = render(doc, {**self.vars, "DISABLED": True, "SECONDS": 86400})
        self.assertTrue(result["agents"][0]["disabled"])
        self.assertEqual(result["agents"][0]["keep_events_for"], 86400)

    def test_embedded_collection_rejected(self):
        self.vars["LOCATION"] = ["Berlin"]
        self.doc["agents"][0]["options"]["headers"]["X-Location"] = "at ${HSK:LOCATION}"
        with self.assertRaisesRegex(KitError, "whole value"):
            render(self.doc, self.vars)

    def test_invalid_rendered_schedule_rejected(self):
        self.vars["SCHEDULE"] = "every_3m"
        with self.assertRaisesRegex(KitError, "unknown schedule"):
            render(self.doc, self.vars)

    def test_no_recursive_variable_evaluation(self):
        self.vars["LOCATION"] = "${HSK:LOCATION}"
        with self.assertRaisesRegex(KitError, "single-pass"):
            render(self.doc, self.vars)

    def test_mapping_key_placeholders_and_malformed_tokens_rejected(self):
        for value in ({"${HSK:KEY}": 1}, {"x": "${HSK:invalid space}"}):
            with self.subTest(value=value), self.assertRaises(KitError):
                extract(value)

    def test_credentials_sorted_deduplicated_quoted_and_trimmed(self):
        value = {"x": "{% credential z_key %} {% credential 'alpha' %} {%- credential \"alpha\" -%}"}
        self.assertEqual(extract(value)["credentials"], ["alpha", "z_key"])

    def test_dynamic_credential_reference_explicit_error(self):
        with self.assertRaisesRegex(KitError, "complex credential"):
            extract({"x": "{% credential user.name | downcase %}"})

    def test_repeated_large_variables_rejected_before_expansion(self):
        doc = from_export(raw())
        doc["agents"][0]["options"] = {"a": "${HSK:BIG}", "b": "prefix ${HSK:BIG}"}
        with self.assertRaisesRegex(KitError, "expansion limit"):
            render(doc, {"BIG": "x" * (3 * 1024 * 1024)})

    def test_repeated_collection_node_expansion_rejected(self):
        doc = from_export(raw())
        doc["agents"][0]["options"] = {"a": "${HSK:BIG}", "b": "${HSK:BIG}"}
        with self.assertRaisesRegex(KitError, "depth/node"):
            render(doc, {"BIG": [0] * 60000})

    def test_combined_variable_depth_rejected(self):
        doc = from_export(raw())
        nested = "${HSK:DEEP}"
        for _ in range(35):
            nested = [nested]
        doc["agents"][0]["options"] = {"nested": nested}
        value = 0
        for _ in range(35):
            value = [value]
        with self.assertRaisesRegex(KitError, "depth/node"):
            render(doc, {"DEEP": value})

    def test_expression_strings_are_data_and_do_not_execute(self):
        self.vars["LOCATION"] = "$(touch /tmp/never-execute-hsk); {{ system('x') }}"
        result = render(self.doc, self.vars)
        self.assertEqual(result["agents"][0]["options"]["headers"]["X-Location"], self.vars["LOCATION"])


class DiffLintTests(unittest.TestCase):
    def test_diff_metadata_agents_options_and_both_link_kinds(self):
        before = from_export(raw("control"))
        after = deepcopy(before)
        removed = after["agents"].pop(0)["guid"]
        added = deepcopy(after["agents"][0])
        added["guid"] = "new-guid"
        after["agents"].append(added)
        after["agents"][0]["options"]["a/b~"] = [True]
        after["name"] = "changed"
        after["links"] = []
        after["control_links"] = [e for e in after["control_links"] if removed not in e.values()]
        after["control_links"].append(dict(controller=added["guid"], control_target=after["agents"][0]["guid"]))
        diff = semantic_diff(before, after)
        self.assertTrue(diff["different"])
        self.assertEqual(diff["agents"]["removed"], [removed])
        self.assertEqual(diff["agents"]["added"], ["new-guid"])
        self.assertIn({"path": "/options/a~1b~0", "change": "added"},
                      diff["agents"]["changed"][after["agents"][0]["guid"]])
        self.assertEqual(diff["scenario"], [{"path": "/name", "change": "changed"}])
        self.assertEqual(len(diff["links"]["removed"]), 1)
        self.assertEqual(len(diff["control_links"]["added"]), 1)
        self.assertEqual(len(diff["control_links"]["removed"]), 1)

    def test_diff_ignores_agent_link_order_and_timestamp(self):
        before = from_export(raw())
        after = deepcopy(before)
        after["agents"].reverse()
        after["links"].reverse()
        after["exported_at"] = "anything"
        self.assertFalse(semantic_diff(before, after)["different"])

    def test_diff_option_list_removals_and_type_changes_without_values(self):
        before = from_export(raw())
        before["agents"][0]["options"] = {"nested": [1, "secret-sentinel"]}
        after = deepcopy(before)
        after["agents"][0]["options"] = {"nested": [True]}
        diff = semantic_diff(before, after)
        self.assertNotIn("secret-sentinel", json.dumps(diff))
        self.assertEqual(next(iter(diff["agents"]["changed"].values())), [
            {"path": "/options/nested/0", "change": "changed"},
            {"path": "/options/nested/1", "change": "removed"},
        ])

    def test_lint_clean_weather_and_control(self):
        for name in ("weather", "control"):
            with self.subTest(name=name):
                self.assertEqual(lint(from_export(raw(name))), {"issues": [], "ok": True})

    def test_lint_orphan(self):
        doc = from_export(raw("unicode"))
        self.assertEqual(lint(doc)["issues"], [dict(code="orphan-agent", severity="warning", guid=doc["agents"][0]["guid"])])

    def test_cycles_both_kinds_and_unknown_schedule(self):
        doc = from_export(raw("control"))
        a, b, c = [a["guid"] for a in doc["agents"]]
        doc["agents"][0]["schedule"] = "unexpected"
        doc["links"] = [dict(source=a, receiver=b), dict(source=b, receiver=a), dict(source=b, receiver=c)]
        doc["control_links"] = [dict(controller=c, control_target=c)]
        issues = lint(doc)["issues"]
        self.assertEqual(issues[0], dict(code="unknown-schedule", severity="error", guid=a))
        self.assertEqual(issues[1]["guids"], [a, b])
        self.assertEqual(issues[2]["guids"], [c])

    def test_iterative_cycle_detection_large_chain(self):
        vertices = [f"v{i:04}" for i in range(1000)]
        edges = list(zip(vertices, vertices[1:])) + [(vertices[-1], vertices[-2])]
        self.assertEqual(cycle_components(vertices, edges), [vertices[-2:]])
