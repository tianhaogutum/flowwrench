"""Aggregate stored-byte guards precede each table's Python payload selection."""
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


class ContentRowLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.plan, self.out = (self.root / name for name in ('input.sqlite', 'plan.json', 'copy.sqlite'))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.executescript('''CREATE TABLE "z_tail"("odd""column", b,
                v GENERATED ALWAYS AS ("odd""column") VIRTUAL,
                s GENERATED ALWAYS AS (CAST(b AS BLOB)) STORED);
                INSERT INTO z_tail("odd""column",b) VALUES
                (NULL,9223372036854775807),(-1e300,1e999),('',x''),
                ('é',x'00ff'),('é',x'00ff'),(CAST(x'ff0080' AS TEXT),x'ff0080');''')
            conn.executemany('INSERT INTO z_tail("odd""column",b) VALUES (?,?)',
                             [('é' * 31 + 'x', b'x' * 63), ('é' * 32, b'x' * 64),
                              ('é' * 32, b'x' * 64)])
            conn.commit()
        self.original = self.db.read_bytes()

    def test_validation_precedes_io(self):
        operations = [lambda limit: core.reclaim_plan('missing', out='missing', max_content_row_bytes=limit),
                      lambda limit: core.reclaim('missing', out='missing', plan_file='missing', max_content_row_bytes=limit),
                      lambda limit: core.verify_copy('missing', plan_file='missing', max_content_row_bytes=limit),
                      lambda limit: artifacts.content_digests(None, max_content_row_bytes=limit)]
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('source IO')), \
                patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('plan IO')):
            for operation in operations:
                for limit in [-1, True, False, 1.5, '3', [], {}, 2**63]:
                    with self.subTest(operation=operation, limit=limit):
                        with self.assertRaisesRegex(DietError, 'max-content-row-bytes.*integer'):
                            operation(limit)

    def test_aggregate_precedes_select_and_preserves_digests(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('''CREATE TABLE bag(a,b);
                INSERT INTO bag VALUES (NULL,9223372036854775807),(-1e300,1e999),('',x''),
                  ('é',x'00ff'),('é',x'00ff'),(CAST(x'ff0080' AS TEXT),x'ff0080');''')
            reference = legacy_content(conn)
            for limit in [None, 6, 7, 2**53+1, 2**63-1]:
                self.assertEqual(artifacts.content_digests(conn, max_content_row_bytes=limit,
                                 max_content_value_bytes=3, row_hash_memory_bytes=32), reference)
            sqls = []
            conn.set_trace_callback(sqls.append)
            with self.assertRaisesRegex(DietError, 'content row exceeds --max-content-row-bytes'):
                artifacts.content_digests(conn, max_content_row_bytes=5, max_content_value_bytes=3)
            self.assertFalse(any(sql.startswith('SELECT *') for sql in sqls))
            self.assertIs(conn.text_factory, str)
            conn.execute('DELETE FROM bag WHERE typeof(a) IN (\'text\',\'blob\') AND length(CAST(a AS BLOB))>0')
            self.assertEqual(artifacts.content_digests(conn, max_content_row_bytes=0), legacy_content(conn))

    def test_cli_aggregate_refuses_without_publication(self):
        result = cli('reclaim-plan', self.db, '--out', self.plan, '--content',
                     '--max-content-row-bytes', '255', '--max-content-value-bytes', '64',
                     '--row-hash-memory-bytes', '32', '--json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('content row exceeds --max-content-row-bytes', result.stderr)
        self.assertNotIn('z_tail', result.stderr)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_all_content_routes_exact_boundary_and_default_parity(self):
        default = core.reclaim_plan(self.db, out=self.plan, content=True)
        per_value = core.reclaim_plan(self.db, out=self.root / 'value.json', content=True,
                                      max_content_value_bytes=64, row_hash_memory_bytes=32)
        guarded = core.reclaim_plan(self.db, out=self.root / 'row.json', content=True,
                                    max_content_value_bytes=64, max_content_row_bytes=256, row_hash_memory_bytes=32)
        self.assertEqual(default, per_value)
        self.assertEqual(default, guarded)
        with closing(connect(self.db, immutable=True)) as conn:
            self.assertEqual(guarded['content'], legacy_content(conn))
        plain = core.reclaim(self.db, out=self.out, plan_file=self.plan)
        allowed = core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True,
                               max_content_row_bytes=256, max_content_value_bytes=64, row_hash_memory_bytes=32)
        self.assertEqual(plain, allowed)
        verification = core.verify_copy(self.out, plan_file=self.plan)
        self.assertEqual(verification, core.verify_copy(self.out, plan_file=self.plan,
                         max_content_row_bytes=256, row_hash_memory_bytes=32))
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute('UPDATE z_tail SET rowid=1000-rowid')
            conn.commit()
        self.assertEqual(core.verify_copy(self.out, plan_file=self.plan, max_content_row_bytes=256,
                                         row_hash_memory_bytes=32)['content'], guarded['content'])
        before_plan, before_copy = self.plan.read_bytes(), self.out.read_bytes()
        for operation in [lambda: core.reclaim_plan(self.db, out=self.root / 'refused.json', content=True,
                                                    max_content_row_bytes=255),
                          lambda: core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True,
                                               max_content_row_bytes=255),
                          lambda: core.verify_copy(self.out, plan_file=self.plan, max_content_row_bytes=255)]:
            with self.assertRaisesRegex(DietError, 'content row exceeds'):
                operation()
        self.assertFalse((self.root / 'refused.json').exists())
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertEqual(self.plan.read_bytes(), before_plan)
        self.assertEqual(self.out.read_bytes(), before_copy)

    def test_stored_byte_lengths_and_generated_columns(self):
        for encoding, limit in [('UTF-8', 10), ('UTF-16le', 14)]:
            with self.subTest(encoding=encoding), closing(sqlite3.connect(':memory:')) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA encoding='" + encoding + "'")
                conn.executescript('''CREATE TABLE bag(a,b,v AS (a) VIRTUAL,s AS (b) STORED);
                    INSERT INTO bag(a,b) VALUES ('ab',x'ff0080');''')
                reference = legacy_content(conn)
                # ASCII TEXT is 2/4 stored bytes; the 3-byte BLOB is unchanged.
                exact = limit
                self.assertEqual(artifacts.content_digests(conn, max_content_row_bytes=exact), reference)
                with self.assertRaisesRegex(DietError, 'content row exceeds'):
                    artifacts.content_digests(conn, max_content_row_bytes=exact-1)
                conn.execute('DELETE FROM bag')
                conn.execute("INSERT INTO bag(a,b) VALUES (CAST(x'61006263' AS TEXT), x'ff0080')")
                self.assertEqual(artifacts.content_digests(conn, max_content_row_bytes=14), legacy_content(conn))
                with self.assertRaisesRegex(DietError, 'content row exceeds'):
                    artifacts.content_digests(conn, max_content_row_bytes=13)

    def test_wide_table_without_linear_expression_depth_or_parameters(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE bag(' + ','.join('c'+str(i) for i in range(1100)) + ')')
            conn.execute('INSERT INTO bag VALUES (' + ','.join("'x'" for _ in range(1100)) + ')')
            if hasattr(conn, 'setlimit'):
                conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
            self.assertEqual(artifacts.content_digests(conn, max_content_row_bytes=1100), legacy_content(conn))
            with self.assertRaisesRegex(DietError, 'content row exceeds'):
                artifacts.content_digests(conn, max_content_row_bytes=1099)

    def test_structural_only_does_not_scan_and_value_precedes_row(self):
        with patch('flowwrench.n8n_diet.artifacts._check_content_rows', side_effect=AssertionError('no content scan')):
            saved = core.reclaim_plan(self.db, out=self.plan, max_content_row_bytes=0)
            self.assertEqual(saved['version'], 1)
            self.assertNotIn('content', saved)
            self.assertFalse(core.reclaim(self.db, out=self.out, plan_file=self.plan,
                                         max_content_row_bytes=0)['content_verified'])
            self.assertFalse(core.verify_copy(self.out, plan_file=self.plan,
                                             max_content_row_bytes=0)['content_verified'])
            with closing(connect(self.db, immutable=True)) as conn:
                with self.assertRaisesRegex(DietError, 'content value exceeds'):
                    artifacts.content_digests(conn, max_content_value_bytes=0, max_content_row_bytes=0)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_cli_invalid_limits_precede_io_without_echo(self):
        for command in ['reclaim-plan', 'reclaim', 'verify-copy']:
            flags = ['--plan', self.plan] if command == 'verify-copy' else ['--out', self.out]
            for limit in ['-1', 'SECRET-LIMIT', '1.5', str(2**63)]:
                result = cli(command, self.root / 'missing', *flags, '--max-content-row-bytes', limit, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('max-content-row-bytes must be an integer', result.stderr)
                self.assertNotIn('SECRET-LIMIT', result.stderr)
                self.assertFalse(self.out.exists())

    def test_staged_copy_guard_precedes_payload_and_publication(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        before_plan = self.plan.read_bytes()
        self.out.write_bytes(b'existing output')
        real_verify = core.verify_structure
        sqls = []

        def tamper(conn, **kwargs):
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('UPDATE z_tail SET b=zeroblob(65) WHERE length(b)=64')
                writer.commit()
            conn.set_trace_callback(sqls.append)
            return real_verify(conn, **kwargs)

        real_check = artifacts._check_content_rows
        for error in [None, sqlite3.OperationalError('staged preflight IO'), KeyboardInterrupt()]:
            def check(conn, table, limit):
                path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
                if table == 'z_tail' and path != self.db and error is not None:
                    raise error
                return real_check(conn, table, limit)

            expected = DietError if error is None else type(error)
            with self.subTest(error=error), \
                    patch('flowwrench.n8n_diet.core.verify_structure', side_effect=tamper), \
                    patch('flowwrench.n8n_diet.artifacts._check_content_rows', side_effect=check):
                with self.assertRaises(expected):
                    core.reclaim(self.db, out=self.out, overwrite=True, plan_file=self.plan,
                                 max_content_row_bytes=256, row_hash_memory_bytes=32)
            self.assertFalse(any(sql == 'SELECT * FROM "z_tail"' for sql in sqls))
            self.assertEqual(self.out.read_bytes(), b'existing output')
            self.assertEqual(self.plan.read_bytes(), before_plan)
            self.assertEqual(self.db.read_bytes(), self.original)
            self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_refusal_errors_interrupt_close_spills_hold_locks_preserve_outputs(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        core.reclaim(self.db, out=self.out, plan_file=self.plan)
        before_plan, before_copy = self.plan.read_bytes(), self.out.read_bytes()
        real_check, real_temp = artifacts._check_content_rows, tempfile.TemporaryFile
        for operation in ['reclaim-plan', 'reclaim', 'verify-copy']:
            for error, code in [(None, 2), (sqlite3.OperationalError('injected preflight IO'), 2),
                                (KeyboardInterrupt(), 130)]:
                with self.subTest(operation=operation, error=error):
                    opened, checked = [], []
                    target = self.out if operation == 'verify-copy' else self.db

                    def private_file(*args, **kwargs):
                        stream = real_temp(*args, **kwargs)
                        opened.append(stream)
                        return stream

                    def check(conn, table, limit):
                        if table == 'z_tail':
                            self.assertTrue(opened)
                            self.assertTrue(all(stream.closed for stream in opened))
                            attempt = subprocess.run([sys.executable, '-c',
                                'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                                str(target)], capture_output=True, text=True, timeout=5)
                            self.assertNotEqual(attempt.returncode, 0)
                            self.assertIn('locked', attempt.stderr)
                            checked.append(True)
                            if error is not None:
                                raise error
                        return real_check(conn, table, limit)

                    args = [operation, str(target), '--max-content-row-bytes', '255',
                            '--row-hash-memory-bytes', '32', '--json']
                    if operation == 'reclaim-plan':
                        args += ['--out', str(self.root / 'new.json'), '--content']
                    else:
                        args += ['--plan', str(self.plan)]  # no --content
                        if operation == 'reclaim':
                            args += ['--out', str(self.out), '--overwrite']
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=private_file), \
                            patch('flowwrench.n8n_diet.artifacts._check_content_rows', side_effect=check), \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(cli_module.main(args), code)
                    self.assertTrue(checked)
                    self.assertTrue(all(stream.closed for stream in opened))
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertNotIn('z_tail', stderr.getvalue())
                    self.assertFalse((self.root / 'new.json').exists())
                    self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
                    self.assertEqual(self.db.read_bytes(), self.original)
                    self.assertEqual(self.plan.read_bytes(), before_plan)
                    self.assertEqual(self.out.read_bytes(), before_copy)


if __name__ == '__main__':
    unittest.main()
