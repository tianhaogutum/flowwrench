"""Added-event CI budgets preserve full comparison reports and matching."""
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


class CompareAddedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources, self.saved = {}, {}
        for side in ('before', 'after'):
            directory = self.root / side
            directory.mkdir()
            self.sources[side] = directory / 'jobs.crontab'
            self.saved[side] = self.root / f'{side}.json'
        self.write_pair('0 0 * * * a\n', '0 0 * * * a\n0 6 * * * b\n0 12 * * * c\n')

    def write_pair(self, before, after):
        for side, text in (('before', before), ('after', after)):
            self.sources[side].write_text(text)
            self.saved[side].write_text(snapshot_json(extract([self.sources[side]])))

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

    def assert_budget(self, args, added):
        baseline = self.cli(*args)
        self.assertEqual(baseline[0], 0, baseline[2])
        if 'json' in args:
            self.assertEqual(len(json.loads(baseline[1])['added']), added)
        for budget in sorted({0, max(0, added - 1), added, added + 1}):
            for flag in (['--max-added-firings', budget], [f'--max-added-firings={budget}']):
                with self.subTest(args=args, budget=budget):
                    result = self.cli(*args, *flag)
                    self.assertEqual(result, (int(added > budget), *baseline[1:]))
        return baseline

    def test_spread_additions_source_saved_mixed_full_report_parity(self):
        # Two additional daily fires with no increase in either start peak.
        for old, new in ((False, False), (True, True), (True, False), (False, True)):
            for fmt in ('json', 'text'):
                baseline = self.assert_budget(self.args(old, new, fmt), 2)
                if fmt == 'json':
                    report = json.loads(baseline[1])
                    self.assertEqual((report['before']['peak'], report['after']['peak']), (1, 1))

    def test_replacements_and_removals_do_not_offset_additions(self):
        cases = [('0 0 * * * a\n', '0 6 * * * a\n', 1, 1, 0),
                 ('0 0 * * * a\n0 6 * * * b\n0 12 * * * c\n', '0 18 * * * a\n', 1, 3, -2),
                 ('0 0 * * * a\n0 6 * * * b\n', '0 0 * * * a\n', 0, 1, -1),
                 ('0 0 * * * a\n', '0 0 * * * a\n', 0, 0, 0)]
        for before, after, added, removed, delta in cases:
            self.write_pair(before, after)
            for old, new in ((False, False), (True, True), (True, False), (False, True)):
                baseline = self.assert_budget(self.args(old, new), added)
                report = json.loads(baseline[1])
                self.assertEqual((len(report['removed']), report['total_firings_delta'], report['peak_delta']),
                                 (removed, delta, 0))

    def test_fixed_second_and_stable_identity_matching(self):
        def record(second=17, node='job'):
            return make_schedule('n8n', 'jobs.json', 'jobs.json', 'flow', node,
                                 f'{second} 0 0 * * *', 'UTC', dialect='n8n', day_mode='or-values')
        for after, added in ((record(), 0), (record(29), 1), (record(node='renamed'), 1)):
            for side, value in (('before', record()), ('after', after)):
                inv = extract([self.sources[side]])
                inv.schedules = [value]
                self.saved[side].write_text(snapshot_json(inv))
            baseline = self.assert_budget(self.args(True, True), added)
            report = json.loads(baseline[1])
            self.assertEqual((len(report['removed']), report['unchanged_firings'], report['total_firings_delta']),
                             (added, 1 - added, 0))
            if after.second == 29:
                self.assertEqual(report['added'][0]['source_second'], 29)
                self.assertEqual(report['removed'][0]['source_second'], 17)
            self.assertEqual(report['peak_delta'], 0)

    def test_horizon_counts_full_events_and_window_is_irrelevant(self):
        self.write_pair('0 12 * * * a\n', '59 23 1 11 * a\n0 0 2 11 * b\n')
        for old, new in ((False, False), (True, True), (True, False), (False, True)):
            for day, days, added in (('2026-11-01', 1, 1), ('2026-11-02', 1, 1),
                                      ('2026-11-01', 2, 2), ('2026-11-03', 1, 0)):
                for window in (60, 120):
                    self.assert_budget(self.args(old, new, window=window, day=day, days=days), added)

    def test_saved_assumptions_retained_after_exports_deleted(self):
        self.saved['before'].write_text(snapshot_json(extract([self.sources['before']], 'Europe/Berlin')))
        expected = self.cli(*self.args(True, True))
        for path in self.sources.values():
            path.unlink()
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')):
            actual = self.assert_budget(self.args(True, True) + ['--default-timezone', 'Invalid/Zone'], 3)
        self.assertEqual(actual, expected)
        self.assertEqual(json.loads(actual[1])['before']['assumptions']['default_timezone'], 'Europe/Berlin')

    def test_combinations_with_both_peak_gates(self):
        self.write_pair('0 0 * * * a\n', '0 0 * * * a\n0 0 * * * b\n0 0 * * * c\n')
        baseline = self.cli(*self.args())
        for absolute, increase, added in ((3, 2, 2), (4, 3, 3), (2, 2, 2),
                                          (3, 1, 2), (3, 2, 1), (2, 1, 1)):
            flags = ['--max-peak', absolute, '--max-peak-increase', increase, '--max-added-firings', added]
            for ordered in (flags, flags[4:] + flags[:4]):
                result = self.cli(*self.args(), *ordered, '--strict')
                self.assertEqual(result, (int(3 > absolute or 2 > increase or 2 > added), *baseline[1:]))

    def test_zero_active_partial_sides_and_strict_precedence(self):
        originals = {side: p.read_text() for side, p in self.saved.items()}
        for old_active, new_active, added in ((False, False, 0), (True, False, 0), (False, True, 3)):
            for side, active in (('before', old_active), ('after', new_active)):
                inv = extract([self.sources[side]])
                for r in inv.schedules:
                    r.enabled = active
                self.saved[side].write_text(snapshot_json(inv))
            self.assert_budget(self.args(True, True), added)
        for side in self.saved:
            self.saved[side].write_text(originals[side])
        for side in self.saved:
            for enabled in (False, True):
                inv = extract([self.sources[side]])
                inv.schedules.append(make_schedule('crontab', 'bad.crontab', 'bad', 'bad', 'bad',
                                                   '0 0 L * *', 'UTC', enabled=enabled))
                self.saved[side].write_text(snapshot_json(inv))
                baseline = self.assert_budget(self.args(True, True), 2)
                for budget in (0, 2, 10):
                    result = self.cli(*self.args(True, True), '--max-added-firings', budget,
                                      '--max-peak', 0, '--max-peak-increase', 0, '--strict')
                    self.assertEqual(result, (2, *baseline[1:]))
                self.saved[side].write_text(originals[side])
        for side, added in (('before', 3), ('after', 0)):
            original = self.sources[side].read_text()
            self.sources[side].write_text('0 0 L * * bad\n')
            self.assert_budget(self.args(), added)
            self.sources[side].write_text(original)

    def test_duration_sidecars_do_not_change_added_count_and_strict_wins(self):
        self.write_pair('0 0 * * * a\n', '0 0 * * * a\n1 0 * * * b\n')
        for duration, running_peak in ((0, 0), (600, 2), (None, None)):
            sidecars = []
            for side in self.sources:
                path = self.root / f'{side}-durations.json'
                records = extract([self.sources[side]]).schedules
                path.write_text(json.dumps({'version': 1, 'durations_seconds': {r.id: duration for r in records}}))
                sidecars += [f'--{side}-durations', path]
            for old, new in ((False, False), (True, True), (True, False), (False, True)):
                for window in (60, 120):
                    for fmt in ('json', 'text'):
                        args = self.args(old, new, fmt, window) + sidecars
                        baseline = self.assert_budget(args, 1)
                        if fmt == 'json':
                            self.assertEqual(json.loads(baseline[1])['overlap']['after']['peak'], running_peak)
                        for budget in (0, 1):
                            result = self.cli(*args, '--max-added-firings', budget, '--strict')
                            self.assertEqual(result, (2 if duration is None else int(1 > budget), *baseline[1:]))

    def test_invalid_budgets_precede_all_input_reads(self):
        with patch('flowwrench.schedules.cli.extract', side_effect=AssertionError('export read')), \
                patch('flowwrench.schedules.cli.load_snapshot', side_effect=AssertionError('snapshot read')), \
                patch('flowwrench.schedules.cli.load_durations', side_effect=AssertionError('sidecar read')):
            for old, new in ((False, False), (True, True), (True, False), (False, True)):
                for fmt in ('json', 'text'):
                    for value in ('-1', '1.5', '1e3', 'NaN', 'inf', 'true', '', '1m', None):
                        flags = [['--max-added-firings']] if value is None else [
                            ['--max-added-firings', value], [f'--max-added-firings={value}']]
                        for flag in flags:
                            with self.subTest(old=old, new=new, fmt=fmt, value=value):
                                result = self.cli(*self.args(old, new, fmt), '--before-durations', 'missing',
                                                  '--after-durations', 'missing', *flag)
                                self.assertEqual(result[:2], (2, ''))
                                self.assertIn('--max-added-firings', result[2])
                                self.assertIn('error:', result[2])
                                self.assertNotIn('Traceback', result[2])

    def test_budget_preserves_invalid_inputs_and_resource_bounds(self):
        cases = [self.args() + ['--before', self.sources['before']],
                 self.args() + ['--after-snapshot', self.saved['after']],
                 self.args() + ['--before-durations', 'missing.json'],
                 self.args() + ['--before', ''], self.args(window=61),
                 self.args(day='bad'), self.args(days=0)]
        original = self.saved['after'].read_text()
        for mutation in ('version', 'id', 'duplicate'):
            data = json.loads(original)
            if mutation == 'version':
                data['version'] = 2
            elif mutation == 'id':
                data['schedules'][0]['id'] = 'wrong'
            else:
                data['schedules'].append(data['schedules'][0])
            self.saved['after'].write_text(json.dumps(data))
            result = self.cli(*self.args(True, True), '--max-added-firings', 0)
            self.assertEqual(result[:2], (2, ''), result[2])
            self.assertIn('error:', result[2])
        self.saved['after'].write_text(original)
        for args in cases:
            result = self.cli(*args, '--max-added-firings', 0)
            self.assertEqual(result[:2], (2, ''), result[2])
            self.assertIn('error:', result[2])
        for target in ('flowwrench.schedules.analysis.MAX_CANDIDATES', 'flowwrench.schedules.comparison.MAX_FIRINGS',
                       'flowwrench.schedules.comparison.MAX_CHANGES', 'flowwrench.schedules.extract.MAX_RECORDS'):
            with patch(target, 1):
                result = self.cli(*self.args(), '--max-added-firings', 0)
                self.assertEqual(result[:2], (2, ''), result[2])
                self.assertIn('error:', result[2])
                self.assertIn('split', result[2])
