"""Empty source tokens must never implicitly select the current directory."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main


class SourcePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exports = self.root / 'exports'
        self.exports.mkdir()
        self.source = self.exports / 'jobs.crontab'
        self.source.write_text('CRON_TZ=UTC\n0 0 * * * true\n')
        self.other = self.exports / 'other.crontab'
        self.other.write_text('CRON_TZ=UTC\n5 0 * * * true\n')
        self.snapshot = self.root / 'saved.json'
        code, out, err = self.cli('extract', self.exports, '--format', 'snapshot')
        self.assertEqual(code, 0, err)
        self.snapshot.write_text(out)
        self.durations = self.root / 'durations.json'
        self.durations.write_text('{"version":1,"durations_seconds":{}}')
        previous = Path.cwd()
        os.chdir(self.exports)
        self.addCleanup(os.chdir, previous)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def empty_cases(self):
        horizon = ['--from', '2026-11-01', '--days', '1']
        lists = [ [''], ['', self.source], [self.source, ''],
                  [self.source, '', self.other], ['', ''] ]
        for command in ('extract', 'analyze', 'dst', 'suggest', 'overlap'):
            modes = [[]]
            if command == 'overlap':
                modes = [['--durations', self.durations]]
            elif command == 'suggest':
                modes += [['--objective', 'concurrency', '--durations', self.durations]]
            for paths in lists:
                for mode in modes:
                    for fmt in ('json', 'text'):
                        yield [command, *paths, *mode, '--format', fmt,
                               *(horizon if command != 'extract' else [])]
        for side, partner in (('before', 'after'), ('after', 'before')):
            groups = [[f'--{side}', *paths] for paths in lists]
            groups += [[f'--{side}='], [f'--{side}=', self.source],
                       [f'--{side}={self.source}', '']]
            for group in groups:
                for other in ([f'--{partner}', self.exports],
                              [f'--{partner}-snapshot', self.snapshot]):
                    for mode in ([], ['--before-durations', self.durations,
                                      '--after-durations', self.durations]):
                        for fmt in ('json', 'text'):
                            yield ['compare', *other, *group, *mode, *horizon, '--format', fmt]

    def assert_empty(self, args):
        code, out, err = self.cli(*args)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def test_empty_source_tokens_are_usage_errors(self):
        # A cwd containing real schedules makes accidental Path('') succeed.
        for args in self.empty_cases():
            with self.subTest(args=args):
                self.assert_empty(args)

    def test_empty_rejection_precedes_all_input_reads_and_path_conversion(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('source read')), \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('duration read')), \
                patch('flowwrench.schedules.extract.Path', side_effect=AssertionError('Path conversion')), \
                patch('flowwrench.schedules.extract.os.walk', side_effect=AssertionError('directory scan')):
            for args in self.empty_cases():
                with self.subTest(args=args):
                    self.assert_empty(args)

    def test_explicit_dot_multiple_paths_and_deduplication(self):
        code, out, err = self.cli('extract', '.', '--format', 'json')
        self.assertEqual(code, 0, err)
        expected = json.loads(out)
        code, out, err = self.cli('extract', self.source.name, self.other.name,
                                  self.source.name, '--format', 'json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), expected)
        self.assertEqual(len(expected['schedules']), 2)

    def test_whitespace_filename_is_not_trimmed(self):
        source = self.exports / ' .crontab'
        source.write_text('CRON_TZ=UTC\n0 0 * * * true\n')
        code, out, err = self.cli('extract', source.name, '--format', 'json')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)['schedules']), 1)
