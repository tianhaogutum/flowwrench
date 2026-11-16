"""Compare CI gates use after start-count peaks without changing reports."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main
from flowwrench.schedules.extract import extract
from flowwrench.schedules.model import make_schedule
from flowwrench.schedules.snapshot import snapshot_json


class CompareThresholdTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.before = self.root / 'before.crontab'
        self.after = self.root / 'after.crontab'
        self.before.write_text('0 0 * * * a\n' * 3)
        self.after.write_text('0 0 * * * a\n1 0 * * * b\n')
        self.saved = {}
        for side in ('before', 'after'):
            path = self.root / f'{side}.json'
            path.write_text(snapshot_json(extract([getattr(self, side)])))
            self.saved[side] = path

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def args(self, saved_before=False, saved_after=False, fmt='json', window=60, day='2026-11-01', days=1):
        return ['compare',
                '--before-snapshot' if saved_before else '--before', self.saved['before'] if saved_before else self.before,
                '--after-snapshot' if saved_after else '--after', self.saved['after'] if saved_after else self.after,
                '--from', day, '--days', days, '--window', window, '--format', fmt]

    def assert_gate(self, args, peak):
        baseline = self.cli(*args)
        self.assertEqual(baseline[0], 0, baseline[2])
        for threshold in sorted({0, max(0, peak - 1), peak, peak + 1}):
            with self.subTest(threshold=threshold, args=args):
                result = self.cli(*args, '--max-peak', threshold)
                self.assertEqual(result[0], int(peak > threshold), result[2])
                self.assertEqual(result[1:], baseline[1:])
        return json.loads(baseline[1]) if args[-1] == 'json' else None

    def test_threshold_source_saved_mixed_full_report_parity(self):
        for old, new in ((False, False), (True, True), (True, False), (False, True)):
            for fmt in ('json', 'text'):
                for window, peak in ((60, 1), (120, 2)):
                    with self.subTest(old=old, new=new, fmt=fmt, window=window):
                        report = self.assert_gate(self.args(old, new, fmt, window), peak)
                        if report:
                            self.assertEqual(report['after']['peak'], peak)
                            self.assertEqual(report['before']['peak'], 3)
                            self.assertEqual(report['after']['assumptions']['default_timezone'], 'UTC')

    def test_threshold_recomputes_horizon_and_no_warmup(self):
        self.after.write_text('59 23 1 11 * a\n0 0 2 11 * b\n')
        for day, days, window, peak in (('2026-11-01', 1, 120, 1),
                                        ('2026-11-02', 1, 120, 1),
                                        ('2026-11-01', 2, 120, 2),
                                        ('2026-11-01', 2, 60, 1)):
            with self.subTest(day=day, days=days, window=window):
                report = self.assert_gate(self.args(day=day, days=days, window=window), peak)
                self.assertEqual(report['after']['peak'], peak)

    def test_threshold_saved_only_without_exports_keeps_saved_assumptions(self):
        expected = self.cli(*self.args(True, True))
        self.before.unlink()
        self.after.unlink()
        result = self.cli(*self.args(True, True), '--default-timezone', 'Invalid/Zone', '--max-peak=0')
        self.assertEqual(result[0], 1, result[2])
        self.assertEqual(result[1:], expected[1:])
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')):
            self.assert_gate(self.args(True, True), 1)

    def test_threshold_zero_active_partial_and_strict_precedence(self):
        inv = extract([self.after])
        for r in inv.schedules:
            r.enabled = False
        self.saved['after'].write_text(snapshot_json(inv))
        report = self.assert_gate(self.args(True, True), 0)
        self.assertTrue(report['complete'])
        for side in ('before', 'after'):
            for disabled in (False, True):
                inv = extract([getattr(self, side)])
                inv.schedules.append(make_schedule('crontab', 'bad.crontab', 'bad.json', 'bad', 'bad',
                                                    '0 0 L * *', 'UTC', enabled=not disabled))
                self.saved[side].write_text(snapshot_json(inv))
                args = self.args(side == 'before', side == 'after')
                baseline = self.cli(*args)
                for threshold in (0, 100):
                    with self.subTest(side=side, disabled=disabled, threshold=threshold):
                        result = self.cli(*args, '--max-peak', threshold)
                        self.assertEqual(result[0], int(1 > threshold), result[2])
                        self.assertEqual(result[1:], baseline[1:])
                        strict = self.cli(*args, '--max-peak', threshold, '--strict')
                        self.assertEqual(strict[0], 2, strict[2])
                        self.assertEqual(strict[1:], baseline[1:])
        self.after.write_text('0 0 L * * bad\n')
        self.assertFalse(self.assert_gate(self.args(), 0)['complete'])

    def test_threshold_uses_starts_with_zero_long_or_unknown_durations(self):
        # Starts at 00:00/00:01: peak 1 (60s), 2 (120s). Long durations overlap;
        # zero durations have overlap peak 0 but still produce the same starts.
        for value, overlap_peak in ((0, 0), (600, 2), (None, None)):
            sidecars = []
            for side in ('before', 'after'):
                inv = extract([getattr(self, side)])
                path = self.root / f'{side}-duration.json'
                path.write_text(json.dumps({'version': 1, 'durations_seconds': {r.id: value for r in inv.schedules}}))
                sidecars += [f'--{side}-durations', path]
            for window, peak in ((60, 1), (120, 2)):
                args = [*self.args(True, True, window=window), *sidecars]
                baseline = self.cli(*args)
                report = json.loads(baseline[1])
                self.assertEqual(report['overlap']['after']['peak'], overlap_peak)
                self.assertEqual(report['after']['peak'], peak)
                for threshold in (0, 1, 2):
                    with self.subTest(duration=value, window=window, threshold=threshold):
                        result = self.cli(*args, '--max-peak', threshold)
                        self.assertEqual(result[0], int(peak > threshold), result[2])
                        self.assertEqual(result[1:], baseline[1:])
                        strict = self.cli(*args, '--max-peak', threshold, '--strict')
                        self.assertEqual(strict[0], 2 if value is None else int(peak > threshold))
                        self.assertEqual(strict[1:], baseline[1:])

    def test_threshold_invalid_before_any_input_read(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')):
            for value in ('-1', '1.5', '1e3', 'NaN', 'inf', 'true', '', '1m'):
                for fmt in ('json', 'text'):
                    for extra in (['--max-peak', value], [f'--max-peak={value}']):
                        with self.subTest(value=value, fmt=fmt, extra=extra):
                            code, out, err = self.cli(*self.args(True, False, fmt), *extra)
                            self.assertEqual((code, out), (2, ''), err)
                            self.assertIn('--max-peak', err)
                            self.assertIn('error:', err)
                            self.assertNotIn('Traceback', err)
            code, out, err = self.cli(*self.args(), '--max-peak')
            self.assertEqual((code, out), (2, ''), err)
            self.assertIn('expected one argument', err)

    def test_threshold_does_not_bypass_selector_schema_id_or_resource_errors(self):
        cases = [self.args() + ['--before', self.before],
                 self.args() + ['--after-snapshot', self.saved['after']],
                 self.args() + ['--before-durations', 'missing.json'],
                 self.args(window=61)]
        original = json.loads(self.saved['after'].read_text())
        for mutation in ('version', 'id'):
            data = json.loads(json.dumps(original))
            if mutation == 'version':
                data['version'] = 2
            else:
                data['schedules'][0]['id'] = 'wrong'
            self.saved['after'].write_text(json.dumps(data))
            code, out, err = self.cli(*self.args(True, True), '--max-peak', 0)
            self.assertEqual((code, out), (2, ''), err)
            self.assertIn('error:', err)
        self.saved['after'].write_text(json.dumps(original))
        for args in cases:
            code, out, err = self.cli(*args, '--max-peak', 0)
            self.assertEqual((code, out), (2, ''), err)
            self.assertIn('error:', err)
        for target in ('flowwrench.schedules.analysis.MAX_CANDIDATES', 'flowwrench.schedules.comparison.MAX_FIRINGS',
                       'flowwrench.schedules.comparison.MAX_CHANGES'):
            with patch(target, 1):
                code, out, err = self.cli(*self.args(), '--max-peak', 0)
                self.assertEqual((code, out), (2, ''), err)
                self.assertIn('limit', err)
