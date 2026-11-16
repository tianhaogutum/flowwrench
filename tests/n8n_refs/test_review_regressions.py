import unittest
from flowwrench.n8n_refs.scanner import scan, replace_references
from flowwrench.n8n_refs.core import analyze, rename
from .helpers import node, workflow


class LineTerminatorRegressionTests(unittest.TestCase):
    def test_javascript_comment_ends_at_each_line_terminator(self):
        for ending in ["\r", "\n", "\r\n", "\u2028", "\u2029"]:
            with self.subTest(ending=repr(ending)):
                source = "// $items('Ghost')" + ending + "return $items('A');"
                refs = scan(source)
                self.assertEqual([r.name for r in refs], ["A"])
                changed = replace_references(source, refs, "A", "Renamed")
                self.assertEqual([r.name for r in scan(changed)], ["Renamed"])
                self.assertTrue(changed.startswith("// $items('Ghost')" + ending))

    def test_check_and_rename_code_after_unicode_comment(self):
        for ending in ["\r", "\u2028", "\u2029"]:
            with self.subTest(ending=repr(ending)):
                data = workflow([node("A"), node("Use", kind="code")], [("A", "Use")])
                data["nodes"][1]["parameters"] = {"jsCode": "// comment" + ending + "return $items('Missing');"}
                self.assertIn("R001", {f.rule for f in analyze(data)})
                data["nodes"][1]["parameters"]["jsCode"] = "// comment" + ending + "return $items('A');"
                changed = rename(data, "A", "Renamed")
                self.assertEqual([r.name for r in scan(changed["nodes"][1]["parameters"]["jsCode"])], ["Renamed"])
                self.assertEqual(analyze(changed), [])

    def test_python_unicode_separators_do_not_end_comments(self):
        for ending in ["\u2028", "\u2029"]:
            self.assertEqual(scan("# comment" + ending + "_items('Hidden')", language="python"), [])
        for ending in ["\r", "\n", "\r\n"]:
            self.assertEqual([r.name for r in scan("# comment" + ending + "_items('A')", language="python")], ["A"])
