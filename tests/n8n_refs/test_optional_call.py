"""Static JS optional-call contract, established before the scanner change.

$ /* lookup */ ?. ( /* name */ '\\u0041' ).item.json becomes the same
source with only '\\u0041' replaced by "Customer / 雪". Offsets identify
the original quote. Dynamic names, split ?. and Python optional proxies are
R007 and block both CLI write modes, even with supported references elsewhere.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.comparison import compare
from flowwrench.n8n_refs.core import WorkflowError, analyze, load_workflow, rename
from flowwrench.n8n_refs.scanner import ScanError, replace_references, scan
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures/rename-optional-call.json'
REFUSED_JS = [
    '$?.(name).item', '$items?.(name, 0, 0)', '$?.("A" + suffix)',
    '$items?.("A" + suffix, 0, 0)', '$?.(`A${suffix}`)', '$items?.(`A${suffix}`, 0)',
    '$?.(("A"))', '$?.()', '$?.("A", 0)', '$items?.()',
    '$? .("A")', '$? /* gap */ .("A")', '$?\n.("A")',
    '$items? .("A", 0, 0)', '$items? /* gap */ .("A")',
    '$?.("A")? .item', '$?.("A")? /* gap */ .first()', '$?.("A")?\n.["item"]',
    '$?.("A")[property]', '$?.("A")?.[property]', '($?.("A"))[property]',
    r'$?.("\q")', r'$items?.("\u{41}")',
]
REFUSED_PYTHON = ['$?.("A").item', '$items?.("A", 0, 0)', '_?.("A").item', '_items?.("A", 0, 0)']


class OptionalCallTests(unittest.TestCase):
    def test_literal_spans_quotes_escapes_and_pairing(self):
        source = r'''$?.('A').item; $?.("\u0041").first(); $items?.(`雪`, 0, 0);
$?.('O\'Brien').itemMatching(0); $?.('\uD83D\uDE00')?.['item'];'''
        refs = scan(source)
        self.assertEqual([r.name for r in refs], ['A', 'A', '雪', "O'Brien", '😀'])
        self.assertEqual([r.paired for r in refs], [True, False, False, True, True])
        self.assertEqual([r.syntax for r in refs], ['$', '$', '$items', '$', '$'])
        self.assertEqual([source[r.start:r.end] for r in refs],
                         ["'A'", r'"\u0041"', '`雪`', r"'O\'Brien'", r"'\uD83D\uDE00'"])
        self.assertEqual(refs[0].start, source.index("'A'"))

    def test_grouping_optional_properties_and_items_arguments(self):
        source = '''(($?.("A"))).item; f($?.("B")).item; ($?.("C"))?.itemMatching(0);
$?.("D")["item"]; $?.("E").first(); $items?.("F", 0, $?.("G").first());'''
        refs = scan(source)
        self.assertEqual([r.name for r in refs], list('ABCDEFG'))
        self.assertEqual([r.paired for r in refs], [True, False, True, True, False, False, False])

    def test_split_optional_result_properties_refused_but_ternaries_allowed(self):
        for source in ['$?.("A")? .item', '$?.("A")? /* gap */ .first()', '$?.("A")?\n.["item"]']:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source)
        refs = scan('$?.("A") ? yes : no; $?.("A")?.item;')
        self.assertEqual([r.paired for r in refs], [False, True])

    def test_only_literal_changes_and_inert_source_is_preserved(self):
        source = r'''// $?.('A') and $items?.('A')
/* $?.('A') */ const note = "$?.('A')"; const rx = /\$\?\.\('A'\)/;
const t = `$?.('A') ${$?.('A').first().json}`;
return $ /* lookup */ ?. ( /* name */ '\u0041' /* end */ ).item.json
 + $items /* items */ ?. /* call */ (`A`, 0, 0) + $?.('Other').first()
 + obj.$?.('A') + obj?.$items?.('A') + obj.$?.(name);'''
        for new in ['Customer / 雪', 'quote\'"\\😀\u2028\u2029']:
            with self.subTest(new=new):
                quoted = json.dumps(new, ensure_ascii=False).replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
                expected = source.replace("${$?.('A')", '${$?.(' + quoted + ')')
                expected = expected.replace(r"'\u0041' /* end */", quoted + ' /* end */')
                expected = expected.replace('(`A`, 0, 0)', '(' + quoted + ', 0, 0)')
                changed = replace_references(source, scan(source), 'A', new)
                self.assertEqual(changed, expected)
                self.assertEqual([r.name for r in scan(changed)], [new] * 3 + ['Other'])
                restored = replace_references(changed, scan(changed), new, 'A')
                self.assertEqual([r.name for r in scan(restored)], ['A'] * 3 + ['Other'])

    def test_expression_islands_and_properties(self):
        source = '=outside $?.("A") {{ $?.("A").item.json }} / {{ $items?.("A", 0, 0) }}'
        refs = scan(source, expression=True)
        self.assertEqual([r.name for r in refs], ['A', 'A'])
        self.assertEqual([r.paired for r in refs], [True, False])
        self.assertEqual([r.start for r in refs], [source.index('"A"', source.index('{{')),
                                                source.rindex('"A"')])
        self.assertEqual(replace_references(source, refs, 'A', 'Customer / 雪'),
                         '=outside $?.("A") {{ $?.("Customer / 雪").item.json }} / {{ $items?.("Customer / 雪", 0, 0) }}')
        self.assertEqual(scan('obj.$?.(name); obj?.$?.("A"); obj.$items?.(name); obj?.$items?.("A");'), [])

    def test_refusals_in_source_and_expression_islands(self):
        for source in REFUSED_JS:
            for expression in [False, True]:
                with self.subTest(source=source, expression=expression), self.assertRaises(ScanError):
                    scan('={{ ' + source + ' }}' if expression else source, expression=expression)
        for source in REFUSED_PYTHON:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source, language='python')

    def test_r001_to_r005_original_quote_offsets_and_nonpaired_exceptions(self):
        cases = [
            ('R001', [node('A'), node('Use')], [('A', 'Use')], 'Missing'),
            ('R002', [node('A'), node('Use')], [('A', 'Use')], 'Use'),
            ('R003', [node('Start'), node('A'), node('Use')],
             [('Start', 'A'), ('A', 'Use'), ('Start', 'Use')], 'A'),
            ('R004', [node('A'), node('Merge', kind='merge'), node('Use')],
             [('A', 'Merge'), ('Merge', 'Use')], 'A'),
            ('R005', [node('A', disabled=True), node('Use')], [('A', 'Use')], 'A'),
        ]
        for rule, nodes, links, name in cases:
            for form in ['$?.("{name}").item.json', '$?.("{name}").first().json', '$items?.("{name}", 0, 0)']:
                for expression in [False, True]:
                    with self.subTest(rule=rule, form=form, expression=expression):
                        use = form.format(name=name)
                        source = '=text {{ ' + use + ' }}' if expression else 'return ' + use + ';'
                        nodes[-1]['type'] = 'n8n-nodes-base.set' if expression else 'n8n-nodes-base.code'
                        field = 'a/~' if expression else 'jsCode'
                        nodes[-1]['parameters'] = {field: source, 'mode': 'runOnceForEachItem'}
                        findings = analyze(workflow(nodes, links))
                        if rule == 'R004' and '.item.' not in form:
                            self.assertEqual(findings, [])
                            continue
                        finding, = findings
                        self.assertEqual((finding.rule, finding.reference, finding.pointer, finding.offset),
                                         (rule, name, f'/nodes/{len(nodes)-1}/parameters/' +
                                          ('a~1~0' if expression else field), source.index('"' + name + '"')))

    def test_fixture_exact_rename_roundtrip_and_metadata(self):
        data = load_workflow(FIXTURE)
        original = deepcopy(data)
        self.assertEqual(analyze(data), [])
        changed = rename(data, 'A', 'Customer / 雪')
        expected = deepcopy(original)
        expected['nodes'][0]['name'] = 'Customer / 雪'
        expected['connections']['Customer / 雪'] = expected['connections'].pop('A')
        expected['pinData']['Customer / 雪'] = expected['pinData'].pop('A')
        code = expected['nodes'][1]['parameters']['jsCode']
        code = code.replace(r"'\u0041' /* end */", '"Customer / 雪" /* end */')
        expected['nodes'][1]['parameters']['jsCode'] = code.replace('(`A`, 0, 0)', '("Customer / 雪", 0, 0)')
        expected['nodes'][2]['parameters']['text'] = '=outside $?.("A") {{ $?.("Customer / 雪").first().json }} / {{ $items?.("Customer / 雪", 0, 0) }}'
        self.assertEqual(changed, expected)
        self.assertEqual(data, original)
        self.assertEqual(analyze(changed), [])
        restored = rename(changed, 'Customer / 雪', 'A')
        self.assertEqual(analyze(restored), [])
        self.assertEqual(restored['connections'], original['connections'])
        self.assertEqual(restored['pinData'], original['pinData'])

    def test_refusals_block_whole_workflow_and_both_cli_write_modes(self):
        for source, language in [(s, 'js') for s in REFUSED_JS] + [(s, 'python') for s in REFUSED_PYTHON]:
            with self.subTest(source=source, language=language):
                data = load_workflow(FIXTURE)
                field = 'pythonCode' if language == 'python' else 'jsCode'
                data['nodes'][1]['parameters'] = {field: source, 'mode': 'runOnceForEachItem'}
                original = deepcopy(data)
                self.assertEqual([f.rule for f in analyze(data)], ['R007'])
                with self.assertRaises(WorkflowError):
                    rename(data, 'A', 'Customer / 雪')
                self.assertEqual(data, original)
                with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
                    path, output = Path(tmp) / 'input.json', Path(tmp) / 'output.json'
                    path.write_text(json.dumps(data), encoding='utf-8')
                    before = path.read_bytes()
                    for destination in [('-o', str(output)), ('--in-place',)]:
                        result = subprocess.run([sys.executable, '-m', 'flowwrench.n8n_refs', 'rename', str(path),
                                                 '--from', 'A', '--to', 'Customer / 雪', *destination],
                                                cwd=ROOT, capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(path.read_bytes(), before)
                        self.assertFalse(output.exists())

    def test_comparison_identity_matches_ordinary_calls(self):
        for selector in ['$', '$items']:
            baseline = workflow([node('Use', '={{ ' + selector + '("Missing") }}')])
            candidate = deepcopy(baseline)
            candidate['nodes'][0]['parameters']['text'] = '={{ ' + selector + '?.("Missing") }}'
            result = compare(baseline, candidate)
            self.assertTrue(result['complete'])
            self.assertEqual([len(result[k]) for k in ('new', 'resolved', 'unchanged')], [0, 0, 1])
            self.assertEqual(result['unchanged'][0]['candidate']['rule'], 'R001')


if __name__ == '__main__':
    unittest.main()
