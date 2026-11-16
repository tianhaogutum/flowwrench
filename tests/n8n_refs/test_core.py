from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from flowwrench.n8n_refs.core import WorkflowError, analyze, load_workflow, pointer, rename, validate
from .helpers import TEST_TMP, node, workflow


class RuleTests(unittest.TestCase):
    def rules(self, data):
        return {f.rule for f in analyze(data)}

    def test_r001_unknown_and_known(self):
        data = workflow([node("A"), node("B", "={{ $('Ghost').first().json }}")], [("A", "B")])
        self.assertEqual(self.rules(data), {"R001"})
        data["nodes"][1]["parameters"]["text"] = "={{ $('A').first().json }}"
        self.assertEqual(self.rules(data), set())

    def test_r002_downstream_disconnected_and_self(self):
        for ref in ("C", "D", "B"):
            data = workflow([node("A"), node("B", "={{ $items('" + ref + "') }}"), node("C"), node("D")], [("A", "B"), ("B", "C")])
            self.assertEqual(self.rules(data), {"R002"})

    def test_r003_branch_bypass_and_upstream(self):
        data = workflow([node("Start", kind="manualTrigger"), node("A"), node("B"), node("Join", "={{ $('A').first().json }}")], [("Start", "A"), ("Start", "B", 1, 0), ("A", "Join"), ("B", "Join")])
        self.assertEqual(self.rules(data), {"R003"})
        data["nodes"][3]["parameters"]["text"] = "={{ $('Start').first().json }}"
        self.assertEqual(self.rules(data), set())

    def test_r004_merge_pairing_and_first_all(self):
        for syntax in ["$('A').item.json", "$node['A'].json", "$('A')['item'].json"]:
            data = workflow([node("A"), node("Merge", kind="merge"), node("B", "={{ " + syntax + " }}")], [("A", "Merge"), ("Merge", "B")])
            self.assertEqual(self.rules(data), {"R004"})
        for syntax in ["$('A').first().json", "$('A').all()", "$items('A')", "$('Merge').item.json"]:
            data["nodes"][2]["parameters"]["text"] = "={{ " + syntax + " }}"
            self.assertEqual(self.rules(data), set())

    def test_r004_only_between_reference_and_consumer(self):
        data = workflow([node("M", kind="merge"), node("A"), node("B", "={{ $('A').item }}"), node("Other", kind="merge")], [("M", "A"), ("A", "B"), ("A", "Other")])
        self.assertEqual(self.rules(data), set())

    def test_r004_code_mode_and_aggregate(self):
        for kind in ["aggregate", "summarize", "itemLists", "function", "code"]:
            data = workflow([node("A"), node("Breaker", kind=kind), node("B", "={{ $('A').item }}")], [("A", "Breaker"), ("Breaker", "B")])
            self.assertEqual(self.rules(data), {"R004"})
        data["nodes"][1]["parameters"]["mode"] = "runOnceForEachItem"
        self.assertEqual(self.rules(data), set())

    def test_r005_disabled(self):
        data = workflow([node("A", disabled=True), node("B", "={{ $items('A') }}")], [("A", "B")])
        self.assertEqual(self.rules(data), {"R005"})
        data["nodes"][0]["disabled"] = False
        self.assertEqual(self.rules(data), set())

    def test_r006_duplicates_and_both_missing_connection_ends(self):
        self.assertEqual(self.rules(workflow([node("A"), node("A")])), {"R006"})
        data = workflow([node("A")], [("Missing", "Ghost")])
        findings = analyze(data)
        self.assertEqual(len(findings), 2)
        self.assertEqual({f.rule for f in findings}, {"R006"})
        self.assertEqual(self.rules(workflow([node("A"), node("B")], [("A", "B")])), set())

    def test_r007_dynamic_and_nonmain_connections(self):
        data = workflow([node("A", "={{ $(dynamicName).item }}")])
        self.assertEqual(self.rules(data), {"R007"})
        data = workflow([node("A"), node("B")])
        data["connections"] = {"A": {"ai_tool": [[{"node": "B", "type": "ai_tool", "index": 0}]]}}
        self.assertEqual(self.rules(data), {"R007"})
        with self.assertRaises(WorkflowError):
            rename(data, "A", "New")

    def test_recursive_parameters_code_and_location(self):
        data = workflow([node("A/~"), node("B", kind="code")], [("A/~", "B")])
        data["nodes"][1]["parameters"] = {"jsCode": "// $('Fake')\nreturn $items('A/~');", "fields": [{"key/~": "={{ $('Ghost').item }}"}], "literal": "$('Fake')"}
        findings = analyze(data)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].pointer, "/nodes/1/parameters/fields/0/key~1~0")
        data["nodes"][1]["parameters"] = {"pythonCode": "# _('Fake')\nreturn _items('A/~')"}
        self.assertEqual(analyze(data), [])

    def test_data_and_credentials_never_scanned(self):
        data = workflow([node("A"), node("B")], [("A", "B")])
        data["nodes"][1]["credentials"] = {"example": {"name": "={{ $('Ghost') }}", "id": "synthetic"}}
        data["nodes"][1]["notes"] = "={{ $('Ghost') }}"
        data["pinData"] = {"B": [{"json": {"source": "={{ $('Ghost') }}"}}]}
        self.assertEqual(analyze(data), [])

    def test_pointer_escaping(self):
        self.assertEqual(pointer("connections", "a~/b", 0), "/connections/a~0~1b/0")


