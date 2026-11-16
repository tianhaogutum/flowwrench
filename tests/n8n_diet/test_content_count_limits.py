"""A content pass counts every ordinary row before selecting any payload."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import artifacts, cli as cli_module, core
from flowwrench.n8n_diet.storage import DietError, connect
from .test_content_spilling import legacy_content
from .test_content_verification import ROOT, cli


class ContentCountLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.plan, self.out = (self.root / n for n in ('input.sqlite', 'plan.json', 'copy.sqlite'))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.executescript('''CREATE TABLE "z_tail"(a,b,v AS (a) VIRTUAL,s AS (b) STORED);
                INSERT INTO z_tail(a,b) VALUES (NULL,NULL),(NULL,NULL),(-1,1e300),
                  ('é',x'ff0080'),('é',x'ff0080');
                CREATE TABLE empty_table(a);
                CREATE VIEW repeated AS SELECT * FROM z_tail UNION ALL SELECT * FROM z_tail;
                CREATE INDEX aux_index ON z_tail(a);''')
            conn.commit()
        # Fixture: 10 executions + 9 data + 2 workflows + 1 sqlite_sequence.
        self.total = 27  # plus five auxiliary rows, empty table contributes zero
        self.original = self.db.read_bytes()

    def test_validation_precedes_io(self):
        operations = [lambda n: core.reclaim_plan('missing', out='missing', max_content_rows=n),
                      lambda n: core.reclaim('missing', out='missing', plan_file='missing', max_content_rows=n),
                      lambda n: core.verify_copy('missing', plan_file='missing', max_content_rows=n),
                      lambda n: artifacts.content_digests(None, max_content_rows=n)]
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('source IO')), \
                patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('plan IO')):
            for operation in operations:
                for limit in [-1, True, False, 1.0, '3', [], {}]:
                    with self.subTest(operation=operation, limit=limit):
                        with self.assertRaisesRegex(DietError, 'max-content-rows must be a non-negative integer'):
                            operation(limit)
        with self.assertRaisesRegex(DietError, 'row.hash.*at least 32'):
            artifacts.content_digests(None, row_hash_memory_bytes=0, max_content_rows=-1)
        with self.assertRaisesRegex(DietError, 'max-content-value-bytes'):
            artifacts.content_digests(None, max_content_value_bytes=-1, max_content_rows=-1)
        with self.assertRaisesRegex(DietError, 'max-content-row-bytes'):
            artifacts.content_digests(None, max_content_row_bytes=-1, max_content_rows=-1)

    def test_all_tables_preflight_before_any_payload_or_byte_check(self):
        with closing(connect(self.db, immutable=True)) as conn:
            reference = legacy_content(conn)
            for limit in [None, self.total, self.total+1, 2**53+1, 2**100]:
                self.assertEqual(artifacts.content_digests(conn, max_content_rows=limit,
                                 row_hash_memory_bytes=32), reference)
            sqls = []
            conn.set_trace_callback(sqls.append)
            with patch('flowwrench.n8n_diet.artifacts._check_content_values', side_effect=AssertionError('value check')), \
                    patch('flowwrench.n8n_diet.artifacts._check_content_rows', side_effect=AssertionError('row check')), \
                    patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=AssertionError('spill')):
                with self.assertRaisesRegex(DietError, 'content row count exceeds --max-content-rows'):
                    artifacts.content_digests(conn, max_content_rows=self.total-1,
                                             max_content_value_bytes=0, max_content_row_bytes=0)
            self.assertFalse(any(sql.startswith('SELECT *') for sql in sqls), sqls)
            self.assertIs(conn.text_factory, str)

    def test_zero_empty_and_null_numeric_duplicate_rows(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('CREATE TABLE a(x); CREATE TABLE b(y,v AS (y));')
            self.assertEqual(artifacts.content_digests(conn, max_content_rows=0), legacy_content(conn))
            conn.executescript('INSERT INTO a VALUES(NULL),(NULL); INSERT INTO b(y) VALUES (1),(1);')
            self.assertEqual(artifacts.content_digests(conn, max_content_rows=4,
                             max_content_value_bytes=0, max_content_row_bytes=0), legacy_content(conn))
            for limit in [0, 2, 3]:  # each table fits 2; aggregate does not
                with self.assertRaisesRegex(DietError, 'content row count exceeds'):
                    artifacts.content_digests(conn, max_content_rows=limit)

    def test_count_sum_is_exact_above_sqlite_integer_range(self):
        class Cursor:
            def fetchone(self):
                return (2**63-1,)
            def close(self):
                pass
        class Connection:
            def execute(self, sql):
                self_sqls.append(sql)
                return Cursor()
        self_sqls = []
        # Synthetic count results test arithmetic, not an infeasible giant DB.
        with patch('flowwrench.n8n_diet.artifacts.ordinary_tables', return_value=['a', 'b']), \
                patch('flowwrench.n8n_diet.artifacts._table_content', return_value='unused'):
            with self.assertRaisesRegex(DietError, 'content row count exceeds'):
                artifacts.content_digests(Connection(), max_content_rows=2**64-3)
        self.assertEqual(self_sqls, ['SELECT count(*) FROM "a"', 'SELECT count(*) FROM "b"'])

    def test_content_routes_parity_and_refusal_preserve_outputs(self):
        default = core.reclaim_plan(self.db, out=self.plan, content=True)
        guarded = core.reclaim_plan(self.db, out=self.root / 'allowed.json', content=True,
                                   max_content_rows=self.total, max_content_value_bytes=64,
                                   max_content_row_bytes=256, row_hash_memory_bytes=32)
        self.assertEqual(default, guarded)
        self.assertEqual(self.plan.read_bytes(), (self.root / 'allowed.json').read_bytes())
        plain = core.reclaim(self.db, out=self.out, plan_file=self.plan)
        self.assertEqual(plain, core.reclaim(self.db, out=self.out, overwrite=True, plan_file=self.plan,
                                           max_content_rows=self.total, row_hash_memory_bytes=32))
        verify = core.verify_copy(self.out, plan_file=self.plan)
        self.assertEqual(verify, core.verify_copy(self.out, plan_file=self.plan,
                                                max_content_rows=self.total, row_hash_memory_bytes=32))
        before = self.plan.read_bytes(), self.out.read_bytes()
        for operation in [lambda: core.reclaim_plan(self.db, out=self.root / 'refused.json', content=True,
                                                    max_content_rows=self.total-1),
                          lambda: core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True,
                                               max_content_rows=self.total-1),
                          lambda: core.verify_copy(self.out, plan_file=self.plan, max_content_rows=self.total-1)]:
            with self.assertRaisesRegex(DietError, 'content row count exceeds'):
                operation()
        self.assertFalse((self.root / 'refused.json').exists())
        self.assertEqual(before, (self.plan.read_bytes(), self.out.read_bytes()))
        self.assertEqual(self.original, self.db.read_bytes())

    def test_structural_only_does_not_preflight_and_default_skips_count(self):
        with patch('flowwrench.n8n_diet.artifacts._check_content_row_count', side_effect=AssertionError('count preflight')):
            core.reclaim_plan(self.db, out=self.root / 'default-content.json', content=True)
            saved = core.reclaim_plan(self.db, out=self.plan, max_content_rows=0)
            self.assertEqual(saved['version'], 1)
            self.assertNotIn('content', saved)
            self.assertFalse(core.reclaim(self.db, out=self.out, plan_file=self.plan,
                                         max_content_rows=0)['content_verified'])
            self.assertFalse(core.verify_copy(self.out, plan_file=self.plan, max_content_rows=0)['content_verified'])
            with self.assertRaisesRegex(DietError, 'plan has no content digests'):
                core.verify_copy(self.out, plan_file=self.plan, content=True, max_content_rows=0)

    def test_cli_invalid_and_count_refusals_are_identity_free(self):
        for command in ['reclaim-plan', 'reclaim', 'verify-copy']:
            flags = ['--plan', self.plan] if command == 'verify-copy' else ['--out', self.out]
            for limit in ['-1', 'SECRET-LIMIT', '1.5']:
                result = cli(command, self.root / 'missing', *flags, '--max-content-rows', limit, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('max-content-rows must be a non-negative integer', result.stderr)
                self.assertNotIn('SECRET-LIMIT', result.stderr)
        result = cli('reclaim-plan', self.db, '--out', self.plan, '--content',
                     '--max-content-rows', self.total-1, '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'flowwrench n8n-diet: content row count exceeds --max-content-rows; no output published\n')
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.out.exists())

    def test_preflight_refusal_error_interrupt_preserve_locks_and_cleanup(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        core.reclaim(self.db, out=self.out, plan_file=self.plan)
        before = self.plan.read_bytes(), self.out.read_bytes()
        real_check = artifacts._check_content_row_count
        for stage in ['reclaim-plan', 'reclaim', 'staged', 'verify-copy']:
            for error, code in [(None, 2), (sqlite3.OperationalError('injected count IO'), 2), (KeyboardInterrupt(), 130)]:
                checked, sqls = [], []
                target = self.out if stage == 'verify-copy' else self.db

                def check(conn, tables, limit):
                    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
                    if stage == 'staged' and path == self.db:
                        return real_check(conn, tables, limit)
                    checked.append(True)
                    conn.set_trace_callback(sqls.append)
                    attempt = subprocess.run([sys.executable, '-c',
                        'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                        str(target)], capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(attempt.returncode, 0)
                    self.assertIn('locked', attempt.stderr)
                    if error is not None:
                        raise error
                    return real_check(conn, tables, 0)

                command = 'reclaim' if stage == 'staged' else stage
                args = [command, str(target), '--max-content-rows', str(self.total), '--row-hash-memory-bytes', '32', '--json']
                if stage == 'reclaim-plan':
                    args += ['--out', str(self.root / 'new.json'), '--content']
                else:
                    args += ['--plan', str(self.plan)]
                    if command == 'reclaim':
                        args += ['--out', str(self.out), '--overwrite']
                stdout, stderr = io.StringIO(), io.StringIO()
                with self.subTest(stage=stage, error=error), \
                        patch('flowwrench.n8n_diet.artifacts._check_content_row_count', side_effect=check), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(cli_module.main(args), code)
                self.assertTrue(checked)
                self.assertFalse(any(s.startswith('SELECT *') for s in sqls))
                self.assertEqual(stdout.getvalue(), '')
                self.assertNotIn('z_tail', stderr.getvalue())
                self.assertFalse((self.root / 'new.json').exists())
                self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
                self.assertEqual(before, (self.plan.read_bytes(), self.out.read_bytes()))
                self.assertEqual(self.original, self.db.read_bytes())

    def test_wal_snapshot_count_and_safety_precedence(self):
        writer = subprocess.Popen([sys.executable, '-c', '''
import sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0'); c.execute('INSERT INTO z_tail(a,b) VALUES(NULL,1)'); c.commit()
print('ready',flush=True); sys.stdin.readline(); c.close()
''', str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), 'ready')
            paths = [self.db, Path(str(self.db)+'-wal'), Path(str(self.db)+'-shm')]
            before = [p.read_bytes() for p in paths]
            args = ['reclaim-plan', self.db, '--out', self.plan, '--content', '--max-content-rows', self.total, '--json']
            refused = cli(*args)
            self.assertEqual(refused.returncode, 2)
            self.assertIn('non-empty WAL', refused.stderr)
            refused = cli(*args, '--force')
            self.assertEqual(refused.returncode, 2)
            self.assertIn('content row count exceeds', refused.stderr)
            self.assertEqual(refused.stdout, '')
            allowed = cli('reclaim-plan', self.db, '--out', self.plan, '--content', '--force',
                          '--max-content-rows', self.total+1, '--row-hash-memory-bytes', 32, '--json')
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(before, [p.read_bytes() for p in paths])
            self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
        finally:
            _, stderr = writer.communicate('stop\n', timeout=5)
            self.assertEqual(writer.returncode, 0, stderr)
        # Journal safety remains before the content pass.
        journal = Path(str(self.db)+'-journal')
        journal.write_bytes(b'unrecovered')
        result = cli('reclaim-plan', self.db, '--out', self.root/'journal.json', '--content', '--max-content-rows', 0, '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('non-empty rollback journal', result.stderr)
        self.assertEqual(journal.read_bytes(), b'unrecovered')


if __name__ == '__main__':
    unittest.main()
