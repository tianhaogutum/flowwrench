import unittest
from flowwrench.n8n_refs.scanner import ScanError, scan, replace_references


class ScannerTests(unittest.TestCase):
    def names(self, text, **kwargs):
        return [r.name for r in scan(text, **kwargs)]

    def test_three_syntaxes_and_whitespace_comments(self):
        text = '''return $ /* fine */ ('A').item.json + $node [ "B" ].json + $items('C', 0, 1);'''
        refs = scan(text)
        self.assertEqual([r.name for r in refs], ["A", "B", "C"])
        self.assertEqual([r.paired for r in refs], [True, True, False])
        self.assertEqual([text[r.start:r.end] for r in refs], ["'A'", '"B"', "'C'"])

    def test_comments_and_string_contents_inert(self):
        source = '''// $('Ghost1')
/* $node["Ghost2"] */
const s = "$('Ghost3')";
const t = '$items("Ghost4")';
return $('Real').first();'''
        self.assertEqual(self.names(source), ["Real"])

    def test_expression_islands_and_nested_braces(self):
        source = '''=prefix $('Outside') {{ ({a: {b: $('A').item}, c: "}}"}) }} text {{ $node['B'].json }}'''
        self.assertEqual(self.names(source, expression=True), ["A", "B"])
        self.assertEqual(self.names("plain {{ $('Outside') }}", expression=True), [])
        self.assertEqual(self.names("=$items('A')", expression=True), [])

    def test_quotes_escapes_and_unicode(self):
        text = r'''$('O\'Brien'); $("A\\B"); $('\u0041'); $('\uD83D\uDE00'); $('a\x2fb'); $(`Backtick`);'''
        self.assertEqual(self.names(text), ["O'Brien", "A\\B", "A", "😀", "a/b", "Backtick"])

    def test_js_templates_interpolations(self):
        text = '''const x = `$('Ignored') ${$('A').item} ${`nested ${$items('B')}`}`;'''
        self.assertEqual(self.names(text), ["A", "B"])
        with self.assertRaises(ScanError):
            scan('$( `A${1}` ).item')

    def test_regex_and_division(self):
        text = '''const rx = /$('Fake')[}]/g; const n = x / 2; return $('A');'''
        self.assertEqual(self.names(text), ["A"])
        self.assertEqual(self.names("const f = () => /$('Fake')/; return $('A')"), ["A"])
        with self.assertRaises(ScanError):
            scan("if(ok) /$('Fake')/.test(s)")

    def test_python_comments_strings_and_triple_quotes(self):
        text = '''# _("Ghost")
s = "_('Ghost')"
doc = """_('Ghost')"""
return _('A').item, _node["B"].json, _items('C', 0)'''
        self.assertEqual(self.names(text, language="python"), ["A", "B", "C"])

    def test_optional_chaining_and_bracket_item(self):
        refs = scan('$("A")?.item; $("B")["item"]; $("C").items; $("D").first()')
        self.assertEqual([r.paired for r in refs], [True, True, False, False])
        refs = scan("$('A')?.['item']; $('B')?.itemMatching(0)")
        self.assertEqual([r.paired for r in refs], [True, True])

    def test_dynamic_and_indirect_accessors_refused(self):
        for text in ["$(name)", "$('A' + suffix)", "$node[name]", "$items(name)", "const lookup = $; lookup('A')", "$()", "$node['A', 'B']", "$(r'A')"]:
            with self.subTest(text=text), self.assertRaises(ScanError):
                scan(text)

    def test_malformed_sources_refused(self):
        for text in ["$('A)", "/* unterminated", "`no end", "{", "}", "/noend", "$('\\u{41}')", "$('\\q')", "$('\\uD800')", "$('\\01')"]:
            with self.subTest(text=text), self.assertRaises(ScanError):
                scan(text)
        with self.assertRaises(ScanError):
            scan("={{ $('A') ", expression=True)
        with self.assertRaises(ScanError):
            scan('f"{_(\'A\')}"', language="python")

    def test_not_global_selector(self):
        self.assertEqual(self.names("obj.$('Fake'); obj.$node['Fake']; foo$('Fake')"), [])

    def test_rename_only_literal_spans_and_preserve_comments(self):
        source = '''/* $('A') */ const note = "$node['A']"; $('A').item; $node["A"].json; $items('A');'''
        new = "quote'\"\\雪"
        changed = replace_references(source, scan(source), "A", new)
        self.assertTrue(changed.startswith('''/* $('A') */ const note = "$node['A']";'''))
        self.assertEqual(self.names(changed), [new] * 3)
        restored = replace_references(changed, scan(changed), new, "A")
        self.assertEqual(self.names(restored), ["A"] * 3)

    def test_depth_bound(self):
        text = "$('A')"
        for _ in range(42):
            text = "`x ${" + text + "}`"
        with self.assertRaises(ScanError):
            scan(text)

    def test_grouped_paired_item_and_computed_properties(self):
        refs = scan("return (($('A'))).item; f($('B')).item; ($('C')).itemMatching(0)")
        self.assertEqual([r.paired for r in refs], [True, False, True])
        with self.assertRaises(ScanError):
            scan("$('A')[property]")

    def test_source_size_and_token_limits(self):
        with self.assertRaises(ScanError):
            scan("x" * (1024 * 1024 + 1))
        with self.assertRaises(ScanError):
            scan("+ " * 100001)
