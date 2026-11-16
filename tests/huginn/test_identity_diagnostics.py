"""Original identities use occurrence locations, separate from GUID-keyed origins."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, load_scenario, split_files, to_export, validate
from flowwrench.huginn.formats import AgentValidationError, KitError, ReferenceValidationError, write_directory, yaml_text
from flowwrench.huginn.graph import dot
from flowwrench.huginn.relocation import combine
from .test_core import ROOT

INVALID = "agent guid must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric"
DUPLICATE = 'duplicate agent guid'
MISSING = object()


def scenario():
    return dict(schema_version=1, name='Identity test', guid='scenario',
                agents=[dict(guid=g, name=g, type='Agents::ManualEventAgent', options={}, disabled=False)
                        for g in ('z-last', 'a-first', 'm-middle')], links=[], control_links=[])


class IdentityDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.variables = self.path / 'vars.json'; self.variables.write_text('{}')
        self.manifest = self.path / 'manifest.json'
        self.manifest.write_text(json.dumps(dict(relocation_version=1, guids={'left': {}, 'right': {}}, scenario={})))
        self.other = self.path / 'other.json'
        self.other.write_text(json.dumps({**scenario(), 'agents': []}))

    def source(self, kind, value):
        raw = scenario()
        if value is MISSING:
            del raw['agents'][2]['guid']
        else:
            raw['agents'][2]['guid'] = value
        if kind == 'json':
            path = self.path / 'original.json'; path.write_text(json.dumps(raw))
            return path, path, '/agents/2/guid', raw
        path = self.path / 'kit'; path.mkdir(exist_ok=True)
        agents = path / 'agents'; agents.mkdir(exist_ok=True)
        metadata = {k: v for k, v in raw.items() if k not in ('agents', 'links', 'control_links')}
        (path / 'scenario.yaml').write_text(yaml_text(dict(kit_version=1, scenario=metadata, links=[], control_links=[])))
        # Original filename order and canonical GUID order deliberately differ.
        for filename, agent in zip(('00-renamed.yaml', '40-unrelated.yaml', '99-offending.yaml'), raw['agents']):
            (agents / filename).write_text(yaml_text(agent))
        return path, agents / '99-offending.yaml', '/guid', raw

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'flowwrench.huginn', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def snapshot(self):
        return {str(p.relative_to(self.path)): p.read_bytes() for p in self.path.rglob('*') if p.is_file()}

    def refusal(self, args, source, pointer, reason):
        before = self.snapshot()
        r = self.cli(*args)
        self.assertEqual((r.returncode, r.stdout, r.stderr),
                         (2, '', f'error: {source.absolute()}#{pointer}: {reason}\n'))
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_original_occurrence_regression(self):
        for kind in ('json', 'kit'):
            with self.subTest(kind=kind):
                path, source, pointer, _ = self.source(kind, 'z-last')
                self.refusal(['graph', path], source, pointer, DUPLICATE)

    def test_invalid_strings_types_missing_and_structured_library_errors(self):
        for kind in ('json', 'kit'):
            for value in ('', 'SECRET_SENTINEL/invalid', ' white ', '_bad', 'é', 'x'*129,
                          '${HSK:ID}', None, False, 0, 1.5, ['SECRET_SENTINEL'], {'secret': 'SECRET_SENTINEL'}, MISSING):
                with self.subTest(kind=kind, value=value):
                    path, source, pointer, raw = self.source(kind, value)
                    self.refusal(['graph', path], source, pointer, INVALID)
                    before = deepcopy(raw)
                    origins = {'keep': ('unchanged', '')}
                    with self.assertRaises(AgentValidationError) as raised:
                        load_scenario(path, agent_origins=origins)
                    e = raised.exception
                    self.assertEqual((e.source, e.pointer, e.reason), (str(source.absolute()), pointer, INVALID))
                    self.assertEqual(origins, {'keep': ('unchanged', '')})
                    for source_arg in (None, self.path / 'memory-source.json'):
                        with self.assertRaises(AgentValidationError) as raised:
                            from_export(raw, source=source_arg)
                        e = raised.exception
                        self.assertEqual((e.source, e.pointer, e.reason),
                                         (str(source_arg) if source_arg else None, '/agents/2/guid', INVALID))
                    self.assertEqual(raw, before)

    def test_all_consumers_output_protection_and_both_combine_inputs(self):
        for kind in ('json', 'kit'):
            for value, reason in ((['SECRET_SENTINEL'], INVALID), ('z-last', DUPLICATE)):
                path, source, pointer, _ = self.source(kind, value)
                output = self.path / 'output'
                for existing in (False, True):
                    if existing: output.write_text('KEEP OUTPUT')
                    commands = [['graph', path], ['lint', path], ['extract-vars', path],
                                ['diff', path, self.other], ['diff', self.other, path]]
                    for dry in ([], ['--dry-run']):
                        commands += [[('split' if kind == 'json' else 'join'), path, '-o', output, *dry],
                                     ['render', path, '--vars', self.variables, '-o', output, *dry]]
                        for left, right in ((path, self.other), (self.other, path)):
                            commands += [['combine', left, right, '--manifest', self.manifest, '-o', output, *dry]]
                    commands += [['graph', path, '-o', output]]
                    for args in commands:
                        with self.subTest(kind=kind, value=value, args=args, existing=existing):
                            self.refusal(args, source, pointer, reason)
                    if existing: output.unlink()

    def test_duplicate_library_fallback_and_first_repeated_occurrence(self):
        raw = scenario(); raw['agents'] += [deepcopy(raw['agents'][0]), deepcopy(raw['agents'][0])]
        before = deepcopy(raw)
        for side in ('left', 'right'):
            with self.subTest(side=side), self.assertRaises(AgentValidationError) as raised:
                inputs = {'left': {**scenario(), 'agents': []}, 'right': {**scenario(), 'agents': []}}
                inputs[side] = raw
                combine(**inputs, manifest=json.loads(self.manifest.read_text()))
            e = raised.exception
            self.assertEqual((e.source, e.pointer, e.reason), (None, '/agents/3/guid', DUPLICATE))
        self.assertEqual(raw, before)

    def test_precedence_and_final_collisions_keep_existing_diagnostics(self):
        raw = scenario(); raw['agents'][2]['guid'] = 'z-last'
        # Earlier fields, schema and export index parsing keep their precedence.
        raw['agents'][0]['disabled'] = 1
        with self.assertRaises(AgentValidationError) as raised: from_export(raw)
        self.assertEqual(raised.exception.pointer, '/agents/0/disabled')
        raw['schema_version'] = 2
        with self.assertRaisesRegex(KitError, 'unsupported schema_version'): from_export(raw)
        raw['links'] = [{'source': 999, 'receiver': 0}]
        with self.assertRaises(ReferenceValidationError) as raised: from_export(raw)
        self.assertEqual(raised.exception.pointer, '/links/0/source')
        left = scenario(); right = deepcopy(left)
        right['agents'] = [right['agents'][0]]
        manifest = json.loads(self.manifest.read_text())
        with self.assertRaises(KitError) as raised:
            combine(left, right, manifest, left_source=self.other, right_source=self.other)
        self.assertIs(type(raised.exception), KitError)
        self.assertEqual(str(raised.exception), 'duplicate agent guid: z-last')
        # Direct validation has no original occurrence provenance.
        doc = from_export(left); doc['agents'].append(deepcopy(doc['agents'][0]))
        with self.assertRaises(KitError) as raised: validate(doc)
        self.assertIs(type(raised.exception), KitError)

    def test_valid_exact_identity_sorting_and_original_file_reordering(self):
        raw = scenario()
        ids = ('a', 'A', 'a_', 'a-', 'a'*128)
        raw['agents'] = [{**raw['agents'][0], 'guid': g} for g in ids]
        raw['links'] = [{'source': 0, 'receiver': 1}]
        raw['control_links'] = [{'controller': 3, 'control_target': 2}]
        expected = from_export(raw); graph = dot(expected)
        kit = self.path / 'valid-kit'; write_directory(kit, split_files(expected))
        files = sorted((kit / 'agents').glob('*.yaml'))
        contents = [p.read_bytes() for p in files]
        for p in files: p.unlink()
        for i, content in enumerate(reversed(contents)):
            (kit / f'agents/{i}-renamed.yaml').write_bytes(content)
        self.assertEqual(load_scenario(kit), expected)
        self.assertEqual(dot(load_scenario(kit)), graph)
        self.assertEqual(to_export(load_scenario(kit)), to_export(expected))
        self.assertEqual({a['guid'] for a in expected['agents']}, set(ids))


if __name__ == '__main__':
    unittest.main()
