"""An empty reviewed-plan path is a usage error before current inputs are read."""
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


class ReviewedPlanPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='plan-path-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.source, self.mapping, self.catalog, self.pieces = [self.work / n for n in
            ('flow.json', 'map.json', 'connections.json', 'pieces.json')]
        for path, fixture in ((self.source, 'support-flow.json'), (self.mapping, 'map.json'),
                              (self.catalog, 'target-connections-versioned.json'),
                              (self.pieces, 'target-pieces.json')):
            path.write_bytes((FIXTURE / fixture).read_bytes())
        self.plan = self.work / ' reviewed plan.json '
        self.output = self.work / 'output.json'
        self.source_bytes = self.source.read_bytes()
        self.expected = self.source_bytes.replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')

    def config(self, gated):
        return ['--map-file', str(self.mapping), '--target-connections', str(self.catalog),
                *(['--target-pieces', str(self.pieces)] if gated else [])]

    def args(self, path, gated=True, dry=False, equals=False):
        source = ['--source=' + str(self.source)] if equals else ['--source', str(self.source)]
        return ['apply', path, *source, *self.config(gated), '-o', str(self.output), '--json',
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

    def assert_usage(self, stdout, stderr):
        self.assertEqual(stdout, '')
        self.assertTrue(stderr.startswith('usage: flowwrench ap-promote apply '), stderr)
        self.assertIn('argument file: expected nonempty path', stderr)
        self.assertNotIn('SENTINEL', stderr)
        self.assertNotIn('refused:', stderr)

    def test_empty_plan_path_preserves_inputs_reviewed_artifact_and_output(self):
        for gated in (False, True):
            self.make_plan(gated)
            self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
            before = self.snapshot()
            for dry in (False, True):
                for equals in (False, True):
                    with self.subTest(gated=gated, dry=dry, equals=equals):
                        result = self.cli(self.args('', gated, dry, equals), code=2)
                        self.assert_usage(result.stdout, result.stderr)
                        self.assertEqual(self.snapshot(), before)
                        self.assertEqual(set(self.work.iterdir()), set(before))

    def test_empty_plan_path_never_dispatches_even_with_invalid_or_missing_inputs(self):
        self.make_plan(True)
        self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
        original = self.snapshot()
        cases = [('valid', None), ('omitted-mapping', None), ('omitted-required-options', None)]
        cases += [(kind, path) for path in (self.source, self.mapping, self.catalog, self.pieces)
                  for kind in ('malformed', 'missing')]
        for kind, path in cases:
            for p, content in original.items():
                p.write_bytes(content)
            if kind == 'malformed':
                path.write_bytes(b'invalid input CREDENTIAL_SENTINEL')
            elif kind == 'missing':
                path.unlink()
            before = self.snapshot()
            for dry in (False, True):
                for equals in (False, True):
                    argv = self.args('', dry=dry, equals=equals)
                    if kind == 'omitted-mapping':
                        i = argv.index('--map-file')
                        del argv[i:i + 2]
                    elif kind == 'omitted-required-options':
                        argv = ['apply', '', *(['--dry-run'] if dry else [])]
                    with self.subTest(kind=kind, path=path, dry=dry, equals=equals), \
                            patch('flowwrench.ap_promote.cli.run', return_value=0) as run, \
                            patch('flowwrench.ap_promote.cli.Flow.load') as flow_load, \
                            patch('flowwrench.ap_promote.cli.Document.load') as doc_load, \
                            patch('flowwrench.ap_promote.cli.atomic_write') as write:
                        out, err = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            with self.assertRaises(SystemExit) as error:
                                main(argv)
                        self.assertEqual(error.exception.code, 2)
                        run.assert_not_called()
                        flow_load.assert_not_called()
                        doc_load.assert_not_called()
                        write.assert_not_called()
                        self.assert_usage(out.getvalue(), err.getvalue())
                        self.assertEqual(self.snapshot(), before)
                        self.assertEqual(set(self.work.iterdir()), set(before))

    def test_literal_plan_path_hashes_omission_and_verification_remain_compatible(self):
        config_hashes = ('a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb',
                         'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')
        for gated in (False, True):
            saved = json.loads(self.make_plan(gated).stdout)
            plan_bytes, map_bytes = self.plan.read_bytes(), self.mapping.read_bytes()
            self.assertEqual(saved['sourceSha256'], hashlib.sha256(self.source_bytes).hexdigest())
            self.assertEqual(saved['configSha256'], config_hashes[gated])
            self.assertEqual(saved['outputSha256'], hashlib.sha256(self.expected).hexdigest())
            for dry in (False, True):
                for equals in (False, True):
                    self.output.write_bytes(b'prior output')
                    result = self.cli(self.args(str(self.plan), gated, dry, equals))
                    report = json.loads(result.stdout)
                    self.assertTrue(report['planVerified'])
                    self.assertEqual(report['dryRun'], dry)
                    self.assertEqual(report.get('pieceCatalogVerified', False), gated)
                    self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
                    if not dry:
                        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
                before = self.snapshot()
                argv = self.args(str(self.plan), gated, dry)
                del argv[1]
                result = self.cli(argv, code=2)
                self.assertEqual(result.stdout, '')
                self.assertIn('required: file', result.stderr)
                self.assertEqual(self.snapshot(), before)
            for problem in ('stale source', 'stale configuration', 'plan contents differ'):
                if problem == 'stale source':
                    self.source.write_bytes(self.source_bytes + b'\n')
                elif problem == 'stale configuration':
                    value = json.loads(map_bytes)
                    value['gmail-dev'] = 'gmail-other'
                    self.mapping.write_text(json.dumps(value))
                else:
                    value = dict(saved, outputSha256='tampered')
                    self.plan.write_text(json.dumps(value))
                before = self.snapshot()
                for dry in (False, True):
                    result = self.cli(self.args(str(self.plan), gated, dry), code=1)
                    self.assertEqual(result.stdout, '')
                    self.assertIn(problem, result.stderr)
                    self.assertEqual(self.snapshot(), before)
                self.source.write_bytes(self.source_bytes)
                self.mapping.write_bytes(map_bytes)
                self.plan.write_bytes(plan_bytes)


if __name__ == '__main__':
    unittest.main()
