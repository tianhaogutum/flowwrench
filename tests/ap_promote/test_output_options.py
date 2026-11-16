"""An invocation selects one output path before any command reads or writes."""
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
COMMANDS = ('remap', 'remap-dry-run', 'plan', 'apply', 'apply-dry-run', 'scrub', 'scrub-dry-run')


class OutputOptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='output-options-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.source, self.mapping, self.catalog, self.pieces = [self.work / n for n in
            ('flow.json', 'map.json', 'connections.json', 'pieces.json')]
        for path, name in ((self.source, 'support-flow.json'), (self.mapping, 'map.json'),
                           (self.catalog, 'target-connections-versioned.json'),
                           (self.pieces, 'target-pieces.json')):
            path.write_bytes((FIXTURE / name).read_bytes())
        self.expected = self.source.read_bytes().replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        self.plan, self.first, self.last = [self.work / n for n in ('plan.json', 'first.json', 'last.json')]

    def args(self, command, gated=False):
        name = command.split('-dry-run')[0]
        config = ['--map-file', str(self.mapping), '--target-connections', str(self.catalog)]
        if gated:
            config += ['--target-pieces', str(self.pieces)]
        if name == 'apply':
            args = [name, str(self.plan), '--source', str(self.source), *config, '--json']
        elif name == 'plan':
            args = [name, str(self.source), *config, '--json']
        elif name == 'remap':
            args = [name, str(self.source), *config, '--strict', '--json']
        else:
            args = [name, str(self.source)]
        return [*args, *(['--dry-run'] if command.endswith('dry-run') else [])]

    def cli(self, args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def prior_artifacts(self):
        self.cli([*self.args('plan'), '-o', str(self.plan)])
        self.first.write_bytes(b'first output CREDENTIAL_SENTINEL')
        self.last.write_bytes(b'last output CREDENTIAL_SENTINEL')
        return {p: p.read_bytes() for p in (self.source, self.mapping, self.catalog,
                                           self.pieces, self.plan, self.first, self.last)}

    def test_repeated_outputs_reject_all_spellings_and_preserve_every_file(self):
        before = self.prior_artifacts()
        missing = self.work / 'absent' / 'output.json'
        link = self.work / 'link.json'
        link.symlink_to(self.first)
        for first in (self.first, self.last, missing, self.source, link):
            # Existing distinct, identical, missing-parent, protected and symlink
            # first paths must never be silently hidden by the final selection.
            for command in COMMANDS:
                for flags in (['-o', str(first), '--output', str(self.last)],
                              ['--output=' + str(first), '-o', str(self.last)],
                              ['-o' + str(first), '--output=' + str(self.last)],
                              ['--output', str(first), '--output=' + str(self.last)]):
                    with self.subTest(command=command, flags=flags):
                        # The pre-fix CLI may overwrite an artifact; isolate the
                        # failing cases so they each receive the same valid plan.
                        for path, content in before.items():
                            path.write_bytes(content)
                        result = self.cli([*self.args(command), *flags], code=2)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('argument -o/--output: may only be supplied once', result.stderr)
                        self.assertNotIn('SENTINEL', result.stderr)
                        self.assertEqual({p: p.read_bytes() for p in before}, before)
                        self.assertFalse(missing.parent.exists())
                        self.assertTrue(link.is_symlink())
                        self.assertEqual(set(self.work.iterdir()), {*before, link})

    def test_bad_output_selection_never_executes_command_even_with_invalid_inputs(self):
        self.source.write_text('invalid flow CREDENTIAL_SENTINEL')
        self.mapping.unlink()
        for command in COMMANDS:
            for flags in (['-o', 'first.json', '--output=last.json'],
                          ['--output=first.json', '-olast.json'],
                          ['-o', ''], ['--output=']):
                with self.subTest(command=command, flags=flags), patch('flowwrench.ap_promote.cli.run') as run:
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*self.args(command), *flags])
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()
                    self.assertEqual(out.getvalue(), '')
                    self.assertIn('argument -o/--output:', err.getvalue())
                    self.assertNotIn('SENTINEL', err.getvalue())
                    self.assertFalse(self.plan.exists())
                    self.assertFalse(self.first.exists())

    def test_empty_output_paths_preserve_prior_artifacts(self):
        before = self.prior_artifacts()
        for command in COMMANDS:
            for flags in (['-o', ''], ['--output=']):
                with self.subTest(command=command, flags=flags):
                    result = self.cli([*self.args(command), *flags], code=2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('argument -o/--output: expected nonempty path', result.stderr)
                    self.assertNotIn('SENTINEL', result.stderr)
                    self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_single_paths_hashes_omission_and_scrub_modes_remain_compatible(self):
        spaced = self.work / ' output with spaces.json '
        hashes = ('a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb',
                  'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')
        for gated in (False, True):
            plan = self.cli([*self.args('plan', gated), '-o', str(self.plan)])
            saved = self.plan.read_bytes()
            self.assertEqual(json.loads(plan.stdout)['configSha256'], hashes[gated])
            self.assertEqual(json.loads(plan.stdout)['outputSha256'], hashlib.sha256(self.expected).hexdigest())
            for flags in (['-o', str(spaced)], ['--output=' + str(spaced)], ['-o' + str(spaced)]):
                result = self.cli([*self.args('apply', gated), *flags])
                self.assertTrue(json.loads(result.stdout)['planVerified'])
                self.assertEqual(spaced.read_bytes(), self.expected)
                self.assertEqual(spaced.stat().st_mode & 0o777, 0o600)
                self.cli([*self.args('plan', gated), '--output=' + str(self.plan)])
                self.assertEqual(self.plan.read_bytes(), saved)
            spaced.write_bytes(b'previous output')
            self.cli([*self.args('apply-dry-run', gated), '--output=' + str(spaced)])
            self.assertEqual(spaced.read_bytes(), b'previous output')
        self.cli([*self.args('remap'), '--output=' + str(spaced)])
        self.assertEqual(spaced.read_bytes(), self.expected)
        for command in ('remap-dry-run', 'apply-dry-run', 'scrub-dry-run'):
            missing = self.work / 'absent' / 'output.json'
            self.cli([*self.args(command, gated=command.startswith('apply')), '-o', str(missing)])
            self.assertFalse(missing.parent.exists())
        for command in ('remap', 'remap-dry-run', 'plan', 'apply', 'apply-dry-run'):
            result = self.cli(self.args(command), code=2)
            self.assertEqual(result.stdout, '')
            self.assertIn('required: -o/--output', result.stderr)
        original = self.source.read_bytes()
        scrubbed = self.cli(self.args('scrub')).stdout.encode('utf-8')
        self.assertEqual(self.source.read_bytes(), original)
        self.cli([*self.args('scrub'), '--output=' + str(spaced)])
        self.assertEqual(spaced.read_bytes(), scrubbed)
        self.cli([*self.args('scrub'), '--in-place'])
        self.assertEqual(self.source.read_bytes(), scrubbed)
        for flags in (['--in-place', '-o', str(spaced)], ['-o', str(spaced), '--in-place']):
            self.cli([*self.args('scrub'), *flags], code=2)
        self.assertEqual(spaced.read_bytes(), scrubbed)
        self.assertEqual(self.cli(self.args('scrub-dry-run')).stdout, '')


if __name__ == '__main__':
    unittest.main()