class RenameTests(unittest.TestCase):
    def sample(self):
        data = workflow([node("A"), node("B", "=ref {{ $('A').item.json }} {{ $node['A'].json }} {{ $items('A') }}"), node("Code", kind="code")], [("A", "B"), ("B", "Code")])
        data["nodes"][2]["parameters"] = {"jsCode": "/* $('A') */ return $('A').first();", "pythonCode": "# _('A')\nreturn _('A').first()", "mode": "runOnceForEachItem"}
        data["pinData"] = {"A": [{"json": {"text": "A", "literal": "$('A')"}}]}
        return data

    def test_rename_roundtrip_and_clean_check(self):
        original = self.sample()
        data = deepcopy(original)
        new = "New ' \\\" 雪 😀"
        changed = rename(data, "A", new)
        self.assertEqual(data, original)
        self.assertEqual(analyze(changed), [])
        self.assertEqual(changed["nodes"][0]["name"], new)
        self.assertIn(new, changed["connections"])
        self.assertIn(new, changed["pinData"])
        self.assertEqual(changed["pinData"][new], original["pinData"]["A"])
        self.assertIn("/* $('A') */", changed["nodes"][2]["parameters"]["jsCode"])
        restored = rename(changed, new, "A")
        self.assertEqual(analyze(restored), [])
        # Formatting inside changed literals normalizes quotes; semantics roundtrip.
        for i in (0, 1):
            self.assertEqual(restored["nodes"][i]["name"], original["nodes"][i]["name"])
        self.assertEqual(restored["connections"], original["connections"])
        self.assertEqual(restored["pinData"], original["pinData"])
        self.assertIn("😀", changed["nodes"][2]["parameters"]["pythonCode"])

    def test_connection_target_rewritten(self):
        data = workflow([node("A"), node("B")], [("A", "B")])
        changed = rename(data, "B", "New")
        self.assertEqual(changed["connections"]["A"]["main"][0][0]["node"], "New")

    def test_collisions_noops_and_bad_names_refused(self):
        data = self.sample()
        for old, new in [("A", "B"), ("A", "A"), ("Missing", "New"), ("A", ""), ("A", " \t"), ("A", "bad\nname"), ("A", "\ud800")]:
            with self.subTest(old=old, new=repr(new)), self.assertRaises(WorkflowError):
                rename(data, old, new)
        data["pinData"]["Stale"] = []
        with self.assertRaises(WorkflowError):
            rename(data, "A", "Stale")

    def test_unsupported_and_duplicate_input_refused(self):
        for data in [workflow([node("A"), node("A")]), workflow([node("A", "={{ $(name) }}")]), workflow([node("A")], [("A", "Missing")])]:
            with self.assertRaises(WorkflowError):
                rename(data, "A", "New")


class InputTests(unittest.TestCase):
    def test_invalid_schema(self):
        for data in [None, [], {}, {"nodes": [], "connections": {}}, workflow([node("A", disabled="false")]), workflow([{"name": "A", "parameters": {}}])]:
            with self.subTest(data=data), self.assertRaises(WorkflowError):
                validate(data)
        data = workflow([node("A")], [("A", "A")])
        for change in [{"index": True}, {"index": -1}, {"type": None}, {"node": 3}]:
            bad = deepcopy(data)
            bad["connections"]["A"]["main"][0][0].update(change)
            with self.assertRaises(WorkflowError):
                validate(bad)

    def test_json_parse_errors_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp) / "bad.json"
            for text in ['{bad secret-source-line', '{"nodes":[],"nodes":[],"connections":{}}', '{"nodes":[],"connections":{},"x":NaN}', '[1,2]', '\ud800']:
                path.write_bytes(text.encode("utf-8", errors="surrogatepass"))
                with self.subTest(text=repr(text)), self.assertRaises(WorkflowError):
                    load_workflow(path)

    def test_size_node_and_nesting_limits(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            path = Path(tmp) / "huge.json"
            path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
            with self.assertRaises(WorkflowError):
                load_workflow(path)
        with self.assertRaises(WorkflowError):
            validate(workflow([node(str(i)) for i in range(2001)]))
        data = workflow([node("A")])
        nested = []
        for _ in range(102):
            nested = [nested]
        data["extra"] = nested
        with self.assertRaises(WorkflowError):
            validate(data)
