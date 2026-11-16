"""Saved deterministic jitter plans, assumptions and atomic failure contracts."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main


class SuggestSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'exports'
        self.source.mkdir()
        (self.source / 'jobs.crontab').write_text('CRON_TZ=Europe/Berlin\n30 2 * * * true\n31 2 * * * true\n')
        (self.source / 'flows.json').write_text(json.dumps([
            {'id': 'cron', 'type': 'inject', 'crontab': '30 2 * * *', 'once': True},
            {'id': 'repeat', 'type': 'inject', 'repeat': '60'}]))
        (self.source / 'tick.schedule.yaml').write_text('schedule: "17 30 2 * * * *"\n')
        (self.source / 'off.schedule.yaml').write_text('schedule: "0 0 0 * * *"\nenabled: false\ntimezone: UTC\n')
        code, raw, err = self.cli('extract', self.source, '--format', 'snapshot',
                                  '--default-timezone', 'Europe/Berlin')
        self.assertEqual(code, 0, err)
        self.saved = json.loads(raw)
        self.snapshot = self.root / 'inventory.json'
        self.snapshot.write_text(raw)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def suggest(self, *extra):
        return self.cli('suggest', '--snapshot', self.snapshot, '--from', '2026-10-25',
                        '--days', '1', '--max-shift', '10m', '--window', '120', '--format', 'json', *extra)

    def invalid(self, *extra):
        code, out, err = self.suggest(*extra)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def test_plans_equal_source_after_export_deletion_and_record_reorder(self):
        expected = {}
        for day in ('2026-10-25', '2027-03-28', '2026-11-01'):
            code, raw, err = self.cli('suggest', self.source, '--default-timezone', 'Europe/Berlin',
                                      '--from', day, '--days', '1', '--max-shift', '10m',
                                      '--window', '120', '--format', 'json')
            self.assertEqual(code, 0, err)
            expected[day] = json.loads(raw)
        for file in self.source.iterdir():
            file.unlink()
        self.source.rmdir()
        self.saved['schedules'].reverse()
        self.snapshot.write_text(json.dumps(self.saved))
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('original export read')):
            for day, fires in [('2026-10-25', 8), ('2027-03-28', 0), ('2026-11-01', 4)]:
                with self.subTest(day=day):
                    code, raw, err = self.suggest('--from', day, '--default-timezone', 'Not/AZone')
                    self.assertEqual(code, 0, err)
                    report = json.loads(raw)
                    self.assertEqual(report, expected[day])
                    self.assertEqual(self.suggest('--from', day)[1], raw)
                    self.assertEqual((report['records'], report['analyzed'], report['disabled'],
                                      report['unsupported'], report['complete']), (6, 4, 1, 1, False))
                    self.assertEqual(report['before']['total_firings'], fires)
                    self.assertEqual(report['after']['total_firings'], fires)
                    self.assertLessEqual(report['after']['peak'], report['before']['peak'])
                    self.assertEqual(report['warnings'], self.saved['warnings'])
                    self.assertTrue(report['warnings'])
                    self.assertEqual(report['assumptions'], self.saved['assumptions'])
                    self.assertGreater(report['assumptions']['enabled_assumed_records'], 0)
                    self.assertIn('saved default_timezone=Europe/Berlin', err)
                    self.assertNotIn('Not/AZone', err)
                    records = {r['id']: r for r in self.saved['schedules']}
                    self.assertEqual([r['id'] for r in report['plan']],
                                     sorted(r['id'] for r in report['plan']))
                    for row in report['plan']:
                        original = records[row['id']]
                        for key in ('platform', 'file', 'workflow', 'node', 'timezone'):
                            self.assertEqual(row[key], original[key])
                        self.assertIn(row['offset_minutes'], range(11))
                        if original['platform'] == 'windmill':
                            fields = row['proposed_source_expr'].split()
                            self.assertEqual((fields[0], fields[-1], len(fields)), ('17', '*', 7))

    def test_strict_partial_disabled_unsupported_and_text(self):
        code, raw, err = self.suggest('--strict')
        self.assertEqual(code, 2, err)
        self.assertFalse(json.loads(raw)['complete'])
        code, out, err = self.suggest('--format', 'text')
        self.assertEqual(code, 0, err)
        self.assertIn('Peak ', out)
        self.assertIn('Read-only plan.', out)
        # Disabled unsupported records still trigger strict even with complete coverage.
        for r in self.saved['schedules']:
            if not r['supported']:
                r['enabled'] = False
        self.snapshot.write_text(json.dumps(self.saved))
        code, raw, err = self.suggest('--strict')
        self.assertEqual(code, 2, err)
        self.assertTrue(json.loads(raw)['complete'])
        self.saved['schedules'] = [r for r in self.saved['schedules'] if r['supported']]
        self.recount()
        code, raw, err = self.suggest('--strict')
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(raw)['complete'])

    def recount(self):
        for key, flag in [('timezone_assumed_records', 'timezone_assumed'),
                          ('enabled_assumed_records', 'enabled_assumed')]:
            self.saved['assumptions'][key] = sum(r[flag] for r in self.saved['schedules'])
        self.snapshot.write_text(json.dumps(self.saved))

    def test_no_active_and_all_disabled_match_source(self):
        for content in ['schedule: "0 30 2 * * *"\nenabled: false\n',
                        'schedule: "not cron"\n']:
            with self.subTest(content=content):
                source = self.root / 'only.schedule.yaml'
                source.write_text(content)
                code, raw, err = self.cli('extract', source, '--format', 'snapshot')
                self.assertEqual(code, 0, err)
                self.snapshot.write_text(raw)
                for strict in [(), ('--strict',)]:
                    code, out, err = self.cli('suggest', source, '--from', '2026-10-25', '--days', '1',
                                              '--format', 'json', *strict)
                    self.assertEqual((code, out), (2, ''), err)
                    self.assertIn('no enabled, supported', err)
                    code, out, err = self.suggest(*strict)
                    self.assertEqual((code, out), (2, ''), err)
                    self.assertIn('no enabled, supported', err)

    def test_input_groups_and_repeated_selector_reject_before_reads(self):
        for args in [(), ('--snapshot', self.snapshot, self.source),
                     (self.source, '--snapshot', self.snapshot),
                     ('--snapshot', self.snapshot, '--snapshot', self.snapshot),
                     ('--snapshot=missing.json', f'--snapshot={self.snapshot}')]:
            with self.subTest(args=args), \
                    patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                    patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')):
                code, out, err = self.cli('suggest', *args)
                self.assertEqual((code, out), (2, ''), err)
        self.invalid('--format', 'snapshot')
        for extra in [('--from', 'bad'), ('--from', '2099-01-01'), ('--days', '0'), ('--days', '731'),
                      ('--max-shift', '-1'), ('--max-shift', '60m'),
                      ('--window', '59'), ('--window', '61'), ('--window', '86460')]:
            self.invalid(*extra)
        code, raw, err = self.cli('suggest', self.source / 'jobs.crontab', self.source / 'tick.schedule.yaml',
                                  '--from', '2026-10-25', '--days', '1', '--format', 'json',
                                  '--default-timezone', 'Europe/Berlin')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(raw)['before']['total_firings'], 6)

    def test_invalid_schema_ids_assumptions_and_json_are_atomic(self):
        cases = []
        for key, value in [('schema', 'other'), ('version', 2), ('version', True),
                           ('schedules', []), ('extra', 1), ('warnings', [False])]:
            data = deepcopy(self.saved); data[key] = value; cases.append(data)
        for key, value in [('id', 'bad'), ('second', 59), ('expr', '0 0 * * *'),
                           ('timezone', 'Invalid/Zone')]:
            data = deepcopy(self.saved)
            next(r for r in data['schedules'] if r['supported'] and r['enabled'])[key] = value
            cases.append(data)
        for key, value in [('timezone_assumed_records', 0), ('enabled_assumed_records', True),
                           ('default_timezone', 'UTC')]:
            data = deepcopy(self.saved); data['assumptions'][key] = value; cases.append(data)
        data = deepcopy(self.saved); data['schedules'].append(data['schedules'][0]); cases.append(data)
        cases += [{'schedules': self.saved['schedules']}, None, []]
        for data in cases:
            with self.subTest(data=data):
                self.snapshot.write_text(json.dumps(data)); self.invalid()
        for raw in [b'{', b'\xff', b'{"version":1,"version":1}', b'{"x":NaN}',
                    b'[' * 1500 + b']' * 1500]:
            self.snapshot.write_bytes(raw); self.invalid()

    def test_file_record_byte_forecast_optimizer_and_result_bounds_are_atomic(self):
        real = self.root / 'real.json'; self.snapshot.rename(real)
        self.snapshot.symlink_to(real); self.invalid(); self.snapshot.unlink()
        self.invalid()  # missing
        code, out, err = self.cli('suggest', '--snapshot', '', '--format', 'json')
        self.assertEqual((code, out), (2, ''), err)
        self.assertIn('snapshot', err)
        self.snapshot.mkdir(); self.invalid(); self.snapshot.rmdir()
        os.mkfifo(self.snapshot); self.invalid(); self.snapshot.unlink()
        self.snapshot.write_bytes(real.read_bytes())
        for target, limit in [('flowwrench.schedules.snapshot.MAX_SNAPSHOT_BYTES', 10),
                              ('flowwrench.schedules.snapshot.MAX_RECORDS', 1),
                              ('flowwrench.schedules.analysis.MAX_CANDIDATES', 1),
                              ('flowwrench.schedules.analysis.MAX_SCORE_WORK', 1),
                              ('flowwrench.schedules.analysis.MAX_FIRINGS', 1)]:
            with self.subTest(target=target), patch(target, limit):
                self.invalid()

    def test_zero_shift_and_window_boundaries(self):
        code, raw, err = self.suggest('--max-shift', '0', '--window', '86400')
        self.assertEqual(code, 0, err)
        report = json.loads(raw)
        self.assertEqual(report['before'], report['after'])
        self.assertTrue(all(r['offset_minutes'] == 0 and r['original_expr'] == r['proposed_expr']
                            for r in report['plan']))
        self.assertEqual(report['before']['window_seconds'], 86400)
        code, raw, err = self.suggest('--max-shift', '59m', '--window', '60')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(raw)['max_shift_minutes'], 59)

    def test_same_hour_no_fire_and_half_hour_dst_fire_counts(self):
        cases = [('UTC', '2026-11-01', 1, '59 * * * *', 24, 0),
                 ('UTC', '2026-11-01', 1, '0 0 29 FEB *', 0, 0),
                 ('Australia/Lord_Howe', '2027-10-02', 2, '25 2 * * *', 1, 4),
                 ('Australia/Lord_Howe', '2027-04-03', 1, '25 1 * * *', 1, 4),
                 ('Australia/Lord_Howe', '2027-04-03', 1, '30 1 * * *', 2, 10)]
        for zone, day, days, expr, fires, max_offset in cases:
            with self.subTest(zone=zone, day=day, expr=expr):
                source = self.root / 'boundary.crontab'
                source.write_text(f'CRON_TZ={zone}\n{expr} first\n{expr} second\n')
                code, raw, err = self.cli('extract', source, '--format', 'snapshot')
                self.assertEqual(code, 0, err)
                self.snapshot.write_text(raw)
                source.unlink()
                code, raw, err = self.suggest('--from', day, '--days', days)
                self.assertEqual(code, 0, err)
                report = json.loads(raw)
                self.assertEqual(report['before']['total_firings'], 2 * fires)
                self.assertEqual(report['after']['total_firings'], 2 * fires)
                self.assertTrue(all(r['offset_minutes'] <= max_offset for r in report['plan']))
