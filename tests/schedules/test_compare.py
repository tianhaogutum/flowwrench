"""Synthetic inventory pairs with hand-calculated UTC changes and provenance."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.analysis import horizon
from flowwrench.schedules.model import InputError, make_schedule

ROOT = Path(__file__).resolve().parent
PAIR = ROOT / 'examples/comparison'


def record(expr='30 2 * * *', tz='Europe/Berlin', node='job', **kwargs):
    return make_schedule('test', 'jobs.json', 'jobs.json', 'workflow', node, expr, tz, **kwargs)


class CompareTests(unittest.TestCase):
    def compare(self, before, after, day='2026-10-25', window=60):
        from flowwrench.schedules.comparison import compare
        return compare(before, after, *horizon(day, 1), window)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'flowwrench.schedules', 'compare', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def pair_args(self):
        return ['--before', PAIR / 'before', '--after', PAIR / 'after',
                '--from', '2026-10-25', '--days', '1']

    def test_cli_fall_back_changes_and_peaks(self):
        result = self.run_cli(*self.pair_args(), '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([e['utc_minute'] for e in report['removed']],
                         ['2026-10-25T00:30Z', '2026-10-25T01:30Z'])
        self.assertEqual([e['utc_minute'] for e in report['added']], ['2026-10-25T02:30Z'])
        self.assertEqual((report['before']['peak'], report['after']['peak'], report['peak_delta']), (1, 2, 1))
        self.assertEqual((report['before']['total_firings'], report['after']['total_firings']), (3, 2))
        self.assertEqual(report['unchanged_firings'], 1)
        self.assertEqual(report['total_firings_delta'], -1)
        for event, fold, offset in zip(report['removed'], (0, 1), (7200, 3600)):
            self.assertEqual(event['timezone'], 'Europe/Berlin')
            self.assertEqual(event['local_time'], '2026-10-25T02:30')
            self.assertEqual(event['fold'], fold)
            self.assertEqual(event['utc_offset_seconds'], offset)
            self.assertTrue(event['ambiguous'])
            self.assertEqual(event['possible_utc_minutes'], ['2026-10-25T00:30Z', '2026-10-25T01:30Z'])
        self.assertFalse(report['added'][0]['ambiguous'])
        self.assertEqual(report['after']['schedules'][0]['source_dialect'], 'standard')
        self.assertEqual(report['before']['dst_hazards'][0]['hazard'], 'ambiguous')
        self.assertTrue(report['complete'])

    def test_text_shows_changes_and_provenance(self):
        result = self.run_cli(*self.pair_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Peak 1 -> 2', result.stdout)
        self.assertIn('2 removed; 1 added; 1 unchanged', result.stdout)
        self.assertIn('Europe/Berlin', result.stdout)
        self.assertIn('fold=1', result.stdout)
        self.assertIn('2026-10-25T02:30Z', result.stdout)

    def test_per_record_changes_do_not_cancel_in_aggregate(self):
        before = [record('0 0 * * *', 'UTC', 'a'), record('0 1 * * *', 'UTC', 'b')]
        after = [record('0 1 * * *', 'UTC', 'a'), record('0 0 * * *', 'UTC', 'b')]
        report = self.compare(before, after)
        self.assertEqual((len(report['removed']), len(report['added']), report['unchanged_firings']), (2, 2, 0))
        self.assertEqual(report['peak_delta'], 0)
        self.assertEqual(report, self.compare(before[::-1], after[::-1]))

    def test_fixed_seconds_change_is_visible_despite_same_minute_peak(self):
        report = self.compare([record('17 30 3 * * *', dialect='n8n')],
                              [record('29 30 3 * * *', dialect='n8n')])
        self.assertEqual([e['source_second'] for e in report['removed']], [17])
        self.assertEqual([e['source_second'] for e in report['added']], [29])
        self.assertEqual(report['added'][0]['utc_minute'], report['removed'][0]['utc_minute'])
        self.assertEqual(report['unchanged_firings'], 0)
        self.assertEqual(report['peak_delta'], 0)

    def test_timezone_only_change_keeps_equal_utc_firings_and_both_metadata(self):
        before = [record('30 3 * * *')]
        after = [record('30 2 * * *', 'UTC')]
        report = self.compare(before, after)
        self.assertEqual((report['added'], report['removed'], report['unchanged_firings']), ([], [], 1))
        self.assertEqual(report['before']['schedules'][0]['timezone'], 'Europe/Berlin')
        self.assertEqual(report['after']['schedules'][0]['timezone'], 'UTC')

    def test_gap_retains_hazard_even_without_removed_fire(self):
        report = self.compare([record()], [record('30 3 * * *')], '2027-03-28')
        self.assertEqual(report['removed'], [])
        self.assertEqual([e['utc_minute'] for e in report['added']], ['2027-03-28T01:30Z'])
        self.assertEqual(report['before']['dst_hazards'][0]['hazard'], 'skipped')
        self.assertEqual(report['before']['dst_hazards'][0]['utc_instants'], [])
        self.assertEqual(report['before']['peak'], 0)

    def test_trailing_window_and_exclusive_end(self):
        before = [record('59 23 * * *', 'UTC')]
        after = [*before, record('58 23 * * *', 'UTC', node='b')]
        report = self.compare(before, after, window=120)
        self.assertEqual((report['before']['peak'], report['after']['peak']), (1, 2))
        self.assertEqual(report['after']['peak_at'], '2026-10-25T23:59Z')
        self.assertEqual([e['utc_minute'] for e in report['added']], ['2026-10-25T23:58Z'])

    def test_all_disabled_or_unsupported_side_is_zero_with_coverage(self):
        disabled = record(enabled=False)
        report = self.compare([record()], [disabled, record('0 0 L * *', node='unsupported')])
        self.assertEqual((report['after']['peak'], report['after']['total_firings']), (0, 0))
        self.assertEqual((len(report['removed']), len(report['added'])), (2, 0))
        self.assertFalse(report['complete'])
        self.assertEqual((report['after']['disabled'], report['after']['unsupported']), (1, 1))

    def test_ambiguity_provenance_includes_fold_outside_horizon(self):
        from datetime import timedelta
        from flowwrench.schedules.comparison import compare
        start, end = horizon('2026-10-25', 1)
        report = compare([], [record()], start + timedelta(hours=1), end)
        self.assertEqual(report['before']['records'], 0)
        self.assertEqual(report['before']['peak'], 0)
        self.assertEqual(len(report['added']), 1)
        event = report['added'][0]
        self.assertEqual(event['fold'], 1)
        self.assertTrue(event['ambiguous'])
        self.assertEqual(event['possible_utc_minutes'], ['2026-10-25T00:30Z', '2026-10-25T01:30Z'])
        self.assertEqual(report['after']['dst_hazards'][0]['hazard'], 'ambiguous')

    def test_historical_subminute_offset_preserves_scheduled_wall_minute(self):
        report = self.compare([record('0 0 * * *', 'Africa/Monrovia')],
                              [record('0 2 * * *', 'Africa/Monrovia')], '1971-01-01')
        self.assertEqual(report['removed'][0]['local_time'], '1971-01-01T00:00')
        self.assertEqual(report['removed'][0]['utc_minute'], '1971-01-01T00:44Z')
        self.assertEqual(report['removed'][0]['utc_offset_seconds'], -2670)
        self.assertEqual(report['added'][0]['local_time'], '1971-01-01T02:00')

    def test_shared_work_and_result_bounds(self):
        from flowwrench.schedules.comparison import compare
        bounds = horizon('2026-10-25', 1)
        with patch('flowwrench.schedules.analysis.MAX_CANDIDATES', 1):
            with self.assertRaisesRegex(InputError, 'work limit'):
                compare([record('0 0 * * *', 'UTC')], [record('0 0 * * *', 'UTC')], *bounds)
        with patch('flowwrench.schedules.comparison.MAX_FIRINGS', 1):
            with self.assertRaisesRegex(InputError, 'result size limit'):
                compare([record('0 0 * * *', 'UTC')], [record('0 0 * * *', 'UTC')], *bounds)
        with patch('flowwrench.schedules.comparison.MAX_HAZARDS', 1):
            with self.assertRaisesRegex(InputError, 'result size limit'):
                compare([record()], [record()], *bounds)
        with patch('flowwrench.schedules.comparison.MAX_CHANGES', 1):
            with self.assertRaisesRegex(InputError, 'change limit'):
                compare([record()], [], *bounds)

    def test_cli_limit_failure_has_no_partial_json(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from flowwrench.schedules.cli import main
        out, err = StringIO(), StringIO()
        with patch('flowwrench.schedules.comparison.MAX_CHANGES', 1), redirect_stdout(out), redirect_stderr(err):
            code = main(['compare', *map(str, self.pair_args()), '--format', 'json'])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('change limit', err.getvalue())

    def test_cli_strict_checks_both_sides_and_side_assumptions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before, after = root / 'before', root / 'after'
            before.mkdir()
            after.mkdir()
            (before / 'jobs.crontab').write_text('0 * * * * echo hidden-command\n0 0 L * * true\n')
            (after / 'jobs.crontab').write_text('5 * * * * true\n')
            args = ['--before', before, '--after', after, '--from', '2026-11-01',
                    '--days', '1', '--format', 'json', '--default-timezone', 'Asia/Kathmandu']
            result = self.run_cli(*args, '--strict')
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report['complete'])
            self.assertEqual(report['before']['assumptions']['timezone_assumed_records'], 2)
            self.assertEqual(report['after']['assumptions']['timezone_assumed_records'], 1)
            self.assertEqual(report['before']['assumptions']['default_timezone'], 'Asia/Kathmandu')
            self.assertNotIn('hidden-command', result.stdout)
            self.assertIn('before:', result.stderr)
            self.assertEqual(self.run_cli(*args).returncode, 0)
            # Unsupported-only input is reported rather than mistaken for an empty schedule change.
            (after / 'jobs.crontab').write_text('0 0 L * * true\n')
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['complete'])

    def test_cli_invalid_input_has_no_partial_json(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'bad.schedule.yaml'
            bad.write_text('schedule: [bad]\n')
            for extra in [('--days', '0'), ('--window', '61'), ('--after', bad),
                          ('--after', Path(temp) / 'missing'), ('--before', Path(temp))]:
                with self.subTest(extra=extra):
                    result = self.run_cli(*self.pair_args(), '--format', 'json', *extra)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, '')
                    self.assertNotIn('Traceback', result.stderr)
