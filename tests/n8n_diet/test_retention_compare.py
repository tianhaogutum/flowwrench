"""Independent synthetic retention comparison contracts; no n8n service needed."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from flowwrench.n8n_diet import core
from flowwrench.n8n_diet.storage import DietError

ROOT = Path(__file__).resolve().parent
AS_OF = '2026-11-01T00:00:00Z'
BASELINE = {'max_count': 2}
CANDIDATE = {'max_age_days': 20, 'max_count': 1, 'keep_failed': True}
GROUPS = ('keep', 'drop', 'protected')
METRICS = ('executions', 'data_rows', 'payload_bytes')
# Independent fixture literals, including missing execution_data rows 7 and 10.
BYTES = {i: len(('{}' + data).encode('utf-8')) for i, data in {
    1: '{"ok":1}', 2: '{"error":"synthetic"}', 3: '{"订单":"你好"}',
    4: '[1,2,3]', 5: '{}', 6: '{}', 8: 'null', 9: 'false'}.items()}
BYTES.update({7: 0, 10: 0})


class RetentionCompareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
        self.baseline = self.root / 'baseline.json'
        self.candidate = self.root / 'candidate.json'
        self.baseline.write_text(json.dumps(BASELINE))
        self.candidate.write_text(json.dumps(CANDIDATE))
        self.original = self.db.read_bytes()

    def compare(self, **kwargs):
        return core.compare(self.db, baseline=BASELINE, candidate=CANDIDATE, as_of=AS_OF, **kwargs)

    def cli(self, *extra, as_of=AS_OF):
        return subprocess.run([sys.executable, '-m', 'flowwrench.n8n_diet', 'compare', str(self.db),
                               '--baseline', str(self.baseline), '--candidate', str(self.candidate),
                               '--as-of', as_of, *extra], cwd=ROOT,
                              text=True, capture_output=True, timeout=30)

    def update(self, sql, values=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(sql, values)
            conn.commit()

    @contextmanager
    def observe_compare_names(self, *, variable_limit=None):
        """Observe rows actually fetched into Python, under the real source locks."""
        observed = {'ids': [], 'queries': []}
        original_source = core.source_database

        @contextmanager
        def source(*args, **kwargs):
            with original_source(*args, **kwargs) as (conn, path, wal):
                def row_factory(cursor, values):
                    if [col[0] for col in cursor.description] == ['id', 'name']:
                        observed['ids'].append(values[0])
                    return sqlite3.Row(cursor, values)
                conn.row_factory = row_factory
                conn.set_trace_callback(lambda sql: observed['queries'].append(sql)
                                        if sql.lstrip().upper().startswith('SELECT')
                                        and 'workflow_entity' in sql else None)
                if variable_limit is not None:
                    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
                yield conn, path, wal

        with mock.patch.object(core, 'source_database', source):
            yield observed

    def test_observed_names_only_original_null_empty_string_ids(self):
        self.db.unlink()
        sql = (ROOT / 'fixtures/split-schema.sql').read_text().replace(
            'workflowId VARCHAR(36) NOT NULL', 'workflowId VARCHAR(36)').replace(
            'id VARCHAR(36) PRIMARY KEY NOT NULL', 'id VARCHAR(36) PRIMARY KEY').replace(
            'name VARCHAR(128) NOT NULL', 'name VARCHAR(128)')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(sql)
            conn.executemany('INSERT INTO workflow_entity VALUES (?,?)',
                             [(None, 'null ID'), ('', 'empty ID'), ('None', 'literal None'),
                              ('null', None), *[(f'unused-{i}', f'unused name {i}') for i in range(1500)]])
            conn.executescript("""
                UPDATE execution_entity SET workflowId=NULL WHERE id=1;
                UPDATE execution_entity SET workflowId='None' WHERE id=2;
                UPDATE execution_entity SET workflowId='null' WHERE id=3;
                UPDATE execution_entity SET workflowId='' WHERE id=10;
            """)
        original = self.db.read_bytes()
        expected = [(None, 'null ID'), ('', 'empty ID'), ('None', 'literal None'),
                    ('null', None), ('wf-a', 'Email'), ('wf-b', '订单')]
        for summary in (False, True):
            for limits in ({}, {'max_workflows': 6, 'max_members': 0 if summary else 10}):
                with self.observe_compare_names() as names:
                    report = self.compare(summary_only=summary, **limits)
                self.assertEqual([(w['workflow_id'], w['workflow_name']) for w in report['workflows']], expected)
                self.assertEqual(set(names['ids']), {key for key, _ in expected})
                self.assertEqual(len(names['ids']), len(expected))
                self.assertEqual(len(names['queries']), 1)
                if not summary:
                    full = report
                else:
                    self.assert_summary_only(full, report)
                self.assert_workflow_accounting(report, full)
        self.assertEqual(self.db.read_bytes(), original)

    def test_observed_names_empty_orphans_and_absent_catalog(self):
        self.update('DELETE FROM execution_entity')
        for phase in ('orphans', 'empty', 'absent'):
            if phase == 'empty':
                self.update('DELETE FROM execution_data')
            if phase == 'absent':
                self.update('DROP TABLE workflow_entity')
            original = self.db.read_bytes()
            for summary in (False, True):
                with self.observe_compare_names() as names:
                    report = self.compare(summary_only=summary, max_workflows=0, max_members=0)
                self.assertEqual(report['workflows'], [])
                self.assertEqual(names['ids'], [])
                self.assertLessEqual(len(names['queries']), 1 if phase != 'absent' else 0)
            self.assertEqual(self.db.read_bytes(), original)

    def test_observed_names_preserve_raw_key_equality(self):
        # A SQL join must not broaden Python dict lookup via affinity or NOCASE.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                DROP TABLE workflow_entity;
                CREATE TABLE workflow_entity (id PRIMARY KEY COLLATE NOCASE, name TEXT);
                INSERT INTO workflow_entity VALUES ('WF-A', 'wrong case'), (42, 'wrong type'),
                    ('wf-b', '订单'), ('42', 'string number');
                UPDATE execution_entity SET workflowId='42' WHERE id=10;
            """)
        for summary in (False, True):
            with self.observe_compare_names() as names:
                report = self.compare(summary_only=summary)
            self.assertEqual([(w['workflow_id'], w['workflow_name']) for w in report['workflows']],
                             [('42', 'string number'), ('wf-a', None), ('wf-b', '订单')])
            self.assertEqual(set(names['ids']), {'42', 'wf-b'})

    @unittest.skipUnless(hasattr(sqlite3.Connection, 'setlimit'), 'SQLite runtime limit API needs Python 3.11+')
    def test_observed_names_set_query_without_workflow_parameters(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany('INSERT INTO workflow_entity VALUES (?,?)',
                             [(f'observed-{i}', f'name {i}') for i in range(1100)])
            conn.executemany('INSERT INTO execution_entity(workflowId,status) VALUES (?,?)',
                             [(f'observed-{i}', 'running') for i in range(1100)])
            conn.commit()
        with self.observe_compare_names(variable_limit=1) as names:
            report = self.compare(summary_only=True, max_workflows=1103, max_members=0)
        self.assertEqual(len(names['queries']), 1)
        self.assertEqual(len(names['ids']), 1102)
        self.assertEqual(len(report['workflows']), 1103)  # missing-workflow has no name

    def test_observed_names_after_classification_and_both_count_checks(self):
        for summary, limits, message in (
                (False, {'max_workflows': 0, 'max_members': 0}, 'observed workflow count 3'),
                (True, {'max_workflows': 0, 'max_members': 0}, 'observed workflow count 3'),
                (False, {'max_workflows': 3, 'max_members': 0}, 'emitted execution member count 10')):
            with self.observe_compare_names() as names:
                with self.assertRaisesRegex(DietError, message):
                    self.compare(summary_only=summary, **limits)
            self.assertEqual(names, {'ids': [], 'queries': []})
        self.update("UPDATE execution_entity SET stoppedAt='private-invalid' WHERE id=2")
        for baseline, candidate in ((BASELINE, CANDIDATE), (CANDIDATE, BASELINE)):
            with self.observe_compare_names() as names:
                with self.assertRaisesRegex(DietError, 'execution stoppedAt must be an ISO-8601 timestamp'):
                    core.compare(self.db, baseline=baseline, candidate=candidate, as_of=AS_OF,
                                 max_workflows=0, max_members=0)
            self.assertEqual(names, {'ids': [], 'queries': []})

    def external_hashes(self, paths):
        # Closing a main DB/SHM fd in the writer's process drops its POSIX locks.
        result = subprocess.run([sys.executable, '-c',
            'import hashlib,json,pathlib,sys; print(json.dumps([hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in sys.argv[1:]]))',
            *map(str, paths)], text=True, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def assert_workflow_accounting(self, report, full=None):
        pairs = [(a, b) for a in GROUPS for b in GROUPS]
        workflows = report['workflows']
        for workflow in workflows:
            self.assertEqual([(c['from'], c['to']) for c in workflow['transitions']], pairs)
            for cell, global_cell in zip(workflow['transitions'], (full or report)['transitions']):
                members = [m for m in global_cell['members'] if m['workflow_id'] == workflow['workflow_id']]
                self.assertNotIn('members', cell)  # rollups do not duplicate execution identities
                self.assertEqual(cell['executions'], len(members))
                for metric in ('data_rows', 'payload_bytes'):
                    self.assertEqual(cell[metric], sum(m[metric] for m in members))
            for metric in METRICS:
                self.assertEqual(sum(c[metric] for c in workflow['transitions']), workflow['totals'][metric])
                for side, field in [('baseline', 'from'), ('candidate', 'to')]:
                    for group in GROUPS:
                        self.assertEqual(sum(c[metric] for c in workflow['transitions'] if c[field] == group),
                                         workflow[side]['groups'][group][metric])
                self.assertEqual(workflow['drop_delta'][metric],
                                 workflow['candidate']['groups']['drop'][metric] - workflow['baseline']['groups']['drop'][metric])
        for metric in METRICS:
            self.assertEqual(sum(c[metric] for c in report['transitions']), report['totals'][metric])
            self.assertEqual(sum(w['totals'][metric] for w in workflows), report['totals'][metric])
            self.assertEqual(sum(w['drop_delta'][metric] for w in workflows), report['drop_delta'][metric])
            for index, cell in enumerate(report['transitions']):
                self.assertEqual(sum(w['transitions'][index][metric] for w in workflows), cell[metric])
            for side in ('baseline', 'candidate'):
                for group in GROUPS:
                    field = 'from' if side == 'baseline' else 'to'
                    self.assertEqual(sum(c[metric] for c in report['transitions'] if c[field] == group),
                                     report[side]['groups'][group][metric])
                    self.assertEqual(sum(w[side]['groups'][group][metric] for w in workflows),
                                     report[side]['groups'][group][metric])

    def assert_summary_only(self, full, summary):
        # Compare every field except the explicit mode marker and explanatory notes.
        expected = {k: v for k, v in full.items() if k != 'notes'}
        expected['transitions'] = [{k: v for k, v in c.items() if k != 'members'}
                                   for c in full['transitions']]
        expected['members_included'] = False
        self.assertEqual({k: v for k, v in summary.items() if k != 'notes'}, expected)
        self.assertIs(summary['members_included'], False)
        encoded = json.dumps(summary)
        for key in ('members', 'execution_id', 'baseline_reason', 'candidate_reason'):
            self.assertNotIn('"' + key + '"', encoded)
        self.assertEqual(len(summary['transitions']), 9)
        self.assert_workflow_accounting(summary, full)

    def test_member_limit_api_boundaries_equal_unrestricted(self):
        full = self.compare()
        self.assertEqual(sum(len(c['members']) for c in full['transitions']), 10)
        self.assertEqual(full['drop_delta'], dict(zip(METRICS, (-2, -2, -15))))
        self.assert_workflow_accounting(full)
        for limit in (None, 10, 11, 10**100):
            with self.subTest(limit=limit):
                self.assertEqual(self.compare(max_members=limit, max_workflows=3), full)
        for limit in (0, 8, 9):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                    DietError, f'^emitted execution member count 10 exceeds max_members {limit}$'):
                self.compare(max_members=limit)
        summary = self.compare(summary_only=True)
        self.assert_summary_only(full, summary)
        for limit in (None, 0, 1, 10):
            self.assertEqual(self.compare(summary_only=True, max_members=limit, max_workflows=3), summary)
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertEqual(sorted(p.name for p in self.root.glob('input*')), ['input.sqlite'])

    def test_member_limit_invalid_api_and_validation_precedence(self):
        args = dict(baseline={}, candidate={}, as_of='bad')
        absent = self.root / 'absent.sqlite'
        for summary in (False, True):
            for value in (True, False, -1, 0.0, 10.0, '10', 'private-value', [], {}):
                with self.subTest(summary=summary, value=value), self.assertRaisesRegex(
                        DietError, '^max_members must be a non-negative integer or None$'):
                    core.compare(absent, **args, summary_only=summary, max_members=value)
        with self.assertRaisesRegex(DietError, '^summary_only must be a boolean$'):
            core.compare(absent, **args, summary_only=0, max_workflows=-1, max_members=-1)
        with self.assertRaisesRegex(DietError, '^max_workflows must be a non-negative integer or None$'):
            core.compare(absent, **args, max_workflows=-1, max_members=-1)
        with self.assertRaisesRegex(DietError, '--as-of'):
            core.compare(absent, **args, max_members=0)
        with self.assertRaisesRegex(DietError, 'baseline policy requires'):
            core.compare(absent, baseline={}, candidate={}, as_of=AS_OF, max_members=0)
        self.assertFalse(absent.exists())

    def test_member_limit_cli_equality_refusal_and_invalid_values(self):
        self.update("UPDATE execution_data SET data='secret-payload-marker' WHERE executionId=8")
        original = self.db.read_bytes()
        for mode in ((), ('--summary-only',)):
            for output in ((), ('--json',)):
                unrestricted = self.cli(*mode, *output)
                self.assertEqual(unrestricted.returncode, 0, unrestricted.stderr)
                for limit in (('0', '10') if mode else ('10', '11')):
                    allowed = self.cli(*mode, *output, '--max-members', limit, '--max-workflows', '3')
                    self.assertEqual(allowed.returncode, 0, allowed.stderr)
                    self.assertEqual(allowed.stdout, unrestricted.stdout)
                    self.assertEqual(allowed.stderr, '')
                if not mode:
                    for limit in ('0', '9'):
                        refused = self.cli(*output, '--max-members', limit)
                        self.assertEqual(refused.returncode, 2, refused.stderr)
                        self.assertEqual(refused.stdout, '')
                        self.assertEqual(refused.stderr, 'flowwrench n8n-diet: emitted execution member count 10 '
                                         f'exceeds max_members {limit}\n')
        self.baseline.unlink()  # Parser invalid-limit refusal must precede policy IO.
        for mode in ((), ('--summary-only',)):
            for value in ('-1', '1.5', 'true', 'private-value', ''):
                result = self.cli(*mode, '--json', '--max-members=' + value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error: argument --max-members: max_members must be a non-negative integer\n', result.stderr)
                self.assertNotIn('private-value', result.stderr)
                self.assertNotIn('cannot read', result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_member_limit_protected_missing_data_and_orphan_exclusion(self):
        self.update('DELETE FROM execution_entity WHERE id NOT IN (5,7,10)')
        self.update("UPDATE execution_entity SET stoppedAt='private-invalid-time' WHERE id IN (5,10)")
        self.update('DROP TABLE workflow_entity')
        original = self.db.read_bytes()
        full = self.compare(max_members=3)
        self.assertEqual(full, self.compare())
        self.assertEqual(full['totals'], dict(zip(METRICS, (3, 1, BYTES[5]))))
        protected = next(c for c in full['transitions'] if c['from'] == c['to'] == 'protected')
        self.assertEqual([m['execution_id'] for m in protected['members']], [10, 5, 7])
        self.assertEqual([m['data_rows'] for m in protected['members']], [0, 1, 0])
        with self.assertRaisesRegex(DietError, '^emitted execution member count 3 exceeds max_members 2$'):
            self.compare(max_members=2)
        self.assert_summary_only(full, self.compare(summary_only=True, max_members=0))
        self.assertEqual(self.db.read_bytes(), original)

    def test_member_limit_zero_empty_and_orphan_only(self):
        self.update('DELETE FROM execution_entity')
        for orphan_only in (True, False):
            if not orphan_only:
                self.update('DELETE FROM execution_data')
            original = self.db.read_bytes()
            for summary in (False, True):
                report = self.compare(summary_only=summary, max_members=0, max_workflows=0)
                self.assertEqual(report, self.compare(summary_only=summary))
                self.assertEqual(report['totals'], dict.fromkeys(METRICS, 0))
                self.assertEqual(report['workflows'], [])
                self.assertEqual(len(report['transitions']), 9)
                result = self.cli('--max-members', '0', '--max-workflows', '0', '--json',
                                  *(['--summary-only'] if summary else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), report)
            self.assertEqual(self.db.read_bytes(), original)

    def test_member_limit_workflow_and_malformed_time_precedence(self):
        for mode in ((), ('--summary-only',)):
            result = self.cli(*mode, '--max-workflows', '0', '--max-members', '0', '--json')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr, 'flowwrench n8n-diet: observed workflow count 3 exceeds max_workflows 0\n')
        self.update("UPDATE execution_entity SET stoppedAt='secret-invalid-time' WHERE id=2")
        original = self.db.read_bytes()
        for side in ('baseline', 'candidate'):
            self.baseline.write_text(json.dumps({'max_count': 0, 'keep_failed': side != 'baseline'}))
            self.candidate.write_text(json.dumps({'max_count': 0, 'keep_failed': side != 'candidate'}))
            for mode in ((), ('--summary-only',)):
                for output in ((), ('--json',)):
                    result = self.cli(*mode, *output, '--max-workflows', '0', '--max-members', '0')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('execution stoppedAt', result.stderr)
                    for private in ('secret-invalid-time', 'observed workflow count', 'emitted execution member count'):
                        self.assertNotIn(private, result.stderr)
        self.baseline.write_text('{"max_count":0,"keep_failed":true}')
        self.candidate.write_text(self.baseline.read_text())
        result = self.cli('--max-members', '0', '--json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'flowwrench n8n-diet: emitted execution member count 10 exceeds max_members 0\n')
        summary = self.cli('--summary-only', '--max-members', '0', '--json')
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertEqual(json.loads(summary.stdout)['totals']['executions'], 10)
        self.assertEqual(self.db.read_bytes(), original)

    def test_workflow_limit_api_boundaries_equal_unrestricted(self):
        for summary in (False, True):
            unrestricted = self.compare(summary_only=summary)
            for limit in (None, 3, 4, 10**100):
                with self.subTest(summary=summary, limit=limit):
                    self.assertEqual(self.compare(summary_only=summary, max_workflows=limit), unrestricted)
            for limit in (0, 2):
                with self.subTest(summary=summary, limit=limit), self.assertRaisesRegex(
                        DietError, f'^observed workflow count 3 exceeds max_workflows {limit}$'):
                    self.compare(summary_only=summary, max_workflows=limit)
            self.assertEqual(len(unrestricted['transitions']), 9)
            self.assertEqual(unrestricted['drop_delta'], dict(zip(METRICS, (-2, -2, -15))))
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertEqual(sorted(p.name for p in self.root.glob('input*')), ['input.sqlite'])

    def test_workflow_limit_invalid_api_before_source_access(self):
        for value in (True, False, -1, 0.0, 3.0, '3', 'private-value', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                    DietError, '^max_workflows must be a non-negative integer or None$'):
                core.compare(self.root / 'absent.sqlite', baseline={}, candidate={},
                             as_of='bad', max_workflows=value)
        self.assertFalse((self.root / 'absent.sqlite').exists())

    def test_workflow_limit_cli_equality_refusal_and_invalid_values(self):
        self.update("UPDATE execution_data SET data='secret-payload-marker' WHERE executionId=8")
        original = self.db.read_bytes()
        for mode in ((), ('--summary-only',)):
            for output in ((), ('--json',)):
                unrestricted = self.cli(*mode, *output)
                self.assertEqual(unrestricted.returncode, 0, unrestricted.stderr)
                for limit in ('3', '4'):
                    allowed = self.cli(*mode, *output, '--max-workflows', limit)
                    self.assertEqual(allowed.returncode, 0, allowed.stderr)
                    self.assertEqual(allowed.stdout, unrestricted.stdout)
                    self.assertEqual(allowed.stderr, '')
                for limit in ('0', '2'):
                    refused = self.cli(*mode, *output, '--max-workflows', limit)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertEqual(refused.stdout, '')
                    self.assertEqual(refused.stderr, 'flowwrench n8n-diet: observed workflow count 3 '
                                     f'exceeds max_workflows {limit}\n')
        # Parser validation precedes policy/source access and never echoes input values.
        self.baseline.unlink()
        for value in ('-1', '1.5', 'true', 'private-value', ''):
            result = self.cli('--json', '--max-workflows=' + value)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('max_workflows must be a non-negative integer', result.stderr)
            self.assertNotIn('private-value', result.stderr)
            self.assertNotIn('cannot read', result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_workflow_limit_distinct_null_empty_string_ids_and_unused_catalog(self):
        self.db.unlink()
        sql = (ROOT / 'fixtures/split-schema.sql').read_text().replace(
            'workflowId VARCHAR(36) NOT NULL', 'workflowId VARCHAR(36)')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(sql)
            conn.executescript("""
                INSERT INTO workflow_entity VALUES ('unused','unused private name');
                UPDATE execution_entity SET workflowId=NULL WHERE id IN (1,7);
                UPDATE execution_entity SET workflowId='None' WHERE id=2;
                UPDATE execution_entity SET workflowId='null' WHERE id=3;
                UPDATE execution_entity SET workflowId='' WHERE id=10;
            """)
        original = self.db.read_bytes()
        for names in (True, False):
            if not names:
                self.update('DROP TABLE workflow_entity')
                original = self.db.read_bytes()
            for summary in (False, True):
                unlimited = self.compare(summary_only=summary)
                self.assertEqual([w['workflow_id'] for w in unlimited['workflows']],
                                 [None, '', 'None', 'null', 'wf-a', 'wf-b'])
                self.assertEqual(self.compare(summary_only=summary, max_workflows=6), unlimited)
                with self.assertRaisesRegex(DietError, '^observed workflow count 6 exceeds max_workflows 5$'):
                    self.compare(summary_only=summary, max_workflows=5)
                cli = self.cli('--json', *(['--summary-only'] if summary else []), '--max-workflows', '6')
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout), unlimited)
            self.assertEqual(self.db.read_bytes(), original)

    def test_workflow_limit_zero_empty_and_orphan_only(self):
        self.update('DELETE FROM execution_entity')
        for orphan_only in (True, False):
            if not orphan_only:
                self.update('DELETE FROM execution_data')
            original = self.db.read_bytes()
            for summary in (False, True):
                report = self.compare(summary_only=summary, max_workflows=0)
                self.assertEqual(report, self.compare(summary_only=summary))
                self.assertEqual(report['workflows'], [])
                self.assertEqual(len(report['transitions']), 9)
                result = self.cli('--max-workflows', '0', '--json',
                                  *(['--summary-only'] if summary else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), report)
            self.assertEqual(self.db.read_bytes(), original)

    def test_workflow_limit_malformed_time_precedes_count_either_policy(self):
        self.update("UPDATE execution_entity SET stoppedAt='secret-invalid-time' WHERE id=2")
        original = self.db.read_bytes()
        for side in ('baseline', 'candidate'):
            self.baseline.write_text(json.dumps({'max_count': 0, 'keep_failed': side != 'baseline'}))
            self.candidate.write_text(json.dumps({'max_count': 0, 'keep_failed': side != 'candidate'}))
            for mode in ((), ('--summary-only',)):
                for output in ((), ('--json',)):
                    result = self.cli(*mode, *output, '--max-workflows', '0')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('execution stoppedAt', result.stderr)
                    self.assertNotIn('secret-invalid-time', result.stderr)
                    self.assertNotIn('observed workflow count', result.stderr)
        # Both-side protection skips parsing, so the count refusal then wins.
        self.baseline.write_text('{"max_count":0,"keep_failed":true}')
        self.candidate.write_text(self.baseline.read_text())
        result = self.cli('--max-workflows', '0', '--json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'flowwrench n8n-diet: observed workflow count 3 exceeds max_workflows 0\n')
        self.assertEqual(self.db.read_bytes(), original)

    def test_summary_only_api_preserves_all_aggregates_and_default_members(self):
        for baseline, candidate in ((BASELINE, CANDIDATE), (CANDIDATE, BASELINE), (BASELINE, BASELINE)):
            args = dict(baseline=baseline, candidate=candidate, as_of=AS_OF)
            full = core.compare(self.db, **args)
            self.assertEqual(core.compare(self.db, summary_only=False, **args), full)
            self.assertNotIn('members_included', full)  # default JSON contract stays unchanged
            summary = core.compare(self.db, summary_only=True, **args)
            self.assert_summary_only(full, summary)
            self.assertLess(len(json.dumps(summary)), len(json.dumps(full)))
        full = self.compare()
        members = {(c['from'], c['to']): c['members'] for c in full['transitions']}
        self.assertEqual([m['execution_id'] for m in members['protected', 'protected']], [10, 5, 6, 7])
        self.assertEqual(members['keep', 'drop'], [{'execution_id': 3, 'workflow_id': 'wf-b',
            'data_rows': 1, 'payload_bytes': 21, 'baseline_reason': 'within_limits', 'candidate_reason': 'count'}])
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_summary_only_cli_json_and_human_contracts(self):
        self.update("UPDATE execution_data SET data='secret-payload-marker' WHERE executionId=8")
        original = self.db.read_bytes()
        full, summary = self.cli('--json'), self.cli('--summary-only', '--json')
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assert_summary_only(json.loads(full.stdout), json.loads(summary.stdout))
        self.assertEqual(json.loads(summary.stdout), self.compare(summary_only=True))
        self.assertEqual(summary.stdout, self.cli('--summary-only', '--json',
                         as_of='2026-11-01T01:00:00+01:00').stdout)
        human, summary_human = self.cli(), self.cli('--summary-only')
        self.assertEqual(summary_human.returncode, 0, summary_human.stderr)
        self.assertIn('Summary only: execution members omitted (members_included=false).', summary_human.stdout)
        for line in human.stdout.splitlines():
            if not line.startswith(('  id=', 'Note: Transitions include')):
                self.assertIn(line, summary_human.stdout)
        self.assertNotIn('  id=', summary_human.stdout)
        for reason in ('within_limits', 'non_terminal_or_unknown_status', 'missing_stopped_at'):
            self.assertNotIn(reason, summary.stdout + summary_human.stdout)
        self.assertNotIn('keep_failed', summary_human.stdout)  # JSON retains the policy's keep_failed key
        self.assertNotIn('secret-payload-marker', full.stdout + summary.stdout + summary_human.stdout)
        self.assertEqual(self.db.read_bytes(), original)

    def test_summary_only_empty_and_orphan_only_reports(self):
        self.update('DELETE FROM execution_entity')
        for orphan_only in (True, False):
            if not orphan_only:
                self.update('DELETE FROM execution_data')
            original = self.db.read_bytes()
            full, summary = self.compare(), self.compare(summary_only=True)
            self.assert_summary_only(full, summary)
            self.assertEqual(summary['totals'], dict.fromkeys(METRICS, 0))
            self.assertEqual(summary['workflows'], [])
            result = self.cli('--summary-only', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), summary)
            self.assertEqual(self.db.read_bytes(), original)

    def test_summary_only_malformed_either_side_and_protected_times(self):
        self.update("UPDATE execution_entity SET stoppedAt='secret-invalid-time' WHERE id=2")
        original = self.db.read_bytes()
        protected, evaluated = {'max_count': 0, 'keep_failed': True}, {'max_count': 0}
        for left, right in ((protected, evaluated), (evaluated, protected)):
            self.baseline.write_text(json.dumps(left))
            self.candidate.write_text(json.dumps(right))
            with self.assertRaisesRegex(DietError, 'stoppedAt'):
                core.compare(self.db, baseline=left, candidate=right, as_of=AS_OF, summary_only=True)
            for output in ((), ('--json',)):
                result = self.cli('--summary-only', *output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('stoppedAt', result.stderr)
                self.assertNotIn('secret-invalid-time', result.stderr)
            self.assertEqual(self.db.read_bytes(), original)
        args = dict(baseline=protected, candidate=protected, as_of=AS_OF)
        self.assert_summary_only(core.compare(self.db, **args), core.compare(self.db, summary_only=True, **args))
        self.assertEqual(self.db.read_bytes(), original)

    def test_summary_only_requires_bool_before_source_access(self):
        for value in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(DietError, 'summary_only.*boolean'):
                core.compare(self.root / 'absent.sqlite', baseline=BASELINE, candidate=CANDIDATE,
                             as_of=AS_OF, summary_only=value)

    def test_report_limits_count_committed_wal_snapshot_not_main_file(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO execution_entity(id,workflowId,status) VALUES (11,'wal-private-workflow','running')")
            writer.execute("INSERT INTO workflow_entity VALUES ('wal-private-workflow','WAL-only name')")
            writer.execute("UPDATE workflow_entity SET name='WAL-updated name' WHERE id='wf-b'")
            writer.commit()
            paths = [self.db, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm')]
            hashes = self.external_hashes(paths)
            for mode in ((), ('--summary-only',)):
                # Source/WAL validation wins even though max_workflows=0 is exceeded.
                refused = self.cli(*mode, '--max-workflows', '0', '--json')
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertEqual(refused.stdout, '')
                self.assertIn('non-empty WAL', refused.stderr)
                full = self.cli(*mode, '--force', '--json')
                exact = self.cli(*mode, '--force', '--max-workflows', '4', '--json')
                self.assertEqual(full.returncode, 0, full.stderr)
                self.assertEqual(exact.returncode, 0, exact.stderr)
                self.assertEqual(exact.stdout, full.stdout)
                self.assertEqual(len(json.loads(exact.stdout)['workflows']), 4)
                workflow_names = {w['workflow_id']: w['workflow_name']
                                  for w in json.loads(exact.stdout)['workflows']}
                self.assertEqual(workflow_names['wal-private-workflow'], 'WAL-only name')
                self.assertEqual(workflow_names['wf-b'], 'WAL-updated name')
                refused = self.cli(*mode, '--force', '--max-workflows', '3', '--json')
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertEqual(refused.stdout, '')
                self.assertEqual(refused.stderr, 'flowwrench n8n-diet: observed workflow count 4 exceeds max_workflows 3\n')
                member_limit = '0' if mode else '11'
                exact_members = self.cli(*mode, '--force', '--max-members', member_limit, '--json')
                self.assertEqual(exact_members.returncode, 0, exact_members.stderr)
                self.assertEqual(exact_members.stdout, full.stdout)
                refused = self.cli(*mode, '--max-members', '0', '--json')
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertEqual(refused.stdout, '')
                self.assertIn('non-empty WAL', refused.stderr)
                if not mode:
                    # Ten rows in the main DB, eleven in the committed WAL snapshot.
                    refused = self.cli('--force', '--max-members', '10', '--json')
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertEqual(refused.stdout, '')
                    self.assertEqual(refused.stderr,
                                     'flowwrench n8n-diet: emitted execution member count 11 exceeds max_members 10\n')
                self.assertEqual(self.external_hashes(paths), hashes)

    def test_summary_only_rollback_journal_and_writer_refusals(self):
        journal = Path(str(self.db) + '-journal')
        journal.write_bytes(b'unrecovered')
        for mode in ((), ('--summary-only',), ('--max-workflows', '0'),
                     ('--summary-only', '--max-workflows', '0'),
                     ('--max-members', '0'), ('--summary-only', '--max-members', '0'),
                     ('--max-workflows', '0', '--max-members', '0'),
                     ('--summary-only', '--max-workflows', '0', '--max-members', '0')):
            result = self.cli(*mode, '--force', '--json')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertIn('rollback journal', result.stderr)
            self.assertEqual(journal.read_bytes(), b'unrecovered')
            self.assertEqual(self.db.read_bytes(), self.original)
        journal.unlink()
        original_hashes = self.external_hashes([self.db])
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            journal_bytes = journal.read_bytes() if journal.exists() else None
            for mode in ((), ('--summary-only',), ('--max-workflows', '0'),
                     ('--summary-only', '--max-workflows', '0'),
                     ('--max-members', '0'), ('--summary-only', '--max-members', '0'),
                     ('--max-workflows', '0', '--max-members', '0'),
                     ('--summary-only', '--max-workflows', '0', '--max-members', '0')):
                result = self.cli(*mode, '--force', '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('locked', result.stderr)
                self.assertEqual(self.external_hashes([self.db]), original_hashes)
                self.assertEqual(journal.read_bytes() if journal.exists() else None, journal_bytes)
            writer.rollback()

    def test_workflow_rollups_exact_totals_global_count_and_orphan_exclusion(self):
        report = self.compare()
        self.assertEqual([w['workflow_id'] for w in report['workflows']], ['missing-workflow', 'wf-a', 'wf-b'])
        expected = [
            (None, (1, 0, 0), (0, 0, 0)),
            ('Email', (4, 3, 37), (-1, -1, -23)),
            ('订单', (5, 5, 47), (-1, -1, 8)),
        ]
        for workflow, (name, totals, delta) in zip(report['workflows'], expected):
            self.assertEqual(workflow['workflow_name'], name)
            self.assertEqual(workflow['totals'], dict(zip(METRICS, totals)))
            self.assertEqual(workflow['drop_delta'], dict(zip(METRICS, delta)))
        # The global newest-two budget keeps only wf-b rows; never restart it per workflow.
        self.assertEqual(report['workflows'][1]['baseline']['groups']['keep']['executions'], 0)
        self.assertEqual(report['workflows'][2]['baseline']['groups']['keep']['executions'], 2)
        self.assert_workflow_accounting(report)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_workflow_rollups_optional_names_null_ids_and_exact_blob_bytes(self):
        # Structural detection also admits nullable workflowId. Null must not
        # collide with a string sentinel or disappear through an inner join.
        self.db.unlink()
        sql = (ROOT / 'fixtures/split-schema.sql').read_text().replace(
            'workflowId VARCHAR(36) NOT NULL', 'workflowId VARCHAR(36)')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(sql)
            conn.executescript('''DROP TABLE workflow_entity;
                UPDATE execution_entity SET workflowId=NULL WHERE id IN (1,7);
                UPDATE execution_entity SET workflowId='None' WHERE id=2;
                UPDATE execution_entity SET workflowId='' WHERE id=10;
                UPDATE execution_data SET data=x'00ff80',workflowData='é' WHERE executionId=1;''')
        original = self.db.read_bytes()
        report = self.compare()
        self.assertEqual([w['workflow_id'] for w in report['workflows']], [None, '', 'None', 'wf-a', 'wf-b'])
        self.assertTrue(all(w['workflow_name'] is None for w in report['workflows']))
        self.assertEqual(report['workflows'][0]['totals'], dict(zip(METRICS, (2, 1, 5))))
        self.assertEqual(report['workflows'][1]['totals'], dict(zip(METRICS, (1, 0, 0))))
        self.assert_workflow_accounting(report)
        self.assert_summary_only(report, self.compare(summary_only=True))
        self.assertEqual(self.db.read_bytes(), original)

    def test_workflow_rollups_reverse_and_identical_policies(self):
        report = self.compare()
        reverse = core.compare(self.db, baseline=CANDIDATE, candidate=BASELINE, as_of=AS_OF)
        same = core.compare(self.db, baseline=BASELINE, candidate=BASELINE, as_of=AS_OF)
        self.assert_workflow_accounting(reverse)
        self.assert_workflow_accounting(same)
        for workflow, flipped, identical in zip(report['workflows'], reverse['workflows'], same['workflows']):
            self.assertEqual(workflow['workflow_id'], flipped['workflow_id'])
            self.assertEqual(workflow['baseline'], flipped['candidate'])
            self.assertEqual(workflow['candidate'], flipped['baseline'])
            self.assertEqual(flipped['drop_delta'], {k: -v for k, v in workflow['drop_delta'].items()})
            cells = {(c['from'], c['to']): c for c in flipped['transitions']}
            for cell in workflow['transitions']:
                self.assertEqual([cell[k] for k in METRICS], [cells[cell['to'], cell['from']][k] for k in METRICS])
            self.assertEqual(identical['drop_delta'], dict.fromkeys(METRICS, 0))
            self.assertTrue(all(c['executions'] == 0 for c in identical['transitions'] if c['from'] != c['to']))

    def test_workflow_rollups_cli_order_replay_and_escaped_human_output(self):
        self.update("UPDATE workflow_entity SET name=? WHERE id='wf-a'", ('line\n\x1b[31m',))
        self.update("UPDATE execution_entity SET workflowId=? WHERE id=10", ('id\n\x1b[31m',))
        self.update("INSERT INTO workflow_entity VALUES ('unused','no executions')")
        original = self.db.read_bytes()
        first = self.cli('--json')
        second = self.cli('--json', as_of='2026-11-01T01:00:00+01:00')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report, self.compare())
        self.assertEqual(len(report['workflows']), 3)
        self.assert_workflow_accounting(report)
        human = self.cli()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('Workflow transition rollups:', human.stdout)
        self.assertIn('Workflow "wf-a" "line\\n\\u001b[31m": 4 executions / 3 data rows; 37 payload bytes', human.stdout)
        self.assertIn('Workflow "id\\n\\u001b[31m" null:', human.stdout)
        self.assertIn('drop -> protected: 1 executions / 1 data rows; 23 payload bytes', human.stdout)
        self.assertNotIn('\x1b', human.stdout)
        self.assertEqual(self.db.read_bytes(), original)

    def test_exact_transitions_and_partition_accounting(self):
        report = self.compare()
        expected = {('keep', 'drop'): [3], ('keep', 'keep'): [4],
                    ('drop', 'drop'): [1], ('drop', 'protected'): [2, 8, 9],
                    ('protected', 'protected'): [10, 5, 6, 7]}
        self.assertEqual(len(report['transitions']), 9)
        for cell in report['transitions']:
            ids = expected.get((cell['from'], cell['to']), [])
            self.assertEqual([row['execution_id'] for row in cell['members']], ids)
            self.assertEqual(cell['executions'], len(ids))
            self.assertEqual(cell['data_rows'], sum(i not in (7, 10) for i in ids))
            self.assertEqual(cell['payload_bytes'], sum(BYTES[i] for i in ids))
            for row in cell['members']:
                self.assertEqual(row['payload_bytes'], BYTES[row['execution_id']])
        self.assertEqual(report['totals'], {'executions': 10, 'data_rows': 8, 'payload_bytes': sum(BYTES.values())})
        self.assertEqual(report['drop_delta'], {'executions': -2, 'data_rows': -2,
                         'payload_bytes': BYTES[3] - BYTES[2] - BYTES[8] - BYTES[9]})
        for side, field in [('baseline', 'from'), ('candidate', 'to')]:
            summary = report[side]['groups']
            for metric in METRICS:
                self.assertEqual(sum(g[metric] for g in summary.values()), report['totals'][metric])
                for group in GROUPS:
                    self.assertEqual(sum(c[metric] for c in report['transitions'] if c[field] == group), summary[group][metric])
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertEqual(sorted(p.name for p in self.root.glob('input*')), ['input.sqlite'])

    def test_summaries_match_single_policy_and_reversing_policies(self):
        report = self.compare()
        reverse = core.compare(self.db, baseline=CANDIDATE, candidate=BASELINE, as_of=AS_OF)
        for metric in METRICS:
            self.assertEqual(reverse['drop_delta'][metric], -report['drop_delta'][metric])
        for side, policy in [('baseline', BASELINE), ('candidate', CANDIDATE)]:
            single = core.plan(self.db, as_of=AS_OF, **policy)
            self.assertEqual(report[side]['policy'], single['policy'])
            self.assertEqual(report[side]['groups']['drop'], {k: single['delete'][k] for k in METRICS})
        cells = {(c['from'], c['to']): c for c in reverse['transitions']}
        for cell in report['transitions']:
            other = cells[(cell['to'], cell['from'])]
            self.assertEqual(other['payload_bytes'], cell['payload_bytes'])
            self.assertEqual([m['execution_id'] for m in other['members']], [m['execution_id'] for m in cell['members']])

    def test_same_policy_has_only_diagonal_members(self):
        report = core.compare(self.db, baseline=BASELINE, candidate=BASELINE, as_of=AS_OF)
        self.assertTrue(all(c['executions'] == 0 for c in report['transitions'] if c['from'] != c['to']))
        self.assertEqual(report['drop_delta'], dict.fromkeys(METRICS, 0))

    def test_cli_determinism_normalized_time_and_human_output(self):
        first = self.cli('--json')
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli('--json', as_of='2026-11-01T01:00:00+01:00')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout), self.compare())
        human = self.cli()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('keep -> drop: 1 executions', human.stdout)
        self.assertIn('id=3', human.stdout)
        self.assertIn('drop -> protected: 3 executions', human.stdout)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_policy_validation_and_explicit_reference_time(self):
        for bad in [{}, [], {'max_count': True}, {'max_count': -1}, {'max_count': 1.0},
                    {'max_age_days': False}, {'max_age_days': 365001},
                    {'max_count': 1, 'keep_failed': 'false'}, {'max_count': 1, 'as_of': AS_OF},
                    {'max_count': 1, 'typo': 2}]:
            for side in ('baseline', 'candidate'):
                with self.subTest(bad=bad, side=side), self.assertRaises(DietError):
                    core.compare(self.db, as_of=AS_OF, **{'baseline': BASELINE, 'candidate': CANDIDATE, side: bad})
        for stamp in (None, '', 'not-a-time', '2026-11-01'):
            with self.subTest(stamp=stamp), self.assertRaises(DietError):
                core.compare(self.db, baseline=BASELINE, candidate=CANDIDATE, as_of=stamp)
        with self.assertRaisesRegex(DietError, 'cutoff'):
            core.compare(self.db, baseline={'max_age_days': 365000}, candidate=BASELINE,
                         as_of='0001-01-01T00:00:00Z')

    def test_bad_policy_files_fail_without_partial_report(self):
        for content in ['not JSON', '[]', '{"max_count":1,"max_count":2}',
                        '{"max_count":NaN}', '{"max_count":1,"keep_failed":0}',
                        '{"max_count":1,"unknown":2}', ' ' * 65537, '\udcff']:
            self.candidate.write_bytes(content.encode('utf-8', errors='surrogateescape'))
            result = self.cli('--json')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('Traceback', result.stderr)
        self.candidate.unlink()
        self.assertEqual(self.cli('--json').returncode, 2)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_malformed_timestamp_evaluated_by_either_policy_fails(self):
        # Baseline protects errors without parsing; candidate must evaluate this row.
        self.update("UPDATE execution_entity SET stoppedAt=? WHERE id=2", ('secret-invalid-time',))
        self.baseline.write_text(json.dumps({'max_count': 0, 'keep_failed': True}))
        self.candidate.write_text(json.dumps({'max_count': 0}))
        result = self.cli('--json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('stoppedAt', result.stderr)
        self.assertNotIn('secret-invalid-time', result.stderr)
        # Even if the other side protects it, malformed eligible input is never old.
        self.baseline.write_text(json.dumps({'max_count': 0}))
        self.candidate.write_text(json.dumps({'max_count': 0, 'keep_failed': True}))
        self.assertEqual(self.cli('--json').returncode, 2)

    def test_protected_rows_do_not_require_parsable_times(self):
        self.update("UPDATE execution_entity SET stoppedAt='invalid' WHERE id IN (2,5,6,8,9,10)")
        policy = {'max_count': 0, 'keep_failed': True}
        report = core.compare(self.db, baseline=policy, candidate=policy, as_of=AS_OF)
        self.assertEqual(report['candidate']['groups']['protected']['executions'], 7)
        reasons = {m['execution_id']: m['candidate_reason'] for c in report['transitions'] for m in c['members']}
        self.assertEqual(reasons[2], 'keep_failed')
        self.assertEqual(reasons[5], 'non_terminal_or_unknown_status')
        self.assertEqual(reasons[7], 'missing_stopped_at')

    def test_time_boundaries_ties_future_blob_bytes_and_missing_data(self):
        self.update("UPDATE execution_entity SET stoppedAt='2026-10-12T01:00:00+01:00' WHERE id IN (1,2,3,4,8,9)")
        self.update("UPDATE execution_entity SET stoppedAt='2026-12-01T00:00:00Z' WHERE id=4")
        self.update("UPDATE execution_entity SET status='success',stoppedAt='2026-10-12 00:00:00' WHERE id=10")
        self.update("UPDATE execution_data SET data=x'00ff80',workflowData='é' WHERE executionId=9")
        # At the age boundary no row is old. Lexicographic IDs 9,8 are newest ties.
        report = core.compare(self.db, baseline={'max_age_days': 20}, candidate={'max_count': 2}, as_of=AS_OF)
        moved = next(c for c in report['transitions'] if (c['from'], c['to']) == ('keep', 'drop'))
        self.assertEqual([m['execution_id'] for m in moved['members']], [1, 10, 2, 3])
        kept = next(c for c in report['transitions'] if (c['from'], c['to']) == ('keep', 'keep'))
        self.assertEqual([m['execution_id'] for m in kept['members']], [8, 9])
        self.assertEqual(kept['members'][1]['payload_bytes'], 5)  # 3 BLOB + 2 UTF-8
        protected = next(c for c in report['transitions'] if c['from'] == c['to'] == 'protected')
        self.assertIn('future_stopped_at', [m['candidate_reason'] for m in protected['members']])

    def test_failure_protection_changes_global_count_budget_without_payload_leaks(self):
        self.update("UPDATE execution_data SET data='secret-payload-marker' WHERE executionId=8")
        self.baseline.write_text(json.dumps({'max_count': 2, 'keep_failed': True}))
        self.candidate.write_text(json.dumps({'max_count': 4}))
        result = self.cli('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        cells = {(c['from'], c['to']): c for c in json.loads(result.stdout)['transitions']}
        self.assertEqual([m['execution_id'] for m in cells['protected', 'keep']['members']], [8, 9])
        self.assertEqual([m['execution_id'] for m in cells['protected', 'drop']['members']], [2])
        self.assertNotIn('secret-payload-marker', result.stdout + result.stderr)

    def test_empty_database_zero_limits(self):
        self.update('DELETE FROM execution_entity')
        report = self.compare()
        self.assertEqual(report['totals'], dict.fromkeys(METRICS, 0))
        self.assertEqual(len(report['transitions']), 9)
        self.assertTrue(all(not c['members'] for c in report['transitions']))
        self.assertEqual(report['workflows'], [])
        self.assert_workflow_accounting(report)

    def test_wal_snapshot_and_writer_refusal(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE execution_data SET data='committed WAL bytes' WHERE executionId=1")
            writer.commit()
            paths = [self.db, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm')]
            hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
            self.assertEqual(self.cli('--json').returncode, 2)
            refused = self.cli('--summary-only', '--json')
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertEqual(refused.stdout, '')
            self.assertIn('non-empty WAL', refused.stderr)
            result = self.cli('--force', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['wal_snapshot'])
            self.assert_workflow_accounting(report)
            workflow = next(w for w in report['workflows'] if w['workflow_id'] == 'wf-a')
            self.assertEqual(workflow['totals']['payload_bytes'], 27 + len('{}committed WAL bytes'))
            cell = next(c for c in report['transitions'] if c['from'] == c['to'] == 'drop')
            self.assertEqual(cell['payload_bytes'], len('{}committed WAL bytes'))
            summary = self.cli('--summary-only', '--force', '--json')
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assert_summary_only(report, json.loads(summary.stdout))
            self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
            writer.execute('BEGIN IMMEDIATE')
            locked_hashes = self.external_hashes(paths)
            for mode in ((), ('--summary-only',), ('--max-workflows', '0'),
                         ('--summary-only', '--max-workflows', '0'),
                     ('--max-members', '0'), ('--summary-only', '--max-members', '0'),
                     ('--max-workflows', '0', '--max-members', '0'),
                     ('--summary-only', '--max-workflows', '0', '--max-members', '0')):
                locked = self.cli(*mode, '--force', '--json')
                self.assertEqual(locked.returncode, 2)
                self.assertIn('locked', locked.stderr)
                self.assertEqual(locked.stdout, '')
                self.assertEqual(locked_hashes, self.external_hashes(paths))
            writer.rollback()
            # A stopped backup with WAL but no shared-memory lock file is refused.
            backup = self.root / 'missing-shm.sqlite'
            backup.write_bytes(self.db.read_bytes())
            backup_wal = Path(str(backup) + '-wal')
            backup_wal.write_bytes(paths[1].read_bytes())
            original_path, self.db = self.db, backup
            backup_bytes = [p.read_bytes() for p in (backup, backup_wal)]
            try:
                for mode in ((), ('--summary-only',), ('--max-workflows', '0'),
                         ('--summary-only', '--max-workflows', '0'),
                     ('--max-members', '0'), ('--summary-only', '--max-members', '0'),
                     ('--max-workflows', '0', '--max-members', '0'),
                     ('--summary-only', '--max-workflows', '0', '--max-members', '0')):
                    refused = self.cli(*mode, '--force', '--json')
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertEqual(refused.stdout, '')
                    self.assertIn('SHM', refused.stderr)
                    self.assertEqual([p.read_bytes() for p in (backup, backup_wal)], backup_bytes)
                    self.assertFalse(Path(str(backup) + '-shm').exists())
            finally:
                self.db = original_path


if __name__ == '__main__':
    unittest.main()
