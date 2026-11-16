from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
import unittest
from unittest.mock import patch
from flowwrench.schedules.analysis import analyze, dst, suggest, horizon, forecast, Budget, local_instants, rolling
from flowwrench.schedules.model import make_schedule, timezone, InputError
from flowwrench.schedules.extract import extract
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
UTC = dt_timezone.utc


def record(expr='0 * * * *', tz='UTC', node='a', **kwargs):
    return make_schedule('test', 'test.json', 'test.json', 'workflow', node, expr, tz, **kwargs)


class AnalysisTests(unittest.TestCase):
    def test_peak_and_platform_breakdown(self):
        records = [record(node='a'), record(node='b'), record('5 * * * *', node='c')]
        start, end = horizon('2026-11-01', 1)
        report = analyze(records, start, end)
        self.assertEqual(report['peak'], 2)
        self.assertEqual(report['total_firings'], 72)
        self.assertEqual(report['zero_minutes'], 1392)
        self.assertEqual(report['histogram']['2026-11-01T00:00Z'], 2)
        self.assertEqual(report['per_platform']['test']['total_firings'], 72)
        self.assertEqual(report['peak_at'], '2026-11-01T00:00Z')

    def test_rolling_window_and_horizon_truncation(self):
        start, end = horizon('2026-11-01', 1)
        report = analyze([record('0 * * * *'), record('1 * * * *', node='b')], start, end, 120)
        self.assertEqual(report['peak'], 2)
        self.assertEqual(report['histogram']['2026-11-01T00:00Z'], 1)
        self.assertEqual(report['window_histogram']['2026-11-01T00:00Z'], 1)
        self.assertEqual(report['window_histogram']['2026-11-01T00:01Z'], 2)
        self.assertEqual(report['window_histogram']['2026-11-01T00:02Z'], 1)
        self.assertNotIn('2026-11-01T00:03Z', report['window_histogram'])
        self.assertEqual(rolling(Counter({0: 2, 2: 1}), 5, 3), {0: 2, 1: 2, 2: 3, 3: 1, 4: 1})

    def test_dst_skipped_and_ambiguous_four_cases(self):
        cases = [('Europe/Berlin', '30 2 * * *', '2027-03-28', 'skipped', 0),
                 ('Europe/Berlin', '30 2 * * *', '2026-10-25', 'ambiguous', 2),
                 ('America/New_York', '30 2 * * *', '2027-03-14', 'skipped', 0),
                 ('America/New_York', '30 1 * * *', '2026-11-01', 'ambiguous', 2)]
        for tz, expr, day, kind, count in cases:
            with self.subTest(tz=tz, day=day):
                r = record(expr, tz)
                start, end = horizon(day, 1)
                forecast_report = analyze([r], start, end)
                report = dst([r], start, end)
                self.assertEqual(forecast_report['total_firings'], count)
                self.assertEqual(forecast_report['dst_hazards'], report['hazards'])
                self.assertEqual(len(report['hazards']), 1)
                self.assertEqual(report['hazards'][0]['hazard'], kind)
                self.assertEqual(len(report['hazards'][0]['utc_instants']), count)

    def test_dst_half_hour_transition(self):
        r = record('15 2 * * *', 'Australia/Lord_Howe')
        start, end = horizon('2026-10-03', 2)
        hazards = dst([r], start, end)['hazards']
        self.assertEqual(len(hazards), 1)
        self.assertEqual(hazards[0]['local_time'], '2026-10-04T02:15')
        self.assertEqual(hazards[0]['hazard'], 'skipped')

    def test_dst_fold_hazard_when_only_second_instant_is_in_horizon(self):
        r = record('30 2 * * *', 'Europe/Berlin')
        start, end = horizon('2026-10-25', 1)
        start += timedelta(hours=1)
        report = analyze([r], start, end)
        self.assertEqual(report['histogram'], {'2026-10-25T01:30Z': 1})
        self.assertEqual(len(report['dst_hazards']), 1)
        self.assertEqual(report['dst_hazards'], dst([r], start, end)['hazards'])
        self.assertEqual(report['dst_hazards'][0]['utc_instants'],
                         ['2026-10-25T00:30Z', '2026-10-25T01:30Z'])

    def test_instants_ordinary_time_and_no_false_utc_hazards(self):
        self.assertEqual(len(local_instants(datetime(2026, 11, 1, 12), timezone('UTC'))), 1)
        start, end = horizon('2026-01-01', 365)
        self.assertEqual(dst([record()], start, end)['hazards'], [])

    def test_utc_axis_matches_different_timezones(self):
        records = [record('0 1 * * *', 'Europe/Berlin'), record('0 0 * * *', node='b'),
                   record('45 5 * * *', 'Asia/Kathmandu', node='c')]
        start, end = horizon('2026-11-01', 1)
        report = analyze(records, start, end)
        self.assertEqual(report['peak'], 3)
        self.assertEqual(report['histogram'], {'2026-11-01T00:00Z': 3})

    def test_forecast_independent_utc_scan_oracle(self):
        # Brute-force UTC iteration is independent of the local-date candidate generator.
        cases = [('15,45 0-4 * * *', 'Europe/Berlin', '2026-10-24'),
                 ('*/7 * * * *', 'America/New_York', '2026-10-31'),
                 ('0 0 * * *', 'Pacific/Kiritimati', '2026-11-01'),
                 ('30 23 * * *', 'America/Adak', '2026-11-01'),
                 ('0 0 1 * MON', 'UTC', '2026-11-01')]
        for expr, tz, day in cases:
            with self.subTest(expr=expr, tz=tz):
                r = record(expr, tz)
                start, end = horizon(day, 3)
                actual, _ = forecast(r, start, end, Budget())
                cron, zone = r.cron(), timezone(tz)
                expected = [i for i in range(3 * 1440) if cron.matches((start + timedelta(minutes=i)).astimezone(zone))]
                self.assertEqual(actual, expected)

    def test_disabled_and_unsupported_excluded_explicitly(self):
        records = [record(), record(enabled=False, node='b'), record('0 0 L * *', node='c')]
        start, end = horizon('2026-11-01', 1)
        report = analyze(records, start, end)
        self.assertEqual((report['analyzed'], report['disabled'], report['unsupported']), (1, 1, 1))
        self.assertFalse(report['complete'])
        self.assertEqual(len(report['excluded']), 2)
        with self.assertRaisesRegex(InputError, 'no enabled'):
            analyze(records[1:], start, end)

    def test_fixture_end_to_end_baseline(self):
        inv = extract([FIXTURES])
        start, end = horizon('2026-11-01', 7)
        report = analyze(inv.schedules, start, end)
        self.assertEqual((report['records'], report['analyzed']), (14, 13))
        self.assertEqual(report['peak'], 9)
        self.assertEqual(report['total_firings'], 1541)
        self.assertEqual(sum(p['total_firings'] for p in report['per_platform'].values()), 1541)
        self.assertEqual(len(report['dst_hazards']), 1)

    def test_suggest_determinism_reduction_and_replayed_expressions(self):
        records = extract([FIXTURES]).schedules
        start, end = horizon('2026-11-01', 7)
        result = suggest(records, start, end, 10)
        self.assertEqual(result, suggest(list(reversed(records)), start, end, 10))
        self.assertEqual(result['before']['peak'], 9)
        self.assertLessEqual(result['after']['peak'], 2)
        self.assertEqual(result['after']['total_firings'], 1541)
        original = {r.id: r for r in records}
        replay = []
        for row in result['plan']:
            r = original[row['id']]
            self.assertIn(row['offset_minutes'], range(11))
            self.assertEqual(row['proposed_expr'].split()[1:], r.expr.split()[1:])
            replay.append(record(row['proposed_expr'], r.timezone, node=r.id, day_mode=r.day_mode))
        replayed = analyze(replay, start, end)
        self.assertEqual(replayed['peak'], result['after']['peak'])
        self.assertEqual(replayed['total_firings'], result['after']['total_firings'])

    def test_suggest_no_hour_rollover_or_regression(self):
        start, end = horizon('2026-11-01', 1)
        records = [record('55 * * * *'), record('59 * * * *', node='b'), record('* * * * *', node='c')]
        plan = suggest(records, start, end, 10)
        self.assertLessEqual(plan['after']['peak'], plan['before']['peak'])
        lookup = {r['id']: r for r in plan['plan']}
        self.assertLessEqual(lookup[records[0].id]['offset_minutes'], 4)
        self.assertEqual(lookup[records[1].id]['offset_minutes'], 0)
        self.assertEqual(lookup[records[2].id]['offset_minutes'], 0)

    def test_suggest_does_not_drop_dst_fires(self):
        start, end = horizon('2027-03-14', 1)
        records = [record('55 1 * * *', 'America/New_York', node=str(n)) for n in range(3)]
        plan = suggest(records, start, end, 10)
        self.assertEqual(plan['before']['total_firings'], plan['after']['total_firings'])
        self.assertTrue(all(p['offset_minutes'] <= 4 for p in plan['plan']))

    def test_suggest_preserves_fixed_source_seconds_and_year_wildcard(self):
        start, end = horizon('2026-11-01', 1)
        r = record('17 0 * * * * *', dialect='windmill')
        plan = suggest([r], start, end, 10)
        source = plan['plan'][0]['proposed_source_expr'].split()
        self.assertEqual((source[0], source[-1], len(source)), ('17', '*', 7))

    def test_suggest_zero_shift(self):
        start, end = horizon('2026-11-01', 1)
        plan = suggest([record(), record(node='b')], start, end, 0)
        self.assertEqual(plan['after']['peak'], plan['before']['peak'])
        self.assertTrue(all(p['offset_minutes'] == 0 for p in plan['plan']))

    def test_no_fires_horizon(self):
        start, end = horizon('2026-11-01', 1)
        report = analyze([record('0 0 29 FEB *')], start, end)
        self.assertEqual(report['peak'], 0)
        self.assertIsNone(report['peak_at'])
        self.assertEqual(report['zero_minutes'], 1440)

    def test_bounds_and_resource_limits(self):
        for day, days in [('not-date', 1), ('9999-01-01', 1), ('2026-11-01', 0), ('2026-11-01', 731)]:
            with self.subTest(day=day, days=days):
                with self.assertRaises(InputError):
                    horizon(day, days)
        start, end = horizon('2026-11-01', 1)
        with patch('flowwrench.schedules.analysis.MAX_CANDIDATES', 1):
            with self.assertRaisesRegex(InputError, 'work limit'):
                analyze([record()], start, end)
        with patch('flowwrench.schedules.analysis.MAX_SCORE_WORK', 1):
            with self.assertRaisesRegex(InputError, 'optimizer work limit'):
                suggest([record()], start, end)

    def test_suggest_does_not_change_unobserved_schedules(self):
        start, end = horizon('2026-11-01', 1)
        plan = suggest([record('0 0 29 FEB *')], start, end, 10)
        self.assertEqual(plan['plan'][0]['offset_minutes'], 0)
        self.assertEqual(plan['plan'][0]['proposed_expr'], '0 0 29 FEB *')
