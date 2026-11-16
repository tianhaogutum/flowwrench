"""Versioned baseline contracts; original exports are unnecessary after saving."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.schedules.cli import main

ROOT = Path(__file__).resolve().parent
PAIR = ROOT / 'examples/comparison'


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, args)))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def save(self, source, name='baseline.json', tz='UTC'):
        code, out, err = self.cli('extract', source, '--format', 'snapshot', '--default-timezone', tz)
        self.assertEqual(code, 0, err)
        path = self.root / name
        path.write_text(out)
        return path, json.loads(out)

    def compare(self, path, *extra):
        return self.cli('compare', '--before-snapshot', path, '--after', PAIR / 'after',
                        '--from', '2026-10-25', '--days', '1', '--format', 'json', *extra)

    def assert_invalid(self, path):
        code, out, err = self.compare(path)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertIn('error:', err)
        self.assertNotIn('Traceback', err)

    def test_snapshot_baseline_matches_source_comparison_without_original_exports(self):
        source = self.root / 'jobs.crontab'
        source.write_bytes((PAIR / 'before/jobs.crontab').read_bytes())
        path, data = self.save(source)
        code, out, err = self.cli('compare', '--before', source, '--after', PAIR / 'after',
                                  '--from', '2026-10-25', '--days', '1', '--format', 'json')
        self.assertEqual(code, 0, err)
        expected = json.loads(out)
        source.unlink()
        code, out, err = self.compare(path)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report, expected)
        self.assertEqual(data['schema'], 'cronherd.inventory')
        self.assertEqual(data['version'], 1)
        self.assertEqual((len(report['removed']), len(report['added']), report['unchanged_firings']), (2, 1, 1))
        self.assertEqual((report['before']['peak'], report['after']['peak']), (1, 2))
        self.assertEqual([e['fold'] for e in report['removed']], [0, 1])
        self.assertEqual({r['file'] for r in report['before']['schedules']}, {str(source)})
        self.assertEqual(self.compare(path)[1], out)

    def test_both_snapshots_and_after_only_keep_events_and_fixed_seconds(self):
        source = self.root / 'tick.schedule.yaml'
        source.write_text('schedule: "17 0 0 * * *"\ntimezone: UTC\n')
        before, _ = self.save(source)
        source.write_text('schedule: "29 0 0 * * *"\ntimezone: UTC\n')
        after, _ = self.save(source, 'candidate.json')
        for old in [('--before', source), ('--before-snapshot', before)]:
            code, out, err = self.cli('compare', *old, '--after-snapshot', after,
                                     '--from', '2026-10-25', '--days', '1', '--format', 'json')
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            if old[0] == '--before-snapshot':
                self.assertEqual([e['source_second'] for e in report['removed']], [17])
                self.assertEqual([e['source_second'] for e in report['added']], [29])
            else:
                self.assertEqual(report['unchanged_firings'], 1)

    def test_saved_warnings_assumptions_and_unsupported_state_survive(self):
        source = self.root / 'flows.json'
        source.write_text(json.dumps([
            {'id': 'a', 'type': 'inject', 'crontab': '0 0 * * *', 'once': True},
            {'id': 'b', 'type': 'inject', 'repeat': '60'}]))
        path, data = self.save(source, tz='Asia/Kathmandu')
        self.assertEqual(len(data['warnings']), 1)
        code, out, err = self.compare(path, '--default-timezone', 'America/New_York', '--strict')
        self.assertEqual(code, 2, err)
        report = json.loads(out)['before']
        self.assertEqual(report['warnings'], data['warnings'])
        self.assertEqual(report['assumptions'], data['assumptions'])
        self.assertEqual(report['assumptions']['default_timezone'], 'Asia/Kathmandu')
        self.assertEqual(report['assumptions']['timezone_assumed_records'], 2)
        self.assertEqual(report['unsupported'], 1)
        self.assertFalse(report['complete'])
        self.assertIn('saved default_timezone=Asia/Kathmandu', err)
        self.assertNotIn('assume --default-timezone=America/New_York', err)

    def test_six_format_roundtrip_retains_all_fields(self):
        path, data = self.save(ROOT / 'fixtures')
        code, out, err = self.cli('extract', ROOT / 'fixtures', '--format', 'json')
        self.assertEqual(code, 0, err)
        raw = json.loads(out)
        self.assertEqual(data['schedules'], sorted(raw['schedules'], key=lambda r: r['id']))
        self.assertEqual(data['assumptions'], raw['assumptions'])
        code, out, err = self.cli('compare', '--before-snapshot', path, '--after-snapshot', path,
                                 '--from', '2026-11-01', '--days', '1', '--format', 'json')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['added'], [])
        self.assertEqual(report['removed'], [])
        self.assertEqual(report['unchanged_firings'], report['before']['total_firings'])

    def test_disabled_activation_assumptions_and_early_n8n_errors_roundtrip(self):
        source = self.root / 'n8n.json'
        source.write_text(json.dumps({'name': 'example', 'nodes': [
            {'name': 'disabled', 'type': 'n8n-nodes-base.scheduleTrigger', 'disabled': True,
             'parameters': {'rule': {'interval': [{'field': 'cronExpression', 'expression': '@hourly'}]}}},
            {'name': 'missing rules', 'type': 'n8n-nodes-base.scheduleTrigger'}]}))
        path, data = self.save(source)
        self.assertEqual(data['assumptions']['enabled_assumed_records'], 1)
        code, out, err = self.compare(path, '--strict')
        self.assertEqual(code, 2, err)
        report = json.loads(out)['before']
        self.assertEqual((report['disabled'], report['unsupported'], report['total_firings']), (1, 1, 0))
        self.assertEqual(report['schedules'], data['schedules'])

    def test_assumed_timezone_consistency_and_invalid_unicode_rejected(self):
        source = self.root / 'jobs.crontab'; source.write_text('0 0 * * * true\n')
        path, valid = self.save(source, tz='Asia/Kathmandu')
        for key, value in [('timezone', 'UTC'), ('workflow', '\ud800'), ('file', 'x' * 4097),
                           ('source_expr', None), ('day_mode', 'unknown')]:
            data = deepcopy(valid); data['schedules'][0][key] = value
            path.write_text(json.dumps(data)); self.assert_invalid(path)
        for key, value in [('warnings', ['x' * 8193]), ('warnings', [''] * 10001)]:
            data = deepcopy(valid); data[key] = value
            path.write_text(json.dumps(data)); self.assert_invalid(path)

    def test_snapshot_never_promotes_unsupported_records_and_validates_errors(self):
        source = self.root / 'jobs.crontab'; source.write_text('@reboot true\n')
        path, valid = self.save(source)
        # A parseable expression does not erase a saved platform limitation.
        valid['schedules'][0].update(expr='0 0 * * *', source_expr='0 0 * * *')
        path.write_text(json.dumps(valid))
        code, out, err = self.compare(path, '--strict')
        self.assertEqual(code, 2, err)
        self.assertEqual(json.loads(out)['before']['unsupported'], 1)
        for error in [None, '', False, ['error']]:
            data = deepcopy(valid); data['schedules'][0]['error'] = error
            path.write_text(json.dumps(data)); self.assert_invalid(path)

    def test_schema_and_record_negative_contract(self):
        path, valid = self.save(PAIR / 'before')
        cases = []
        for key, value in [('version', True), ('version', 1.0), ('version', 2), ('schema', 'other'),
                           ('schedules', []), ('schedules', None), ('warnings', [42]),
                           ('assumptions', None), ('extra', 1)]:
            data = deepcopy(valid); data[key] = value; cases.append((key, data))
        data = deepcopy(valid); del data['warnings']; cases.append(('missing', data))
        data = deepcopy(valid); data['schedules'].append(data['schedules'][0]); cases.append(('duplicate ID', data))
        for key, value in [('id', 'not-an-id'), ('platform', 'unknown'), ('file', None), ('workflow', 1),
                           ('node', 'x' * 513), ('expr', 'bad'), ('timezone', 'Not/AZone'),
                           ('enabled', 1), ('supported', 'yes'), ('error', 'contradiction'),
                           ('source_expr', '0 0 * * *'), ('source_dialect', 'n8n'),
                           ('second', True), ('second', 60), ('second', 1.5), ('day_mode', 'and'),
                           ('timezone_assumed', 0), ('enabled_assumed', None), ('kind', 'interval'),
                           ('extra', False)]:
            data = deepcopy(valid); data['schedules'][0][key] = value; cases.append((key, data))
        data = deepcopy(valid); del data['schedules'][0]['enabled']; cases.append(('missing record field', data))
        for key, value in [('default_timezone', 'No/Zone'), ('timezone_assumed_records', True),
                           ('timezone_assumed_records', 1), ('enabled_assumed_records', -1), ('extra', 0)]:
            data = deepcopy(valid); data['assumptions'][key] = value; cases.append((key, data))
        for name, data in cases:
            with self.subTest(name=name, data=data):
                path.write_text(json.dumps(data))
                self.assert_invalid(path)

    def test_malformed_bytes_and_duplicate_keys_rejected(self):
        path = self.root / 'bad.json'
        for raw in [b'\xff', b'{', b'[]', b'null', b'{"version":1,"version":1}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'[' * 1500 + b']' * 1500]:
            with self.subTest(raw=raw[:30]):
                path.write_bytes(raw)
                self.assert_invalid(path)

    def test_ordinary_extract_json_is_not_a_snapshot(self):
        code, out, err = self.cli('extract', PAIR / 'before', '--format', 'json')
        self.assertEqual(code, 0, err)
        path = self.root / 'ordinary.json'; path.write_text(out)
        self.assert_invalid(path)

    def test_argument_sources_are_required_and_mutually_exclusive(self):
        path, _ = self.save(PAIR / 'before')
        for args in [('--before', PAIR / 'before', '--before-snapshot', path, '--after', PAIR / 'after'),
                     ('--before-snapshot', path),
                     ('--before-snapshot', path, '--after', PAIR / 'after', '--after-snapshot', path)]:
            code, out, err = self.cli('compare', *args, '--format', 'json')
            self.assertEqual(code, 2, err)
            self.assertEqual(out, '')

    def test_snapshot_file_and_resource_limits_are_atomic(self):
        path, data = self.save(PAIR / 'before')
        link = self.root / 'link.json'; link.symlink_to(path)
        for invalid in [link, self.root, self.root / 'missing.json']:
            self.assert_invalid(invalid)
        import os
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        self.assert_invalid(fifo)
        with patch('flowwrench.schedules.snapshot.MAX_SNAPSHOT_BYTES', 10):
            self.assert_invalid(path)
            code, out, err = self.cli('extract', PAIR / 'before', '--format', 'snapshot')
            self.assertEqual(code, 2, err)
            self.assertEqual(out, '')
        data['schedules'] *= 1001
        path.write_text(json.dumps(data)); self.assert_invalid(path)

    def test_bom_allowed_and_excluded_records_validated(self):
        source = self.root / 'jobs.crontab'; source.write_text('@reboot true\n')
        path, data = self.save(source)
        path.write_text('\ufeff' + json.dumps(data))
        code, out, err = self.compare(path)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['before']['total_firings'], 0)
        data['schedules'][0]['timezone'] = 'Invalid/Zone'
        path.write_text(json.dumps(data)); self.assert_invalid(path)
