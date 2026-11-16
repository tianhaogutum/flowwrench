"""Manifest syntax belongs to original strings/mappings, never supplied data."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.formats import LocatedValidationError, yaml_text
from flowwrench.huginn.relocation import combine
from .test_core import ROOT
from .test_relocation import EXAMPLE, example

MALFORMED = 'malformed placeholder; use ${HSK:NAME}'
KEY = 'placeholders in mapping keys are not supported'
INTRODUCED = 'variable values may not introduce placeholders (rendering is single-pass)'


class ManifestDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.variables = self.path / 'vars.json'

    def prepare(self, version, fmt, value, *, guids=False, bindings=None):
        manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
        if guids:
            manifest['guids']['right']['feed'] = value
            pointer = '/guids/right/feed'
        else:
            manifest['scenario']['extra'] = {'a/b': {'~key': [value]}}
            pointer = '/scenario/extra/a~1b/~0key/0'
        self.manifest = self.path / ('manifest.' + fmt)
        self.manifest.write_text(json.dumps(manifest) if fmt == 'json' else yaml_text(manifest))
        self.variables.write_text(json.dumps({**example('vars.yaml'), **(bindings or {})}))
        return pointer

    def cli(self, *args, strict=True):
        return subprocess.run([sys.executable, '-m', 'flowwrench.huginn', 'combine',
            str(EXAMPLE / 'left.json'), str(EXAMPLE / 'right.json'),
            '--manifest', str(self.manifest), '--vars', str(self.variables),
            *(['--reject-unused-vars'] if strict else []), *map(str, args)],
            cwd=ROOT, capture_output=True, text=True, timeout=10)

    def snapshot(self):
        return {p: p.read_bytes() for root in (self.path, EXAMPLE)
                for p in root.rglob('*') if p.is_file()}

    def refusal(self, pointer, reason, *args, strict=True):
        before = self.snapshot()
        result = self.cli(*args, strict=strict)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, f'error: {self.manifest.absolute()}#{pointer}: {reason}\n')
        self.assertNotIn('SENSITIVE_SENTINEL', result.stderr)
        self.assertNotIn('INSERTED_NAME', result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_original_malformed_values_locate_strings_v1_v2_yaml_json(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for guids in (False, True):
                    with self.subTest(version=version, fmt=fmt, guids=guids):
                        pointer = self.prepare(version, fmt, 'SENSITIVE_SENTINEL ${HSK:bad space}', guids=guids)
                        self.refusal(pointer, MALFORMED)

    def test_original_reserved_keys_locate_containing_mapping(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for token in ('${HSK:INSERTED_NAME}', '${HSK:bad SENSITIVE_SENTINEL}'):
                    for guids in (False, True):
                        with self.subTest(version=version, fmt=fmt, token=token, guids=guids):
                            pointer = self.prepare(version, fmt, {'SENSITIVE_SENTINEL ' + token: 1}, guids=guids)
                            self.refusal(pointer, KEY)
                # Direct GUID source keys and direct scenario keys are also reserved.
                for section, pointer in (('guids', '/guids/left'), ('scenario', '/scenario')):
                    self.prepare(version, fmt, 1)
                    manifest = json.loads(json.dumps(example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')))
                    target = manifest['guids']['left'] if section == 'guids' else manifest['scenario']
                    target['${HSK:bad SENSITIVE_SENTINEL}'] = 1
                    self.manifest.write_text(json.dumps(manifest) if fmt == 'json' else yaml_text(manifest))
                    self.refusal(pointer, KEY)

    def test_introduced_values_and_keys_locate_original_substitution_string(self):
        cases = [('SENSITIVE_SENTINEL ${HSK:INSERTED_NAME}', INTRODUCED),
                 ('SENSITIVE_SENTINEL ${HSK:bad space}', MALFORMED),
                 ({'INSERTED_NAME': [0, {'nested': '${HSK:INSERTED_NAME}'}]}, INTRODUCED),
                 ({'INSERTED_NAME': [0, {'nested': '${HSK:bad space}'}]}, MALFORMED),
                 ({'INSERTED_NAME': [{'${HSK:INSERTED_NAME}': 'SENSITIVE_SENTINEL'}]}, KEY),
                 ({'INSERTED_NAME': [{'${HSK:bad SENSITIVE_SENTINEL}': 1}]}, KEY)]
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for guids in (False, True):
                    for value, reason in cases:
                        with self.subTest(version=version, fmt=fmt, guids=guids, reason=reason, value=value):
                            pointer = self.prepare(version, fmt, '${HSK:VALUE}', guids=guids,
                                                   bindings={'VALUE': value})
                            self.refusal(pointer, reason)

    def test_embedded_introduced_and_assembled_tokens_keep_string_origin(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for tail, reason in (('K:INSERTED_NAME}', INTRODUCED), ('K:bad space}', MALFORMED)):
                    pointer = self.prepare(version, fmt, 'SENSITIVE_SENTINEL ${HSK:A}${HSK:B}',
                                           bindings={'A': '${HS', 'B': tail})
                    self.refusal(pointer, reason)
                pointer = self.prepare(version, fmt, 'prefix ${HSK:A}',
                                       bindings={'A': 'SENSITIVE_SENTINEL ${HSK:INSERTED_NAME}'})
                self.refusal(pointer, INTRODUCED)

    def test_failures_precede_outputs_dry_run_and_existing_destinations(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for value, bindings, reason in (
                    ('SENSITIVE_SENTINEL ${HSK:}', {}, MALFORMED),
                    ({'${HSK:SENSITIVE_SENTINEL}': 1}, {}, KEY),
                    ('${HSK:VALUE}', {'VALUE': {'INSERTED_NAME': ['${HSK:bad space}']}}, MALFORMED)):
                    pointer = self.prepare(version, fmt, value, bindings=bindings)
                    for existing in (False, True):
                        for dry in (False, True):
                            output = self.path / f'output-{existing}-{dry}.json'
                            if existing:
                                output.write_bytes(b'PRESERVE_EXISTING_BYTES\n')
                            for strict in (False, True):
                                with self.subTest(version=version, fmt=fmt, existing=existing, dry=dry, strict=strict):
                                    self.refusal(pointer, reason, '-o', output,
                                                 *(['--dry-run'] if dry else []), strict=strict)

    def test_library_error_attributes_no_source_and_input_immutability(self):
        for value, bindings, reason in (
                ('${HSK:bad space}', {}, MALFORMED),
                ({'${HSK:SENSITIVE_SENTINEL}': 1}, {}, KEY),
                ('${HSK:VALUE}', {'VALUE': {'INSERTED_NAME': ['${HSK:NEXT}']}, 'NEXT': 'SENSITIVE_SENTINEL'}, INTRODUCED)):
            manifest = example('manifest.yaml')
            manifest['scenario']['extra'] = {'a/b': {'~key': [value]}}
            inputs = (example('left.json'), example('right.json'), manifest,
                      {**example('vars.yaml'), **bindings})
            before = deepcopy(inputs)
            for source in (self.path / 'original.yaml', None):
                with self.subTest(source=source, reason=reason), self.assertRaises(LocatedValidationError) as raised:
                    combine(*inputs, manifest_source=source)
                self.assertEqual(raised.exception.source, str(source) if source else None)
                self.assertEqual(raised.exception.pointer, '/scenario/extra/a~1b/~0key/0')
                self.assertEqual(raised.exception.reason, reason)
                self.assertNotIn('INSERTED_NAME', str(raised.exception))
                self.assertEqual(inputs, before)


if __name__ == '__main__':
    unittest.main()
