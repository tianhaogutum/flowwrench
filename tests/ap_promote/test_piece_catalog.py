"""Alias consistency and canonical exact-pin catalogs across every consumer."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.ap_promote.core import piece_catalog
from flowwrench.ap_promote.jsondoc import Document, InvalidInput

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures'


class PieceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='alias-ap-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'flow.json'
        self.source.write_bytes((FIXTURE / 'support-flow.json').read_bytes())
        self.connections = self.work / 'connections.json'
        self.connections.write_bytes((FIXTURE / 'target-connections-versioned.json').read_bytes())
        self.catalog = self.work / 'pieces.json'
        self.rows = json.loads((FIXTURE / 'target-pieces.json').read_text())
        self.catalog.write_text(json.dumps(self.rows))
        self.plan = self.work / 'plan.json'
        self.output = self.work / 'output.json'

    def cli(self, command, code=0, extra=()):
        args = ['--target-pieces', str(self.catalog), '--json']
        if command == 'pieces':
            args = [str(self.source), *args]
        else:
            args += ['--map-file', str(FIXTURE / 'map.json'),
                     '--target-connections', str(self.connections)]
            if command == 'plan':
                args = [str(self.source), *args, '-o', str(self.plan)]
            else:
                args = [str(self.plan), '--source', str(self.source), *args, '-o', str(self.output)]
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', command, *args, *extra],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_conflicting_aliases_fail_before_plan_or_output_writes(self):
        # All preferred aliases describe the original valid environment. The
        # former precedence parser silently accepted these contradictory rows.
        self.cli('plan')
        before = self.plan.read_bytes()
        self.output.write_bytes(b'previous output')
        cases = [
            ('name', dict(self.rows[0], pieceName=self.rows[0]['name'], name='NAME_VALUE_SENTINEL')),
            ('version', dict(self.rows[0], pieceVersion=self.rows[0]['version'], version='9.8.7')),
            ('both', dict(self.rows[0], pieceName=self.rows[0]['name'], name='NAME_VALUE_SENTINEL',
                          pieceVersion=self.rows[0]['version'], version='9.8.7')),
            ('unused', dict(name='UNUSED_VALUE_SENTINEL', pieceName='unused', version='1.2.3')),
            ('build', dict(name='unused', version='1.2.3-rc.1+build.007',
                           pieceVersion='1.2.3-rc.1+build.008')),
            ('release', dict(name='unused', version='1.2.3-rc.1', pieceVersion='1.2.3')),
        ]
        for label, bad in cases:
            bad.update(value={'access_token': 'CREDENTIAL_VALUE_SENTINEL'},
                       UNKNOWN_FIELD_SENTINEL='UNKNOWN_VALUE_SENTINEL')
            # Appending an already installed pair also proves duplicates cannot
            # bypass validation, even after its canonical pair was collected.
            rows = [*self.rows, bad]
            for page in (False, True):
                self.catalog.write_text(json.dumps({'data': rows, 'next': None} if page else rows))
                inputs = {p: p.read_bytes() for p in (self.source, self.connections, self.catalog)}
                for command, extra in (('pieces', ()), ('plan', ()), ('apply', ()),
                                       ('apply', ('--dry-run',))):
                    with self.subTest(case=label, page=page, command=command, extra=extra):
                        result = self.cli(command, code=2, extra=extra)
                        field = 'name and pieceName' if label in {'name', 'both', 'unused'} else 'version and pieceVersion'
                        self.assertIn('conflicting piece catalog aliases: ' + field, result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertNotIn('SENTINEL', result.stderr)
                        self.assertNotIn('9.8.7', result.stderr)
                        self.assertNotIn('build.008', result.stderr)
                        self.assertEqual(self.plan.read_bytes(), before)
                        self.assertEqual(self.output.read_bytes(), b'previous output')
                        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)

    def test_each_supplied_alias_must_be_a_nonempty_string(self):
        valid = dict(name='p', pieceName='p', version='1.2.3', pieceVersion='1.2.3')
        for field in valid:
            for invalid in ('', None, 0, True, [], {'token': 'VALUE_SENTINEL'}):
                with self.subTest(field=field, invalid=invalid):
                    row = dict(valid, **{field: invalid})
                    with self.assertRaisesRegex(InvalidInput, 'expected nonempty string field ' + field) as error:
                        piece_catalog(Document(json.dumps([row])))
                    self.assertNotIn('VALUE_SENTINEL', str(error.exception))
        for missing in (('name', 'pieceName'), ('version', 'pieceVersion')):
            row = {k: v for k, v in valid.items() if k not in missing}
            with self.subTest(missing=missing), self.assertRaises(InvalidInput):
                piece_catalog(Document(json.dumps([row])))

    def test_invalid_secondary_aliases_fail_in_all_commands(self):
        self.cli('plan')
        before = self.plan.read_bytes()
        self.output.write_bytes(b'previous output')
        for field, invalid in (('name', None), ('version', ''), ('pieceName', False), ('pieceVersion', [])):
            rows = copy.deepcopy(self.rows)
            rows[0].update(pieceName=rows[0]['name'], pieceVersion=rows[0]['version'])
            rows[0][field] = invalid
            self.catalog.write_text(json.dumps(rows))
            for command in ('pieces', 'plan', 'apply'):
                with self.subTest(field=field, command=command):
                    result = self.cli(command, code=2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('expected nonempty string field ' + field, result.stderr)
                    self.assertEqual(self.plan.read_bytes(), before)
                    self.assertEqual(self.output.read_bytes(), b'previous output')

    def test_equal_and_single_aliases_preserve_exact_pin_hashes_and_availability(self):
        # Exercise prerelease/build pins on every actual step/tool, rather than
        # only an unused target row. Build numeric identifiers may start with 0.
        for row in self.rows:
            old, new = row['version'], row['version'] + '-rc.1+build.007'
            for path in (self.source, self.connections):
                path.write_text(path.read_text().replace(json.dumps(old), json.dumps(new)))
            row['version'] = new
        self.rows += [dict(name='unused-é', version='1.2.3+001'),
                      dict(name='unused-é', version='1.2.3+002')]
        self.catalog.write_text(json.dumps(self.rows))
        first = json.loads(self.cli('plan').stdout)
        first_pieces = json.loads(self.cli('pieces').stdout)
        self.assertTrue(all(r['status'] == 'available' for r in first_pieces))
        canonical = [dict(pieceName=r['name'], pieceVersion=r['version'])
                     for r in sorted(self.rows, key=lambda r: (r['name'], r['version']))]
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(first['targetPiecesSha256'], digest)
        before = self.plan.read_bytes()
        for mode in ('short', 'namespaced', 'mixed-name', 'mixed-version', 'equal'):
            with self.subTest(mode=mode):
                rows = []
                for row in reversed(self.rows):
                    name, version = row['name'], row['version']
                    nkey = 'pieceName' if mode in {'namespaced', 'mixed-name', 'equal'} else 'name'
                    vkey = 'pieceVersion' if mode in {'namespaced', 'mixed-version', 'equal'} else 'version'
                    record = {vkey: version, nkey: name, 'value': {'token': 'CREDENTIAL_SENTINEL'}}
                    if mode == 'equal':
                        record.update(version=version, name=name)
                    rows.append(record)
                rows += copy.deepcopy(rows)  # Exact duplicates have set semantics.
                self.catalog.write_text(json.dumps({'data': rows, 'next': '', 'nextCursor': None},
                                                   ensure_ascii=True, sort_keys=True, indent=3))
                # Apply the original artifact with equivalent aliases first.
                self.assertTrue(json.loads(self.cli('apply').stdout)['planVerified'])
                self.assertEqual(json.loads(self.cli('pieces').stdout), first_pieces)
                result = self.cli('plan')
                self.assertEqual(json.loads(result.stdout), first)
                self.assertEqual(self.plan.read_bytes(), before)
                self.assertNotIn('CREDENTIAL_SENTINEL', result.stdout)
                expected = self.source.read_bytes().replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
                self.assertEqual(self.output.read_bytes(), expected)

    def test_alias_comparison_is_exact_after_json_decoding(self):
        equal = r'[{"name":"p\u00e9","pieceName":"pé","version":"1.2.3-rc.1+007","pieceVersion":"1.2.3-rc.1+007"}]'
        self.assertEqual(piece_catalog(Document(equal)), {('pé', '1.2.3-rc.1+007')})
        for short, full in (('name', 'pieceName'), ('version', 'pieceVersion')):
            for value in ('P' if short == 'name' else '1.2.3+BUILD',
                          'p ' if short == 'name' else '1.2.3+build '):
                row = dict(name='p', pieceName='p', version='1.2.3+build', pieceVersion='1.2.3+build')
                row[short] = value
                with self.subTest(field=short, value=value), self.assertRaisesRegex(InvalidInput, 'conflicting'):
                    piece_catalog(Document(json.dumps([row])))


if __name__ == '__main__':
    unittest.main()
