"""Saved overlap input contracts, source independence and atomic failures."""
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


class OverlapSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'exports'
        self.source.mkdir()
        (self.source / 'jobs.crontab').write_text('CRON_TZ=UTC\n59 23 * * * true\n0 0 * * * true\n')
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
        self.durations = self.root / 'durations.json'
        values = {'59 23 * * *': 120, '0 0 * * *': 60, '30 2 * * *': 7200}
        self.known = {r['id']: 20 if r['second'] == 17 else values[r['expr']]
                      for r in self.saved['schedules'] if r['supported'] and r['enabled']}
        self.write_durations(self.known)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def write_durations(self, values):
        self.durations.write_text(json.dumps({'version': 1, 'durations_seconds': values}))

    def write_snapshot(self, data):
        self.snapshot.write_text(json.dumps(data))

    def overlap(self, *extra):
        return self.cli('overlap', '--snapshot', self.snapshot, '--durations', self.durations,
                        '--from', '2026-10-25', '--days', '1', '--format', 'json', *extra)

    def invalid(self, *extra):
        code, out, err = self.overlap(*extra)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def supported_only(self):
        data = deepcopy(self.saved)
        data['schedules'] = [r for r in data['schedules'] if r['supported']]
        for key, flag in [('timezone_assumed_records', 'timezone_assumed'),
                          ('enabled_assumed_records', 'enabled_assumed')]:
            data['assumptions'][key] = sum(r[flag] for r in data['schedules'])
        return data

    def test_snapshot_overlap_equals_source_after_export_deletion(self):
        code, out, err = self.cli('overlap', self.source, '--durations', self.durations,
                                  '--default-timezone', 'Europe/Berlin', '--from', '2026-10-25',
                                  '--days', '1', '--format', 'json')
        self.assertEqual(code, 0, err)
        expected = json.loads(out)
        for file in self.source.iterdir():
            file.unlink()
        self.source.rmdir()
        # Any accidental extraction/source read is a regression even with no files.
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('original-export read')):
            code, out, err = self.overlap('--default-timezone', 'Not/AZone')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report, expected)
        self.assertEqual((report['known_peak'], report['known_firings'], report['carry_in_firings']), (3, 6, 1))
        self.assertEqual(report['known_peak_intervals'], [
            {'from': '2026-10-25T01:30:17Z', 'until_exclusive': '2026-10-25T01:30:37Z'}])
        self.assertEqual((report['records'], report['analyzed'], report['disabled'], report['unsupported']), (6, 4, 1, 1))
        self.assertIsNone(report['peak'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['warnings'], self.saved['warnings'])
        self.assertTrue(report['warnings'])
        self.assertEqual(report['assumptions'], self.saved['assumptions'])
        self.assertGreater(report['assumptions']['enabled_assumed_records'], 0)
        self.assertEqual([{k: v for k, v in r.items() if k != 'duration_seconds'}
                          for r in report['schedules']], self.saved['schedules'])
        self.assertIn('saved default_timezone=Europe/Berlin', err)
        self.assertNotIn('Not/AZone', err)
        self.saved['schedules'].reverse()
        self.write_snapshot(self.saved)
        self.assertEqual(json.loads(self.overlap()[1]), report)

    def test_known_unknown_strict_and_text(self):
        self.write_snapshot(self.supported_only())
        code, out, err = self.overlap('--strict')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['peak'], 3)
        self.write_durations({})
        code, out, err = self.overlap()
        self.assertEqual(code, 0, err)
        unknown = json.loads(out)
        self.assertEqual(len(unknown['unknown_duration_ids']), 4)
        self.assertEqual((unknown['known_peak'], unknown['known_peak_intervals']), (0, []))
        self.assertIsNone(unknown['peak'])
        self.write_durations(dict.fromkeys(self.known))
        self.assertEqual(json.loads(self.overlap()[1]), unknown)
        code, out, err = self.overlap('--strict')
        self.assertEqual(code, 2, err)
        self.assertEqual(json.loads(out), unknown)
        code, out, err = self.overlap('--format', 'text')
        self.assertEqual(code, 0, err)
        self.assertIn('Known-duration peak 0; overall peak unknown; 4', out)
        self.write_durations(self.known)
        self.write_snapshot(self.saved)
        self.assertEqual(self.overlap('--strict')[0], 2)
        # Strict still rejects disabled unsupported metadata, even with complete coverage.
        data = deepcopy(self.saved)
        for r in data['schedules']:
            if not r['supported']:
                r['enabled'] = False
        self.write_snapshot(data)
        code, out, err = self.overlap('--strict')
        self.assertEqual(code, 2, err)
        self.assertTrue(json.loads(out)['complete'])

    def test_horizon_and_sidecar_recomputed_and_all_disabled_zero(self):
        self.write_snapshot(self.supported_only())
        code, out, err = self.overlap('--from', '2027-03-28')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report['known_firings'], report['carry_in_firings'], report['peak']), (2, 1, 2))
        self.write_durations({k: 1 for k in self.known})
        code, out, err = self.overlap()
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)['peak'], json.loads(out)['carry_in_firings']), (1, 0))
        data = self.supported_only()
        for r in data['schedules']:
            r['enabled'] = False
        self.write_snapshot(data)
        self.write_durations({})
        code, out, err = self.overlap('--strict')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report['peak'], report['known_firings'], report['carry_in_firings']), (0, 0, 0))
        self.assertTrue(report['complete'])
        self.assertEqual(report['unknown_duration_ids'], [])

    def test_exactly_one_input_and_invalid_options(self):
        for selectors in [(), ('--snapshot', self.snapshot, self.source),
                          (self.source, '--snapshot', self.snapshot), ('--snapshot', '')]:
            with self.subTest(selectors=selectors):
                code, out, err = self.cli('overlap', *selectors, '--durations', self.durations, '--format', 'json')
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
        for extra in [('--format', 'snapshot'), ('--from', 'bad'), ('--days', '0')]:
            self.invalid(*extra)
        code, out, err = self.cli('overlap', '--snapshot', self.snapshot)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        # Multiple source paths are still valid, with a sidecar limited to those IDs.
        self.write_durations({r['id']: self.known[r['id']] for r in self.saved['schedules']
                              if r['platform'] == 'crontab' or r['second'] == 17})
        code, out, err = self.cli('overlap', self.source / 'jobs.crontab', self.source / 'tick.schedule.yaml',
                                  '--durations', self.durations, '--from', '2026-10-25', '--days', '1', '--format', 'json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['records'], 3)

    def test_snapshot_validation_is_atomic(self):
        cases = []
        for key, value in [('schema', 'other'), ('version', 2), ('version', True), ('schedules', []), ('extra', 1)]:
            data = deepcopy(self.saved); data[key] = value; cases.append(data)
        for key, value in [('id', 'bad'), ('expr', 'invalid'), ('timezone', 'Invalid/Zone')]:
            data = deepcopy(self.saved)
            next(r for r in data['schedules'] if r['supported'] and r['enabled'])[key] = value
            cases.append(data)
        data = deepcopy(self.saved); data['schedules'].append(data['schedules'][0]); cases.append(data)
        data = deepcopy(self.saved); data['assumptions']['timezone_assumed_records'] = 0; cases.append(data)
        for data in cases + [None, [], {'schedules': self.saved['schedules']}]:
            with self.subTest(data=data):
                self.write_snapshot(data); self.invalid()
        for raw in [b'{', b'\xff', b'{"version":1,"version":1}', b'{"x":NaN}', b'[' * 1500 + b']' * 1500]:
            self.snapshot.write_bytes(raw); self.invalid()

    def test_sidecar_validation_is_atomic(self):
        for data in [None, {}, {'version': True, 'durations_seconds': {}},
                     {'version': 2, 'durations_seconds': {}}, {'version': 1, 'durations_seconds': []}]:
            self.durations.write_text(json.dumps(data)); self.invalid()
        for value in [-1, True, 1.5, '60', 604801]:
            self.write_durations({next(iter(self.known)): value}); self.invalid()
        self.write_durations({'0' * 20: 60}); self.invalid()
        for raw in [b'{', b'\xff', b'{"version":1,"version":1,"durations_seconds":{}}',
                    b'{"version":1,"durations_seconds":{"x":NaN}}']:
            self.durations.write_bytes(raw); self.invalid()

    def test_file_and_resource_bounds_are_atomic(self):
        for path in [self.snapshot, self.durations]:
            raw = path.read_bytes()
            path.unlink(); self.invalid()
            path.mkdir(); self.invalid(); path.rmdir()
            real = self.root / 'real.json'; real.write_bytes(raw)
            path.symlink_to(real); self.invalid(); path.unlink()
            os.mkfifo(path); self.invalid(); path.unlink()
            path.write_bytes(raw)
        for target in ['flowwrench.schedules.snapshot.MAX_SNAPSHOT_BYTES', 'flowwrench.schedules.snapshot.MAX_RECORDS',
                       'flowwrench.schedules.overlap.MAX_DURATION_BYTES', 'flowwrench.schedules.overlap.MAX_RECORDS',
                       'flowwrench.schedules.analysis.MAX_CANDIDATES', 'flowwrench.schedules.overlap.MAX_FIRINGS']:
            with self.subTest(target=target), patch(target, 1):
                self.invalid()
        with patch('flowwrench.schedules.overlap.MAX_PEAK_INTERVALS', 0):
            self.invalid()
