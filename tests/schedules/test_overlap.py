"""Duration overlap contracts, including an independent second-by-second oracle."""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.analysis import horizon
from flowwrench.schedules.extract import extract
from flowwrench.schedules.model import InputError, make_schedule

ROOT = Path(__file__).resolve().parent


def record(expr='0 0 * * *', tz='UTC', node='a', **kwargs):
    return make_schedule('test', 'jobs.json', 'jobs.json', 'workflow', node, expr, tz, **kwargs)


def interval(start, end):
    return {'from': start, 'until_exclusive': end}


class OverlapTests(unittest.TestCase):
    def overlap(self, records, durations=None, day='2026-11-01'):
        from flowwrench.schedules.overlap import overlap
        return overlap(records, *horizon(day, 1), durations)

    def test_explicit_duration_contract_and_determinism(self):
        a, b = record(), record('5 0 * * *', node='b')
        durations = {a.id: 600, b.id: 300}
        report = self.overlap([a, b], durations)
        self.assertEqual((report['peak'], report['known_peak']), (2, 2))
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T00:05:00Z', '2026-11-01T00:10:00Z')])
        self.assertEqual((report['known_firings'], report['carry_in_firings']), (2, 0))
        self.assertTrue(report['complete'])
        self.assertEqual(report, self.overlap([b, a], dict(reversed(list(durations.items())))))

    def test_missing_and_null_durations_remain_unknown(self):
        a, b = record(), record(node='b')
        missing = self.overlap([a, b], {a.id: 600})
        explicit_null = self.overlap([a, b], {a.id: 600, b.id: None})
        self.assertEqual(missing, explicit_null)
        self.assertEqual(missing['known_peak'], 1)
        self.assertIsNone(missing['peak'])
        self.assertFalse(missing['complete'])
        self.assertEqual(missing['unknown_duration_ids'], [b.id])
        self.assertIsNone(next(r for r in missing['schedules'] if r['id'] == b.id)['duration_seconds'])
        unknown = self.overlap([a, b])
        self.assertEqual(unknown['known_peak'], 0)
        self.assertEqual(unknown['known_peak_intervals'], [])
        self.assertIsNone(unknown['peak'])

    def test_half_open_ties_and_adjacent_equal_peak_merge(self):
        a, b = record(), record('5 0 * * *', node='b')
        report = self.overlap([a, b], {a.id: 300, b.id: 300})
        self.assertEqual(report['peak'], 1)
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T00:00:00Z', '2026-11-01T00:10:00Z')])

    def test_disjoint_peak_intervals_and_fixed_seconds(self):
        a = record('17 0,5 0 * * *', dialect='n8n')
        report = self.overlap([a], {a.id: 20})
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T00:00:17Z', '2026-11-01T00:00:37Z'),
            interval('2026-11-01T00:05:17Z', '2026-11-01T00:05:37Z')])

    def test_carry_in_clipping_and_self_overlap(self):
        a = record('* * * * *')
        report = self.overlap([a], {a.id: 120})
        self.assertEqual(report['peak'], 2)
        self.assertEqual((report['known_firings'], report['carry_in_firings']), (1440, 1))
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z')])

    def test_exact_end_at_start_excluded_and_end_of_horizon_clipped(self):
        a, b = record('59 23 * * *'), record('59 23 * * *', node='b')
        report = self.overlap([a, b], {a.id: 60, b.id: 90})
        self.assertEqual(report['carry_in_firings'], 1)
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T23:59:00Z', '2026-11-02T00:00:00Z')])

    def test_disabled_and_unsupported_coverage(self):
        a, b = record(enabled=False), record('0 0 L * *', node='b')
        report = self.overlap([a, b])
        self.assertEqual(report['known_peak'], 0)
        self.assertIsNone(report['peak'])
        self.assertEqual(report['unknown_duration_ids'], [])
        self.assertEqual((report['disabled'], report['unsupported']), (1, 1))
        self.assertFalse(report['schedule_coverage_complete'])
        self.assertEqual(self.overlap([a])['peak'], 0)

    def test_no_fires_is_known_zero_only_when_duration_is_known(self):
        a = record('0 0 29 FEB *')
        report = self.overlap([a], {a.id: 1})
        self.assertEqual(report['peak'], 0)
        self.assertEqual(report['known_peak_intervals'], [])
        self.assertIsNone(self.overlap([a])['peak'])

    def test_dst_fold_elapsed_durations_and_gap(self):
        a = record('30 2 * * *', 'Europe/Berlin')
        report = self.overlap([a], {a.id: 7200}, day='2026-10-25')
        self.assertEqual((report['known_firings'], report['peak']), (2, 2))
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-10-25T01:30:00Z', '2026-10-25T02:30:00Z')])
        gap = self.overlap([a], {a.id: 7200}, day='2027-03-28')
        self.assertEqual((gap['known_firings'], gap['peak']), (0, 0))

    def test_historical_offset_seconds_are_not_rounded(self):
        a = record('17 0 0 * * *', 'Africa/Monrovia', dialect='n8n')
        report = self.overlap([a], {a.id: 20}, day='1970-01-02')
        self.assertEqual(report['known_peak_intervals'], [
            interval('1970-01-02T00:44:47Z', '1970-01-02T00:45:07Z')])

    def test_max_duration_and_multi_day_carry_in(self):
        a = record()
        report = self.overlap([a], {a.id: 604800})
        self.assertEqual((report['peak'], report['carry_in_firings']), (7, 6))
        self.assertEqual(report['known_peak_intervals'], [
            interval('2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z')])

    def test_invalid_library_horizon(self):
        from flowwrench.schedules.overlap import overlap
        start, end = horizon('2026-11-01', 1)
        for begin, finish in [(end, start), (start, start),
                              (start.replace(tzinfo=None), end),
                              (start, end.replace(microsecond=1)),
                              (start, end + timedelta(days=730))]:
            with self.subTest(begin=begin, finish=finish), self.assertRaises(InputError):
                overlap([record()], begin, finish)

    def test_second_axis_oracle_with_timezone_and_carry_in(self):
        # Direct UTC enumeration and pointwise arithmetic, without the sweep or
        # cron matcher. Jobs fire at :00 and :30 each minute, lasting 80/45s.
        from flowwrench.schedules.overlap import overlap
        a = record('0 * * * * *', tz='Asia/Kathmandu', dialect='n8n')
        b = record('30 * * * * *', node='b', dialect='n8n')
        start, _ = horizon('2026-11-01', 1)
        counts = [sum(t <= sec < t + duration
                      for offset, duration in [(0, 80), (30, 45)]
                      for t in range(-120 + offset, 180, 60)) for sec in range(180)]
        peak = max(counts)
        expected = []
        for sec, count in enumerate(counts):
            if count == peak and (sec == 0 or counts[sec - 1] != peak):
                begin = sec
            if count == peak and (sec == len(counts) - 1 or counts[sec + 1] != peak):
                fmt = lambda s: (start + timedelta(seconds=s)).isoformat().replace('+00:00', 'Z')
                expected.append(interval(fmt(begin), fmt(sec + 1)))
        report = overlap([a, b], start, start + timedelta(seconds=180), {a.id: 80, b.id: 45})
        self.assertEqual((report['known_peak'], report['known_peak_intervals']), (peak, expected))

    def test_invalid_duration_values_and_identities(self):
        a = record()
        for value in [-1, True, False, 1.5, '60', [], {}, 604801]:
            with self.subTest(value=value), self.assertRaises(InputError):
                self.overlap([a], {a.id: value})
        with self.assertRaisesRegex(InputError, 'unknown schedule'):
            self.overlap([a], {'typo': 60})
        with self.assertRaisesRegex(InputError, 'duplicate schedule'):
            self.overlap([a, a], {a.id: 60})

    def test_resource_limits_include_lookback_and_peak_output(self):
        a = record('* * * * *')
        with patch('flowwrench.schedules.analysis.MAX_CANDIDATES', 1):
            with self.assertRaisesRegex(InputError, 'work limit'):
                self.overlap([a], {a.id: 120})
        with patch('flowwrench.schedules.overlap.MAX_FIRINGS', 1):
            with self.assertRaisesRegex(InputError, 'firing limit'):
                self.overlap([a], {a.id: 120})
        with patch('flowwrench.schedules.overlap.MAX_PEAK_INTERVALS', 1):
            with self.assertRaisesRegex(InputError, 'peak interval limit'):
                self.overlap([a], {a.id: 1})


class OverlapCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.jobs = self.path / 'jobs.crontab'
        self.jobs.write_text('CRON_TZ=UTC\n0 0 * * * true\n5 0 * * * true\n')
        self.records = extract([self.jobs]).schedules
        self.durations = self.path / 'durations.json'
        self.durations.write_text(json.dumps({'version': 1, 'durations_seconds': {
            self.records[0].id: 600, self.records[1].id: 300}}))

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'flowwrench.schedules', 'overlap', str(self.jobs),
                               '--durations', str(self.durations), '--from', '2026-11-01',
                               '--days', '1', *map(str, extra)], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_json_text_determinism_and_read_only(self):
        before = {p: p.read_bytes() for p in self.path.iterdir()}
        result = self.run_cli('--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['peak'], 2)
        self.assertEqual(result.stdout, self.run_cli('--format', 'json').stdout)
        self.assertIn('Known-duration peak 2', self.run_cli().stdout)
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.iterdir()})

    def test_unknown_duration_report_and_strict(self):
        self.durations.write_text('{"version": 1, "durations_seconds": {}}')
        result = self.run_cli('--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIsNone(report['peak'])
        self.assertEqual(len(report['unknown_duration_ids']), 2)
        self.assertIn('overall peak unknown', self.run_cli().stdout)
        strict = self.run_cli('--format', 'json', '--strict')
        self.assertEqual(strict.returncode, 2, strict.stderr)
        self.assertEqual(json.loads(strict.stdout), report)

    def test_strict_unsupported_preserves_partial_report(self):
        self.jobs.write_text(self.jobs.read_text() + '@reboot true\n')
        result = self.run_cli('--format', 'json', '--strict')
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['known_peak'], report['unsupported']), (2, 1))
        self.assertIsNone(report['peak'])

    def test_invalid_sidecars_fail_atomically(self):
        identity = self.records[0].id
        cases = ['{', '[]', '{}', '{"version": true, "durations_seconds": {}}',
                 '{"version": 2, "durations_seconds": {}}',
                 '{"version": 1, "durations_seconds": {}, "extra": 1}',
                 '{"version": 1, "durations_seconds": []}',
                 '{"version": 1, "durations_seconds": null}',
                 '{"version": 1, "version": 1, "durations_seconds": {}}',
                 '{"version": 1, "durations_seconds": {"typo": 60}}',
                 '{"version": 1, "durations_seconds": {"%s": NaN}}' % identity,
                 '{"version": 1, "durations_seconds": {"%s": Infinity}}' % identity,
                 '{"version": 1, "durations_seconds": {"%s": -1}}' % identity,
                 '{"version": 1, "durations_seconds": {"%s": 1, "%s": 2}}' % (identity, identity)]
        for body in cases:
            with self.subTest(body=body):
                self.durations.write_text(body)
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
        self.durations.write_bytes(b'\xff')
        self.assertEqual(self.run_cli().returncode, 2)
        self.durations.unlink()
        self.assertEqual(self.run_cli().returncode, 2)

    def test_sidecar_size_type_and_bom(self):
        from flowwrench.schedules.overlap import load_durations
        self.durations.write_bytes(b'\xef\xbb\xbf' + self.durations.read_bytes())
        self.assertEqual(self.run_cli().returncode, 0)
        with patch('flowwrench.schedules.overlap.MAX_DURATION_BYTES', 1):
            with self.assertRaisesRegex(InputError, 'size limit'):
                load_durations(self.durations)
        self.durations.unlink()
        self.durations.symlink_to(self.jobs)
        self.assertEqual(self.run_cli().returncode, 2)
