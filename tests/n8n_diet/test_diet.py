from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from flowwrench.n8n_diet.core import analyze, plan, reclaim
from flowwrench.n8n_diet.fixture import generate
from flowwrench.n8n_diet.storage import DietError, source_database, staged_output

ROOT = Path(__file__).resolve().parent
AS_OF = '2026-11-01T00:00:00Z'


@contextmanager
def database(*args, **kwargs):
    conn = sqlite3.connect(*args, **kwargs)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'flowwrench.n8n_diet', *map(str, args)],
                          cwd=ROOT, text=True, capture_output=True, timeout=30)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.sqlite'
        with database(self.db) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
        self.original_hash = digest(self.db)

    def assertUnchanged(self):
        self.assertEqual(digest(self.db), self.original_hash)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith('input.')), ['input.sqlite'])

    def test_known_analysis_utf8_status_orphans_and_missing(self):
        report = analyze(self.db, top=2)
        self.assertEqual(report['totals']['executions'], 10)
        self.assertEqual(report['totals']['orphan_data_rows'], 1)
        self.assertEqual(report['totals']['orphan_payload_bytes'], len('{}{"orphan":true}'.encode()))
        self.assertEqual(report['totals']['missing_data_rows'], 2)
        workflows = {w['workflow_id']: w for w in report['workflows']}
        self.assertEqual(workflows['wf-a']['executions'], 4)
        self.assertEqual(workflows['wf-a']['statuses'], {'error': 1, 'success': 2, 'waiting': 1})
        self.assertEqual(workflows['wf-b']['workflow_name'], '订单')
        self.assertIsNone(workflows['missing-workflow']['workflow_name'])
        expected_b = sum(len(v.encode()) + 2 for v in ['{"订单":"你好"}', '[1,2,3]', '{}', 'null', 'false'])
        self.assertEqual(workflows['wf-b']['payload_bytes'], expected_b)
        self.assertEqual([e['id'] for e in report['largest_executions']], [2, 3])
        with database(self.db) as conn:
            pages, free, size = [conn.execute('PRAGMA ' + key).fetchone()[0] for key in ['page_count', 'freelist_count', 'page_size']]
        self.assertEqual(report['storage']['freelist_bytes'], free * size)
        self.assertEqual(report['storage']['database_page_bytes'], pages * size)
        self.assertUnchanged()

    def test_dbstat_allocation_and_fallback(self):
        report = analyze(self.db)
        tables = report['table_storage']
        self.assertEqual(tables['method'], 'dbstat')
        with database(self.db) as conn:
            expected = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name IN ('execution_entity','execution_workflow')").fetchone()[0]
        self.assertEqual(next(t['allocated_bytes'] for t in tables['tables'] if t['table'] == 'execution_entity'), expected)
        fallback = analyze(self.db, use_dbstat=False)['table_storage']
        self.assertEqual(fallback['method'], 'value-bytes-estimate')
        self.assertTrue(all(t['allocated_bytes'] is None for t in fallback['tables']))
        with database(self.db) as conn:
            expected = conn.execute('SELECT sum(length(CAST(id AS BLOB))+length(CAST(name AS BLOB))) FROM workflow_entity').fetchone()[0]
        self.assertEqual(next(t['value_bytes_estimate'] for t in fallback['tables'] if t['table'] == 'workflow_entity'), expected)
        self.assertUnchanged()

    def test_plan_union_arithmetic(self):
        report = plan(self.db, max_age_days=20, max_count=2, as_of=AS_OF)
        # Eligible newest IDs: 4,3,9,8,2,1; age selects 1,2; count selects 9,8,2,1.
        self.assertEqual(report['eligible_executions'], 6)
        self.assertEqual(report['delete']['executions'], 4)
        self.assertEqual(report['delete']['data_rows'], 4)
        self.assertEqual(report['delete']['reasons'], {'count': 2, 'both': 2})
        self.assertEqual(report['executions_after'], 6)
        with database(self.db) as conn:
            expected = conn.execute('SELECT sum(length(CAST(data AS BLOB))+length(CAST(workflowData AS BLOB))) FROM execution_data WHERE executionId IN (1,2,8,9)').fetchone()[0]
        self.assertEqual(report['delete']['payload_bytes'], expected)
        self.assertTrue(report['dry_run'])
        self.assertUnchanged()

    def test_keep_failed_excluded_from_both_policies(self):
        report = plan(self.db, max_age_days=20, max_count=1, keep_failed=True, as_of=AS_OF)
        self.assertEqual(report['eligible_executions'], 3)
        self.assertEqual(report['protected']['keep_failed'], 3)
        self.assertEqual(report['delete']['executions'], 2)  # success 1,3
        self.assertEqual(report['delete']['reasons'], {'count': 1, 'both': 1})
        self.assertUnchanged()

    def test_zero_count_protects_active_unknown_and_missing_time(self):
        report = plan(self.db, max_count=0, as_of=AS_OF)
        self.assertEqual(report['delete']['executions'], 6)
        self.assertEqual(report['protected'], {'non_terminal_or_unknown_status': 3, 'missing_stopped_at': 1})

    def test_age_cutoff_is_strict_and_timezone_aware(self):
        report = plan(self.db, max_age_days=22, as_of='2026-11-01T01:00:01+01:00')
        self.assertEqual(report['delete']['executions'], 1)  # id 2 exactly at cutoff kept

    def test_future_timestamp_protected(self):
        with database(self.db) as conn:
            conn.execute("UPDATE execution_entity SET stoppedAt='2027-01-01T00:00:00Z' WHERE id=1")
        self.assertEqual(plan(self.db, max_count=0, as_of=AS_OF)['protected']['future_stopped_at'], 1)

    def test_bad_timestamp_and_policy_fail(self):
        for kwargs in [{}, {'max_count': -1}, {'max_age_days': 365001}, {'max_count': 1, 'as_of': '2026-11-01'}, {'max_count': 1, 'as_of': 'not-a-date'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(DietError):
                plan(self.db, **kwargs)
        with database(self.db) as conn:
            conn.execute("UPDATE execution_entity SET stoppedAt='bad' WHERE id=1")
        with self.assertRaisesRegex(DietError, 'stoppedAt'):
            plan(self.db, max_count=1)

    def test_missing_workflow_table_supported(self):
        with database(self.db) as conn:
            conn.execute('DROP TABLE workflow_entity')
        self.assertFalse(analyze(self.db)['schema']['workflow_names'])

    def test_cli_e2e_all_commands(self):
        for args in [('analyze', self.db, '--top', '3', '--json'), ('plan', self.db, '--max-count', '2', '--as-of', AS_OF, '--json'), ('reclaim', self.db, '--out', self.root / 'compact.sqlite', '--json')]:
            result = cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertUnchanged()

    def test_no_payload_leak_in_reports(self):
        with database(self.db) as conn:
            conn.execute('UPDATE execution_data SET data=? WHERE executionId=1', ('{"password":"secret-marker"}',))
        for args in [('analyze', self.db), ('analyze', self.db, '--json'), ('plan', self.db, '--max-count', 0, '--json')]:
            result = cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('secret-marker', result.stdout + result.stderr)

    def test_output_refusal_overwrite_and_aliases(self):
        target = self.root / 'out.sqlite'
        target.write_bytes(b'preserve-me')
        with self.assertRaisesRegex(DietError, 'output exists'):
            reclaim(self.db, out=target)
        self.assertEqual(target.read_bytes(), b'preserve-me')
        reclaim(self.db, out=target, overwrite=True)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        alias = self.root / 'hardlink.sqlite'
        os.link(self.db, alias)
        symlink = self.root / 'symlink.sqlite'
        symlink.symlink_to(target)
        for path in (self.db, alias, symlink, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm'), Path(str(self.db) + '-journal'), self.root):
            with self.subTest(path=path), self.assertRaises(DietError):
                reclaim(self.db, out=path, overwrite=True)
        self.assertEqual(digest(self.db), self.original_hash)

    def test_locked_output_refused_without_replacement(self):
        target = self.root / 'busy.sqlite'
        reclaim(self.db, out=target)
        before = digest(target)
        with database(target, timeout=0) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM execution_entity').fetchone()
            result = cli('reclaim', self.db, '--out', target, '--overwrite')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('output database is locked', result.stderr)
            reader.rollback()
        self.assertEqual(digest(target), before)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
        self.assertUnchanged()

    def test_output_sidecar_refused_and_atomic_cleanup(self):
        target = self.root / 'out.sqlite'
        target.write_bytes(b'old')
        Path(str(target) + '-wal').write_bytes(b'')
        with self.assertRaisesRegex(DietError, 'sidecars'):
            reclaim(self.db, out=target, overwrite=True)
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with staged_output(self.root / 'new.sqlite') as staging:
                staging.write_bytes(b'partial')
                raise RuntimeError('injected')
        self.assertFalse((self.root / 'new.sqlite').exists())
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_noclobber_race(self):
        target = self.root / 'race.sqlite'
        with self.assertRaisesRegex(DietError, 'output exists'):
            with staged_output(target) as staging:
                staging.write_bytes(b'new')
                target.write_bytes(b'created meanwhile')
        self.assertEqual(target.read_bytes(), b'created meanwhile')

    def test_sql_looking_filename_bound_safely(self):
        target = self.root / "out'); DROP TABLE execution_entity;--.sqlite"
        result = reclaim(self.db, out=target)
        self.assertTrue(target.is_file())
        self.assertEqual(result['row_counts']['execution_entity'], 10)
        self.assertUnchanged()

    def test_malicious_or_unusual_metadata_not_executed(self):
        marker = self.root / 'must-not-exist'
        with database(self.db) as conn:
            conn.execute('CREATE TABLE "odd""name" (value TEXT)')
            conn.execute('INSERT INTO "odd""name" VALUES (?)', (f'$(touch {marker})',))
            conn.execute('CREATE VIEW malicious AS SELECT load_extension(\'/no/such/extension\')')
            conn.execute('UPDATE workflow_entity SET name=? WHERE id=?', ('\x1b[2J$(touch bad)', 'wf-a'))
        result = cli('analyze', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('\x1b', result.stdout)
        reclaim(self.db, out=self.root / 'copy.sqlite')
        self.assertFalse(marker.exists())

    def test_unsupported_schemas(self):
        for script, expected in [
            ('CREATE TABLE unrelated(x)', 'execution_data, execution_entity'),
            ('CREATE TABLE execution_entity(id PRIMARY KEY); CREATE TABLE execution_data(executionId PRIMARY KEY)', 'missing columns'),
            ('CREATE TABLE execution_entity(id,workflowId,status,startedAt,stoppedAt); CREATE TABLE execution_data(executionId PRIMARY KEY,data,workflowData)', 'primary key'),
        ]:
            db = self.root / 'bad.sqlite'
            db.unlink(missing_ok=True)
            with database(db) as conn:
                conn.executescript(script)
            result = cli('analyze', db, '--json')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(expected, result.stderr)

    def test_source_lock_blocks_new_rollback_writer(self):
        with source_database(self.db):
            result = subprocess.run([sys.executable, '-c',
                'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                str(self.db)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('locked', result.stderr)
        self.assertUnchanged()

    def test_source_lock_blocks_new_wal_writer(self):
        with database(self.db) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE workflow_entity SET name='wal' WHERE id='wf-a'")
            writer.commit()
            with source_database(self.db, force=True):
                result = subprocess.run([sys.executable, '-c',
                    'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                    str(self.db)], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('locked', result.stderr)

    def test_integrity_failure_preserves_source_and_publishes_nothing(self):
        with database(self.db) as conn:
            # Deliberately make an index definition inconsistent with its pages.
            # Some SQLite versions skip CHECK expressions in read-only integrity
            # checks, so this uses a genuine index/data mismatch instead.
            conn.executescript('CREATE TABLE damaged(first INTEGER,other INTEGER); '
                               'INSERT INTO damaged VALUES(1,2); '
                               'CREATE INDEX damaged_index ON damaged(first); '
                               'PRAGMA writable_schema=ON;')
            conn.execute("UPDATE sqlite_schema SET sql='CREATE INDEX damaged_index ON damaged(other)' WHERE name='damaged_index'")
        before = digest(self.db)
        target = self.root / 'failed.sqlite'
        with self.assertRaisesRegex(DietError, 'source integrity_check failed'):
            reclaim(self.db, out=target)
        self.assertFalse(target.exists())
        self.assertEqual(digest(self.db), before)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_readonly_source_and_fifo_refusal(self):
        self.db.chmod(0o400)
        self.assertEqual(analyze(self.db)['totals']['executions'], 10)
        reclaim(self.db, out=self.root / 'readable-copy.sqlite')
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        result = cli('analyze', fifo)
        self.assertEqual(result.returncode, 2)
        self.assertIn('regular', result.stderr)
        self.assertUnchanged()

    def test_dbstat_shadow_and_orphan_output_sidecars_refused(self):
        with database(self.db) as conn:
            conn.execute('CREATE VIEW dbstat AS SELECT 123 AS pgsize')
        with self.assertRaisesRegex(DietError, 'shadows'):
            analyze(self.db)
        # Explicitly requested fallback never queries the fake view.
        self.assertEqual(analyze(self.db, use_dbstat=False)['table_storage']['method'], 'value-bytes-estimate')
        target = self.root / 'new.sqlite'
        Path(str(target) + '-wal').write_bytes(b'stale')
        with self.assertRaisesRegex(DietError, 'sidecars'):
            reclaim(self.db, out=target)
        self.assertFalse(target.exists())

    def test_virtual_table_refused(self):
        with database(self.db) as conn:
            conn.execute('CREATE VIRTUAL TABLE extra USING fts5(content)')
        with self.assertRaisesRegex(DietError, 'virtual tables'):
            analyze(self.db)

    def test_invalid_file_missing_path_and_bad_options(self):
        bad = self.root / 'not.sqlite'
        bad.write_text('no database')
        for args in [('analyze', bad), ('analyze', self.root / 'missing'), ('analyze', self.db, '--top', '0'), ('plan', self.db), ('fixture', 'gen', '--out', self.root / 'fixture', '--deleted-percent', 101)]:
            with self.subTest(args=args):
                result = cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
        link = self.root / 'source-link.sqlite'
        link.symlink_to(self.db)
        with self.assertRaisesRegex(DietError, 'symlink'):
            analyze(link)

    def test_nonempty_rollback_journal_refused_even_force(self):
        Path(str(self.db) + '-journal').write_bytes(b'unrecovered')
        for force in (False, True):
            with self.assertRaisesRegex(DietError, 'rollback journal'):
                analyze(self.db, force=force)

    def test_locked_database_refused_even_force(self):
        # A separate process is essential: POSIX locks are per process.
        with database(self.db, timeout=0) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            for flag in ((), ('--force',)):
                result = cli('analyze', self.db, *flag)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('locked', result.stderr)
            writer.rollback()
        self.assertUnchanged()

    def test_reserved_writer_lock_refused(self):
        with database(self.db, timeout=0) as writer:
            writer.execute('BEGIN IMMEDIATE')
            result = cli('reclaim', self.db, '--out', self.root / 'blocked.sqlite')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('locked', result.stderr)
            self.assertFalse((self.root / 'blocked.sqlite').exists())
            writer.rollback()

    def test_wal_refusal_private_snapshot_and_source_unchanged(self):
        with database(self.db, timeout=0) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO execution_entity(id,workflowId,startedAt,stoppedAt,status) VALUES (100,'wf-a',NULL,NULL,'running')")
            writer.commit()
            paths = [self.db, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm')]
            before = {str(p): digest(p) for p in paths}
            default = cli('analyze', self.db)
            self.assertEqual(default.returncode, 2)
            self.assertIn('non-empty WAL', default.stderr)
            result = cli('analyze', self.db, '--force', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['wal_snapshot'])
            self.assertEqual(report['totals']['executions'], 11)
            out = self.root / 'wal-copy.sqlite'
            result = cli('reclaim', self.db, '--out', out, '--force', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['row_counts']['execution_entity'], 11)
            self.assertEqual(before, {str(p): digest(p) for p in paths})

    def test_active_wal_writer_refused_even_force(self):
        with database(self.db, timeout=0) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('BEGIN IMMEDIATE')
            for command in ('analyze', 'plan', 'reclaim'):
                extra = ['--max-count', '0'] if command == 'plan' else ['--out', str(self.root / 'out')] if command == 'reclaim' else []
                result = cli(command, self.db, '--force', *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('locked', result.stderr)
            writer.rollback()

    def test_wal_without_shm_fails_closed(self):
        # A separately copied WAL backup has no shared-memory lock file.
        with database(self.db) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE workflow_entity SET name='changed' WHERE id='wf-a'")
            writer.commit()
            copy = self.root / 'offline.sqlite'
            copy.write_bytes(self.db.read_bytes())
            Path(str(copy) + '-wal').write_bytes(Path(str(self.db) + '-wal').read_bytes())
            result = cli('analyze', copy, '--force')
            self.assertEqual(result.returncode, 2)
            self.assertIn('SHM', result.stderr)


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_fixture_determinism_and_seed_difference(self):
        files = [self.root / (str(i) + '.sqlite') for i in range(3)]
        for i, path in enumerate(files):
            generate(path, seed=42 if i < 2 else 43, executions=50)
        self.assertEqual(digest(files[0]), digest(files[1]))
        self.assertNotEqual(digest(files[0]), digest(files[2]))
        report = analyze(files[0])
        self.assertEqual(report['totals']['executions'], 20)
        self.assertEqual(report['totals']['orphan_data_rows'], 0)
        self.assertGreater(report['storage']['freelist_count'], 0)

    def test_default_fixture_reclaim_real_comparison(self):
        source = self.root / 'demo.sqlite'
        fixture = generate(source, seed=42)
        original_hash = digest(source)
        self.assertEqual(fixture['remaining_executions'], 800)
        before = analyze(source)
        self.assertEqual(len(before['workflows']), 5)
        out = self.root / 'compact.sqlite'
        report = reclaim(source, out=out)
        self.assertEqual(report['row_counts']['execution_entity'], 800)
        self.assertEqual(report['row_counts']['execution_data'], 800)
        self.assertEqual(report['integrity_check'], 'ok')
        self.assertGreaterEqual(report['saved_bytes'], before['storage']['freelist_bytes'])
        self.assertGreater(report['saved_bytes'], 0)
        self.assertEqual(digest(source), original_hash)
        self.assertEqual(analyze(source)['workflows'], analyze(out)['workflows'])
        # Compare actual content as well as row counts, including stable IDs.
        with database(source) as a, database(out) as b:
            for table in ('workflow_entity', 'execution_entity', 'execution_data'):
                self.assertEqual(a.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall(), b.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall())

    def test_empty_fixture_and_all_deleted(self):
        for count in (0, 10):
            path = self.root / f'empty-{count}.sqlite'
            generate(path, executions=count, deleted_percent=100)
            report = analyze(path)
            self.assertEqual(report['totals']['executions'], 0)
            self.assertEqual(plan(path, max_count=0)['delete']['executions'], 0)
            reclaim(path, out=self.root / f'compact-{count}.sqlite')

    def test_generator_cli_e2e_and_overwrite(self):
        path = self.root / 'cli.sqlite'
        result = cli('fixture', 'gen', '--out', path, '--seed', 42, '--executions', 25, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['remaining_executions'], 10)
        self.assertEqual(cli('fixture', 'gen', '--out', path).returncode, 2)
        result = cli('fixture', 'gen', '--out', path, '--executions', 5, '--overwrite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(analyze(path)['totals']['executions'], 2)


if __name__ == '__main__':
    unittest.main()
