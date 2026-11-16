"""A supplied connection catalog selects one nonempty path before file access."""
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
COMMANDS = ('check', 'remap', 'remap-dry-run', 'plan', 'apply', 'apply-dry-run')


class ConnectionOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='connection-options-ap-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source, self.mapping, self.catalog = [self.work / name for name in
                                                  ('flow.json', 'map.json', 'connections.json')]
        for path, fixture in ((self.source, 'support-flow.json'), (self.mapping, 'map.json'),
                              (self.catalog, 'target-connections-versioned.json')):
            path.write_bytes((FIXTURE / fixture).read_bytes())
        self.expected = self.source.read_bytes().replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        self.promoted = self.work / 'promoted.json'
        self.promoted.write_bytes(self.expected)
        self.plan, self.output = self.work / 'plan.json', self.work / 'output.json'

    def args(self, command):
        if command == 'check':
            return ['check', str(self.promoted), '--json']
        config = ['--map-file', str(self.mapping), '--json']
        if command == 'plan':
            return ['plan', str(self.source), *config, '-o', str(self.plan)]
        if command.startswith('remap'):
            return ['remap', str(self.source), *config, '--strict', '-o', str(self.output),
                    *(['--dry-run'] if command.endswith('dry-run') else [])]
        return ['apply', str(self.plan), '--source', str(self.source), *config,
                '-o', str(self.output), *(['--dry-run'] if command.endswith('dry-run') else [])]

    def cli(self, args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def prior_artifacts(self):
        self.cli([*self.args('plan'), '--target-connections', str(self.catalog)])
        self.output.write_bytes(b'previous output CREDENTIAL_SENTINEL')

    def assert_usage_error(self, command, flags, before, problem):
        # Isolate cases on the previous implementation, which may overwrite
        # plan/output when it silently ignores an earlier catalog selection.
        for path in (self.plan, self.output):
            path.write_bytes(before[path])
        result = self.cli([*self.args(command), *flags], code=2)
        self.assertEqual(result.stdout, '')
        self.assertIn('argument --target-connections: ' + problem, result.stderr)
        self.assertNotIn('SENTINEL', result.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_repeated_catalogs_never_hide_a_path_or_write(self):
        self.prior_artifacts()
        second = self.work / 'second.json'
        second.write_bytes(self.catalog.read_bytes())
        bad = self.work / 'bad.json'
        bad.write_text('{"data":"HIDDEN_NAME_SENTINEL","value":{"token":"CREDENTIAL_SENTINEL"}}')
        missing = self.work / 'missing.json'
        before = {p: p.read_bytes() for p in
                  (self.source, self.promoted, self.mapping, self.catalog, second, bad, self.plan, self.output)}
        cases = ((self.catalog, second), (self.catalog, self.catalog),
                 (missing, self.catalog), (bad, self.catalog), (self.catalog, missing))
        for first, last in cases:
            for command in COMMANDS:
                for equals in (False, True):
                    with self.subTest(first=first, last=last, command=command, equals=equals):
                        flags = (['--target-connections=' + str(first), '--target-c=' + str(last)]
                                 if equals else ['--target-connections', str(first), '--target-connections', str(last)])
                        self.assert_usage_error(command, flags, before, 'may only be supplied once')
                        self.assertFalse(missing.exists())

    def test_explicit_empty_path_never_disables_gate_or_writes(self):
        self.prior_artifacts()
        before = {p: p.read_bytes() for p in
                  (self.source, self.promoted, self.mapping, self.catalog, self.plan, self.output)}
        for command in COMMANDS:
            for flags in (['--target-connections', ''], ['--target-connections=']):
                with self.subTest(command=command, flags=flags):
                    self.assert_usage_error(command, flags, before, 'expected nonempty path')

    def test_invalid_selection_is_rejected_before_command_execution(self):
        self.source.write_text('invalid flow CREDENTIAL_SENTINEL')
        self.promoted.unlink()
        for command in COMMANDS:
            for flags in (['--target-connections', 'first.json', '--target-c=last.json'],
                          ['--target-connections', ''], ['--target-c=']):
                with self.subTest(command=command, flags=flags), patch('flowwrench.ap_promote.cli.run') as run:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*self.args(command), *flags])
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()
                    self.assertEqual(out.getvalue(), '')
                    self.assertNotIn('SENTINEL', err.getvalue())
                    self.assertIn('argument --target-connections:', err.getvalue())
                    self.assertFalse(self.plan.exists())
                    self.assertFalse(self.output.exists())

    def test_single_literal_path_omission_and_empty_catalog_keep_existing_behavior(self):
        # Literal spaces in a nonempty path and = syntax remain supported.
        spaced = self.work / ' connections with spaces.json '
        spaced.write_bytes(self.catalog.read_bytes())
        for flag in (['--target-connections', str(self.catalog)], ['--target-connections=' + str(spaced)]):
            plan = json.loads(self.cli([*self.args('plan'), *flag]).stdout)
            self.assertEqual(plan['configSha256'], 'a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb')
            self.assertNotIn('targetPiecesSha256', plan)
            saved = self.plan.read_bytes()
            self.assertTrue(json.loads(self.cli([*self.args('apply'), *flag]).stdout)['planVerified'])
            self.assertEqual(self.output.read_bytes(), self.expected)
            for command in ('check', 'remap', 'remap-dry-run', 'apply-dry-run'):
                self.cli([*self.args(command), *flag])
                self.assertEqual(self.output.read_bytes(), self.expected)
                self.assertEqual(self.plan.read_bytes(), saved)
        # Only remap permits omission; it retains the unverified report.
        for command in ('remap', 'remap-dry-run'):
            result = self.cli(self.args(command))
            self.assertFalse(json.loads(result.stdout)['catalogVerified'])
            self.assertEqual(self.output.read_bytes(), self.expected)
        for command in ('check', 'plan', 'apply', 'apply-dry-run'):
            result = self.cli(self.args(command), code=2)
            self.assertEqual(result.stdout, '')
            self.assertIn('required: --target-connections', result.stderr)
        # A real empty catalog is enforced: normal refusal, not a usage error.
        empty = self.work / 'empty.json'
        empty.write_text('[]')
        before = {p: p.read_bytes() for p in (self.plan, self.output)}
        for command in COMMANDS:
            result = self.cli([*self.args(command), '--target-connections', str(empty)], code=1)
            if command == 'check':
                self.assertEqual(len(json.loads(result.stdout)['errors']), 5)
            else:
                self.assertEqual(result.stdout, '')
            self.assertEqual({p: p.read_bytes() for p in before}, before)


if __name__ == '__main__':
    unittest.main()
