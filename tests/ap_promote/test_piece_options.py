"""A catalog flag selects exactly one file; repetition must never hide a file."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.ap_promote.cli import main

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures'


class PieceOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='piece-options-ap-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'flow.json'
        self.source.write_bytes((FIXTURE / 'support-flow.json').read_bytes())
        self.catalog = self.work / 'pieces.json'
        self.catalog.write_bytes((FIXTURE / 'target-pieces.json').read_bytes())
        self.second = self.work / 'second.json'
        self.second.write_bytes(self.catalog.read_bytes())
        self.plan = self.work / 'plan.json'
        self.output = self.work / 'output.json'

    def args(self, command):
        if command == 'pieces':
            return ['pieces', str(self.source), '--json']
        config = ['--map-file', str(FIXTURE / 'map.json'), '--target-connections',
                  str(FIXTURE / 'target-connections-versioned.json'), '--json']
        if command == 'plan':
            return ['plan', str(self.source), *config, '-o', str(self.plan)]
        return ['apply', str(self.plan), '--source', str(self.source), *config,
                '-o', str(self.output), *(['--dry-run'] if command == 'dry-run' else [])]

    def cli(self, args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_repeated_catalog_options_never_select_a_file_or_write(self):
        # Both catalogs can be valid and identical: repeated selection itself
        # is ambiguous. An ignored missing/malformed catalog is not acceptable.
        self.cli([*self.args('plan'), '--target-pieces', str(self.catalog)])
        self.output.write_bytes(b'previous output')
        bad = self.work / 'bad.json'
        bad.write_text('[{"name":"HIDDEN_NAME_SENTINEL","pieceName":"p",'
                       '"version":"1.2.3","value":{"token":"CREDENTIAL_SENTINEL"}}]')
        missing = self.work / 'missing.json'
        inputs = {p: p.read_bytes() for p in
                  (self.source, self.catalog, self.second, bad, self.plan, self.output)}
        cases = [(self.catalog, self.second), (self.catalog, self.catalog),
                 (missing, self.catalog), (bad, self.catalog), (self.catalog, missing),
                 ('', self.catalog)]
        for first, second in cases:
            for command in ('pieces', 'plan', 'apply', 'dry-run'):
                for equals in (False, True):
                    with self.subTest(first=first, second=second, command=command, equals=equals):
                        # Mix full/abbreviated spellings and =/separate values.
                        flags = (['--target-pieces=' + str(first), '--target-p=' + str(second)]
                                 if equals else ['--target-pieces', str(first), '--target-pieces', str(second)])
                        result = self.cli([*self.args(command), *flags], code=2)
                        self.assertEqual(result.stdout, '')
                        problem = 'expected nonempty path' if first == '' else 'may only be supplied once'
                        self.assertIn('argument --target-pieces: ' + problem, result.stderr)
                        self.assertNotIn('SENTINEL', result.stderr)
                        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)
                        self.assertFalse(missing.exists())

    def test_repetition_is_rejected_before_command_execution(self):
        # This also covers absent/invalid source and plan files: input errors
        # must not obscure the CLI usage error or cause any input processing.
        self.source.write_text('invalid flow')
        for command in ('pieces', 'plan', 'apply', 'dry-run'):
            args = [*self.args(command), '--target-pieces', 'first.json',
                    '--target-pieces', 'second.json']
            with self.subTest(command=command), patch('flowwrench.ap_promote.cli.run') as run:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as error:
                        main(args)
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()
                self.assertEqual(out.getvalue(), '')
                self.assertIn('may only be supplied once', err.getvalue())
                self.assertFalse(self.plan.exists())
                self.assertFalse(self.output.exists())

    def test_single_and_omitted_catalogs_keep_existing_plan_and_availability(self):
        # Compare real single-catalog invocations to the reviewed pre-change
        # fixture digests; omitting it keeps the existing connection-only gate.
        single = ['--target-pieces', str(self.catalog)]
        plan = json.loads(self.cli([*self.args('plan'), *single]).stdout)
        self.assertEqual(plan['targetPiecesSha256'], '2f7248f3c2a008f773b402f918b2f56cf9b0b49e305f1b904b25310b0f533606')
        self.assertEqual(plan['configSha256'], 'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')
        self.assertTrue(json.loads(self.cli([*self.args('apply'), *single]).stdout)['planVerified'])
        self.assertTrue(all(r['status'] == 'available' for r in
                            json.loads(self.cli([*self.args('pieces'), *single]).stdout)))
        self.assertTrue(all(r['status'] == 'pinned' for r in
                            json.loads(self.cli(self.args('pieces')).stdout)))
        legacy = json.loads(self.cli(self.args('plan')).stdout)
        self.assertNotIn('targetPiecesSha256', legacy)
        self.assertNotIn('pieceAvailability', legacy)
        self.assertTrue(json.loads(self.cli(self.args('apply')).stdout)['planVerified'])
        expected = self.source.read_bytes().replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        self.assertEqual(self.output.read_bytes(), expected)

    def test_explicit_empty_catalog_path_never_disables_gate_or_writes(self):
        # An empty argument is a supplied but invalid path, not omission.
        # Test both reviewed-plan forms: the legacy form used to accept the
        # empty flag and the gated form failed only after processing inputs.
        for gated in (False, True):
            flags = ['--target-pieces', str(self.catalog)] if gated else []
            self.cli([*self.args('plan'), *flags])
            self.output.write_bytes(b'previous output CREDENTIAL_SENTINEL')
            inputs = {p: p.read_bytes() for p in
                      (self.source, self.catalog, self.plan, self.output,
                       FIXTURE / 'map.json', FIXTURE / 'target-connections-versioned.json')}
            for command in ('pieces', 'plan', 'apply', 'dry-run'):
                for empty in (['--target-pieces', ''], ['--target-pieces=']):
                    with self.subTest(gated=gated, command=command, empty=empty):
                        # Isolate cases even on the old implementation, which
                        # can overwrite the reviewed plan or output.
                        for path in (self.plan, self.output):
                            path.write_bytes(inputs[path])
                        result = self.cli([*self.args(command), *empty], code=2)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('argument --target-pieces: expected nonempty path', result.stderr)
                        self.assertNotIn('SENTINEL', result.stderr)
                        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)

    def test_empty_path_is_rejected_before_command_execution(self):
        # Invalid/absent source and plan must not obscure the usage error.
        self.source.write_text('invalid flow CREDENTIAL_SENTINEL')
        for command in ('pieces', 'plan', 'apply', 'dry-run'):
            for empty in (['--target-pieces', ''], ['--target-p=']):
                with self.subTest(command=command, empty=empty), patch('flowwrench.ap_promote.cli.run') as run:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*self.args(command), *empty])
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()
                    self.assertEqual(out.getvalue(), '')
                    self.assertIn('expected nonempty path', err.getvalue())
                    self.assertNotIn('SENTINEL', err.getvalue())
                    self.assertFalse(self.plan.exists())
                    self.assertFalse(self.output.exists())

    def test_real_empty_catalog_is_enforced_and_nonempty_paths_are_literal(self):
        self.cli([*self.args('plan'), '--target-pieces', str(self.catalog)])
        self.output.write_bytes(b'previous output')
        before = {p: p.read_bytes() for p in (self.plan, self.output)}
        empty = self.work / 'empty.json'
        empty.write_text('[]')
        for command in ('pieces', 'plan', 'apply', 'dry-run'):
            with self.subTest(command=command):
                result = self.cli([*self.args(command), '--target-pieces', str(empty)], code=1)
                if command == 'pieces':
                    self.assertTrue(all(r['status'] == 'missing-version' for r in json.loads(result.stdout)))
                else:
                    self.assertEqual(result.stdout, '')
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertEqual(empty.read_text(), '[]')
        # Do not trim or normalize valid path strings as part of this guard.
        spaced = self.work / ' pieces with spaces.json '
        spaced.write_bytes(self.catalog.read_bytes())
        result = self.cli([*self.args('plan'), '--target-pieces', str(spaced)])
        self.assertEqual(self.plan.read_bytes(), before[self.plan])
        self.assertEqual(json.loads(result.stdout)['configSha256'],
                         'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')


if __name__ == '__main__':
    unittest.main()
