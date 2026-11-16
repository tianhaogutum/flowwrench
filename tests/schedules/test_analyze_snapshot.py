"""Standalone saved-analysis IO, provenance, negative and exit contracts."""
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


class AnalyzeSnapshotTests(unittest.TestCase):
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
        code, out, err = self.cli('extract', self.source, '--format', 'snapshot',
                                  '--default-timezone', 'Europe/Berlin')
        self.assertEqual(code, 0, err)
        self.saved = json.loads(out)
        self.snapshot = self.root / 'inventory.json'
        self.snapshot.write_text(out)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def analyze(self, *extra):
        return self.cli('analyze', '--snapshot', self.snapshot, '--from', '2026-10-25',
                        '--days', '1', '--window', '120', '--format', 'json', *extra)

    def invalid(self, *extra):
        code, out, err = self.analyze(*extra)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def test_snapshot_analysis_equals_source_after_export_deletion(self):
        code, out, err = self.cli('analyze', self.source, '--default-timezone', 'Europe/Berlin',
                                  '--from', '2026-10-25', '--days', '1', '--window', '120', '--format', 'json')
        self.assertEqual(code, 0, err)
        expected = json.loads(out)
        for file in self.source.iterdir():
            file.unlink()
        self.source.rmdir()
        code, out, err = self.analyze('--default-timezone', 'Not/AZone')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report, expected)
        self.assertEqual((report['total_firings'], report['peak'], report['peak_at']),
                         (8, 4, '2026-10-25T00:31Z'))
        self.assertEqual((report['records'], report['disabled'], report['unsupported']), (6, 1, 1))
        self.assertEqual([h['hazard'] for h in report['dst_hazards']], ['ambiguous'] * 4)
        self.assertFalse(report['complete'])
        self.assertEqual(report['schedules'], self.saved['schedules'])
        self.assertEqual(report['warnings'], self.saved['warnings'])
        self.assertTrue(report['warnings'])
        self.assertEqual(report['assumptions'], self.saved['assumptions'])
        self.assertGreater(report['assumptions']['enabled_assumed_records'], 0)
        self.assertIn('saved default_timezone=Europe/Berlin', err)
        self.assertNotIn('Not/AZone', err)
        # Even an externally reordered snapshot yields identical report arrays.
        self.saved['schedules'].reverse()
        self.snapshot.write_text(json.dumps(self.saved))
        self.assertEqual(json.loads(self.analyze()[1]), report)

    def test_snapshot_recomputes_horizon_and_window_and_keeps_source_seconds(self):
        code, out, err = self.analyze('--window', '60')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['peak'], 3)
        self.assertNotIn('window_histogram', report)
        self.assertIn(17, [r['second'] for r in report['schedules']])
        code, out, err = self.analyze('--from', '2027-03-28')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report['total_firings'], report['peak']), (0, 0))
        self.assertEqual([h['hazard'] for h in report['dst_hazards']], ['skipped'] * 4)

    def test_strict_threshold_precedence_and_text(self):
        for extra, expected in [((), 0), (('--max-peak', '3'), 1), (('--max-peak', '4'), 0),
                                (('--strict',), 2), (('--strict', '--max-peak', '3'), 2)]:
            with self.subTest(extra=extra):
                code, out, err = self.analyze(*extra)
                self.assertEqual(code, expected, err)
                self.assertEqual(json.loads(out)['peak'], 4)
        code, out, err = self.analyze('--format', 'text')
        self.assertEqual(code, 0, err)
        self.assertIn('Peak 4 at 2026-10-25T00:31Z in trailing 120s', out)
        self.assertIn('Coverage: 4/6 analyzed; 1 disabled; 1 unsupported', out)

    def test_exactly_one_input_and_existing_analysis_flags(self):
        for args in [(), ('--snapshot', self.snapshot, self.source),
                     (self.source, '--snapshot', self.snapshot), ('--snapshot', ''),
                     ('--snapshot', self.snapshot, '--format', 'snapshot')]:
            with self.subTest(args=args):
                code, out, err = self.cli('analyze', *args)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
        for extra in [('--window', '61'), ('--from', 'bad'), ('--days', '0'), ('--max-peak', '-1')]:
            self.invalid(*extra)
        # Multiple source paths remain a supported alternative.
        code, out, err = self.cli('analyze', self.source / 'jobs.crontab', self.source / 'tick.schedule.yaml',
                                  '--from', '2026-10-25', '--days', '1', '--format', 'json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['records'], 3)

    def test_invalid_snapshot_schema_and_inconsistent_data_are_atomic(self):
        cases = []
        for key, value in [('schema', 'other'), ('version', 2), ('version', True),
                           ('schedules', []), ('extra', 1)]:
            data = deepcopy(self.saved); data[key] = value; cases.append(data)
        for key, value in [('second', 59), ('expr', '0 0 * * *'), ('timezone', 'Invalid/Zone')]:
            data = deepcopy(self.saved)
            next(r for r in data['schedules'] if r['supported'] and r['enabled'])[key] = value
            cases.append(data)
        data = deepcopy(self.saved); data['assumptions']['timezone_assumed_records'] = 0; cases.append(data)
        data = deepcopy(self.saved); data['schedules'].append(data['schedules'][0]); cases.append(data)
        cases += [{'schedules': self.saved['schedules']}, None, []]
        for data in cases:
            with self.subTest(data=data):
                self.snapshot.write_text(json.dumps(data)); self.invalid()
        for raw in [b'{', b'\xff', b'{"version":1,"version":1}', b'{"x":NaN}',
                    b'[' * 1500 + b']' * 1500]:
            self.snapshot.write_bytes(raw); self.invalid()

    def test_file_record_byte_and_forecast_bounds_remain_atomic(self):
        real = self.root / 'real.json'; self.snapshot.rename(real)
        self.snapshot.symlink_to(real); self.invalid(); self.snapshot.unlink()
        self.invalid()  # missing
        self.snapshot.mkdir(); self.invalid(); self.snapshot.rmdir()
        os.mkfifo(self.snapshot); self.invalid(); self.snapshot.unlink()
        self.snapshot.write_bytes(real.read_bytes())
        for target, limit in [('flowwrench.schedules.snapshot.MAX_SNAPSHOT_BYTES', 10),
                              ('flowwrench.schedules.snapshot.MAX_RECORDS', 1),
                              ('flowwrench.schedules.analysis.MAX_CANDIDATES', 1),
                              ('flowwrench.schedules.analysis.MAX_FIRINGS', 1),
                              ('flowwrench.schedules.analysis.MAX_HAZARDS', 1)]:
            with self.subTest(target=target), patch(target, limit):
                self.invalid()

    def test_no_active_records_and_disabled_unsupported_strict_match_source(self):
        for supported in [True, False]:
            data = deepcopy(self.saved)
            data['schedules'] = [r for r in data['schedules'] if r['supported'] == supported]
            for r in data['schedules']:
                r['enabled'] = False
            for key, flag in [('timezone_assumed_records', 'timezone_assumed'),
                              ('enabled_assumed_records', 'enabled_assumed')]:
                data['assumptions'][key] = sum(r[flag] for r in data['schedules'])
            self.snapshot.write_text(json.dumps(data)); self.invalid()
        data = deepcopy(self.saved)
        for r in data['schedules']:
            if not r['supported']:
                r['enabled'] = False
        self.snapshot.write_text(json.dumps(data))
        code, out, err = self.analyze('--strict')
        self.assertEqual(code, 2, err)
        self.assertTrue(json.loads(out)['complete'])
