"""Observed optional analyze names; report/storage/selection contracts stay complete."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from flowwrench.n8n_diet import core

ROOT = Path(__file__).resolve().parent


class AnalysisNamesTest(unittest.TestCase):
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
                             [(f'unused-{i}', f'unused name {i}') for i in range(1500)])
            conn.commit()

    @contextmanager
    def observe_names(self, *, variable_limit=None):
        """Count actual fetched rows, using real locks/private WAL snapshots."""
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
                                        and 'workflow_entity' in sql and 'id,name' in sql else None)
                if variable_limit is not None:
                    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
                yield conn, path, wal

        with mock.patch.object(core, 'source_database', source):
            yield observed

    def test_only_observed_names_complete_reports_and_top(self):
        original = self.db.read_bytes()
        for dbstat in (True, False):
            for top in (1, 3, 20):
                with self.observe_names() as names:
                    report = core.analyze(self.db, top=top, use_dbstat=dbstat)
                self.assertEqual(names['ids'], ['wf-a', 'wf-b'])
                self.assertEqual(len(names['queries']), 1)
                self.assertEqual([(w['workflow_id'], w['workflow_name'], w['executions'],
                                   w['data_rows'], w['payload_bytes'], w['statuses'])
                                  for w in report['workflows']], [
                    ('wf-b', '订单', 5, 5, 47, {'canceled': 1, 'crashed': 1, 'running': 1, 'success': 2}),
                    ('wf-a', 'Email', 4, 3, 37, {'error': 1, 'success': 2, 'waiting': 1}),
                    ('missing-workflow', None, 1, 0, 0, {'unknown': 1})])
                self.assertEqual(report['totals'], {'executions': 10, 'payload_bytes': 84,
                    'missing_data_rows': 2, 'orphan_data_rows': 1, 'orphan_payload_bytes': 17,
                    'storage_locations': {'db': 10}})
                self.assertEqual([e['id'] for e in report['largest_executions']],
                                 [2, 3, 1, 4, 9, 8, 6, 5, 10, 7][:top])
                table = next(t for t in report['table_storage']['tables'] if t['table'] == 'workflow_entity')
                self.assertEqual(table['rows'], 1502)  # Storage still measures unused definitions.
                self.assertEqual(report['table_storage']['method'], 'dbstat' if dbstat else 'value-bytes-estimate')
                if not dbstat:
                    with closing(sqlite3.connect(self.db)) as conn:
                        expected = conn.execute('SELECT sum(length(CAST(id AS BLOB))+length(CAST(name AS BLOB))) FROM workflow_entity').fetchone()[0]
                    self.assertEqual(table['value_bytes_estimate'], expected)
        self.assertEqual(self.db.read_bytes(), original)

    def test_null_empty_literal_keys_names_and_deterministic_order(self):
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
        original = self.db.read_bytes()
        with self.observe_names() as names:
            report = core.analyze(self.db, top=1)
        self.assertEqual(set(names['ids']), {None, '', 'None', 'null', 'wf-a', 'wf-b'})
        self.assertEqual(len(names['ids']), 6)
        self.assertEqual([(w['workflow_id'], w['workflow_name']) for w in report['workflows']],
                         [('wf-b', '订单'), ('None', 'literal None'), ('null', None),
                          (None, 'null ID'), ('wf-a', 'Email'), ('', 'empty ID')])
        self.assertEqual(report['totals']['payload_bytes'], 84)
        self.assertEqual(self.db.read_bytes(), original)

    def test_empty_orphan_only_absent_and_missing_names(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DELETE FROM execution_entity')
            conn.commit()
        for phase in ('orphans', 'empty', 'absent'):
            with closing(sqlite3.connect(self.db)) as conn:
                if phase == 'empty':
                    conn.execute('DELETE FROM execution_data')
                if phase == 'absent':
                    conn.execute('DROP TABLE workflow_entity')
                conn.commit()
            original = self.db.read_bytes()
            with self.observe_names() as names:
                report = core.analyze(self.db)
            self.assertEqual(names['ids'], [])
            self.assertEqual(report['workflows'], [])
            self.assertEqual(report['largest_executions'], [])
            self.assertEqual(report['totals']['orphan_payload_bytes'], 101 if phase == 'orphans' else 0)
            self.assertEqual(len(names['queries']), 0 if phase == 'absent' else 1)
            self.assertEqual(self.db.read_bytes(), original)
        self.create(self.sql)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TABLE workflow_entity')
        with self.observe_names() as names:
            report = core.analyze(self.db)
        self.assertEqual(names['ids'], [])
        self.assertTrue(all(w['workflow_name'] is None for w in report['workflows']))

    def test_raw_key_collation_and_type_matching(self):
        self.create(self.sql + """
            DROP TABLE workflow_entity;
            CREATE TABLE workflow_entity (id PRIMARY KEY COLLATE NOCASE, name TEXT);
            INSERT INTO workflow_entity VALUES ('WF-A','wrong case'), (42,'wrong type'),
                ('wf-b','订单'), ('42','string number');
            UPDATE execution_entity SET workflowId='42' WHERE id=10;
        """)
        with self.observe_names() as names:
            report = core.analyze(self.db)
        self.assertEqual(set(names['ids']), {'wf-b', '42'})
        self.assertEqual([(w['workflow_id'], w['workflow_name']) for w in report['workflows']],
                         [('wf-b', '订单'), ('wf-a', None), ('42', 'string number')])

    def test_set_query_without_workflow_parameters(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany('INSERT INTO workflow_entity VALUES (?,?)',
                             [(f'observed-{i}', f'name {i}') for i in range(1100)])
            conn.executemany('INSERT INTO execution_entity(workflowId,status) VALUES (?,?)',
                             [(f'observed-{i}', 'running') for i in range(1100)])
            conn.commit()
        with self.observe_names(variable_limit=1) as names:
            report = core.analyze(self.db, top=1)
        self.assertEqual(len(names['ids']), 1102)
        self.assertEqual(len(names['queries']), 1)
        self.assertEqual(len(report['workflows']), 1103)
        self.assertEqual(len(report['largest_executions']), 1)

    def test_force_wal_names_from_same_private_snapshot(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.executescript("""
                UPDATE workflow_entity SET name='WAL name' WHERE id='wf-a';
                INSERT INTO workflow_entity VALUES ('wal-only','WAL only'), ('wal-unused','unused');
                INSERT INTO execution_entity(workflowId,status) VALUES ('wal-only','running');
            """)
            paths = [self.db, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm')]
            original = [p.read_bytes() for p in paths]
            with self.observe_names() as names:
                report = core.analyze(self.db, force=True)
            self.assertEqual(set(names['ids']), {'wf-a', 'wf-b', 'wal-only'})
            self.assertEqual({w['workflow_id']: w['workflow_name'] for w in report['workflows']},
                             {'wf-a': 'WAL name', 'wf-b': '订单', 'wal-only': 'WAL only', 'missing-workflow': None})
            self.assertTrue(report['wal_snapshot'])
            self.assertEqual(report['totals']['executions'], 11)
            self.assertEqual([p.read_bytes() for p in paths], original)


if __name__ == '__main__':
    unittest.main()
