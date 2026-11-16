"""An inventory selector cannot silently discard a previously selected input."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main
from flowwrench.schedules.extract import extract
from flowwrench.schedules.snapshot import load_snapshot


class InventorySelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'first.crontab'
        self.source.write_text('CRON_TZ=UTC\n0 0 * * * true\n')
        self.other = self.root / 'second.crontab'
        self.other.write_text('CRON_TZ=UTC\n5 0 * * * true\n')
        self.snapshot = self.root / 'inventory.json'
        code, out, err = self.cli('extract', self.source, '--format', 'snapshot')
        self.assertEqual(code, 0, err)
        self.snapshot.write_text(out)
        self.durations = self.root / 'durations.json'
        self.durations.write_text('{"version":1,"durations_seconds":{}}')
        self.missing = self.root / 'missing.json'

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_duplicate(self, command, args):
        code, out, err = self.cli(command, *args, '--from', '2026-11-01',
                                  '--days', '1', '--format', 'json')
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('only once', err)
        self.assertNotIn('Traceback', err)

    def snapshot_cases(self):
        for command in ('analyze', 'dst', 'suggest', 'overlap', 'compare'):
            options = ('--before-snapshot', '--after-snapshot') if command == 'compare' else ('--snapshot',)
            for option in options:
                if command == 'compare':
                    other_side = '--after-snapshot' if option == '--before-snapshot' else '--before-snapshot'
                    extra = [other_side, self.snapshot]
                else:
                    extra = ['--durations', self.durations] if command == 'overlap' else []
                for first, last in ((self.snapshot, self.snapshot), (self.missing, self.snapshot),
                                    (self.snapshot, self.missing)):
                    for equals in (False, True):
                        repeated = [f'{option}={first}', f'{option}={last}'] if equals else [option, first, option, last]
                        yield command, repeated + extra

    def test_repeated_snapshot_selectors_are_usage_errors(self):
        for command, args in self.snapshot_cases():
            with self.subTest(command=command, args=args):
                self.assert_duplicate(command, args)

    def test_repeated_comparison_source_groups_are_usage_errors(self):
        for side, other in (('--before', '--after'), ('--after', '--before')):
            for first in (self.source, self.missing):
                with self.subTest(side=side, first=first):
                    self.assert_duplicate('compare', [side, first, side, self.source, other, self.other])

    def test_duplicate_rejection_precedes_inventory_and_sidecar_reads(self):
        # A missing earlier selector must never be hidden by a later valid one.
        with patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')):
            for command, args in self.snapshot_cases():
                with self.subTest(command=command, args=args):
                    self.assert_duplicate(command, args)

    def empty_comparison_cases(self):
        for side, partner in (('before', 'after'), ('after', 'before')):
            option = f'--{side}-snapshot'
            for empty in ([option, ''], [f'{option}=']):
                for other in ([f'--{partner}', self.source],
                              [f'--{partner}-snapshot', self.snapshot]):
                    for durations in ([], ['--before-durations', self.durations,
                                           '--after-durations', self.durations]):
                        for fmt in ('json', 'text'):
                            yield [*empty, *other, *durations, '--format', fmt]

    def assert_empty_comparison(self, args):
        code, out, err = self.cli('compare', *args, '--from', '2026-11-01', '--days', '1')
        self.assertEqual((code, out), (2, ''), err)
        self.assertIn('error:', err)
        self.assertIn('snapshot', err)
        self.assertNotIn('Traceback', err)

    def test_empty_comparison_snapshots_are_input_errors(self):
        for args in self.empty_comparison_cases():
            with self.subTest(args=args):
                self.assert_empty_comparison(args)

    def test_empty_comparison_snapshot_never_falls_back_to_extraction(self):
        def selected_source(paths, default_timezone):
            self.assertIsNotNone(paths, 'snapshot selection must never call extract(None)')
            return extract(paths, default_timezone)

        with patch('flowwrench.schedules.cli.extract', side_effect=selected_source), \
                patch('flowwrench.schedules.cli.load_snapshot', wraps=load_snapshot) as snapshots, \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')) as durations:
            for args in self.empty_comparison_cases():
                with self.subTest(args=args):
                    snapshots.reset_mock()
                    self.assert_empty_comparison(args)
                    snapshots.assert_any_call('')
            durations.assert_not_called()

    def test_single_groups_and_separate_comparison_sides_remain_valid(self):
        horizon = ['--from', '2026-11-01', '--days', '1', '--format', 'json']
        for before, after, count in [
                (['--before-snapshot', self.snapshot], ['--after-snapshot', self.snapshot], 1),
                (['--before', self.source, self.other], ['--after', self.source, self.other], 2),
                (['--before-snapshot', self.snapshot], ['--after', self.source], 1),
                (['--before', self.source], ['--after-snapshot', self.snapshot], 1)]:
            with self.subTest(before=before, after=after):
                code, out, err = self.cli('compare', *before, *after, *horizon)
                self.assertEqual(code, 0, err)
                report = json.loads(out)
                self.assertEqual(report['unchanged_firings'], count)
                self.assertEqual((report['added'], report['removed']), ([], []))
