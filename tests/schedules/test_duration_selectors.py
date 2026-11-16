"""Sidecar selection is single-use and must fail before any input is read."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main


class DurationSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'jobs.crontab'
        self.source.write_text('CRON_TZ=UTC\n0 0 * * * true\n')
        code, out, err = self.cli('extract', self.source, '--format', 'snapshot')
        self.assertEqual(code, 0, err)
        saved = json.loads(out)
        self.snapshot = self.root / 'saved.json'
        self.snapshot.write_text(out)
        self.sidecar = self.root / 'zero.json'
        self.sidecar.write_text(json.dumps({'version': 1, 'durations_seconds': {
            saved['schedules'][0]['id']: 0}}))
        self.other = self.root / 'other.json'
        self.other.write_text(self.sidecar.read_text().replace(': 0', ': 60'))
        self.missing = self.root / 'missing.json'

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def inputs(self):
        for source in ([self.source], ['--snapshot', self.snapshot]):
            yield ['overlap', *source], '--durations', []
            yield ['suggest', *source, '--objective', 'concurrency'], '--durations', []
        for before in (['--before', self.source], ['--before-snapshot', self.snapshot]):
            for after in (['--after', self.source], ['--after-snapshot', self.snapshot]):
                for option, other in (('--before-durations', '--after-durations'),
                                      ('--after-durations', '--before-durations')):
                    yield ['compare', *before, *after], option, [other, self.sidecar]

    def duplicates(self):
        for command, option, extra in self.inputs():
            for first, last in ((self.sidecar, self.sidecar), (self.sidecar, self.other),
                                (self.missing, self.sidecar), (self.sidecar, self.missing),
                                ('', self.sidecar)):
                for first_equals, last_equals in ((False, False), (True, True), (False, True), (True, False)):
                    first_args = [f'{option}={first}'] if first_equals else [option, first]
                    last_args = [f'{option}={last}'] if last_equals else [option, last]
                    yield option, [*command, *first_args, *last_args, *extra]

    def assert_duplicate(self, option, args):
        code, out, err = self.cli(*args, '--from', '2026-11-01', '--days', '1', '--format', 'json')
        self.assertEqual((code, out), (2, ''), err)
        self.assertIn('usage:', err)
        self.assertIn(option, err)
        self.assertIn('only once', err)
        self.assertNotIn('Traceback', err)

    def test_repeated_sidecars_are_usage_errors(self):
        for option, args in self.duplicates():
            with self.subTest(option=option, args=args):
                self.assert_duplicate(option, args)

    def test_duplicate_rejection_precedes_all_input_reads(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')) as exports, \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')) as snapshots, \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')) as durations:
            for option, args in self.duplicates():
                with self.subTest(option=option, args=args):
                    self.assert_duplicate(option, args)
            for loader in (exports, snapshots, durations):
                loader.assert_not_called()

    def test_single_sidecars_equals_and_separate_comparison_sides_preserve_known_zero(self):
        for command, option, extra in self.inputs():
            with self.subTest(command=command, option=option):
                horizon = ['--from', '2026-11-01', '--days', '1', '--format', 'json']
                code, out, err = self.cli(*command, option, self.sidecar, *extra, *horizon)
                self.assertEqual(code, 0, err)
                report = json.loads(out)
                # Ordinary scalars keep last-value-wins; equals and strict must not change the report.
                code, equal, err = self.cli(*command, f'{option}={self.sidecar}', *extra,
                                             '--days', '2', '--format', 'text', *horizon, '--strict')
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(equal), report)
                if command[0] == 'overlap':
                    sides = [report]
                elif command[0] == 'compare':
                    self.assertEqual(report['overlap']['peak_delta'], 0)
                    self.assertEqual(report['unchanged_firings'], 1)
                    sides = [report['overlap'][side] for side in ('before', 'after')]
                else:
                    sides = [report['overlap'][side] for side in ('before', 'after')]
                for side in sides:
                    self.assertTrue(side['complete'])
                    self.assertEqual((side['peak'], side['known_peak'], side['known_firings']), (0, 0, 1))
                    self.assertEqual(side['known_peak_intervals'], [])
                    self.assertEqual(side['unknown_duration_ids'], [])

    def test_unpaired_comparison_sidecars_still_fail_before_reads(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')):
            for option in ('--before-durations', '--after-durations'):
                code, out, err = self.cli('compare', '--before', self.source,
                                          '--after-snapshot', self.snapshot, f'{option}={self.sidecar}')
                self.assertEqual((code, out), (2, ''), err)
                self.assertIn('requires both', err)
