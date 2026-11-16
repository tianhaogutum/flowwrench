"""Duration objective contracts: equal starts need not mean equal running load."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.analysis import horizon, suggest
from flowwrench.schedules.cli import main
from flowwrench.schedules.extract import extract
from flowwrench.schedules.model import InputError, make_schedule
from flowwrench.schedules.overlap import overlap


def job(expr='0 * * * *', node='a', tz='UTC', **kw):
    return make_schedule('test', 'jobs.json', 'jobs.json', 'jobs', node, expr, tz, **kw)


class DurationSuggestTests(unittest.TestCase):
    def suggest(self, records, durations, day='2026-11-01', days=1, max_shift=10, **kw):
        return suggest(records, *horizon(day, days), max_shift, objective='concurrency', durations=durations, **kw)

    def test_equal_fire_density_different_overlap_replayed_and_deterministic(self):
        a, b = job(), job('5 * * * *', 'b')
        durations = {a.id: 600, b.id: 300}
        before = overlap([a, b], *horizon('2026-11-01', 1), durations)
        self.assertEqual(before['peak'], 2)
        result = self.suggest([a, b], durations)
        self.assertEqual(result['objective'], 'concurrency')
        self.assertEqual((result['before']['peak'], result['after']['peak']), (1, 1))
        self.assertEqual((result['before']['total_firings'], result['after']['total_firings']), (48, 48))
        stats = result['overlap']
        self.assertEqual((stats['before']['peak'], stats['after']['peak']), (2, 1))
        self.assertEqual((stats['before_squared_job_seconds'], stats['after_squared_job_seconds']), (36000, 21600))
        self.assertEqual(result, self.suggest([b, a], dict(reversed(list(durations.items())))))
        plans = {p['id']: p for p in result['plan']}
        replay = [replace(r, expr=plans[r.id]['proposed_expr'], source_expr=plans[r.id]['proposed_source_expr']) for r in (a, b)]
        self.assertEqual(stats['after'], overlap(replay, *horizon('2026-11-01', 1), durations))
        for r in (a, b):
            p = plans[r.id]
            self.assertIn(p['offset_minutes'], range(11))
            self.assertEqual(p['proposed_expr'].split()[1:], r.expr.split()[1:])

    def test_secondary_score_and_independent_second_axis_oracle(self):
        a, b = job('0 0 * * *'), job('5 0 * * *', 'b')
        pinned = [job('59 12 * * *', str(n)) for n in range(3)]
        durations = {a.id: 600, b.id: 300, **{r.id: 1 for r in pinned}}
        result = self.suggest([a, b, *pinned], durations)
        stats = result['overlap']
        self.assertEqual((stats['before']['peak'], stats['after']['peak']), (3, 3))
        self.assertEqual((stats['before_squared_job_seconds'], stats['after_squared_job_seconds']), (1509, 909))
        # Independent arithmetic oracle on the finite seconds axis, no sweep or matcher.
        by_id = {p['id']: p for p in result['plan']}
        for side in ('before', 'after'):
            executions = []
            for r in (a, b, *pinned):
                fields = (r.expr if side == 'before' else by_id[r.id]['proposed_expr']).split()
                begin = int(fields[1]) * 3600 + int(fields[0]) * 60
                executions.append((begin, begin + durations[r.id]))
            counts = [sum(begin <= t < end for begin, end in executions) for t in range(86400)]
            self.assertEqual(max(counts), stats[side]['peak'])
            self.assertEqual(sum(c*c for c in counts), stats[side + '_squared_job_seconds'])

    def test_missing_null_invalid_durations_and_pairing(self):
        a = job('0 0 29 FEB *')
        for values in ({}, {a.id: None}, {a.id: True}, {a.id: -1}, {a.id: 604801}, {'bad-id': 1}):
            with self.subTest(values=values), self.assertRaises(InputError):
                self.suggest([a], values)
        with self.assertRaises(InputError):
            suggest([a], *horizon('2026-11-01', 1), objective='concurrency')
        with self.assertRaises(InputError):
            suggest([a], *horizon('2026-11-01', 1), durations={a.id: 1})
        with self.assertRaises(InputError):
            suggest([a], *horizon('2026-11-01', 1), objective='typo')

    def test_zero_duration_no_fires_no_improvement_and_zero_shift(self):
        a, b = job(), job('59 * * * *', 'b')
        for durations, max_shift in [({a.id: 0, b.id: 0}, 10), ({a.id: 1, b.id: 1}, 10),
                                     ({a.id: 600, b.id: 300}, 0)]:
            result = self.suggest([a, b], durations, max_shift=max_shift)
            self.assertTrue(all(p['offset_minutes'] == 0 for p in result['plan']))
            self.assertEqual(result['overlap']['before'], result['overlap']['after'])
            self.assertEqual(result['overlap']['before']['known_firings'], 48)
        zero = overlap([a], *horizon('2026-11-01', 1), {a.id: 0})
        self.assertEqual((zero['peak'], zero['known_firings'], zero['carry_in_firings']), (0, 24, 0))
        self.assertEqual(zero['known_peak_intervals'], [])
        nofire = job('0 0 29 FEB *')
        result = self.suggest([nofire], {nofire.id: 600})
        self.assertEqual(result['plan'][0]['offset_minutes'], 0)
        self.assertEqual(result['overlap']['after']['peak'], 0)

    def test_unsupported_lower_bounds_and_disabled_durations(self):
        a, off, bad = job(), job(node='off', enabled=False), job('0 0 L * *', 'bad')
        result = self.suggest([a, off, bad], {a.id: 1})
        self.assertFalse(result['complete'])
        self.assertEqual((result['disabled'], result['unsupported']), (1, 1))
        self.assertEqual(len(result['excluded']), 2)
        for side in ('before', 'after'):
            self.assertIsNone(result['overlap'][side]['peak'])
            self.assertEqual(result['overlap'][side]['known_peak'], 1)
        with self.assertRaisesRegex(InputError, 'no enabled'):
            self.suggest([off], {})

    def test_exact_seconds_carry_in_half_open_and_self_overlap(self):
        a = job('17 0 * * * * *', dialect='windmill')
        b = job('17 5 * * * *', 'b', dialect='n8n')
        result = self.suggest([a, b], {a.id: 3900, b.id: 300})
        self.assertGreater(result['overlap']['before']['carry_in_firings'], 0)
        self.assertLessEqual(result['overlap']['after']['peak'], result['overlap']['before']['peak'])
        for p in result['plan']:
            self.assertTrue(p['proposed_source_expr'].startswith('17 '))
        plans = {p['id']: p for p in result['plan']}
        replay = [replace(r, expr=plans[r.id]['proposed_expr'], source_expr=plans[r.id]['proposed_source_expr']) for r in (a, b)]
        self.assertEqual(result['overlap']['after'], overlap(replay, *horizon('2026-11-01', 1), {a.id: 3900, b.id: 300}))

    def test_dst_fire_preservation_same_hour_and_unobserved(self):
        cases = [('Europe/Berlin', '30 2 * * *', '2026-10-25', 1),
                 ('Europe/Berlin', '30 2 * * *', '2027-03-28', 1),
                 ('Australia/Lord_Howe', '25 1 * * *', '2027-04-03', 1),
                 ('Australia/Lord_Howe', '25 2 * * *', '2026-10-03', 2)]
        for tz, expr, day, days in cases:
            a, b = job(expr, tz=tz), job(expr, 'b', tz)
            result = self.suggest([a, b], {a.id: 300, b.id: 300}, day, days)
            self.assertEqual(result['before']['total_firings'], result['after']['total_firings'])
            self.assertEqual(result['overlap']['before']['known_firings'], result['overlap']['after']['known_firings'])
            self.assertLessEqual(result['overlap']['after']['known_peak'], result['overlap']['before']['known_peak'])
            if 'Lord_Howe' in tz:
                self.assertTrue(all(p['offset_minutes'] <= 4 for p in result['plan']))
        a = job('59 * * * *')
        self.assertEqual(self.suggest([a], {a.id: 90})['plan'][0]['offset_minutes'], 0)

    def test_atomic_forecast_optimizer_execution_and_output_bounds(self):
        a = job()
        for name, limit, message in [('flowwrench.schedules.analysis.MAX_CANDIDATES', 1, 'work limit'),
                                      ('flowwrench.schedules.analysis.MAX_SCORE_WORK', 1, 'optimizer work limit'),
                                      ('flowwrench.schedules.overlap.MAX_FIRINGS', 1, 'firing limit'),
                                      ('flowwrench.schedules.overlap.MAX_PEAK_INTERVALS', 1, 'peak interval limit')]:
            with self.subTest(name=name), patch(name, limit), self.assertRaisesRegex(InputError, message):
                self.suggest([a], {a.id: 1})


class DurationSuggestCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.crontab'
        self.jobs.write_text('0 * * * * true\n5 * * * * true\n')
        self.records = extract([self.jobs], 'Europe/Berlin').schedules
        self.sidecar = self.root / 'durations.json'
        self.sidecar.write_text(json.dumps({'version': 1, 'durations_seconds': {
            r.id: d for r, d in zip(self.records, [600, 300])}}))

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def run_suggest(self, *extra):
        return self.cli('suggest', self.jobs, '--from', '2026-11-01', '--days', '1',
                        '--objective', 'concurrency', '--durations', self.sidecar, '--format', 'json',
                        '--default-timezone', 'Europe/Berlin', *extra)

    def test_source_snapshot_equivalence_deleted_exports_saved_assumptions_and_text(self):
        code, raw, err = self.run_suggest()
        self.assertEqual(code, 0, err)
        result = json.loads(raw)
        self.assertEqual(result['overlap']['after']['peak'], 1)
        self.assertIn('Objective concurrency', self.run_suggest('--format', 'text')[1])
        code, saved, err = self.cli('extract', self.jobs, '--format', 'snapshot', '--default-timezone', 'Europe/Berlin')
        self.assertEqual(code, 0, err)
        snapshot = self.root / 'saved.json'
        data = json.loads(saved)
        data['schedules'].reverse()
        snapshot.write_text(json.dumps(data))
        original = snapshot.read_bytes(), self.sidecar.read_bytes()
        self.jobs.unlink()
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('source read')):
            args = ['suggest', '--snapshot', snapshot, '--objective', 'concurrency', '--durations', self.sidecar,
                    '--from', '2026-11-01', '--days', '1', '--default-timezone', 'Not/AZone', '--format', 'json']
            code, saved_result, err = self.cli(*args)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(saved_result), result)
            self.assertEqual(self.cli(*args)[1], saved_result)
        self.assertEqual((snapshot.read_bytes(), self.sidecar.read_bytes()), original)

    def test_cli_pairing_sidecar_rejection_strict_and_default_compatibility(self):
        for args in [('--objective', 'concurrency'), ('--durations', self.sidecar), ('--objective', 'bad')]:
            code, raw, err = self.cli('suggest', self.jobs, *args, '--format', 'json')
            self.assertEqual((code, raw), (2, ''), err)
        for body in ['{', '{"version": true, "durations_seconds": {}}',
                     '{"version":1,"durations_seconds":{}}',
                     '{"version":1,"durations_seconds":{"bad-id":1}}']:
            self.sidecar.write_text(body)
            code, raw, err = self.run_suggest()
            self.assertEqual((code, raw), (2, ''), err)
        self.sidecar.write_text(json.dumps({'version':1, 'durations_seconds': {r.id: 0 for r in self.records}}))
        self.jobs.write_text(self.jobs.read_text() + '@reboot true\n')
        code, raw, err = self.run_suggest('--strict')
        self.assertEqual(code, 2, err)
        self.assertFalse(json.loads(raw)['complete'])
        self.assertIsNone(json.loads(raw)['overlap']['after']['peak'])
        args = ['suggest', self.jobs, '--from', '2026-11-01', '--days', '1', '--format', 'json']
        default = self.cli(*args)
        self.assertEqual(default, self.cli(*args, '--objective', 'fire-density'))
        self.assertNotIn('objective', json.loads(default[1]))
        self.assertNotIn('overlap', json.loads(default[1]))

    def test_repeated_suggestion_sidecars_rejected_before_reads(self):
        for tail in [['--durations', self.sidecar], [f'--durations={self.sidecar}']]:
            with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('read before rejection')):
                code, raw, err = self.run_suggest(*tail)
            self.assertEqual((code, raw), (2, ''), err)
            self.assertIn('only once', err)
