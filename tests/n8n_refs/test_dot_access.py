"""Static JavaScript $node.Name lookup and span-preserving rename regressions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_refs.core import WorkflowError, analyze, load_workflow, rename
from flowwrench.n8n_refs.scanner import ScanError, replace_references, scan
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures' / 'rename-dot.json'


class DotAccessTests(unittest.TestCase):
    def test_static_identifiers_and_exact_offsets(self):
        source = 'return $node.Fetch.json + $node._A.json + $node.$A1.json + $node.default.json;'
        refs = scan(source)
        self.assertEqual([r.name for r in refs], ['Fetch', '_A', '$A1', 'default'])
        self.assertEqual([source[r.start:r.end] for r in refs], ['Fetch', '_A', '$A1', 'default'])
        self.assertTrue(all(r.paired for r in refs))

    def test_preserve_comments_strings_and_unrelated_references(self):
        source = '''// $node.Fetch
const note = "$node.Fetch"; return $node /* lookup */ . /* name */ Fetch.json + $node.Other.json;'''
        changed = replace_references(source, scan(source), 'Fetch', 'Customer / A')
        self.assertEqual(changed, '''// $node.Fetch
const note = "$node.Fetch"; return $node /* lookup */ [ /* name */ "Customer / A"].json + $node.Other.json;''')
        self.assertEqual([r.name for r in scan(changed)], ['Customer / A', 'Other'])

    def test_mixed_literals_templates_and_escaped_new_name(self):
        source = '''const rx = /$node.Fetch/; const t = `$node.Fetch ${$node.Fetch.json}`;
return $node.Fetch.json + $node['Fetch'].json + $('Fetch').first();'''
        new = 'quote\'"\\雪 😀\u2028\u2029'
        changed = replace_references(source, scan(source), 'Fetch', new)
        self.assertIn('const rx = /$node.Fetch/; const t = `$node.Fetch ${', changed)
        self.assertEqual([r.name for r in scan(changed)], [new] * 4)
        self.assertNotIn('\u2028', changed)
        self.assertNotIn('\u2029', changed)
        restored = replace_references(changed, scan(changed), new, 'Fetch')
        self.assertEqual([r.name for r in scan(restored)], ['Fetch'] * 4)

    def test_expression_islands_and_object_properties(self):
        source = '=outside $node.Fetch {{ $node.Fetch.json }} / {{ obj.$node.Fetch }}'
        refs = scan(source, expression=True)
        self.assertEqual([r.name for r in refs], ['Fetch'])
        self.assertEqual(replace_references(source, refs, 'Fetch', 'New Name'),
                         '=outside $node.Fetch {{ $node["New Name"].json }} / {{ obj.$node.Fetch }}')
        self.assertEqual(scan("obj.$node.Fetch; obj?.$node.Fetch; const s = '$node.Fetch';"), [])

    def test_ambiguous_and_out_of_scope_access_still_refused(self):
        for source in [r'$node.\u0041.json', r'$node.A\u0042.json', '$node.A\u200cB.json',
                       '$node.A\u200dB.json', '$node.A\u0301.json',
                       '$node.雪.json', '$node.1.json', '$node..A', '$node.',
                       '$node[name].json', '$node["Fetch" + suffix].json',
                       '$node.Fetch[property]', 'const lookup = $node;']:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source)
        for source in ['_node.Fetch.json', '$node.Fetch.json']:
            with self.subTest(source=source), self.assertRaises(ScanError):
                scan(source, language='python')

    def test_unknown_name_location_and_paired_hazard(self):
        source = '=value {{ $node.Missing.json }}'
        data = workflow([node('Fetch'), node('Use', source)], [('Fetch', 'Use')])
        finding, = analyze(data)
        self.assertEqual((finding.rule, finding.reference, finding.pointer, finding.offset),
                         ('R001', 'Missing', '/nodes/1/parameters/text', source.index('Missing')))
        data = workflow([node('Fetch'), node('Merge', kind='merge'), node('Use', '={{ $node.Fetch.json }}')],
                        [('Fetch', 'Merge'), ('Merge', 'Use')])
        self.assertEqual([f.rule for f in analyze(data)], ['R004'])

    def test_fixture_rename_roundtrip_does_not_mutate_input(self):
        data = load_workflow(FIXTURE)
        original = deepcopy(data)
        self.assertEqual(analyze(data), [])
        changed = rename(data, 'Fetch', 'Customer / A')
        self.assertEqual(data, original)
        self.assertEqual(analyze(changed), [])
        self.assertIn('$node /* lookup */ [ /* name */ "Customer / A"].json',
                      changed['nodes'][1]['parameters']['jsCode'])
        self.assertEqual(changed['pinData']['Customer / A'], original['pinData']['Fetch'])
        restored = rename(changed, 'Customer / A', 'Fetch')
        self.assertEqual(analyze(restored), [])
        self.assertEqual(restored['connections'], original['connections'])
        self.assertEqual(restored['pinData'], original['pinData'])

    def test_ambiguous_reference_blocks_whole_rename(self):
        data = workflow([node('Fetch'), node('Use', '= {{ $node.Fetch.json + $node[name].json }}')],
                        [('Fetch', 'Use')])
        original = deepcopy(data)
        self.assertEqual([f.rule for f in analyze(data)], ['R007'])
        with self.assertRaises(WorkflowError):
            rename(data, 'Fetch', 'Customer / A')
        self.assertEqual(data, original)

    def test_cli_output_and_no_write_on_ambiguity(self):
        def run(*args):
            return subprocess.run([sys.executable, '-m', 'flowwrench.n8n_refs', *map(str, args)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=15)
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as tmp:
            source, output = Path(tmp) / 'source.json', Path(tmp) / 'output.json'
            source.write_bytes(FIXTURE.read_bytes())
            before = source.read_bytes()
            result = run('rename', source, '--from', 'Fetch', '--to', 'Customer / A', '-o', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(run('check', output, '--fail-on', 'warning').returncode, 0)
            data = json.loads(before)
            data['nodes'][1]['parameters']['jsCode'] += '\n$node[name].json;'
            source.write_text(json.dumps(data))
            before = source.read_bytes()
            result = run('rename', source, '--from', 'Fetch', '--to', 'New', '--in-place')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), before)
            refused = Path(tmp) / 'refused.json'
            result = run('rename', source, '--from', 'Fetch', '--to', 'New', '-o', refused)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(refused.exists())
