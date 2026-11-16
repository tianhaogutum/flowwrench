"""Unknown relocation sources belong to original manifest mapping values."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, split_files
from flowwrench.huginn.formats import KitError, LocatedValidationError, yaml_text
from flowwrench.huginn.relocation import combine
from .test_core import ROOT
from .test_relocation import example


class GuidSourceDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.documents = [example('left.json'), example('right.json')]
        # The fixtures already have a right-only "control" identity. Give the
        # left input its own identity too, without changing existing edges.
        agent = deepcopy(self.documents[0]['agents'][0])
        agent['guid'] = 'left_only'
        self.documents[0]['agents'].append(agent)
        self.exports = []
        self.kits = []
        for side, doc in zip(('left', 'right'), self.documents):
            export = self.path / (side + '.json')
            export.write_text(json.dumps(doc))
            self.exports.append(export)
            kit = self.path / (side + '-kit')
            for name, content in split_files(from_export(doc)).items():
                file = kit / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
            self.kits.append(kit)
        self.inputs = self.exports
        self.variables = self.path / 'vars.json'

    def prepare(self, version, fmt, side, source, target_kind='literal'):
        manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
        variables = example('vars.yaml')
        target = 'SENSITIVE_TARGET'
        if target_kind != 'literal':
            variables['TARGET'] = target
            target = '${HSK:TARGET}' + ('_copy' if target_kind == 'embedded' else '')
        manifest['guids'][side][source] = target
        self.manifest = self.path / ('original-manifest.' + fmt)
        self.manifest.write_text(json.dumps(manifest) if fmt == 'json' else yaml_text(manifest))
        self.variables.write_text(json.dumps(variables))
        return manifest, variables

    def refusal(self, side, source, *args):
        before = {p: p.read_bytes() for p in self.path.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, '-m', 'flowwrench.huginn', 'combine',
            *map(str, self.inputs), '--manifest', str(self.manifest), '--vars', str(self.variables),
            *map(str, args)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, f'error: {self.manifest.absolute()}#/guids/{side}/{source}: '
                         f'unknown source guid in manifest.guids.{side}: {source}\n')
        self.assertNotIn('SENSITIVE_TARGET', result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.rglob('*') if p.is_file()})

    def test_unknown_sources_v1_v2_yaml_json_both_sides_and_opposite_input(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for side, opposite in (('left', 'control'), ('right', 'left_only')):
                    for source in ('absent', opposite):
                        for target in ('literal', 'whole', 'embedded'):
                            with self.subTest(version=version, fmt=fmt, side=side,
                                              source=source, target=target):
                                self.prepare(version, fmt, side, source, target)
                                self.refusal(side, source, '--reject-unused-vars')

    def test_refusals_precede_outputs_and_dry_run_for_all_input_pairs(self):
        for i, inputs in enumerate(((self.exports[0], self.exports[1]),
                                    (self.exports[0], self.kits[1]),
                                    (self.kits[0], self.exports[1]), (self.kits[0], self.kits[1]))):
            self.inputs = inputs
            for side, source in (('left', 'control'), ('right', 'left_only')):
                self.prepare(1 if i < 2 else 2, 'yaml' if i % 2 else 'json', side, source, 'whole')
                for existing in (False, True):
                    for dry in (False, True):
                        output = self.path / f'output-{i}-{side}-{existing}-{dry}.json'
                        if existing:
                            output.write_bytes(b'PRESERVE_EXISTING_BYTES\n')
                        for strict in (False, True):
                            with self.subTest(inputs=i, side=side, existing=existing, dry=dry, strict=strict):
                                self.refusal(side, source, '-o', output,
                                             *(['--dry-run'] if dry else []),
                                             *(['--reject-unused-vars'] if strict else []))
                                self.assertEqual(output.exists(), existing)

    def test_library_structured_errors_and_immutability_with_or_without_source(self):
        for version in (1, 2):
            for side, source in (('left', 'control'), ('right', 'left_only'), ('left', 'a' * 128)):
                for target in ('literal', 'whole', 'embedded'):
                    manifest, variables = self.prepare(version, 'json', side, source, target)
                    inputs = (*self.documents, manifest, variables)
                    before = deepcopy(inputs)
                    for filename in (Path('original-manifest.yaml'), None):
                        with self.subTest(version=version, side=side, source=source, target=target,
                                          filename=filename), self.assertRaises(LocatedValidationError) as raised:
                            combine(*inputs, manifest_source=filename)
                        error = raised.exception
                        self.assertEqual(error.source, str(filename.absolute()) if filename else None)
                        self.assertEqual(error.pointer, f'/guids/{side}/{source}')
                        self.assertEqual(error.reason, f'unknown source guid in manifest.guids.{side}: {source}')
                        self.assertEqual(inputs, before)

    def test_source_grammar_then_invalid_target_precede_unknown_source(self):
        for version in (1, 2):
            for side in ('left', 'right'):
                for source in ('bad/~source', '', 'a' * 129):
                    manifest, variables = self.prepare(version, 'json', side, source)
                    manifest['guids'][side][source] = None
                    with self.assertRaises(KitError) as raised:
                        combine(*self.documents, manifest, variables, manifest_source=self.manifest)
                    self.assertNotIsInstance(raised.exception, LocatedValidationError)
                    self.assertIn(f'manifest.guids.{side} source must be', str(raised.exception))
                for target in (None, 'SENSITIVE_TARGET invalid', {'INSERTED_NAME': ['SENSITIVE_TARGET']}):
                    manifest, variables = self.prepare(version, 'json', side, 'absent', 'whole')
                    variables['TARGET'] = target
                    with self.assertRaises(LocatedValidationError) as raised:
                        combine(*self.documents, manifest, variables, manifest_source=self.manifest)
                    self.assertEqual(raised.exception.pointer, f'/guids/{side}/absent')
                    self.assertTrue(raised.exception.reason.startswith(f'manifest.guids.{side} target must be'))
                    self.assertNotIn('SENSITIVE_TARGET', str(raised.exception))
                    self.assertNotIn('INSERTED_NAME', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
