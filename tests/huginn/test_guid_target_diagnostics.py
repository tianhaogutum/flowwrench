"""Invalid relocation targets belong to original manifest mapping values."""
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
from .test_relocation import EXAMPLE, example

INVALID = ['', 'SENSITIVE_SENTINEL invalid', 'bad/target', '~bad', 'é', 'a' * 129,
           True, False, None, 42, 1.5, ['SENSITIVE_SENTINEL'],
           {'INSERTED_NAME': ['SENSITIVE_SENTINEL']}]
REASON = "manifest.guids.{} target must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric"


class GuidTargetDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.manifest = self.path / 'manifest.json'
        self.variables = self.path / 'vars.json'
        self.inputs = [EXAMPLE / 'left.json', EXAMPLE / 'right.json']

    def prepare(self, version, fmt, side, target, *, supplied=False):
        manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
        manifest['guids'][side]['feed'] = '${HSK:TARGET}' if supplied else target
        variables = example('vars.yaml')
        if supplied:
            variables['TARGET'] = target
        self.manifest = self.path / ('manifest.' + fmt)
        self.manifest.write_text(json.dumps(manifest) if fmt == 'json' else yaml_text(manifest))
        self.variables.write_text(json.dumps(variables))
        return manifest, variables

    def refusal(self, side, *args):
        before = {p: p.read_bytes() for root in (self.path, EXAMPLE)
                  for p in root.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, '-m', 'flowwrench.huginn', 'combine',
            *map(str, self.inputs), '--manifest', str(self.manifest), '--vars', str(self.variables),
            *map(str, args)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr,
                         f'error: {self.manifest.absolute()}#/guids/{side}/feed: {REASON.format(side)}\n')
        self.assertNotIn('SENSITIVE_SENTINEL', result.stderr)
        self.assertNotIn('INSERTED_NAME', result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for root in (self.path, EXAMPLE)
                                 for p in root.rglob('*') if p.is_file()})

    def test_original_invalid_targets_v1_v2_yaml_json_left_right(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for side in ('left', 'right'):
                    for target in INVALID:
                        with self.subTest(version=version, fmt=fmt, side=side, target=target):
                            self.prepare(version, fmt, side, target)
                            self.refusal(side)

    def test_supplied_invalid_targets_locate_original_value_not_inserted_paths(self):
        for version in (1, 2):
            for fmt in ('yaml', 'json'):
                for side in ('left', 'right'):
                    for target in INVALID:
                        with self.subTest(version=version, fmt=fmt, side=side, target=target):
                            self.prepare(version, fmt, side, target, supplied=True)
                            self.refusal(side, '--reject-unused-vars')
        # Embedded strings still report the original mapping value.
        self.prepare(2, 'yaml', 'right', 'prefix_${HSK:BAD}', supplied=False)
        variables = example('vars.yaml')
        variables['BAD'] = 'SENSITIVE_SENTINEL invalid'
        self.variables.write_text(json.dumps(variables))
        self.refusal('right')

    def test_refusals_precede_absent_existing_outputs_and_dry_run_for_all_input_pairs(self):
        kits = []
        for side in ('left', 'right'):
            kit = self.path / (side + '-kit')
            for name, content in split_files(from_export(example(side + '.json'))).items():
                file = kit / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
            kits.append(kit)
        exports = list(self.inputs)
        for i, inputs in enumerate(((exports[0], exports[1]), (exports[0], kits[1]),
                                    (kits[0], exports[1]), (kits[0], kits[1]))):
            self.inputs = inputs
            side = 'left' if i % 2 == 0 else 'right'
            self.prepare(1 if i < 2 else 2, 'yaml' if i % 2 else 'json', side,
                         {'INSERTED_NAME': ['SENSITIVE_SENTINEL']}, supplied=i % 2 == 1)
            for existing in (False, True):
                for dry in (False, True):
                    output = self.path / f'output-{i}-{existing}-{dry}.json'
                    if existing:
                        output.write_bytes(b'PRESERVE_EXISTING_BYTES\n')
                    for strict in (False, True):
                        with self.subTest(inputs=i, existing=existing, dry=dry, strict=strict):
                            self.refusal(side, '-o', output, *(['--dry-run'] if dry else []),
                                         *(['--reject-unused-vars'] if strict else []))
                            self.assertEqual(output.exists(), existing)

    def test_library_structured_errors_and_immutability_with_or_without_source(self):
        for version in (1, 2):
            for side in ('left', 'right'):
                for target in INVALID:
                    for supplied in (False, True):
                        manifest, variables = self.prepare(version, 'json', side, target, supplied=supplied)
                        inputs = (example('left.json'), example('right.json'), manifest, variables)
                        before = deepcopy(inputs)
                        for source in (Path('original-manifest.yaml'), None):
                            with self.subTest(version=version, side=side, target=target, supplied=supplied,
                                              source=source), self.assertRaises(LocatedValidationError) as raised:
                                combine(*inputs, manifest_source=source)
                            error = raised.exception
                            self.assertEqual(error.source, str(source.absolute()) if source else None)
                            self.assertEqual(error.pointer, f'/guids/{side}/feed')
                            self.assertEqual(error.reason, REASON.format(side))
                            self.assertEqual(inputs, before)

    def test_source_validation_precedence_and_valid_guid_length_boundary(self):
        # Source grammar excludes slash/tilde; keep that earlier error, rather than
        # inventing a valid source identity to exercise JSON Pointer escaping.
        manifest, variables = self.prepare(1, 'json', 'right', 'copy_feed')
        manifest['guids']['right']['bad/~source'] = None
        with self.assertRaises(KitError) as raised:
            combine(example('left.json'), example('right.json'), manifest, variables)
        self.assertNotIsInstance(raised.exception, LocatedValidationError)
        self.assertIn('manifest.guids.right source must be', str(raised.exception))
        del manifest['guids']['right']['bad/~source']
        manifest['guids']['right']['feed'] = 'a' * 128
        result = combine(example('left.json'), example('right.json'), manifest, variables)
        self.assertIn('a' * 128, {a['guid'] for a in result['agents']})


if __name__ == '__main__':
    unittest.main()
