"""Strict combine bindings refer to original manifest values, never input fields."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.huginn.core import from_export, split_files
from flowwrench.huginn.formats import KitError, load_data, write_directory, yaml_text
from flowwrench.huginn.relocation import combine
from .test_core import ROOT
from .test_relocation import EXAMPLE, example


class CombineUnusedVariableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.manifest = self.path / 'manifest.yaml'
        self.manifest.write_text(yaml_text(example('manifest.yaml')))
        self.variables = self.path / 'vars.yaml'
        self.variables.write_text(yaml_text(example('vars.yaml')))
        self.left, self.right = EXAMPLE / 'left.json', EXAMPLE / 'right.json'

    def cli(self, *extra, strict=True, variables=None):
        return subprocess.run(
            [sys.executable, '-m', 'flowwrench.huginn', 'combine', str(self.left), str(self.right),
             '--manifest', str(self.manifest), '--vars', str(variables or self.variables),
             *(['--reject-unused-vars'] if strict else []), *map(str, extra)],
            cwd=ROOT, capture_output=True, text=True, timeout=10)

    def assert_refusal(self, result, reason):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'error: ' + reason + '\n')
        self.assertNotIn('SENSITIVE_SENTINEL', result.stderr)

    def test_misspelled_extra_manifest_binding_fixture(self):
        variables = EXAMPLE / 'vars.unused.example.yaml'
        default = self.cli(strict=False, variables=variables)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(json.loads(default.stdout), example('expected.json'))
        output = self.path / 'absent.json'
        self.assert_refusal(self.cli('-o', output, variables=variables), 'unused variables: PREFX')
        self.assertFalse(output.exists())

    def snapshot(self):
        return {p: p.read_bytes() for base in (self.path, EXAMPLE)
                for p in base.rglob('*') if p.is_file()}

    def library(self, manifest, variables=None, **kwargs):
        return combine(example('left.json'), example('right.json'), manifest, variables,
                       reject_unused_vars=True, **kwargs)

    def test_all_used_v1_v2_json_kit_pairs_and_json_yaml_parameters(self):
        kits = {}
        for side in ('left', 'right'):
            kits[side] = self.path / side
            write_directory(kits[side], split_files(from_export(example(side + '.json'))))
        for version in (1, 2):
            manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
            # Repeated, nested, embedded and whole-field typed references all count.
            manifest['scenario']['extra'] = {'a/b': {'~key': ['${HSK:_one.dot-1}',
                'x=${HSK:_one.dot-1}', '${HSK:LIST}', '${HSK:NULL}', '${HSK:MAP}']}}
            bindings = {**example('vars.yaml'), '_one.dot-1': True, 'LIST': [1, False],
                        'NULL': None, 'MAP': {'nested': 2}}
            expected = example('expected.json' if version == 1 else 'expected-v2.json')
            expected['extra'] = {'a/b': {'~key': [True, 'x=true', [1, False], None, {'nested': 2}]}}
            for fmt in ('yaml', 'json'):
                self.manifest.write_text(yaml_text(manifest) if fmt == 'yaml' else json.dumps(manifest))
                self.variables.write_text(yaml_text(bindings) if fmt == 'yaml' else json.dumps(bindings))
                for left_kit, right_kit in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(version=version, fmt=fmt, left_kit=left_kit, right_kit=right_kit):
                        self.left = kits['left'] if left_kit else EXAMPLE / 'left.json'
                        self.right = kits['right'] if right_kit else EXAMPLE / 'right.json'
                        before = self.snapshot()
                        default, strict = self.cli(strict=False), self.cli()
                        self.assertEqual(default.returncode, 0, default.stderr)
                        self.assertEqual(strict.returncode, 0, strict.stderr)
                        self.assertEqual(strict.stdout, default.stdout)
                        target = deepcopy(expected)
                        if left_kit and right_kit:
                            target.pop('exported_at')
                        self.assertEqual(json.loads(strict.stdout), target)
                        self.assertEqual(before, self.snapshot())

    def test_no_hsk_references_empty_bindings_and_sorted_unused_names(self):
        manifest = example('manifest.yaml')
        manifest['guids']['right'] = {'feed': 'copy_feed', 'sink': 'copy_sink'}
        manifest['scenario'] = {'guid': 'combined_demo', 'name': 'Combined demo',
                              'literal': '{{ location }} {% credential weather_key %} ${OTHER:VALUE}'}
        self.manifest.write_text(yaml_text(manifest))
        self.variables.write_text('{}\n')
        strict = self.cli()
        self.assertEqual(strict.returncode, 0, strict.stderr)
        self.assertEqual(strict.stdout, self.cli(strict=False).stdout)
        self.assertEqual(self.library(manifest), json.loads(strict.stdout))
        self.variables.write_text(yaml_text({'weather_key': 'SENSITIVE_SENTINEL',
            'location': {'nested': '${HSK:location}'}, 'OTHER': '${HSK:bad syntax}'}))
        self.assert_refusal(self.cli(), 'unused variables: OTHER, location, weather_key')
        self.assertEqual(self.cli(strict=False).returncode, 0)

    def test_unused_rejection_precedes_output_and_dry_run_and_force_stays_unsupported(self):
        for existing in (False, True):
            for dry in (False, True):
                with self.subTest(existing=existing, dry=dry):
                    output = self.path / f'{existing}-{dry}.json'
                    if existing:
                        output.write_bytes(b'PRESERVE_EXISTING_BYTES\n')
                    self.variables.write_text(yaml_text({**example('vars.yaml'),
                        'Z': {'nested': '${HSK:Z}'}, 'A': 'SENSITIVE_SENTINEL'}))
                    args = ['-o', output, *(['--dry-run'] if dry else [])]
                    before = self.snapshot()
                    self.assert_refusal(self.cli(*args), 'unused variables: A, Z')
                    self.assertEqual(before, self.snapshot())
                    forced = self.cli(*args, '--force')
                    self.assertEqual(forced.returncode, 2)
                    self.assertEqual(forced.stdout, '')
                    self.assertIn('unrecognized arguments: --force', forced.stderr)
                    self.assertEqual(before, self.snapshot())
        # Successful strict output still uses private exclusive creation.
        self.variables.write_text(yaml_text(example('vars.yaml')))
        output = self.path / 'success.json'
        result = self.cli('-o', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        before = self.snapshot()
        result = self.cli('-o', output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(before, self.snapshot())

    def test_missing_malformed_and_key_errors_keep_precedence_in_both_modes(self):
        original = example('manifest.yaml')
        cases = [(original, 'missing variables: ENV, PREFIX'),
                 ({**original, 'scenario': {**original['scenario'], 'x': '${HSK:bad space}'}},
                  f'{self.manifest}#/scenario/x: malformed placeholder; use ${{HSK:NAME}}'),
                 ({**original, 'scenario': {**original['scenario'], '${HSK:KEY}': 'SENSITIVE_SENTINEL'}},
                  f'{self.manifest}#/scenario: placeholders in mapping keys are not supported')]
        for manifest, reason in cases:
            with self.subTest(reason=reason):
                self.manifest.write_text(yaml_text(manifest))
                self.variables.write_text(yaml_text({'UNUSED': 'SENSITIVE_SENTINEL ${HSK:bad space}'}))
                self.assert_refusal(self.cli(), reason)
                self.assert_refusal(self.cli(strict=False), reason)

    def test_invalid_keys_are_generic_and_never_disclosed(self):
        self.variables.write_text(yaml_text({**example('vars.yaml'),
            'bad name SENSITIVE_SENTINEL': 'SENSITIVE_SENTINEL', 'VALID': 1}))
        self.assertEqual(self.cli(strict=False).returncode, 0)
        self.assert_refusal(self.cli(), 'variable names must match [A-Za-z_][A-Za-z0-9_.-]*')
        self.variables.write_text('1: SENSITIVE_SENTINEL\n')
        self.assert_refusal(self.cli(), 'mapping keys must be strings')

    def test_json_source_placeholders_stay_literal_and_never_count_as_references(self):
        doc = example('left.json')
        doc['agents'][0]['options']['literal'] = '${HSK:SOURCE} ${HSK:PREFIX} ${HSK:bad space}'
        self.left = self.path / 'left.json'
        self.left.write_text(json.dumps(doc))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = next(a for a in json.loads(result.stdout)['agents'] if a['guid'] == doc['agents'][0]['guid'])
        self.assertEqual(agent['options'], doc['agents'][0]['options'])
        self.variables.write_text(yaml_text({**example('vars.yaml'), 'SOURCE': 'SENSITIVE_SENTINEL'}))
        self.assert_refusal(self.cli(), 'unused variables: SOURCE')

    def test_kit_placeholders_remain_concrete_errors_even_when_binding_is_supplied(self):
        self.left = self.path / 'left-kit'
        write_directory(self.left, split_files(from_export(example('left.json'))))
        agent_file = next((self.left / 'agents').glob('*.yaml'))
        agent = load_data(agent_file)
        agent['options']['literal'] = '${HSK:SOURCE}'
        agent_file.write_text(yaml_text(agent))
        self.variables.write_text(yaml_text({**example('vars.yaml'), 'SOURCE': 'SENSITIVE_SENTINEL'}))
        before = self.snapshot()
        reason = f'{agent_file}#/options/literal: unresolved kit placeholder (use render before combine)'
        self.assert_refusal(self.cli(), reason)
        self.assert_refusal(self.cli('--dry-run'), reason)
        self.assertEqual(before, self.snapshot())

    def test_original_references_only_single_pass_and_library_immutability(self):
        manifest = example('manifest.yaml')
        manifest['scenario']['extra'] = '${HSK:A}'
        bindings = {**example('vars.yaml'), 'A': '${HSK:B}', 'B': 'SENSITIVE_SENTINEL'}
        before = deepcopy((manifest, bindings))
        with self.assertRaisesRegex(KitError, '^unused variables: B$'):
            self.library(manifest, bindings)
        del bindings['B']
        for value, reason in (('${HSK:A}', 'single-pass'),
                              ('${HSK:bad space}', 'malformed placeholder'),
                              ({'${HSK:KEY}': 'SENSITIVE_SENTINEL'}, 'mapping keys')):
            bindings['A'] = value
            with self.subTest(value=value), self.assertRaisesRegex(KitError, reason) as strict:
                self.library(manifest, bindings)
            with self.assertRaises(KitError) as default:
                combine(example('left.json'), example('right.json'), manifest, bindings)
            self.assertEqual(str(strict.exception), str(default.exception))
            self.assertNotIn('SENSITIVE_SENTINEL', str(strict.exception))
        bindings = before[1]
        self.assertEqual(manifest, before[0])
        bindings.pop('B')
        bindings['A'] = {'nested': [1, False, None]}
        before = deepcopy((manifest, bindings))
        self.assertEqual(self.library(manifest, bindings),
                         combine(example('left.json'), example('right.json'), manifest, bindings))
        self.assertEqual((manifest, bindings), before)

    def test_strict_preserves_manifest_endpoint_and_expansion_boundaries(self):
        manifest = example('manifest-v2.yaml')
        manifest['links'][0]['source']['guid'] = '${HSK:GUID}'
        with self.assertRaisesRegex(KitError, 'invalid concrete original guid'):
            self.library(manifest, {**example('vars.yaml'), 'GUID': 'sink'})
        for value, repeated, reason in (
                ('x' * 3000000, '${HSK:LARGE}${HSK:LARGE}', '5 MiB expansion limit'),
                ([0] * 50000, ['${HSK:LARGE}', '${HSK:LARGE}'], 'depth/node limits')):
            manifest = example('manifest.yaml')
            manifest['scenario']['extra'] = repeated
            with self.subTest(reason=reason), self.assertRaisesRegex(KitError, reason):
                self.library(manifest, {**example('vars.yaml'), 'LARGE': value})


if __name__ == '__main__':
    unittest.main()
