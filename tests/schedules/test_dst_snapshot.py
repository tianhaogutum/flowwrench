"""Saved DST input/output, exit, metadata and atomic failure contracts."""
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


class DstSnapshotTests(unittest.TestCase):
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
        (self.source / 'tick.schedule.yaml').write_text('schedule: "17 30 2 * * *"\n')
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

    def dst(self, *extra):
        return self.cli('dst', '--snapshot', self.snapshot, '--from', '2026-10-25',
                        '--days', '1', '--format', 'json', *extra)

    def invalid(self, *extra):
        code, out, err = self.dst(*extra)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def test_gap_fold_reports_equal_source_after_export_deletion(self):
        expected = {}
        for day in ('2026-10-25', '2027-03-28', '2026-11-01'):
            code, raw, err = self.cli('dst', self.source, '--default-timezone', 'Europe/Berlin',
                                      '--from', day, '--days', '1', '--format', 'json')
            self.assertEqual(code, 0, err)
            expected[day] = json.loads(raw)
        for file in self.source.iterdir():
            file.unlink()
        self.source.rmdir()
        self.saved['schedules'].reverse()
        self.snapshot.write_text(json.dumps(self.saved))
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('original export read')):
            for day, hazard in [('2026-10-25', 'ambiguous'), ('2027-03-28', 'skipped'),
                                ('2026-11-01', None)]:
                with self.subTest(day=day):
                    code, raw, err = self.dst('--from', day, '--default-timezone', 'Not/AZone')
                    self.assertEqual(code, 0, err)
                    report = json.loads(raw)
                    self.assertEqual(report, expected[day])
                    self.assertEqual((report['records'], report['analyzed'], report['disabled'],
                                      report['unsupported'], report['complete']), (6, 4, 1, 1, False))
                    self.assertEqual(report['warnings'], self.saved['warnings'])
                    self.assertTrue(report['warnings'])
                    self.assertEqual(report['assumptions'], self.saved['assumptions'])
                    self.assertGreater(report['assumptions']['enabled_assumed_records'], 0)
                    self.assertIn('saved default_timezone=Europe/Berlin', err)
                    self.assertNotIn('Not/AZone', err)
                    self.assertEqual(len(report['hazards']), 4 if hazard else 0)
                    records = {r['id']: r for r in self.saved['schedules']}
                    for h in report['hazards']:
                        self.assertEqual(h['hazard'], hazard)
                        self.assertEqual(len(h['utc_instants']), 2 if hazard == 'ambiguous' else 0)
                        for key in ('platform', 'workflow', 'node', 'timezone'):
                            self.assertEqual(h[key], records[h['id']][key])
                        if hazard == 'ambiguous':
                            minute = h['local_time'][-2:]
                            self.assertEqual(h['utc_instants'],
                                             [f'{day}T00:{minute}Z', f'{day}T01:{minute}Z'])

    def test_strict_partial_disabled_unsupported_and_text(self):
        code, raw, err = self.dst('--strict')
        self.assertEqual(code, 2, err)
        self.assertFalse(json.loads(raw)['complete'])
        code, out, err = self.dst('--format', 'text')
        self.assertEqual(code, 0, err)
        self.assertIn('4 DST hazards; 4 schedules analyzed', out)
        self.assertIn('ambiguous: 2026-10-25T02:30 Europe/Berlin', out)
        # Disabled unsupported records still trigger strict even with complete coverage.
        for r in self.saved['schedules']:
            if not r['supported']:
                r['enabled'] = False
        self.snapshot.write_text(json.dumps(self.saved))
        code, raw, err = self.dst('--strict')
        self.assertEqual(code, 2, err)
        self.assertTrue(json.loads(raw)['complete'])
        self.saved['schedules'] = [r for r in self.saved['schedules'] if r['supported']]
        self.recount()
        code, raw, err = self.dst('--strict')
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
                    code, out, err = self.cli('dst', source, '--from', '2026-10-25', '--days', '1',
                                              '--format', 'json', *strict)
                    self.assertEqual((code, out), (2, ''), err)
                    self.assertIn('no enabled, supported', err)
                    code, out, err = self.dst(*strict)
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
                code, out, err = self.cli('dst', *args)
                self.assertEqual((code, out), (2, ''), err)
        self.invalid('--format', 'snapshot')
        for extra in [('--from', 'bad'), ('--from', '2099-01-01'), ('--days', '0'), ('--days', '731')]:
            self.invalid(*extra)
        code, raw, err = self.cli('dst', self.source / 'jobs.crontab', self.source / 'tick.schedule.yaml',
                                  '--from', '2026-10-25', '--days', '1', '--format', 'json',
                                  '--default-timezone', 'Europe/Berlin')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(raw)['hazards']), 3)

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

    def test_file_record_byte_and_dst_bounds_are_atomic(self):
        real = self.root / 'real.json'; self.snapshot.rename(real)
        self.snapshot.symlink_to(real); self.invalid(); self.snapshot.unlink()
        self.invalid()  # missing
        self.invalid('--snapshot', '')  # empty path
        self.snapshot.mkdir(); self.invalid(); self.snapshot.rmdir()
        os.mkfifo(self.snapshot); self.invalid(); self.snapshot.unlink()
        self.snapshot.write_bytes(real.read_bytes())
        for target, limit in [('flowwrench.schedules.snapshot.MAX_SNAPSHOT_BYTES', 10),
                              ('flowwrench.schedules.snapshot.MAX_RECORDS', 1),
                              ('flowwrench.schedules.analysis.MAX_CANDIDATES', 1),
                              ('flowwrench.schedules.analysis.MAX_HAZARDS', 1)]:
            with self.subTest(target=target), patch(target, limit):
                self.invalid()
