"""Contract for static JS $node?.[literal] lookups, never computed keys.

Offsets include only the original quoted key. Rename changes only that span,
preserving the optional chain and trivia. Optional access still has graph and
pairing hazards; it does not prove that a referenced node ran. Unsupported keys
produce R007 and block the entire rename before any file write.
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
FIXTURE = ROOT / 'fixtures' / 'rename-optional.json'


class OptionalLookupTests(unittest.TestCase):
    def test_static_keys_keep_literal_offsets_and_paired_semantics(self):
        for literal, name in [('"A"', 'A'), ("'A'", 'A'), ('`A`', 'A'),
                              (r'"\u0041"', 'A'), ('"雪 / 😀"', '雪 / 😀')]:
            with self.subTest(literal=literal):
                source = f'return ($node /* lookup */ ?. [ /* key */ {literal} /* end */ ])?.json;'
                ref, = scan(source)
                self.assertEqual((ref.name, ref.paired, ref.syntax, ref.dot_start),
                                 (name, True, '$node', None))
                self.assertEqual((ref.start, ref.end),
                                 (source.index(literal), source.index(literal) + len(literal)))
                self.assertEqual(source[ref.start:ref.end], literal)

    def test_rename_changes_only_keys_and_preserves_chain_and_inert_text(self):
        source = '''// $node?.['A']
const note = "$node?.['A']"; const rx = /$node\\?\\./;
const t = `$node?.['A'] ${$node?.['A'].json}`;
return $node /* lookup */ ?. [ /* key */ '\\u0041' /* end */ ]?.json
    + $node?.['Other'].json + obj.$node?.['A'] + obj?.$node?.['A'];'''
        new = 'Customer / "雪"\\😀\u2028\u2029'
        quoted = json.dumps(new, ensure_ascii=False).replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
        expected = source.replace("${$node?.['A']", '${$node?.[' + quoted + ']')
        expected = expected.replace("'\\u0041' /* end */", quoted + ' /* end */')
        changed = replace_references(source, scan(source), 'A', new)
        self.assertEqual(changed, expected)
        self.assertEqual([r.name for r in scan(changed)], [new, new, 'Other'])
        self.assertEqual([r.name for r in scan(replace_references(changed, scan(changed), new, 'A'))],
                         ['A', 'A', 'Other'])

    def test_expression_islands_and_object_properties(self):
        source = '=outside $node?.["A"] {{ $node?.["A"].json }} / {{ obj?.$node?.["A"] }}'
        refs = scan(source, expression=True)
        self.assertEqual([r.name for r in refs], ['A'])
        self.assertEqual(replace_references(source, refs, 'A', 'New Name'),
                         '=outside $node?.["A"] {{ $node?.["New Name"].json }} / {{ obj?.$node?.["A"] }}')
        self.assertEqual(scan('obj.$node?.[name]; obj?.$node?.[name];'), [])

    def test_computed_and_out_of_scope_keys_stay_refused(self):
        for source in ['$node?.[name].json', '$node?.["A" + suffix].json',
                       '$node?.[("A")].json', '$node?.["A", "B"].json',
                       '$node?.[`A${suffix}`].json', '$node?.["A"][property]',
                       '$node?.["A"]?.[property]', '$node?.[]', '$node?.["A"',
                       '$node?.', '$node? .["A"]',
                       '$node? /* gap */ .["A"]']:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source)
        for source in ['$node?.["A"].json', '_node?.["A"].json']:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source, language='python')

    def test_missing_node_reports_original_pointer_and_literal_offset(self):
        for expression in [False, True]:
            with self.subTest(expression=expression):
                source = 'return $node?.["Missing"].json;'
                data = workflow([node('A'), node('Use', kind='code')], [('A', 'Use')])
                field = 'jsCode'
                if expression:
                    source = '=text {{ $node?.["Missing"].json }}'
                    data['nodes'][1]['type'] = 'n8n-nodes-base.set'
                    field = 'a/~'
                data['nodes'][1]['parameters'] = {field: source}
                finding, = analyze(data)
                self.assertEqual((finding.rule, finding.reference, finding.offset, finding.pointer),
                                 ('R001', 'Missing', source.index('"Missing"'),
                                  '/nodes/1/parameters/' + ('a~1~0' if expression else 'jsCode')))

    def test_optional_lookup_does_not_suppress_graph_or_pairing_hazards(self):
        cases = [
            ('R002', [node('A'), node('Use', '={{ $node?.["Use"].json }}')], [('A', 'Use')]),
            ('R003', [node('Start'), node('A'), node('Use', '={{ $node?.["A"].json }}')],
             [('Start', 'A'), ('A', 'Use'), ('Start', 'Use')]),
            ('R004', [node('A'), node('Merge', kind='merge'), node('Use', '={{ ($node?.["A"])?.json }}')],
             [('A', 'Merge'), ('Merge', 'Use')]),
            ('R005', [node('A', disabled=True), node('Use', '={{ $node?.["A"].json }}')], [('A', 'Use')]),
        ]
        for rule, nodes, links in cases:
            with self.subTest(rule=rule):
                self.assertEqual([f.rule for f in analyze(workflow(nodes, links))], [rule])

    def test_fixture_rename_preserves_source_and_workflow_metadata(self):
        data = load_workflow(FIXTURE)
        original = deepcopy(data)
        self.assertEqual(analyze(data), [])
        changed = rename(data, 'A', 'Customer / 雪')
        self.assertEqual(data, original)
        self.assertEqual(analyze(changed), [])
        self.assertEqual(changed['nodes'][1]['parameters']['jsCode'],
                         original['nodes'][1]['parameters']['jsCode'].replace(
                             "'\\u0041' /* end */", '"Customer / 雪" /* end */'))
        self.assertEqual(changed['nodes'][2]['parameters']['text'],
                         '=outside $node?.["A"] {{ $node?.["Customer / 雪"].json }}')
        self.assertEqual(changed['nodes'][0]['id'], original['nodes'][0]['id'])
        self.assertEqual(changed['pinData']['Customer / 雪'], original['pinData']['A'])
        restored = rename(changed, 'Customer / 雪', 'A')
        self.assertEqual(analyze(restored), [])
        self.assertEqual(restored['connections'], original['connections'])
        self.assertEqual(restored['pinData'], original['pinData'])

    def test_computed_key_blocks_whole_rename_without_writes(self):
        data = load_workflow(FIXTURE)
        data['nodes'][1]['parameters']['jsCode'] += '\n$node?.[name].json;'
        original = deepcopy(data)
        self.assertEqual([f.rule for f in analyze(data)], ['R007'])
        with self.assertRaises(WorkflowError):
            rename(data, 'A', 'New Name')
        self.assertEqual(data, original)
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source, output = Path(tmp) / 'input.json', Path(tmp) / 'output.json'
            source.write_text(json.dumps(data), encoding='utf-8')
            before = source.read_bytes()
            for destination in [('-o', str(output)), ('--in-place',)]:
                with self.subTest(destination=destination):
                    result = subprocess.run([sys.executable, '-m', 'flowwrench.n8n_refs', 'rename', str(source),
                                             '--from', 'A', '--to', 'New Name', *destination],
                                            cwd=ROOT, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(source.read_bytes(), before)
                    self.assertFalse(output.exists())

    def test_comparison_matches_optional_and_plain_bracket_reference_identity(self):
        baseline = workflow([node('Use', '={{ $node["Missing"].json }}')])
        candidate = deepcopy(baseline)
        candidate['nodes'][0]['parameters']['text'] = '={{ $node?.["Missing"].json }}'
        result = compare(baseline, candidate)
        self.assertTrue(result['complete'])
        self.assertEqual([len(result[k]) for k in ('new', 'resolved', 'unchanged')], [0, 0, 1])
        self.assertEqual(result['unchanged'][0]['candidate']['rule'], 'R001')


if __name__ == '__main__':
    unittest.main()
