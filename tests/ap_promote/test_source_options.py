"""Apply selects one explicit source before processing any promotion inputs."""
import contextlib
import hashlib
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


class SourceOptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='source-options-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        # Leading/trailing spaces are part of the literal filename.
        self.source, self.mapping, self.catalog, self.pieces = [self.work / n for n in
            (' source flow.json ', 'map.json', 'connections.json', 'pieces.json')]
        for path, fixture in ((self.source, 'support-flow.json'), (self.mapping, 'map.json'),
                              (self.catalog, 'target-connections-versioned.json'),
                              (self.pieces, 'target-pieces.json')):
            path.write_bytes((FIXTURE / fixture).read_bytes())
        self.plan, self.output = [self.work / n for n in ('plan.json', 'output.json')]
        self.expected = self.source.read_bytes().replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')

    def config(self, gated):
        return ['--map-file', str(self.mapping), '--target-connections', str(self.catalog),
                *(['--target-pieces', str(self.pieces)] if gated else [])]

    def args(self, gated, dry=False):
        return ['apply', str(self.plan), *self.config(gated), '-o', str(self.output), '--json',
                *(['--dry-run'] if dry else [])]

    def cli(self, args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def make_plan(self, gated):
        return self.cli(['plan', str(self.source), *self.config(gated), '-o', str(self.plan), '--json'])

    def snapshot(self):
        return {p: p.read_bytes() for p in self.work.iterdir() if p.is_file()}

    def test_repeated_sources_reject_all_forms_and_preserve_inputs_plan_and_output(self):
        other, malformed, missing = [self.work / n for n in ('other.json', 'malformed.json', 'missing.json')]
        other.write_bytes(self.source.read_bytes())
        malformed.write_bytes(b'invalid source CREDENTIAL_SENTINEL')
        for gated in (False, True):
            self.make_plan(gated)
            self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
            before = self.snapshot()
            for dry in (False, True):
                for first in (str(self.source), str(other), str(missing), str(malformed), ''):
                    for flags in (['--source', first, '--source', str(self.source)],
                                  ['--source=' + first, '--source=' + str(self.source)],
                                  ['--source', first, '--source=' + str(self.source)],
                                  ['--source=' + first, '--source', str(self.source)]):
                        with self.subTest(gated=gated, dry=dry, flags=flags):
                            # Restore the same valid fixture after each pre-fix failure.
                            for path, content in before.items():
                                path.write_bytes(content)
                            result = self.cli([*self.args(gated, dry), *flags], code=2)
                            self.assertEqual(result.stdout, '')
                            problem = 'may only be supplied once' if first else 'expected nonempty path'
                            self.assertIn('argument --source: ' + problem, result.stderr)
                            self.assertNotIn('SENTINEL', result.stderr)
                            self.assertEqual(self.snapshot(), before)
                            self.assertEqual(set(self.work.iterdir()), set(before))

    def test_invalid_source_selection_never_executes_even_with_invalid_inputs(self):
        self.source.write_bytes(b'invalid source CREDENTIAL_SENTINEL')
        self.mapping.unlink()
        before = self.snapshot()
        for dry in (False, True):
            for flags in (['--source', 'first.json', '--source=last.json'],
                          ['--source=first.json', '--source', 'last.json'],
                          ['--source', '', '--source=last.json'],
                          ['--source=', '--source', 'last.json'],
                          ['--source', ''], ['--source=']):
                with self.subTest(dry=dry, flags=flags), patch('flowwrench.ap_promote.cli.run') as run:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*self.args(True, dry), *flags])
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()
                    self.assertEqual(out.getvalue(), '')
                    self.assertIn('argument --source:', err.getvalue())
                    self.assertNotIn('SENTINEL', err.getvalue())
                    self.assertEqual(self.snapshot(), before)

    def test_empty_source_paths_are_usage_errors_and_preserve_prior_artifacts(self):
        for gated in (False, True):
            self.make_plan(gated)
            self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
            before = self.snapshot()
            for dry in (False, True):
                for flags in (['--source', ''], ['--source=']):
                    with self.subTest(gated=gated, dry=dry, flags=flags):
                        result = self.cli([*self.args(gated, dry), *flags], code=2)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('argument --source: expected nonempty path', result.stderr)
                        self.assertNotIn('SENTINEL', result.stderr)
                        self.assertEqual(self.snapshot(), before)

    def test_single_literal_source_hashes_required_error_and_stale_refusal(self):
        config_hashes = ('a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb',
                         'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')
        source_bytes = self.source.read_bytes()
        for gated in (False, True):
            self.source.write_bytes(source_bytes)
            plan = json.loads(self.make_plan(gated).stdout)
            saved = self.plan.read_bytes()
            self.assertEqual(plan['sourceSha256'], hashlib.sha256(source_bytes).hexdigest())
            self.assertEqual(plan['outputSha256'], hashlib.sha256(self.expected).hexdigest())
            self.assertEqual(plan['configSha256'], config_hashes[gated])
            for flags in (['--source', str(self.source)], ['--source=' + str(self.source)]):
                for dry in (False, True):
                    self.output.write_bytes(b'prior output')
                    result = self.cli([*self.args(gated, dry), *flags])
                    report = json.loads(result.stdout)
                    self.assertTrue(report['planVerified'])
                    self.assertEqual(report['dryRun'], dry)
                    self.assertEqual(report.get('pieceCatalogVerified', False), gated)
                    self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
                    if not dry:
                        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
            before = self.snapshot()
            for dry in (False, True):
                result = self.cli(self.args(gated, dry), code=2)
                self.assertIn('required: --source', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(self.snapshot(), before)
            self.source.write_bytes(source_bytes + b'\n')
            before = self.snapshot()
            for dry in (False, True):
                result = self.cli([*self.args(gated, dry), '--source', str(self.source)], code=1)
                self.assertIn('stale source', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.plan.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
