"""Each mapping occurrence is nonempty; repeatable inline/file composition stays valid."""
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

from flowwrench.ap_promote.cli import main, parser

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures'


class MappingOptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mapping-options-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.source, self.mapping, self.catalog, self.pieces = [self.work / n for n in
            ('flow.json', 'map.json', 'connections.json', 'pieces.json')]
        for path, fixture in ((self.source, 'support-flow.json'), (self.mapping, 'map.json'),
                              (self.catalog, 'target-connections-versioned.json'),
                              (self.pieces, 'target-pieces.json')):
            path.write_bytes((FIXTURE / fixture).read_bytes())
        self.plan, self.output, self.destination = [self.work / n for n in
            ('plan.json', 'output.json', 'destination.json')]
        self.source_bytes = self.source.read_bytes()
        self.expected = self.source_bytes.replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        self.destination.write_bytes(self.expected)

    def args(self, command, flags, dry=False, gated=False):
        argv = [command, str(self.plan if command == 'apply' else self.source)]
        if command == 'verify-roundtrip':
            argv += [str(self.destination)]
        else:
            argv += ['-o', str(self.output)]
        if command == 'apply':
            argv += ['--source', str(self.source)]
        if command != 'verify-roundtrip':
            argv += ['--target-connections', str(self.catalog)]
        if gated:
            argv += ['--target-pieces', str(self.pieces)]
        return [*argv, *flags, '--json', *(['--dry-run'] if dry else [])]

    def modes(self):
        for command in ('remap', 'plan', 'apply', 'verify-roundtrip'):
            for gated in ((False, True) if command in ('plan', 'apply') else (False,)):
                for dry in ((False, True) if command in ('remap', 'apply') else (False,)):
                    yield command, dry, gated

    def empty_flags(self, inline=False):
        if inline:
            for empty in (['--map', ''], ['--map=']):
                yield empty
                for valid in (['--map', 'slack-dev=slack-prod'], ['--map-file', str(self.mapping)]):
                    yield [*empty, *valid]
                    yield [*valid, *empty]
            return
        valid = ['--map-file', str(self.mapping)]
        for empty in (['--map-file', ''], ['--map-file=']):
            yield empty
            yield [*empty, *valid]
            yield [*valid, *empty]

    def cli(self, argv, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *argv],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def make_plan(self, gated=False):
        argv = self.args('plan', ['--map-file', str(self.mapping)], gated=gated)
        argv[argv.index('-o') + 1] = str(self.plan)
        return self.cli(argv)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.work.iterdir() if p.is_file()}

    def assert_usage(self, command, out, err, inline=False):
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('usage: flowwrench ap-promote ' + command + ' '), err)
        self.assertIn('argument --map: expected nonempty mapping' if inline else
                      'argument --map-file: expected nonempty path', err)
        self.assertNotIn('SENTINEL', err)
        self.assertNotIn('refused:', err)
        self.assertNotIn('error: invalid JSON', err)

    def test_empty_mapping_paths_never_dispatch_or_read_or_write(self):
        self.assert_empty_mappings_never_dispatch()

    def test_empty_inline_mappings_never_dispatch_or_read_or_write(self):
        self.assert_empty_mappings_never_dispatch(inline=True)

    def assert_empty_mappings_never_dispatch(self, inline=False):
        self.make_plan(True)
        self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
        original = self.snapshot()
        cases = [('valid', None)] + [(kind, path) for path in original
                                     if path != self.output for kind in ('malformed', 'missing')]
        for kind, path in cases:
            for p, content in original.items():
                p.write_bytes(content)
            if kind == 'malformed':
                path.write_bytes(b'invalid input CREDENTIAL_SENTINEL')
            elif kind == 'missing':
                path.unlink()
            before = self.snapshot()
            for command, dry, gated in self.modes():
                for flags in self.empty_flags(inline):
                    with self.subTest(kind=kind, path=path, command=command, dry=dry, gated=gated, flags=flags), \
                            patch('flowwrench.ap_promote.cli.run', return_value=0) as run, \
                            patch('flowwrench.ap_promote.cli.Flow.load') as flow_load, \
                            patch('flowwrench.ap_promote.cli.Document.load') as doc_load, \
                            patch('flowwrench.ap_promote.cli.atomic_write') as write:
                        out, err = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            with self.assertRaises(SystemExit) as error:
                                main(self.args(command, flags, dry, gated))
                        self.assertEqual(error.exception.code, 2)
                        run.assert_not_called()
                        flow_load.assert_not_called()
                        doc_load.assert_not_called()
                        write.assert_not_called()
                        self.assert_usage(command, out.getvalue(), err.getvalue(), inline)
                        self.assertEqual(self.snapshot(), before)

    def test_empty_mapping_paths_preserve_artifacts_with_valid_and_invalid_current_flows(self):
        self.assert_empty_mappings_preserve_artifacts()

    def test_empty_inline_mappings_preserve_artifacts_with_valid_and_invalid_current_flows(self):
        self.assert_empty_mappings_preserve_artifacts(inline=True)

    def assert_empty_mappings_preserve_artifacts(self, inline=False):
        for gated in (False, True):
            self.source.write_bytes(self.source_bytes)
            self.make_plan(gated)
            self.output.write_bytes(b'prior output CREDENTIAL_SENTINEL')
            for kind in ('valid', 'malformed', 'missing'):
                if kind == 'valid':
                    self.source.write_bytes(self.source_bytes)
                elif kind == 'malformed':
                    self.source.write_bytes(b'invalid source CREDENTIAL_SENTINEL')
                else:
                    self.source.unlink()
                before = self.snapshot()
                for command, dry, mode_gated in self.modes():
                    if mode_gated != gated and command in ('plan', 'apply'):
                        continue
                    if gated and command in ('remap', 'verify-roundtrip'):
                        continue
                    for flags in self.empty_flags(inline):
                        with self.subTest(kind=kind, command=command, dry=dry, gated=gated, flags=flags):
                            result = self.cli(self.args(command, flags, dry, mode_gated), code=2)
                            self.assert_usage(command, result.stdout, result.stderr, inline)
                            self.assertEqual(self.snapshot(), before)
                            self.assertEqual(set(self.work.iterdir()), set(before))

    def test_nonempty_literal_mapping_paths_repeat_in_order_and_keep_hashes(self):
        values = json.loads(self.mapping.read_text())
        first, second, duplicate = [self.work / n for n in (' first map.json ', 'last map.json ', ' ')]
        first.write_text(json.dumps({'slack-dev': values['slack-dev']}))
        second.write_text(json.dumps({'gmail-dev': values['gmail-dev']}))
        duplicate.write_text(first.read_text())
        flags = ['--map-file', str(first), '--map-file=' + str(second), '--map-file', str(duplicate)]
        hashes = ('a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb',
                  'd179b34ad24fbb4894bc0ec54402d954fbb1c64db34b1e205c00bde8f9a121ec')
        for gated in (False, True):
            self.make_plan(gated)
            historical = self.plan.read_bytes()
            argv = self.args('plan', flags, gated=gated)
            self.assertEqual(parser().parse_args(argv).map_file, [str(first), str(second), str(duplicate)])
            result = self.cli(argv)
            saved = json.loads(result.stdout)
            self.assertEqual(self.output.read_bytes(), historical)
            self.assertEqual(saved['sourceSha256'], hashlib.sha256(self.source_bytes).hexdigest())
            self.assertEqual(saved['configSha256'], hashes[gated])
            self.assertEqual(saved['outputSha256'], hashlib.sha256(self.expected).hexdigest())
            for dry in (False, True):
                self.output.write_bytes(b'prior output')
                report = json.loads(self.cli(self.args('apply', flags, dry, gated)).stdout)
                self.assertTrue(report['planVerified'])
                self.assertEqual(report['dryRun'], dry)
                self.assertEqual(report.get('pieceCatalogVerified', False), gated)
                self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
        for dry in (False, True):
            self.output.write_bytes(b'prior output')
            report = json.loads(self.cli(self.args('remap', flags, dry)).stdout)
            self.assertEqual(report['rewritten'], 5)
            self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
        report = json.loads(self.cli(self.args('verify-roundtrip', flags)).stdout)
        self.assertEqual((report['verified'], report['errors'], report['ok']), (5, [], True))

    def test_nonempty_inline_mappings_repeat_compose_and_keep_hashes(self):
        # Inline mappings cannot carry piece assertions. Compare composition
        # with an equivalent file-only config retaining the Slack assertion.
        # The all-guarded historical plans are checked separately below.
        values = json.loads(self.mapping.read_text())
        values['gmail-dev'] = 'gmail-prod'
        self.mapping.write_text(json.dumps(values))
        split = self.work / 'slack-only.json'
        split.write_text(json.dumps({'slack-dev': values['slack-dev']}))
        flags = ['--map', 'gmail-dev=gmail-prod', '--map-file', str(split),
                 '--map=gmail-dev=gmail-prod', '--map-file=' + str(split)]
        for gated in (False, True):
            self.make_plan(gated)
            historical = self.plan.read_bytes()
            result = self.cli(self.args('plan', flags, gated=gated))
            self.assertEqual(self.output.read_bytes(), historical)
            saved = json.loads(result.stdout)
            self.assertEqual(saved, json.loads(historical))
            self.assertEqual(saved['sourceSha256'], hashlib.sha256(self.source_bytes).hexdigest())
            self.assertEqual(saved['outputSha256'], hashlib.sha256(self.expected).hexdigest())
            for dry in (False, True):
                self.output.write_bytes(b'prior output')
                report = json.loads(self.cli(self.args('apply', flags, dry, gated)).stdout)
                self.assertTrue(report['planVerified'])
                self.assertEqual(report.get('pieceCatalogVerified', False), gated)
                self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
        # Both inline forms repeat, accept identical duplicates and dispatch
        # their exact values, including Unicode and whitespace, without trimming.
        literal = ['slack-dev=slack-prod', 'gmail-dev=gmail-prod', 'slack-dev=slack-prod',
                   'Unused=Dest=Extra', 'ünused=dest', ' spaced=value ']
        flags = [flag for i, entry in enumerate(literal)
                 for flag in (['--map', entry] if i % 2 == 0 else ['--map=' + entry])]
        for command, dry, gated in self.modes():
            with self.subTest(command=command, dry=dry, gated=gated), \
                    patch('flowwrench.ap_promote.cli.run', return_value=0) as run:
                self.assertEqual(main(self.args(command, flags, dry, gated)), 0)
                self.assertEqual(run.call_args.args[0].map, literal)
        valid = ['--map', 'slack-dev=slack-prod', '--map=gmail-dev=gmail-prod',
                 '--map', 'slack-dev=slack-prod']
        for dry in (False, True):
            self.output.write_bytes(b'prior output')
            report = json.loads(self.cli(self.args('remap', valid, dry)).stdout)
            self.assertEqual(report['rewritten'], 5)
            self.assertEqual(self.output.read_bytes(), b'prior output' if dry else self.expected)
        report = json.loads(self.cli(self.args('verify-roundtrip', valid)).stdout)
        self.assertEqual((report['verified'], report['errors'], report['ok']), (5, [], True))

    def test_nonempty_inline_mapping_errors_and_omissions_are_unchanged(self):
        bad = [(['--map', 'slack-dev=slack-prod', '--map=slack-dev=other'], 'conflicting duplicate mapping'),
               (['--map', 'a=b', '--map=b=c'], 'mapping chains/cycles'),
               (['--map', 'a=b', '--map=b=a'], 'mapping chains/cycles'),
               (['--map', ' '], '--map requires old=new'),
               (['--map', 'not-a-pair'], '--map requires old=new'),
               (['--map', 'slack-dev=slack-prod '], 'connection externalId must be 1..512 characters'),
               (['--map', '=new'], 'connection externalId must be 1..512 characters'),
               (['--map', 'old='], 'connection externalId must be 1..512 characters'),
               ([], 'requires --map or --map-file')]
        for command, dry, gated in self.modes():
            if command == 'apply':
                self.make_plan(gated)
            self.output.write_bytes(b'prior output')
            before = self.snapshot()
            for flags, diagnostic in bad:
                with self.subTest(command=command, dry=dry, gated=gated, flags=flags):
                    result = self.cli(self.args(command, flags, dry, gated), code=2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn(diagnostic, result.stderr)
                    self.assertFalse(result.stderr.startswith('usage:'))
                    self.assertEqual(self.snapshot(), before)

    def test_mapping_conflicts_composition_and_omission_rules_are_unchanged(self):
        split = self.work / 'slack-only.json'
        split.write_text(json.dumps({'slack-dev': 'slack-prod'}))
        for command, dry, gated in self.modes():
            if command == 'apply':
                self.make_plan(gated)
            self.output.write_bytes(b'prior output')
            before = self.snapshot()
            full = ['--map-file', str(self.mapping)]
            for flags in ([], [*full, '--map', 'gmail-dev=other'],
                          [*full, '--map-file', str(split)]):
                result = self.cli(self.args(command, flags, dry, gated), code=2)
                self.assertEqual(result.stdout, '')
                self.assertIn('requires --map or --map-file' if not flags else 'conflicting duplicate mapping', result.stderr)
                self.assertEqual(self.snapshot(), before)
            # Inline entries can complete a file, and identical string entries can repeat.
            flags = ['--map-file', str(split), '--map-file=' + str(split), '--map', 'gmail-dev=gmail-prod']
            if command == 'apply':
                # The effective mapping must match the reviewed piece assertions.
                result = self.cli(self.args(command, flags, dry, gated), code=1)
                self.assertIn('stale configuration', result.stderr)
                self.assertEqual(self.snapshot(), before)
            else:
                self.cli(self.args(command, flags, dry, gated))
        empty = self.work / 'empty.json'
        empty.write_text('{}')
        # Empty mapping objects remain meaningful and distinct from an empty path.
        result = self.cli(['remap', str(self.source), '--map-file', str(empty), '-o', str(self.output), '--json'])
        self.assertEqual(json.loads(result.stdout)['rewritten'], 0)
        self.assertEqual(self.output.read_bytes(), self.source_bytes)


if __name__ == '__main__':
    unittest.main()
