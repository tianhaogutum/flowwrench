"""Per-side duration comparison contracts; start counts remain independent."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.analysis import horizon
from flowwrench.schedules.comparison import compare
from flowwrench.schedules.extract import extract
from flowwrench.schedules.model import InputError, make_schedule
from flowwrench.schedules.snapshot import snapshot_json

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / 'examples/overlap'


def record(expr='0 0 * * *', node='a', tz='UTC', **kwargs):
    return make_schedule('test', 'jobs.json', 'jobs.json', 'workflow', node, expr, tz, **kwargs)


class CompareOverlapTests(unittest.TestCase):
    def run_compare(self, before, after, old, new, day='2026-11-01', window=60):
        return compare(before, after, *horizon(day, 1), window,
                       before_durations=old, after_durations=new)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'flowwrench.schedules', 'compare', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def args(self, after='short.json'):
        return ['--before', EXAMPLE / 'jobs.crontab', '--after', EXAMPLE / 'jobs.crontab',
                '--before-durations', EXAMPLE / 'durations.json',
                '--after-durations', EXAMPLE / after, '--from', '2026-11-01', '--days', '1']

    def test_duration_only_change_keeps_start_counts_and_changes_overlap(self):
        a, b = record(), record('5 0 * * *', node='b')
        report = self.run_compare([a, b], [a, b], {a.id: 600, b.id: 300}, {a.id: 300, b.id: 300})
        self.assertEqual((report['peak_delta'], report['unchanged_firings'], report['added'], report['removed']),
                         (0, 2, [], []))
        ov = report['overlap']
        self.assertEqual((ov['before']['peak'], ov['after']['peak'], ov['peak_delta']), (2, 1, -1))
        self.assertTrue(ov['complete'])
        self.assertTrue(report['complete'])
        self.assertEqual(ov['before']['known_peak_intervals'], [
            {'from': '2026-11-01T00:05:00Z', 'until_exclusive': '2026-11-01T00:10:00Z'}])
        self.assertEqual(ov['after']['known_peak_intervals'], [
            {'from': '2026-11-01T00:00:00Z', 'until_exclusive': '2026-11-01T00:10:00Z'}])
        self.assertEqual(report, self.run_compare([b, a], [b, a], {b.id: 300, a.id: 600},
                                                {b.id: 300, a.id: 300}))
        windowed = self.run_compare([a, b], [a, b], {a.id: 600, b.id: 300},
                                    {a.id: 300, b.id: 300}, window=600)
        self.assertEqual(windowed['overlap'], ov)
        self.assertEqual(windowed['before']['peak'], 2)

    def test_partial_sides_never_subtract_known_lower_bounds(self):
        a = record('0 0 29 FEB *')  # No fires still cannot bound unknown carry-in.
        for old, new in [({}, {a.id: 1}), ({a.id: 1}, {}), ({}, {a.id: None})]:
            with self.subTest(old=old, new=new):
                report = self.run_compare([a], [a], old, new)
                ov = report['overlap']
                self.assertIsNone(ov['peak_delta'])
                self.assertFalse(ov['complete'])
                self.assertFalse(report['complete'])
                for side, durations in [('before', old), ('after', new)]:
                    self.assertEqual(ov[side]['known_peak'], 0)
                    self.assertEqual(ov[side]['peak'], 0 if durations.get(a.id) else None)
                    self.assertEqual(ov[side]['unknown_duration_ids'], [] if durations.get(a.id) else [a.id])

    def test_disabled_zero_and_unsupported_unknown(self):
        a = record(enabled=False)
        report = self.run_compare([a], [a], {}, {})
        self.assertEqual(report['overlap']['peak_delta'], 0)
        bad = record('0 0 L * *', node='bad')
        report = self.run_compare([a], [bad], {}, {})
        self.assertIsNone(report['overlap']['after']['peak'])
        self.assertIsNone(report['overlap']['peak_delta'])
        self.assertFalse(report['complete'])

    def test_carry_in_seconds_and_dst_use_shared_overlap_model(self):
        a = record('17 * * * * *', dialect='n8n')
        report = self.run_compare([a], [a], {a.id: 120}, {a.id: 60})['overlap']
        self.assertEqual((report['before']['peak'], report['after']['peak'], report['peak_delta']), (2, 1, -1))
        self.assertEqual((report['before']['carry_in_firings'], report['after']['carry_in_firings']), (2, 1))
        self.assertEqual(report['before']['known_peak_intervals'][0]['from'], '2026-11-01T00:00:00Z')
        a = record('30 2 * * *', tz='Europe/Berlin')
        report = self.run_compare([a], [a], {a.id: 7200}, {a.id: 3600}, day='2026-10-25')
        self.assertEqual(report['overlap']['peak_delta'], -1)
        self.assertEqual(report['overlap']['before']['known_firings'], 2)
        self.assertEqual(report['before']['dst_hazards'][0]['hazard'], 'ambiguous')

    def test_side_ids_validated_independently_and_pair_required(self):
        a, b = record(), record(node='b')
        for old, new in [({b.id: 1}, {b.id: 1}), ({a.id: 1}, {a.id: 1})]:
            with self.assertRaisesRegex(InputError, 'unknown schedule ID'):
                self.run_compare([a], [b], old, new)
        with self.assertRaisesRegex(InputError, 'both'):
            self.run_compare([a], [a], {}, None)

    def test_shared_comparison_bounds_and_atomic_cli_failure(self):
        a = record()
        with patch('flowwrench.schedules.overlap.MAX_FIRINGS', 1):
            with self.assertRaisesRegex(InputError, 'firing limit'):
                self.run_compare([a], [a], {a.id: 1}, {a.id: 1})
        with patch('flowwrench.schedules.analysis.MAX_CANDIDATES', 3):
            with self.assertRaisesRegex(InputError, 'work limit'):
                self.run_compare([a], [a], {a.id: 1}, {a.id: 1})
        from flowwrench.schedules.cli import main
        out, err = StringIO(), StringIO()
        with patch('flowwrench.schedules.overlap.MAX_PEAK_INTERVALS', 1), redirect_stdout(out), redirect_stderr(err):
            code = main(['compare', *map(str, self.args()), '--format', 'json'])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('interval limit', err.getvalue())

    def test_cli_complete_partial_text_and_strict(self):
        result = self.cli(*self.args(), '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['overlap']['peak_delta'], -1)
        text = self.cli(*self.args()).stdout
        self.assertIn('Overlap peak 2 -> 1; delta -1', text)
        result = self.cli(*self.args('partial.json'), '--format', 'json', '--strict')
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['overlap']['after']['known_peak'], 1)
        self.assertIsNone(report['overlap']['after']['peak'])
        self.assertIsNone(report['overlap']['peak_delta'])
        self.assertTrue(report['after']['complete'])
        self.assertFalse(report['complete'])
        text = self.cli(*self.args('partial.json'))
        self.assertEqual(text.returncode, 0)
        self.assertIn('Overlap peak 2 -> unknown; delta unknown', text.stdout)
        self.assertIn('after known peak 1; 1 unknown durations', text.stdout)

    def test_snapshot_sides_keep_saved_ids_and_assumptions(self):
        inv = extract([EXAMPLE / 'jobs.crontab'])
        with tempfile.TemporaryDirectory() as temp:
            saved = Path(temp) / 'saved.json'
            saved.write_text(snapshot_json(inv))
            expected = self.cli(*self.args(), '--format', 'json')
            for inputs in [('--before-snapshot', saved, '--after', EXAMPLE / 'jobs.crontab'),
                           ('--before', EXAMPLE / 'jobs.crontab', '--after-snapshot', saved),
                           ('--before-snapshot', saved, '--after-snapshot', saved)]:
                result = self.cli(*inputs, *self.args()[4:], '--default-timezone', 'Asia/Tokyo', '--format', 'json')
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['overlap'], json.loads(expected.stdout)['overlap'])
                for side in ('before', 'after'):
                    if f'--{side}-snapshot' in inputs:
                        self.assertEqual(report[side]['assumptions']['default_timezone'], 'UTC')

    def test_cli_negative_sidecar_contracts_on_either_side(self):
        identities = [r.id for r in extract([EXAMPLE / 'jobs.crontab']).schedules]
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'bad.json'
            invalid = ['{"version":1,"durations_seconds":{"unknown":1}}',
                       '{"version":1,"version":1,"durations_seconds":{}}',
                       '{"version":true,"durations_seconds":{}}',
                       '{"version":1,"durations_seconds":{},"extra":1}',
                       '{"version":2,"durations_seconds":{}}', '{', 'NaN']
            for value in (True, -1, 604801, 1.5, '60', float('inf')):
                invalid.append(json.dumps({'version': 1, 'durations_seconds': {identities[0]: value}}))
            for side in ('before', 'after'):
                for payload in invalid:
                    bad.write_text(payload)
                    args = self.args()
                    args[args.index(f'--{side}-durations') + 1] = bad
                    result = self.cli(*args, '--format', 'json')
                    self.assertEqual(result.returncode, 2, (side, payload, result.stderr))
                    self.assertEqual(result.stdout, '')
                    self.assertNotIn('Traceback', result.stderr)
            for side in ('before', 'after'):
                args = self.args()
                index = args.index(f'--{side}-durations')
                del args[index:index + 2]
                result = self.cli(*args, '--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')

    def test_cli_sidecar_file_rejections_and_empty_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = root / 'bad.json'
            bad.write_bytes(b'\xff')
            link = root / 'link.json'
            link.symlink_to(EXAMPLE / 'durations.json')
            for side in ('before', 'after'):
                for path in ('', root, root / 'missing', bad, link):
                    args = self.args()
                    args[args.index(f'--{side}-durations') + 1] = path
                    result = self.cli(*args, '--format', 'json')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                bad.write_bytes(b' ' * (1024 * 1024 + 1))
                args = self.args()
                args[args.index(f'--{side}-durations') + 1] = bad
                result = self.cli(*args, '--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('size limit', result.stderr)
                bad.write_bytes(b'\xff')

    def test_changed_inventories_accept_distinct_sidecars_and_disabled_strictness(self):
        a, b = record(), record('5 0 * * *', node='b')
        report = self.run_compare([a], [b], {a.id: 600}, {b.id: 300})
        self.assertEqual((len(report['removed']), len(report['added'])), (1, 1))
        self.assertEqual(report['overlap']['peak_delta'], 0)
        with tempfile.TemporaryDirectory() as temp:
            saved = Path(temp) / 'saved.json'
            # Disabled unsupported records do not make modeled overlap unknown,
            # but --strict must still reject unsupported metadata on either side.
            inv = extract([EXAMPLE / 'jobs.crontab'])
            inv.schedules.append(make_schedule('crontab', 'bad.crontab', 'bad.crontab',
                                               'bad', 'bad', '0 0 L * *', 'UTC',
                                               enabled=False))
            saved.write_text(snapshot_json(inv))
            for side in ('before', 'after'):
                args = self.args()
                idx = args.index(f'--{side}')
                args[idx:idx + 2] = [f'--{side}-snapshot', saved]
                result = self.cli(*args, '--strict', '--format', 'json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(json.loads(result.stdout)['overlap']['complete'])
