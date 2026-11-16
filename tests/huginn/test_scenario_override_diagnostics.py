"""Scenario identity replacements belong to their original manifest fields."""
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

REASONS = {
    'name': 'scenario.name must be a nonempty string',
    'guid': "scenario.guid must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric",
}
TYPES = [True, False, None, 42, 1.5, [], ['SENSITIVE_SENTINEL'],
         {'INSERTED_NAME': ['SENSITIVE_SENTINEL']}]
INVALID = {'name': ['', ' \t\n', *TYPES],
           'guid': ['', 'SENSITIVE_SENTINEL invalid', 'bad/guid', '~bad', 'é', 'a' * 129, *TYPES]}


class ScenarioOverrideDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.inputs = [EXAMPLE / 'left.json', EXAMPLE / 'right.json']
        self.variables = self.path / 'vars.json'

    def prepare(self, version, fmt, field, value, *, supplied=False):
        manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
        manifest['scenario'][field] = '${HSK:VALUE}' if supplied else value
        variables = example('vars.yaml')
        if supplied:
            variables['VALUE'] = value
        self.manifest = self.path / ('manifest.' + fmt)
        self.manifest.write_text(json.dumps(manifest) if fmt == 'json' else yaml_text(manifest))
        self.variables.write_text(json.dumps(variables))
        return manifest, variables

    def snapshot(self):
        return {p: p.read_bytes() for root in (self.path, EXAMPLE)
                for p in root.rglob('*') if p.is_file()}

    def refusal(self, field, *args):
        before = self.snapshot()
        result = subprocess.run([sys.executable, '-m', 'flowwrench.huginn', 'combine',
            *map(str, self.inputs), '--manifest', str(self.manifest), '--vars', str(self.variables),
            *map(str, args)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr,
                         f'error: {self.manifest.absolute()}#/scenario/{field}: {REASONS[field]}\n')
        self.assertNotIn('SENSITIVE_SENTINEL', result.stderr)
        self.assertNotIn('INSERTED_NAME', result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_supplied_collection_regression(self):
        for field in ('guid', 'name'):
            with self.subTest(field=field):
                self.prepare(1, 'json', field, {'INSERTED_NAME': ['SENSITIVE_SENTINEL']}, supplied=True)
                self.refusal(field, '--dry-run')

    def test_original_supplied_and_embedded_values_v1_v2_json_yaml(self):
        for version in (1, 2):
            for fmt in ('json', 'yaml'):
                for field, values in INVALID.items():
                    for value in values:
                        for supplied in (False, True):
                            with self.subTest(version=version, fmt=fmt, field=field,
                                              value=value, supplied=supplied):
                                self.prepare(version, fmt, field, value, supplied=supplied)
                                self.refusal(field, '--reject-unused-vars')
                    # An embedded scalar can make either field invalid. A scalar
                    # null/bool/number in name is valid text, so is not a negative.
                    self.prepare(version, fmt, field, '${HSK:A}${HSK:B}')
                    bindings = example('vars.yaml')
                    bindings.update(A='SENSITIVE_SENTINEL ' if field == 'guid' else ' ', B=' ')
                    self.variables.write_text(json.dumps(bindings))
                    self.refusal(field)

    def test_library_structured_errors_and_immutable_arguments(self):
        for version in (1, 2):
            for field, values in INVALID.items():
                for value in values:
                    for supplied in (False, True):
                        manifest, variables = self.prepare(version, 'json', field, value, supplied=supplied)
                        inputs = (example('left.json'), example('right.json'), manifest, variables)
                        before = deepcopy(inputs)
                        for source in (Path('original.yaml'), None):
                            with self.subTest(field=field, value=value, source=source), self.assertRaises(LocatedValidationError) as raised:
                                combine(*inputs, manifest_source=source)
                            error = raised.exception
                            self.assertEqual(error.source, str(source.absolute()) if source else None)
                            self.assertEqual(error.pointer, '/scenario/' + field)
                            self.assertEqual(error.reason, REASONS[field])
                            self.assertEqual(inputs, before)

    def test_output_protection_all_json_kit_pairs(self):
        kits = []
        for side in ('left', 'right'):
            kit = self.path / (side + '-kit')
            for name, content in split_files(from_export(example(side + '.json'))).items():
                file = kit / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
            kits.append(kit)
        exports = list(self.inputs)
        for pair in ((exports[0], exports[1]), (exports[0], kits[1]),
                     (kits[0], exports[1]), (kits[0], kits[1])):
            self.inputs = pair
            for version in (1, 2):
                for field in ('guid', 'name'):
                    self.prepare(version, 'yaml' if field == 'guid' else 'json', field,
                                 {'INSERTED_NAME': ['SENSITIVE_SENTINEL']}, supplied=True)
                    for existing in (False, True):
                        for dry in (False, True):
                            output = self.path / f'output-{pair[0].name}-{pair[1].name}-{version}-{field}-{existing}-{dry}.json'
                            if existing:
                                output.write_bytes(b'PRESERVE_EXISTING_BYTES\n')
                            for strict in (False, True):
                                with self.subTest(pair=pair, version=version, field=field,
                                                  existing=existing, dry=dry, strict=strict):
                                    self.refusal(field, '-o', output, *(['--dry-run'] if dry else []),
                                                 *(['--reject-unused-vars'] if strict else []))
                                    self.assertEqual(output.exists(), existing)

    def test_prior_validation_precedence_and_name_before_guid(self):
        manifest, variables = self.prepare(2, 'json', 'name', None)
        manifest['scenario']['guid'] = None
        left, right = example('left.json'), example('right.json')

        def error(expected, *, located=None):
            with self.assertRaises(KitError) as raised:
                combine(left, right, manifest, variables, manifest_source=self.manifest)
            self.assertIn(expected, str(raised.exception))
            if located is not None:
                self.assertEqual(isinstance(raised.exception, LocatedValidationError), located)

        manifest['links'][0]['source']['input'] = 'bad'
        error('#/links/0/source/input: input must be concrete left or right')
        manifest['links'] = example('manifest-v2.yaml')['links']
        variables.pop('PREFIX')
        error('missing variables: PREFIX', located=False)
        variables['PREFIX'] = 'copy'
        left['agents'][0]['disabled'] = 1
        error('must be a boolean')
        left['agents'][0]['disabled'] = False
        left['extra'] = 1; right['extra'] = 2
        error('scenario metadata conflicts: extra', located=False)
        del left['extra']; del right['extra']
        manifest['guids']['left']['bad/~key'] = None
        error('manifest.guids.left source must be', located=False)
        del manifest['guids']['left']['bad/~key']
        manifest['guids']['left']['absent'] = None
        error('#/guids/left/absent: manifest.guids.left target must be')
        manifest['guids']['left']['absent'] = 'valid'
        error('#/guids/left/absent: unknown source guid')
        del manifest['guids']['left']['absent']
        manifest['links'][0]['source']['guid'] = 'absent'
        error('#/links/0/source/guid: unknown original guid')
        manifest['links'] = example('manifest-v2.yaml')['links']
        # Final JSON bounds precede metadata checks, even if an override is bad.
        for doc in (left, right):
            doc['agents'][0]['options']['nodes'] = [0] * 51_000
        error('document exceeds depth/node limits', located=False)
        for doc in (left, right):
            del doc['agents'][0]['options']['nodes']
        error('#/scenario/name: ' + REASONS['name'])
        manifest['scenario']['name'] = 'valid'
        error('#/scenario/guid: ' + REASONS['guid'])
        # Duplicate final identity validation remains after scenario metadata.
        manifest['guids']['right']['feed'] = 'feed'
        # V2 added-edge duplicate checks still run earlier than final metadata.
        error('#/control_links/0: duplicate edge after relocation')
        manifest['links'] = []
        manifest['control_links'] = []
        error('#/scenario/guid: ' + REASONS['guid'])
        manifest['scenario']['guid'] = 'valid'
        error('duplicate agent guid: feed', located=False)

    def test_valid_overrides_match_explicit_exports_and_typed_embedding(self):
        for version in (1, 2):
            manifest = example('manifest.yaml' if version == 1 else 'manifest-v2.yaml')
            inputs = (example('left.json'), example('right.json'))
            variables = example('vars.yaml')
            result = combine(*inputs, manifest, variables)
            self.assertEqual(result, example('expected.json' if version == 1 else 'expected-v2.json'))
            manifest['scenario'].update(name='${HSK:NAME}', guid='${HSK:GUID}')
            variables.update(NAME='  Valid Unicode é name  ', GUID='a' * 128)
            actual = combine(*inputs, manifest, variables)
            self.assertEqual(actual, {**result, 'name': variables['NAME'], 'guid': variables['GUID']})
            manifest['scenario']['name'] = '${HSK:A}/${HSK:B}/${HSK:C}'
            variables.update(A=True, B=None, C=1.5)
            actual = combine(*inputs, manifest, variables)
            self.assertEqual(actual['name'], 'true/null/1.5')


if __name__ == '__main__':
    unittest.main()
