"""Optional JS dot contract: A -> ["Customer / 雪"], with all trivia intact.

$node /* lookup */ ?. /* key */ A.json becomes
$node /* lookup */ ?. /* key */ ["Customer / 雪"].json. Only the identifier
span changes; offsets still identify that original plain ASCII name. R001-R005
apply as for bracket lookups. Unsupported identifiers, computed keys, split ?.
and Python proxies produce R007 and block rename before either output mode writes.
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
FIXTURE = ROOT / 'fixtures/rename-optional-dot.json'
REFUSED_JS = [
    r'$node?.\u0041.json', r'$node?.A\u0042.json', '$node?.雪.json',
    '$node?.A\u200cB.json', '$node?.A\u200dB.json', '$node?.A\u0301.json',
    '$node?.A雪.json', '$node?.1.json', '$node?.', '$node?..A.json',
    '$node? .A.json', '$node? /* gap */ .A.json', '$node?\n.A.json',
    '$node?.[name].json', '$node?.["A" + suffix].json', '$node?.[`A${suffix}`].json',
    '$node?.A[property]', '$node?.A?.[property]',
]
REFUSED_PYTHON = ['$node?.A.json', '_node?.A.json', '_node.A.json', '_node?.["A"].json']


class OptionalDotTests(unittest.TestCase):
    def test_ascii_names_original_spans_and_paired_semantics(self):
        source = 'return $node?.A.json + $node?.$A1.json + $node?._A.json + $node?.default.json;'
        refs = scan(source)
        self.assertEqual([r.name for r in refs], ['A', '$A1', '_A', 'default'])
        self.assertEqual([source[r.start:r.end] for r in refs], ['A', '$A1', '_A', 'default'])
        self.assertTrue(all(r.paired and r.syntax == '$node' for r in refs))
        self.assertEqual(refs[0].start, source.index('A.json'))

    def test_only_identifier_changes_with_trivia_and_mixed_references(self):
        source = '''// $node?.A is inert
const note = "$node?.A"; const rx = /$node\\?\\.A/;
const t = `$node?.A ${$node?.A.json}`;
return $node /* lookup */ ?. /* key */ A.json + $node?.A?.json
  + $node?.["A"].json + $node.A.json + $node['A'].json + $node?.Other.json
  + obj.$node?.A + obj?.$node?.A;'''
        for new in ['Customer / 雪', 'quote\'"\\😀\u2028\u2029']:
            with self.subTest(new=new):
                quoted = json.dumps(new, ensure_ascii=False).replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
                expected = source.replace('${$node?.A.json}', '${$node?.[' + quoted + '].json}')
                expected = expected.replace('/* key */ A.json', '/* key */ [' + quoted + '].json')
                expected = expected.replace('$node?.A?.json', '$node?.[' + quoted + ']?.json')
                expected = expected.replace('$node?.["A"]', '$node?.[' + quoted + ']')
                expected = expected.replace('$node.A.json', '$node[' + quoted + '].json')
                expected = expected.replace("$node['A']", '$node[' + quoted + ']')
                changed = replace_references(source, scan(source), 'A', new)
                self.assertEqual(changed, expected)
                self.assertEqual([r.name for r in scan(changed)], [new] * 6 + ['Other'])
                restored = replace_references(changed, scan(changed), new, 'A')
                self.assertEqual([r.name for r in scan(restored)], ['A'] * 6 + ['Other'])

    def test_expression_islands_and_object_properties_stay_inert(self):
        source = '=outside $node?.A {{ $node /* lookup */ ?. /* key */ A.json }} / {{ obj?.$node?.A }}'
        refs = scan(source, expression=True)
        self.assertEqual([r.name for r in refs], ['A'])
        self.assertEqual(refs[0].start, source.index('A.json'))
        self.assertEqual(replace_references(source, refs, 'A', 'Customer / 雪'),
                         source.replace('/* key */ A.json', '/* key */ ["Customer / 雪"].json'))
        self.assertEqual(scan('obj.$node?.A; obj?.$node?.A; obj.$node?.[name];'), [])

    def test_refused_syntax_in_source_and_expression_islands(self):
        for source in REFUSED_JS:
            for expression in [False, True]:
                with self.subTest(source=source, expression=expression), self.assertRaises(ScanError):
                    scan('={{ ' + source + ' }}' if expression else source, expression=expression)
        for source in REFUSED_PYTHON:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source, language='python')

    def test_r001_to_r005_keep_original_pointer_identifier_offset_and_pairing(self):
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
            for expression in [False, True]:
                with self.subTest(rule=rule, expression=expression):
                    source = f'return ($node /* lookup */ ?. /* key */ {name})?.json;'
                    field = 'jsCode'
                    nodes[-1]['type'] = 'n8n-nodes-base.code'
                    if expression:
                        source = f'=text {{{{ ($node /* lookup */ ?. /* key */ {name})?.json }}}}'
                        field = 'a/~'
                        nodes[-1]['type'] = 'n8n-nodes-base.set'
                    nodes[-1]['parameters'] = {field: source, 'mode': 'runOnceForEachItem'}
                    finding, = analyze(workflow(nodes, links))
                    pointer = f'/nodes/{len(nodes) - 1}/parameters/' + ('a~1~0' if expression else field)
                    self.assertEqual((finding.rule, finding.reference, finding.pointer, finding.offset),
                                     (rule, name, pointer, source.index(name + ')')))

    def test_fixture_rename_roundtrip_and_metadata(self):
        data = load_workflow(FIXTURE)
        original = deepcopy(data)
        self.assertEqual(analyze(data), [])
        changed = rename(data, 'A', 'Customer / 雪')
        self.assertEqual(data, original)
        self.assertEqual(analyze(changed), [])
        expected = deepcopy(original)
        expected['nodes'][0]['name'] = 'Customer / 雪'
        expected['connections']['Customer / 雪'] = expected['connections'].pop('A')
        expected['pinData']['Customer / 雪'] = expected['pinData'].pop('A')
        code = expected['nodes'][1]['parameters']['jsCode']
        expected['nodes'][1]['parameters']['jsCode'] = code.replace('/* key */ A', '/* key */ ["Customer / 雪"]')
        expected['nodes'][2]['parameters']['text'] = '=outside $node?.A {{ $node?.["Customer / 雪"].json }}'
        self.assertEqual(changed, expected)
        restored = rename(changed, 'Customer / 雪', 'A')
        self.assertEqual(analyze(restored), [])
        self.assertEqual(restored['connections'], original['connections'])
        self.assertEqual(restored['pinData'], original['pinData'])

    def test_refusals_block_whole_rename_and_both_cli_write_modes(self):
        for source, language in [(s, 'js') for s in REFUSED_JS] + [(s, 'python') for s in REFUSED_PYTHON]:
            with self.subTest(source=source, language=language):
                data = load_workflow(FIXTURE)
                # Put a supported reference in a separate field to ensure an
                # unsupported field blocks the entire workflow, not just itself.
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

    def test_comparison_identity_matches_plain_and_optional_brackets(self):
        baseline = workflow([node('Use', '={{ $node["Missing"].json }}')])
        for source in ['={{ $node?.Missing.json }}', '={{ $node /* x */ ?. Missing.json }}']:
            candidate = deepcopy(baseline)
            candidate['nodes'][0]['parameters']['text'] = source
            result = compare(baseline, candidate)
            self.assertTrue(result['complete'])
            self.assertEqual([len(result[k]) for k in ('new', 'resolved', 'unchanged')], [0, 0, 1])
            self.assertEqual(result['unchanged'][0]['candidate']['rule'], 'R001')


if __name__ == '__main__':
    unittest.main()
