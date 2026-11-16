"""Analyze limits count emitted keys, without truncating or bypassing safety."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from flowwrench.n8n_diet import core
from flowwrench.n8n_diet.storage import DietError
from .test_diet import cli

ROOT = Path(__file__).resolve().parent


class AnalysisLimitsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'input.sqlite'
        self.sql = (ROOT / 'fixtures/split-schema.sql').read_text()
        self.create(self.sql)

    def create(self, sql):
        self.db.unlink(missing_ok=True)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(sql)
            conn.executemany('INSERT INTO workflow_entity VALUES (?,?)',
                             [(f'unused-{i}', f'unused name {i}') for i in range(50)])
            conn.commit()

    @contextmanager
    def trace(self):
        queries = []
        original = core.source_database

        @contextmanager
        def source(*args, **kwargs):
            with original(*args, **kwargs) as (conn, path, wal):
                conn.set_trace_callback(queries.append)
                yield conn, path, wal

        with mock.patch.object(core, 'source_database', source):
            yield queries

    def test_default_exact_above_and_top_keep_complete_reports(self):
        original = self.db.read_bytes()
        for dbstat in (True, False):
            for top in (1, 20):
                full = core.analyze(self.db, use_dbstat=dbstat, top=top)
                self.assertEqual(len(full['workflows']), 3)
                self.assertEqual(full['totals']['executions'], 10)
                for limit in (None, 3, 4, 10**100):
                    self.assertEqual(core.analyze(self.db, max_workflows=limit,
                                                  use_dbstat=dbstat, top=top), full)
        self.assertEqual(self.db.read_bytes(), original)

    def test_exceeded_count_only_before_names_and_report_construction(self):
        original = self.db.read_bytes()
        for limit in (0, 2):
            with self.trace() as queries, mock.patch.object(core, 'page_stats', side_effect=AssertionError('report built')):
                with self.assertRaisesRegex(DietError, f'^observed workflow count 3 exceeds max_workflows {limit}$'):
                    core.analyze(self.db, max_workflows=limit, top=1, force=True)
            self.assertFalse(any('SELECT id,name' in q for q in queries))
            self.assertFalse(any('LIMIT' in q.upper() or 'FROM dbstat' in q for q in queries))
        self.assertEqual(self.db.read_bytes(), original)
        for flags in (('--json',), (), ('--json', '--force', '--no-dbstat')):
            result = cli('analyze', self.db, *flags, '--max-workflows', '2')
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                (2, '', 'flowwrench n8n-diet: observed workflow count 3 exceeds max_workflows 2\n'))
        self.assertEqual(self.db.read_bytes(), original)

    def test_nullable_empty_literal_missing_absent_and_orphan_only(self):
        self.create(self.sql.replace('workflowId VARCHAR(36) NOT NULL', 'workflowId VARCHAR(36)')
                    .replace('id VARCHAR(36) PRIMARY KEY NOT NULL', 'id VARCHAR(36) PRIMARY KEY')
                    .replace('name VARCHAR(128) NOT NULL', 'name VARCHAR(128)') + """
            INSERT INTO workflow_entity VALUES (NULL,'null ID'), ('','empty ID'),
                ('None','literal None'), ('null',NULL);
            UPDATE execution_entity SET workflowId=NULL WHERE id=1;
            UPDATE execution_entity SET workflowId='None' WHERE id=2;
            UPDATE execution_entity SET workflowId='null' WHERE id=3;
            UPDATE execution_entity SET workflowId='' WHERE id=10;
        """)
        for absent in (False, True):
            if absent:
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute('DROP TABLE workflow_entity')
            original = self.db.read_bytes()
            full = core.analyze(self.db)
            self.assertEqual({w['workflow_id'] for w in full['workflows']}, {None, '', 'None', 'null', 'wf-a', 'wf-b'})
            self.assertEqual(core.analyze(self.db, max_workflows=6), full)
            with self.assertRaisesRegex(DietError, 'count 6 exceeds max_workflows 5'):
                core.analyze(self.db, max_workflows=5)
            self.assertEqual(self.db.read_bytes(), original)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DELETE FROM execution_entity')
            conn.commit()
        for empty in (False, True):
            if empty:
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute('DELETE FROM execution_data')
                    conn.commit()
            full = core.analyze(self.db)
            self.assertEqual(full['workflows'], [])
            self.assertEqual(core.analyze(self.db, max_workflows=0), full)

    def test_count_preserves_sql_group_representatives_and_python_key_equality(self):
        # NOCASE can emit distinct raw representatives across status groups;
        # SQLite DISTINCT workflowId would incorrectly count just one.
        self.create(self.sql.replace('workflowId VARCHAR(36) NOT NULL', 'workflowId COLLATE NOCASE') + """
            DELETE FROM execution_entity;
            INSERT INTO execution_entity(workflowId,status) VALUES
                ('Case','running'), ('case','waiting'), ('CASE','running');
        """)
        full = core.analyze(self.db)
        self.assertEqual(len(full['workflows']), 2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(DISTINCT workflowId) FROM execution_entity').fetchone()[0], 1)
        self.assertEqual(core.analyze(self.db, max_workflows=2), full)
        with self.assertRaisesRegex(DietError, 'count 2 exceeds max_workflows 1'):
            core.analyze(self.db, max_workflows=1)
        # INTEGER 42 and REAL 42.0 merge in Python even across status groups;
        # TEXT '42', empty, NULL and literal 'None' remain distinct keys.
        self.create(self.sql.replace('workflowId VARCHAR(36) NOT NULL', 'workflowId') + """
            DELETE FROM execution_entity;
            INSERT INTO execution_entity(workflowId,status) VALUES
                (42,'running'), (42.0,'waiting'), ('42','unknown'),
                (NULL,'running'), ('','running'), ('None','running');
        """)
        full = core.analyze(self.db)
        self.assertEqual(len(full['workflows']), 5)
        self.assertEqual(core.analyze(self.db, max_workflows=5), full)
        with self.assertRaisesRegex(DietError, 'count 5 exceeds max_workflows 4'):
            core.analyze(self.db, max_workflows=4)

    def test_invalid_api_and_connection_values_precede_source_and_schema(self):
        for value in (True, False, 1.0, '3', [], {}, (), -1):
            with self.subTest(value=value):
                with mock.patch.object(core, 'source_database', side_effect=AssertionError('source opened')):
                    with self.assertRaisesRegex(DietError, '^max_workflows must be a non-negative integer or None$'):
                        core.analyze(self.db, max_workflows=value)
                with mock.patch.object(core, 'detect_schema', side_effect=AssertionError('schema read')):
                    with self.assertRaisesRegex(DietError, '^max_workflows must be a non-negative integer or None$'):
                        core.analyze_connection(None, self.db, max_workflows=value)

    def test_cli_invalid_values_are_generic_and_precede_missing_source(self):
        for value in ('-1', '1.0', 'true', 'private-workflow', '', '[]', '9'*5000):
            result = cli('analyze', self.db.with_name('missing.sqlite'), '--json', '--max-workflows=' + value)
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertTrue(result.stderr.splitlines()[-1].endswith(
                'analyze: error: argument --max-workflows: max_workflows must be a non-negative integer'))
            self.assertNotIn('No such file', result.stderr)
        for flags in (('--json',), (), ('--json', '--no-dbstat', '--top', '1')):
            full = cli('analyze', self.db, *flags)
            for limit in (3, 4):
                allowed = cli('analyze', self.db, *flags, '--max-workflows', str(limit))
                self.assertEqual((allowed.returncode, allowed.stdout, allowed.stderr),
                                 (full.returncode, full.stdout, full.stderr))

    def test_schema_source_and_journal_refusals_precede_zero_limit_even_force(self):
        for flags in ((), ('--force',)):
            journal = Path(str(self.db) + '-journal')
            journal.write_bytes(b'unrecovered')
            original = [self.db.read_bytes(), journal.read_bytes()]
            result = cli('analyze', self.db, '--json', '--max-workflows', '0', *flags)
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertIn('rollback journal', result.stderr)
            self.assertEqual([self.db.read_bytes(), journal.read_bytes()], original)
            journal.unlink()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TABLE execution_data')
        original = self.db.read_bytes()
        result = cli('analyze', self.db, '--json', '--max-workflows', '0', '--force')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertIn('unsupported schema', result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_committed_wal_count_names_and_source_bytes_same_snapshot(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.executescript("""
                UPDATE workflow_entity SET name='WAL name' WHERE id='wf-a';
                INSERT INTO workflow_entity VALUES ('wal-only','WAL only');
                INSERT INTO execution_entity(workflowId,status) VALUES ('wal-only','running');
            """)
            paths = [self.db, Path(str(self.db)+'-wal'), Path(str(self.db)+'-shm')]
            original = [p.read_bytes() for p in paths]
            full = core.analyze(self.db, force=True)
            self.assertEqual(len(full['workflows']), 4)
            self.assertTrue(full['wal_snapshot'])
            self.assertEqual(core.analyze(self.db, force=True, max_workflows=4), full)
            self.assertEqual({w['workflow_id']:w['workflow_name'] for w in full['workflows']}['wf-a'], 'WAL name')
            with self.trace() as queries:
                with self.assertRaisesRegex(DietError, 'count 4 exceeds max_workflows 3'):
                    core.analyze(self.db, force=True, max_workflows=3)
            self.assertFalse(any('SELECT id,name' in q for q in queries))
            with self.assertRaisesRegex(DietError, 'WAL'):
                core.analyze(self.db, max_workflows=0)
            self.assertEqual([p.read_bytes() for p in paths], original)

    def test_storage_shadow_and_virtual_schema_refuse_before_count(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE dbstat (name TEXT)')
        original = self.db.read_bytes()
        for limit in (0, 3):
            with self.assertRaisesRegex(DietError, 'dbstat name shadows'):
                core.analyze(self.db, max_workflows=limit, force=True)
        # As before, explicit fallback does not use or reject a dbstat shadow.
        self.assertEqual(core.analyze(self.db, max_workflows=3, use_dbstat=False),
                         core.analyze(self.db, use_dbstat=False))
        self.assertEqual(self.db.read_bytes(), original)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TABLE dbstat')
            conn.execute('CREATE VIRTUAL TABLE extra USING fts5(value)')
        original = self.db.read_bytes()
        result = cli('analyze', self.db, '--json', '--force', '--max-workflows', '0')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertIn('virtual tables are unsupported', result.stderr)
        self.assertEqual(self.db.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
