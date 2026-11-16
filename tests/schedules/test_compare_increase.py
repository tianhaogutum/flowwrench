"""Comparison regression budgets gate signed minute-start peak changes."""
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


class CompareIncreaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = {side: self.root / f'{side}.crontab' for side in ('before', 'after')}
        self.saved = {side: self.root / f'{side}.json' for side in self.sources}
        self.write_pair(1, 3)

    def write_pair(self, before, after):
        for side, count in (('before', before), ('after', after)):
            self.sources[side].write_text('0 0 * * * job\n' * count)
            self.save(side)

    def save(self, side):
        inv = extract([self.sources[side]])
        self.saved[side].write_text(snapshot_json(inv))
        return inv

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def args(self, old=False, new=False, fmt='json', window=60, day='2026-11-01', days=1):
        return ['compare',
                '--before-snapshot' if old else '--before', (self.saved if old else self.sources)['before'],
                '--after-snapshot' if new else '--after', (self.saved if new else self.sources)['after'],
                '--from', day, '--days', days, '--window', window, '--format', fmt]

    def assert_budget(self, args, delta):
        baseline = self.cli(*args)
        self.assertEqual(baseline[0], 0, baseline[2])
        if 'json' in args:
            self.assertEqual(json.loads(baseline[1])['peak_delta'], delta)
        for allowance in sorted({0, max(0, delta - 1), max(0, delta), max(0, delta + 1)}):
            with self.subTest(allowance=allowance, delta=delta, args=args):
                for flag in (['--max-peak-increase', allowance], [f'--max-peak-increase={allowance}']):
                    result = self.cli(*args, *flag)
                    self.assertEqual(result[0], int(delta > allowance), result[2])
                    self.assertEqual(result[1:], baseline[1:])
        return baseline

    def test_signed_budgets_source_saved_mixed_full_report_parity(self):
        for before, after in ((3, 1), (2, 2), (1, 3)):
            self.write_pair(before, after)
            for old, new in ((False, False), (True, True), (True, False), (False, True)):
                for fmt in ('json', 'text'):
                    self.assert_budget(self.args(old, new, fmt), after - before)

    def test_horizon_window_and_no_warmup_recompute_delta(self):
        self.sources['before'].write_text('0 12 * * * a\n')
        self.sources['after'].write_text('59 23 1 11 * a\n0 0 2 11 * b\n')
        for side in self.sources:
            self.save(side)
        for old, new in ((False, False), (True, True), (True, False), (False, True)):
            for day, days, window, delta in (('2026-11-01', 1, 120, 0),
                                            ('2026-11-02', 1, 120, 0),
                                            ('2026-11-01', 2, 120, 1),
                                            ('2026-11-01', 2, 60, 0)):
                self.assert_budget(self.args(old, new, window=window, day=day, days=days), delta)

    def test_saved_without_exports_retains_assumptions(self):
        # Saved defaults are metadata, never replaced by a CLI source default.
        self.saved['before'].write_text(snapshot_json(extract([self.sources['before']], 'Europe/Berlin')))
        expected = self.cli(*self.args(True, True))
        self.sources['before'].unlink()
        self.sources['after'].unlink()
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')):
            actual = self.assert_budget(self.args(True, True) + ['--default-timezone', 'Invalid/Zone'], 2)
        self.assertEqual(actual, expected)
        self.assertEqual(json.loads(actual[1])['before']['assumptions']['default_timezone'], 'Europe/Berlin')

    def test_absolute_and_increase_gates_fail_if_either_fails(self):
        args = self.args()
        baseline = self.cli(*args)
        for absolute, allowance, code in ((3, 2, 0), (4, 3, 0), (2, 2, 1), (3, 1, 1), (2, 1, 1)):
            for flags in (['--max-peak', absolute, '--max-peak-increase', allowance],
                          ['--max-peak-increase', allowance, '--max-peak', absolute]):
                with self.subTest(absolute=absolute, allowance=allowance):
                    result = self.cli(*args, *flags, '--strict')
                    self.assertEqual(result, (code, *baseline[1:]))

    def test_zero_active_partial_sides_and_strict_precedence(self):
        originals = {side: self.saved[side].read_text() for side in self.saved}
        for before_active, after_active, delta in ((False, False, 0), (True, False, -1), (False, True, 3)):
            for side, active in (('before', before_active), ('after', after_active)):
                inv = extract([self.sources[side]])
                for record in inv.schedules:
                    record.enabled = active
                self.saved[side].write_text(snapshot_json(inv))
            self.assert_budget(self.args(True, True), delta)
        for side in self.saved:
            self.saved[side].write_text(originals[side])
        for side in self.saved:
            for enabled in (False, True):
                inv = extract([self.sources[side]])
                inv.schedules.append(make_schedule('crontab', 'bad.crontab', 'bad', 'bad', 'bad',
                                                    '0 0 L * *', 'UTC', enabled=enabled))
                self.saved[side].write_text(snapshot_json(inv))
                args = self.args(True, True)
                baseline = self.assert_budget(args, 2)
                for allowance in (0, 2, 10):
                    result = self.cli(*args, '--max-peak-increase', allowance, '--max-peak', 0, '--strict')
                    self.assertEqual(result, (2, *baseline[1:]))
                self.saved[side].write_text(originals[side])
        # Entirely unsupported sides still report observed zero starts.
        for side, delta in (('before', 3), ('after', -1)):
            original = self.sources[side].read_text()
            self.sources[side].write_text('0 0 L * * bad\n')
            self.assert_budget(self.args(), delta)
            self.sources[side].write_text(original)

    def test_durations_never_change_start_delta_and_strict_wins(self):
        self.sources['before'].write_text('0 0 * * * a\n')
        self.sources['after'].write_text('0 0 * * * a\n1 0 * * * b\n')
        for side in self.sources:
            self.save(side)
        for duration, running_delta in ((0, 0), (600, 1), (None, None)):
            sidecars = []
            for side in self.sources:
                path = self.root / f'{side}-durations.json'
                records = extract([self.sources[side]]).schedules
                path.write_text(json.dumps({'version': 1, 'durations_seconds': {r.id: duration for r in records}}))
                sidecars += [f'--{side}-durations', path]
            for window, delta in ((60, 0), (120, 1)):
                for fmt in ('json', 'text'):
                    args = self.args(True, True, fmt, window) + sidecars
                    baseline = self.assert_budget(args, delta)
                    if fmt == 'json':
                        self.assertEqual(json.loads(baseline[1])['overlap']['peak_delta'], running_delta)
                    for allowance in (0, 1):
                        result = self.cli(*args, '--max-peak-increase', allowance, '--strict')
                        self.assertEqual(result, (2 if duration is None else int(delta > allowance), *baseline[1:]))

    def test_invalid_allowances_precede_all_input_reads(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')):
            for old, new in ((False, False), (True, True), (True, False), (False, True)):
                for fmt in ('json', 'text'):
                    for value in ('-1', '1.5', '1e3', 'NaN', 'inf', 'true', '', '1m'):
                        for flag in (['--max-peak-increase', value], [f'--max-peak-increase={value}']):
                            with self.subTest(old=old, new=new, fmt=fmt, value=value):
                                result = self.cli(*self.args(old, new, fmt), '--before-durations', 'missing',
                                                  '--after-durations', 'missing', *flag)
                                self.assertEqual(result[:2], (2, ''))
                                self.assertIn('--max-peak-increase', result[2])
                                self.assertIn('error:', result[2])
                                self.assertNotIn('Traceback', result[2])
            result = self.cli(*self.args(), '--max-peak-increase')
            self.assertEqual(result[:2], (2, ''))
            self.assertIn('expected one argument', result[2])

    def test_budget_preserves_invalid_inputs_and_shared_resource_bounds(self):
        cases = [self.args() + ['--before', self.sources['before']],
                 self.args() + ['--after-snapshot', self.saved['after']],
                 self.args() + ['--before-durations', 'missing.json'],
                 self.args() + ['--before', ''], self.args(window=61),
                 self.args(day='bad'), self.args(days=0)]
        original = self.saved['after'].read_text()
        for field, value in (('version', 2), ('id', 'wrong')):
            data = json.loads(original)
            if field == 'version':
                data[field] = value
            else:
                data['schedules'][0][field] = value
            self.saved['after'].write_text(json.dumps(data))
            result = self.cli(*self.args(True, True), '--max-peak-increase', 0)
            self.assertEqual(result[:2], (2, ''), result[2])
            self.assertIn('error:', result[2])
        self.saved['after'].write_text(original)
        for args in cases:
            result = self.cli(*args, '--max-peak-increase', 0)
            self.assertEqual(result[:2], (2, ''), result[2])
            self.assertIn('error:', result[2])
        for target in ('flowwrench.schedules.analysis.MAX_CANDIDATES', 'flowwrench.schedules.comparison.MAX_FIRINGS',
                       'flowwrench.schedules.comparison.MAX_CHANGES', 'flowwrench.schedules.extract.MAX_RECORDS'):
            with patch(target, 1):
                result = self.cli(*self.args(), '--max-peak-increase', 0)
                self.assertEqual(result[:2], (2, ''), result[2])
                self.assertIn('error:', result[2])
                self.assertIn('split', result[2])
