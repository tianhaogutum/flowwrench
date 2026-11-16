"""Whole-pass stored-byte policy must precede every Python payload selection."""
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


def utf8_stored_total(conn):
    """Tiny UTF-8 fixtures only: independent Python payload oracle, not preflight."""
    previous = conn.text_factory
    tables = artifacts.ordinary_tables(conn)
    conn.text_factory = bytes
    try:
        return sum(len(value) for table in tables
                   for row in conn.execute('SELECT * FROM ' + artifacts.quote(table))
                   for value in row if isinstance(value, bytes))
    finally:
        conn.text_factory = previous


class ContentByteLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.plan, self.out = (self.root / n for n in ('input.sqlite', 'plan.json', 'copy.sqlite'))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.executescript('''CREATE TABLE "z_tail"("odd""column",b,v AS ("odd""column") VIRTUAL,s AS (b) STORED);
                INSERT INTO z_tail("odd""column",b) VALUES (NULL,NULL),(-1,1e300),('',x''),
                    ('é',x'ff0080'),('é',x'ff0080'),(CAST(x'6100ff80' AS TEXT),x'00ff');
                CREATE TABLE empty_table(a);
                CREATE VIEW repeated AS SELECT * FROM z_tail UNION ALL SELECT * FROM z_tail;
                CREATE INDEX aux_index ON z_tail("odd""column");''')
            conn.commit()
        self.original = self.db.read_bytes()
        with closing(connect(self.db, immutable=True)) as conn:
            self.total = utf8_stored_total(conn)

    def test_validation_precedes_io_and_keeps_existing_priority(self):
        operations = [lambda n: core.reclaim_plan('missing', out='missing', max_content_bytes=n),
                      lambda n: core.reclaim('missing', out='missing', plan_file='missing', max_content_bytes=n),
                      lambda n: core.verify_copy('missing', plan_file='missing', max_content_bytes=n),
                      lambda n: artifacts.content_digests(None, max_content_bytes=n)]
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('source IO')), \
                patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('plan IO')):
            for operation in operations:
                for limit in [-1, True, False, 1.0, '3', [], {}]:
                    with self.subTest(operation=operation, limit=limit):
                        with self.assertRaisesRegex(DietError, 'max-content-bytes must be a non-negative integer'):
                            operation(limit)
        for kwargs, diagnostic in [({'row_hash_memory_bytes':0}, 'row.hash'),
                                   ({'max_content_value_bytes':-1}, 'max-content-value-bytes'),
                                   ({'max_content_row_bytes':-1}, 'max-content-row-bytes'),
                                   ({'max_content_rows':-1}, 'max-content-rows')]:
            with self.assertRaisesRegex(DietError, diagnostic):
                artifacts.content_digests(None, max_content_bytes=-1, **kwargs)

    def test_all_tables_preflight_before_any_payload_or_per_table_guard(self):
        with closing(connect(self.db, immutable=True)) as conn:
            reference = legacy_content(conn)
            for limit in [None, self.total, self.total+1, 2**53+1, 2**100]:
                self.assertEqual(artifacts.content_digests(conn, max_content_bytes=limit,
                                                         row_hash_memory_bytes=32), reference)
            sqls = []
            conn.set_trace_callback(sqls.append)
            with patch('flowwrench.n8n_diet.artifacts._check_content_values', side_effect=AssertionError('value check')), \
                    patch('flowwrench.n8n_diet.artifacts._check_content_rows', side_effect=AssertionError('row check')), \
                    patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=AssertionError('spill')):
                with self.assertRaisesRegex(DietError, 'content bytes exceed --max-content-bytes'):
                    artifacts.content_digests(conn, max_content_bytes=self.total-1,
                                             max_content_value_bytes=0, max_content_row_bytes=0)
            self.assertFalse(any(sql.startswith('SELECT *') for sql in sqls), sqls)
            self.assertIs(conn.text_factory, str)
            # Count policy still precedes byte scans when both would refuse.
            with patch('flowwrench.n8n_diet.artifacts._check_content_bytes', side_effect=AssertionError('byte check')):
                with self.assertRaisesRegex(DietError, 'content row count exceeds'):
                    artifacts.content_digests(conn, max_content_rows=0, max_content_bytes=0)

    def test_zero_duplicate_generated_invalid_utf8_and_cross_table_boundaries(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('''CREATE TABLE a(x,y,v AS (x),s AS (y) STORED);
                CREATE TABLE b(x); CREATE VIEW repeated AS SELECT * FROM a UNION ALL SELECT * FROM a;
                CREATE INDEX ignored ON a(x);
                INSERT INTO a(x,y) VALUES (NULL,1),(-1e300,1e999),('',x'');''')
            self.assertEqual(artifacts.content_digests(conn, max_content_bytes=0), legacy_content(conn))
            conn.executescript('''INSERT INTO a(x,y) VALUES ('é',x'ff0080'),('é',x'ff0080'),
                (CAST(x'6100ff80' AS TEXT),x'00ff'); INSERT INTO b VALUES('a'),('b');''')
            # a: 10 + 10 + 12; b: 1 + 1. Views/indexes/rowids add nothing.
            for limit in [34, 35]:
                self.assertEqual(artifacts.content_digests(conn, max_content_bytes=limit,
                                 max_content_value_bytes=4, max_content_row_bytes=12,
                                 max_content_rows=8, row_hash_memory_bytes=32), legacy_content(conn))
            for limit in [0, 20, 32, 33]:
                with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                    artifacts.content_digests(conn, max_content_bytes=limit)

    def test_utf16_budget_counts_stored_bytes_not_python_utf8(self):
        for encoding in ['UTF-16le', 'UTF-16be']:
            with self.subTest(encoding=encoding), closing(sqlite3.connect(':memory:')) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA encoding='" + encoding + "'")
                conn.executescript('''CREATE TABLE bag(a,b,v AS (a));
                    INSERT INTO bag(a,b) VALUES (CAST(x'610062006300' AS TEXT),x'00ff');'''
                    if encoding == 'UTF-16le' else '''CREATE TABLE bag(a,b,v AS (a));
                    INSERT INTO bag(a,b) VALUES (CAST(x'006100620063' AS TEXT),x'00ff');''')
                # 6 bytes stored per TEXT, 3 UTF-8 bytes delivered to Python.
                reference = legacy_content(conn)
                self.assertEqual(artifacts.content_digests(conn, max_content_bytes=14), reference)
                with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                    artifacts.content_digests(conn, max_content_bytes=13)

    def test_wide_table_and_exact_sum_above_sqlite_integer_range(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE bag(' + ','.join('c'+str(i) for i in range(1100)) + ')')
            conn.execute('INSERT INTO bag VALUES (' + ','.join("'x'" for _ in range(1100)) + ')')
            if hasattr(conn, 'setlimit'):
                conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
            self.assertEqual(artifacts.content_digests(conn, max_content_bytes=1100), legacy_content(conn))
            with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                artifacts.content_digests(conn, max_content_bytes=1099)
        # Synthetic integer row sizes test exact accumulation without allocating
        # petabytes. Each integer is below the <2^46 hard per-row bound; total
        # crosses both 2^53 and 2^63 across rows and tables. No benchmark claim.
        row_bytes, rows = 2**45-1, 262145
        class Cursor:
            def __iter__(self):
                return iter([(row_bytes,)] * rows)
            def close(self):
                pass
        class Connection:
            def execute(self, sql):
                self_sqls.append(sql)
                return Cursor()
        self_sqls = []
        with patch('flowwrench.n8n_diet.artifacts._content_row_bytes_sql', return_value='synthetic_row_size'):
            total = row_bytes * rows * 2
            artifacts._check_content_bytes(Connection(), ['a', 'b'], total)
            with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                artifacts._check_content_bytes(Connection(), ['a', 'b'], total-1)
        self.assertEqual(self_sqls, ['SELECT synthetic_row_size FROM "a"', 'SELECT synthetic_row_size FROM "b"']*2)

    def test_content_routes_parity_and_refusal_preserve_outputs(self):
        default = core.reclaim_plan(self.db, out=self.plan, content=True)
        guarded = core.reclaim_plan(self.db, out=self.root/'allowed.json', content=True,
                                   max_content_bytes=self.total, max_content_rows=28,
                                   max_content_value_bytes=64, max_content_row_bytes=256, row_hash_memory_bytes=32)
        self.assertEqual(default, guarded)
        self.assertEqual(self.plan.read_bytes(), (self.root/'allowed.json').read_bytes())
        plain = core.reclaim(self.db, out=self.out, plan_file=self.plan)
        self.assertEqual(plain, core.reclaim(self.db, out=self.out, overwrite=True, plan_file=self.plan,
                                           max_content_bytes=self.total, row_hash_memory_bytes=32))
        verify = core.verify_copy(self.out, plan_file=self.plan)
        self.assertEqual(verify, core.verify_copy(self.out, plan_file=self.plan,
                                                max_content_bytes=self.total, row_hash_memory_bytes=32))
        before = self.plan.read_bytes(), self.out.read_bytes()
        for operation in [lambda: core.reclaim_plan(self.db, out=self.root/'refused.json', content=True,
                                                    max_content_bytes=self.total-1),
                          lambda: core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True,
                                               max_content_bytes=self.total-1),
                          lambda: core.verify_copy(self.out, plan_file=self.plan, max_content_bytes=self.total-1)]:
            with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                operation()
        self.assertFalse((self.root/'refused.json').exists())
        self.assertEqual(before, (self.plan.read_bytes(), self.out.read_bytes()))
        self.assertEqual(self.original, self.db.read_bytes())

    def test_staged_copy_rechecks_actual_bytes_before_any_payload(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        before_plan = self.plan.read_bytes()
        self.out.write_bytes(b'existing output')
        real_verify = core.verify_structure
        sqls = []
        def grow_staged(conn, **kwargs):
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            with closing(sqlite3.connect(path)) as writer:
                # Same schema/counts; one NULL b becomes 2 BLOB bytes and its
                # stored generated copy contributes 2 more. Source still fits.
                writer.execute("UPDATE z_tail SET b=x'7878' WHERE rowid=1")
                writer.commit()
            conn.set_trace_callback(sqls.append)
            return real_verify(conn, **kwargs)
        with patch('flowwrench.n8n_diet.core.verify_structure', side_effect=grow_staged):
            with self.assertRaisesRegex(DietError, 'content bytes exceed'):
                core.reclaim(self.db, out=self.out, overwrite=True, plan_file=self.plan,
                             max_content_bytes=self.total, row_hash_memory_bytes=32)
        self.assertTrue(sqls)
        self.assertFalse(any(sql.startswith('SELECT *') for sql in sqls))
        self.assertEqual(self.out.read_bytes(), b'existing output')
        self.assertEqual(self.plan.read_bytes(), before_plan)
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_structural_only_validates_without_scan_and_default_skips_preflight(self):
        with patch('flowwrench.n8n_diet.artifacts._check_content_bytes', side_effect=AssertionError('byte preflight')):
            core.reclaim_plan(self.db, out=self.root/'default-content.json', content=True)
            saved = core.reclaim_plan(self.db, out=self.plan, max_content_bytes=0)
            self.assertEqual(saved['version'], 1)
            self.assertNotIn('content', saved)
            self.assertFalse(core.reclaim(self.db, out=self.out, plan_file=self.plan,
                                         max_content_bytes=0)['content_verified'])
            self.assertFalse(core.verify_copy(self.out, plan_file=self.plan, max_content_bytes=0)['content_verified'])
            with self.assertRaisesRegex(DietError, 'plan has no content digests'):
                core.verify_copy(self.out, plan_file=self.plan, content=True, max_content_bytes=0)

    def test_cli_invalid_and_byte_refusals_are_identity_free(self):
        for command in ['reclaim-plan', 'reclaim', 'verify-copy']:
            flags = ['--plan', self.plan] if command == 'verify-copy' else ['--out', self.out]
            for limit in ['-1', 'SECRET-LIMIT', '1.5']:
                result = cli(command, self.root/'missing', *flags, '--max-content-bytes', limit, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('max-content-bytes must be a non-negative integer', result.stderr)
                self.assertNotIn('SECRET-LIMIT', result.stderr)
        result = cli('reclaim-plan', self.db, '--out', self.plan, '--content',
                     '--max-content-bytes', self.total-1, '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'flowwrench n8n-diet: content bytes exceed --max-content-bytes; no output published\n')
        self.assertFalse(self.plan.exists())

    def test_preflight_refusal_error_interrupt_preserve_locks_and_cleanup(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        core.reclaim(self.db, out=self.out, plan_file=self.plan)
        before = self.plan.read_bytes(), self.out.read_bytes()
        real_check = artifacts._check_content_bytes
        for stage in ['reclaim-plan', 'reclaim', 'staged', 'verify-copy']:
            for error, code in [(None, 2), (sqlite3.OperationalError('SECRET identity 123 bytes'), 2),
                                (OSError('SECRET path 123'), 2), (KeyboardInterrupt(), 130)]:
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
                        class FailedConnection:
                            def execute(self, *args):
                                raise error
                        return real_check(FailedConnection(), tables, limit)
                    return real_check(conn, tables, 0)
                command = 'reclaim' if stage == 'staged' else stage
                args = [command, str(target), '--max-content-bytes', str(self.total), '--row-hash-memory-bytes', '32', '--json']
                if stage == 'reclaim-plan':
                    args += ['--out', str(self.root/'new.json'), '--content']
                else:
                    args += ['--plan', str(self.plan)]
                    if command == 'reclaim':
                        args += ['--out', str(self.out), '--overwrite']
                stdout, stderr = io.StringIO(), io.StringIO()
                with self.subTest(stage=stage, error=error), \
                        patch('flowwrench.n8n_diet.artifacts._check_content_bytes', side_effect=check), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(cli_module.main(args), code)
                self.assertTrue(checked)
                self.assertFalse(any(s.startswith('SELECT *') for s in sqls))
                self.assertEqual(stdout.getvalue(), '')
                self.assertNotIn('SECRET', stderr.getvalue())
                self.assertNotIn('z_tail', stderr.getvalue())
                self.assertFalse((self.root/'new.json').exists())
                self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
                self.assertEqual(before, (self.plan.read_bytes(), self.out.read_bytes()))
                self.assertEqual(self.original, self.db.read_bytes())

    def test_wal_snapshot_bytes_and_safety_precedence(self):
        writer = subprocess.Popen([sys.executable, '-c', '''
import sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0'); c.execute("INSERT INTO z_tail(b) VALUES(x'ff')"); c.commit()
print('ready',flush=True); sys.stdin.readline(); c.close()
''', str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), 'ready')
            paths = [self.db, Path(str(self.db)+'-wal'), Path(str(self.db)+'-shm')]
            before = [p.read_bytes() for p in paths]
            args = ['reclaim-plan', self.db, '--out', self.plan, '--content', '--max-content-bytes', self.total, '--json']
            refused = cli(*args)
            self.assertEqual(refused.returncode, 2)
            self.assertIn('non-empty WAL', refused.stderr)
            refused = cli(*args, '--force')
            self.assertEqual(refused.returncode, 2)
            self.assertIn('content bytes exceed', refused.stderr)
            self.assertEqual(refused.stdout, '')
            allowed = cli('reclaim-plan', self.db, '--out', self.plan, '--content', '--force',
                          '--max-content-bytes', self.total+2, '--row-hash-memory-bytes', 32, '--json')
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(before, [p.read_bytes() for p in paths])
        finally:
            _, stderr = writer.communicate('stop\n', timeout=5)
            self.assertEqual(writer.returncode, 0, stderr)
        journal = Path(str(self.db)+'-journal')
        journal.write_bytes(b'unrecovered')
        result = cli('reclaim-plan', self.db, '--out', self.root/'journal.json', '--content', '--max-content-bytes', 0, '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('non-empty rollback journal', result.stderr)
        self.assertEqual(journal.read_bytes(), b'unrecovered')


if __name__ == '__main__':
    unittest.main()
