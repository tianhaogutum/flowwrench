import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'flowwrench.schedules', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'TZ': 'Pacific/Honolulu'})

    def cron(self, text):
        p = self.path / 'test.crontab'
        p.write_text(text)
        return p

    def test_extract_fixture_json(self):
        result = self.run_cli('extract', 'fixtures/', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['schedules']), 14)
        self.assertTrue(report['complete'])
        self.assertIn('assume --default-timezone=UTC', result.stderr)

    def test_analyze_fixture_json(self):
        result = self.run_cli('analyze', 'fixtures/', '--from', '2026-11-01', '--days', '7', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['peak'], report['total_firings']), (9, 1541))
        self.assertEqual(report['dst_hazards'][0]['hazard'], 'ambiguous')

    def test_peak_threshold_exit_1_and_equality_exit_0(self):
        for threshold, expected in [('8', 1), ('9', 0)]:
            with self.subTest(threshold=threshold):
                r = self.run_cli('analyze', 'fixtures/', '--from', '2026-11-01', '--days', '1', '--max-peak', threshold)
                self.assertEqual(r.returncode, expected, r.stderr)
                self.assertIn('Peak 9', r.stdout)

    def test_unknown_timezone_exit_2(self):
        r = self.run_cli('extract', self.cron('CRON_TZ=Not/AZone\n0 * * * * true\n'))
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown timezone', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_unsupported_record_continues_and_strict_fails(self):
        p = self.cron('0 0 L * * true\n0 * * * * true\n')
        args = ['analyze', p, '--from', '2026-11-01', '--days', '1', '--format', 'json']
        r = self.run_cli(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['total_firings'], 24)
        self.assertFalse(report['complete'])
        self.assertEqual(report['unsupported'], 1)
        self.assertIn('unsupported', r.stderr)
        self.assertEqual(self.run_cli(*args, '--strict').returncode, 2)

    def test_all_unsupported_and_empty_exit_2(self):
        for text in ['# empty\n', '@reboot true\n']:
            with self.subTest(text=text):
                r = self.run_cli('analyze', self.cron(text), '--from', '2026-11-01', '--days', '1')
                self.assertEqual(r.returncode, 2)
                self.assertNotIn('Traceback', r.stderr)

    def test_unrecognized_file_warned(self):
        self.cron('0 * * * * true\n')
        (self.path / 'unknown.txt').write_text('not a workflow')
        r = self.run_cli('extract', self.path, '--format', 'json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('unrecognised file skipped', r.stderr)
        self.assertEqual(len(json.loads(r.stdout)['schedules']), 1)

    def test_dst_cli(self):
        p = self.cron('CRON_TZ=Europe/Berlin\n30 2 * * * true\n')
        r = self.run_cli('dst', p, '--from', '2027-03-28', '--days', '1', '--format', 'json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['hazards'][0]['hazard'], 'skipped')

    def test_suggest_cli_deterministic_and_read_only(self):
        p = self.cron('0 * * * * touch MUST_NEVER_EXECUTE\n0 * * * * echo secret-command\n')
        before = {f: f.read_bytes() for f in self.path.iterdir()}
        args = ['suggest', p, '--from', '2026-11-01', '--days', '1', '--max-shift', '10m', '--format', 'json']
        first = self.run_cli(*args)
        second = self.run_cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual({f: f.read_bytes() for f in self.path.iterdir()}, before)
        self.assertNotIn('secret-command', first.stdout)
        self.assertFalse((ROOT / 'MUST_NEVER_EXECUTE').exists())
        report = json.loads(first.stdout)
        self.assertEqual((report['before']['peak'], report['after']['peak']), (2, 1))

    def test_argument_errors_exit_2(self):
        p = self.cron('0 * * * * true\n')
        cases = [('analyze', p, '--days', '0'), ('analyze', p, '--days', '731'),
                 ('analyze', p, '--from', 'invalid'), ('analyze', p, '--window', '1'),
                 ('analyze', p, '--window', '61'), ('analyze', p, '--max-peak', '-1'),
                 ('suggest', p, '--max-shift', '60m'), ('suggest', p, '--max-shift', '-1'),
                 ('extract', self.path / 'missing')]
        for args in cases:
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 2)
                self.assertNotIn('Traceback', r.stderr)

    def test_malformed_file_and_expression_never_evaluated(self):
        marker = self.path / 'should-not-exist'
        p = self.path / 'bad.schedule.yaml'
        p.write_text(f'schedule: !!python/object/apply:os.system ["touch {marker}"]\ntimezone: UTC\n')
        result = self.run_cli('extract', p)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertNotIn('touch', result.stderr)

    def test_terminal_controls_in_metadata_are_escaped(self):
        p = self.path / 'input.json'
        p.write_text(json.dumps({'name': '\x1b[31munsafe\nlabel', 'active': True,
                      'nodes': [{'name': 'tick', 'type': 'n8n-nodes-base.scheduleTrigger',
                      'parameters': {'rule': {'interval': [{'field': 'cronExpression', 'expression': '@hourly'}]}}}]}))
        r = self.run_cli('extract', p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('\x1b', r.stdout)
        self.assertIn('\\x1b', r.stdout)

    def test_extremely_long_shift_is_usage_error(self):
        r = self.run_cli('suggest', 'fixtures/', '--max-shift', '1' * 5000)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('Traceback', r.stderr)
