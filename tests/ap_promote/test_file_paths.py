"""Single source FILE arguments validate before dispatch or local side effects."""
import contextlib
import hashlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.ap_promote.cli import main

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'
COMMANDS = ('remap', 'plan', 'check', 'pieces', 'scrub')


def empty_cases(work):
    """Orthogonal optional-piece coverage plus all map/connection state pairs."""
    paths = {state: str(work / (state + '.json')) for state in ('valid', 'missing', 'malformed')}
    maps = [[], *[['--map-file', paths[s]] for s in paths],
            ['--map', 'slack-dev=slack-prod'], ['--map', 'not-a-mapping']]
    connections = [[], *[['--target-connections', paths[s]] for s in paths]]
    pieces = [[], *[['--target-pieces', paths[s]] for s in paths]]
    outputs = [str(work / 'prior.output.json'), str(work / 'absent-parent' / 'new.json')]
    for output, report in itertools.product(outputs, ([], ['--json'])):
        for mapping, catalog in itertools.product(maps, connections):
            for dry in ([], ['--dry-run']):
                yield ['remap', '', *mapping, *catalog, '-o', output, *dry, *report]
            yield ['plan', '', *mapping, *catalog, '--target-pieces', paths['valid'],
                   '-o', output, *report]
        for catalog in pieces:
            yield ['plan', '', '--map-file', paths['valid'],
                   '--target-connections', paths['valid'], *catalog, '-o', output, *report]
    for report in ([], ['--json']):
        for catalog in connections:
            yield ['check', '', *catalog, *report]
        for catalog in pieces:
            yield ['pieces', '', *catalog, *report]
    for mode in ([], ['--in-place'], *[['-o', p] for p in outputs]):
        for dry in ([], ['--dry-run']):
            yield ['scrub', '', *mode, *dry]
    # The source diagnostic also precedes otherwise missing required options.
    for command in COMMANDS:
        yield [command, '']


def snapshot(work):
    return {str(p.relative_to(work)): (p.read_bytes() if p.is_file() else None, p.stat().st_mode)
            for p in (work, *work.rglob('*'))}


class FilePathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='file-path-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        for name, data, mode in (
                ('valid.json', b'{}', 0o640),
                ('malformed.json', b'invalid JSON CONTENT_SENTINEL', 0o600),
                ('reviewed.plan.json', b'{"prior":"CONTENT_SENTINEL"}', 0o600),
                ('prior.output.json', b'prior output CONTENT_SENTINEL', 0o644)):
            p = self.work / name
            p.write_bytes(data)
            p.chmod(mode)
        (self.work / 'empty-directory').mkdir(mode=0o700)

    def assert_usage(self, command, out, err):
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('usage: flowwrench ap-promote ' + command + ' '), err)
        self.assertIn('argument file: expected nonempty path', err)
        for forbidden in ('CONTENT_SENTINEL', 'Traceback', 'refused:', 'error: cannot read'):
            self.assertNotIn(forbidden, err)

    def test_empty_file_never_dispatches_reads_reports_or_writes(self):
        for argv in empty_cases(self.work):
            before = snapshot(self.work)
            with self.subTest(argv=argv), \
                    patch('flowwrench.ap_promote.cli.run', return_value=0) as run, \
                    patch('flowwrench.ap_promote.cli.Flow.load') as flow_load, \
                    patch('flowwrench.ap_promote.cli.Document.load') as doc_load, \
                    patch('flowwrench.ap_promote.cli.atomic_write') as write, \
                    patch('flowwrench.ap_promote.cli.dump') as dump, \
                    patch('flowwrench.ap_promote.cli.table') as table:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as error:
                        main(argv)
                self.assertEqual(error.exception.code, 2)
                for mock in (run, flow_load, doc_load, write, dump, table):
                    mock.assert_not_called()
                self.assert_usage(argv[0], out.getvalue(), err.getvalue())
                self.assertEqual(snapshot(self.work), before)

    def cli(self, argv, code=0, cwd=None):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *map(str, argv)],
                                cwd=cwd or ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def config(self, command):
        f = FIXTURES
        if command in ('remap', 'plan'):
            return ['--map-file', str(f / 'map.json'), '--target-connections',
                    str(f / 'target-connections-versioned.json'), '-o', str(self.work / 'prior.output.json')]
        if command == 'check':
            return ['--target-connections', str(f / 'target-connections-versioned.json')]
        if command == 'pieces':
            return ['--target-pieces', str(f / 'target-pieces.json')]
        return []

    def test_empty_file_subprocess_preserves_bytes_modes_and_directory_entries(self):
        # Use genuinely valid map/connection/piece files in the valid state.
        # Other matrix paths are missing/malformed, so no parsing should occur.
        for argv in empty_cases(self.work):
            for flag, fixture in (('--map-file', 'map.json'),
                                  ('--target-connections', 'target-connections-versioned.json'),
                                  ('--target-pieces', 'target-pieces.json')):
                if flag in argv and argv[argv.index(flag) + 1] == str(self.work / 'valid.json'):
                    argv[argv.index(flag) + 1] = str(FIXTURES / fixture)
            before = snapshot(self.work)
            with self.subTest(argv=argv):
                result = self.cli(argv, 2)
                self.assert_usage(argv[0], result.stdout, result.stderr)
                self.assertEqual(snapshot(self.work), before)

    def test_nonempty_literal_filenames_preserve_normal_reports_bytes_and_hashes(self):
        source = (FIXTURES / 'support-flow.json').read_bytes()
        promoted = source.replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        for command in COMMANDS:
            baseline = None
            for name in ('flow.json', ' flow.json ', ' ', '  '):
                path = self.work / name
                path.write_bytes(promoted if command == 'check' else source)
                config = self.config(command)
                # Assert raw relative whitespace strings survive argparse unchanged.
                with patch('flowwrench.ap_promote.cli.run', return_value=0) as run:
                    self.assertEqual(main([command, name, *config]), 0)
                    self.assertEqual(run.call_args.args[0].file, name)
                result = self.cli([command, path, *config, *([] if command == 'scrub' else ['--json'])])
                observed = result.stdout, result.stderr
                if baseline is None:
                    baseline = observed
                self.assertEqual(observed, baseline)
                self.assertEqual(path.read_bytes(), promoted if command == 'check' else source)
                if command in ('remap', 'plan'):
                    report = json.loads(result.stdout)
                    output = self.work / 'prior.output.json'
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    if command == 'remap':
                        self.assertEqual(report['rewritten'], 5)
                        self.assertEqual(output.read_bytes(), promoted)
                    else:
                        self.assertEqual(json.loads(output.read_bytes()), report)
                        self.assertEqual(report['sourceSha256'], hashlib.sha256(source).hexdigest())
                        self.assertEqual(report['outputSha256'], hashlib.sha256(promoted).hexdigest())
                        self.assertEqual(report['configSha256'], 'a4935b92f8628a763697038af21f67c9568134e174e1f3bf4a9f16ae39608cbb')
                elif command == 'check':
                    self.assertEqual(json.loads(result.stdout), {'references': 5, 'errors': [], 'ok': True})
                elif command == 'pieces':
                    self.assertEqual(len(json.loads(result.stdout)), 6)
                    self.assertTrue(all(r['status'] == 'available' for r in json.loads(result.stdout)))
                else:
                    self.assertEqual(json.loads(result.stderr)['count'], 4)
                    self.assertEqual(len(json.loads(result.stdout)['flows']), 1)

    def test_omitted_missing_and_malformed_nonempty_paths_keep_safe_diagnostics(self):
        for command in COMMANDS:
            config = self.config(command)
            for report in ([], ['--json']) if command != 'scrub' else ([],):
                result = self.cli([command, *config, *report], 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('the following arguments are required: file', result.stderr)
                for state in ('missing', 'malformed'):
                    before = snapshot(self.work)
                    result = self.cli([command, self.work / (state + '.json'), *config, *report], 2)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, ('error: cannot read UTF-8 input (FileNotFoundError)' if state == 'missing'
                                                    else 'error: invalid JSON at offset 0') + '\n')
                    self.assertEqual(snapshot(self.work), before)


if __name__ == '__main__':
    unittest.main()
