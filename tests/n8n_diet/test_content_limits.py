"""Opt-in value-byte limits must precede Python payload row materialization."""
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
import io
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import artifacts, cli as cli_module, core
from flowwrench.n8n_diet.storage import DietError, connect
from .test_content_spilling import legacy_content
from .test_content_verification import ROOT, cli


class ContentLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.plan, self.out = (self.root / name for name in ('input.sqlite', 'plan.json', 'copy.sqlite'))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.executescript('''CREATE TABLE bag(a,b);
                INSERT INTO bag(rowid,a,b) VALUES
                  (11, NULL, 9223372036854775807), (23, -1e300, 1e999),
                  (40, '', x''), (60, 'é', x'00ff'), (70, 'é', x'00ff'),
                  (91, CAST(x'ff0080' AS TEXT), x'ff0080');''')
        self.original = self.db.read_bytes()

    def test_validation_precedes_io(self):
        operations = [lambda limit: core.reclaim_plan('missing', out='missing', max_content_value_bytes=limit),
                      lambda limit: core.reclaim('missing', out='missing', plan_file='missing', max_content_value_bytes=limit),
                      lambda limit: core.verify_copy('missing', plan_file='missing', max_content_value_bytes=limit),
                      lambda limit: artifacts.content_digests(None, max_content_value_bytes=limit)]
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('source IO')), \
                patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('plan IO')):
            for operation in operations:
                for limit in [-1, True, False, 1.5, '3', 2**63]:
                    with self.subTest(operation=operation, limit=limit):
                        with self.assertRaisesRegex(DietError, 'max.content.value.bytes.*integer'):
                            operation(limit)

    def test_exact_stored_bytes_before_payload_select(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('''CREATE TABLE bag(a,b);
                INSERT INTO bag VALUES (NULL,9223372036854775807),(-1e300,1e999),('',x''),
                    ('é',x'00ff'),('é',x'00ff'),(CAST(x'ff0080' AS TEXT),x'ff0080');''')
            reference = legacy_content(conn)
            for limit in [3, 4, 2**63-1, None]:
                self.assertEqual(artifacts.content_digests(conn, max_content_value_bytes=limit,
                                                         row_hash_memory_bytes=32), reference)
            selects = []
            conn.set_trace_callback(selects.append)
            with self.assertRaisesRegex(DietError, 'content value exceeds --max-content-value-bytes'):
                artifacts.content_digests(conn, max_content_value_bytes=2, row_hash_memory_bytes=32)
            self.assertFalse(any(sql.startswith('SELECT *') for sql in selects))
            self.assertIs(conn.text_factory, str)
            conn.execute("DELETE FROM bag WHERE typeof(a) IN ('text','blob') AND length(CAST(a AS BLOB))>0")
            zero = legacy_content(conn)
            self.assertEqual(artifacts.content_digests(conn, max_content_value_bytes=0), zero)

    def test_content_plan_cli_refuses_without_report_or_publication(self):
        result = cli('reclaim-plan', self.db, '--out', self.plan, '--content',
                     '--max-content-value-bytes', '2', '--row-hash-memory-bytes', '32', '--json')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('content value exceeds --max-content-value-bytes', result.stderr)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.db.read_bytes(), self.original)


    def test_auxiliary_boundaries_and_unchanged_artifacts_through_all_routes(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''CREATE TABLE "odd""table"("odd""column", b,
                generated TEXT GENERATED ALWAYS AS ("odd""column") VIRTUAL);
                INSERT INTO "odd""table" VALUES (NULL,NULL);''')
            conn.executemany('INSERT INTO "odd""table" VALUES (?,?)',
                             [('é' * 31 + 'x', b'x' * 63), ('é' * 32, b'x' * 64),
                              ('é' * 32, b'x' * 64), (None, None)])
            conn.commit()
        original = self.db.read_bytes()
        default = core.reclaim_plan(self.db, out=self.plan, content=True)
        saved = core.reclaim_plan(self.db, out=self.root / 'guarded.json', content=True,
                                  max_content_value_bytes=64, row_hash_memory_bytes=32)
        self.assertEqual(saved, default)  # including exact v2 field set
        with closing(connect(self.db, immutable=True)) as conn:
            self.assertEqual(saved['content'], legacy_content(conn))
        self.assertTrue(core.reclaim(self.db, out=self.out, plan_file=self.plan,
                                    max_content_value_bytes=64, row_hash_memory_bytes=32)['content_verified'])
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute('UPDATE bag SET rowid=1000-rowid')
            conn.commit()
        self.assertEqual(core.verify_copy(self.out, plan_file=self.plan, max_content_value_bytes=64,
                                         row_hash_memory_bytes=32)['content'], saved['content'])
        before_plan, before_copy = self.plan.read_bytes(), self.out.read_bytes()
        for operation in [lambda: core.reclaim_plan(self.db, out=self.root / 'refused.json', content=True,
                                                    max_content_value_bytes=63),
                          lambda: core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True,
                                               max_content_value_bytes=63),
                          lambda: core.verify_copy(self.out, plan_file=self.plan, max_content_value_bytes=63)]:
            with self.assertRaisesRegex(DietError, 'content value exceeds'):
                operation()
        self.assertFalse((self.root / 'refused.json').exists())
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(self.plan.read_bytes(), before_plan)
        self.assertEqual(self.out.read_bytes(), before_copy)

    def test_blob_text_and_generated_values_use_sqlite_byte_lengths(self):
        # Exactly 3 is accepted; each different 4-byte value must refuse before
        # SELECT * (including embedded NUL and invalid UTF-8, not character count).
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('''CREATE TABLE bag(a,b,
                generated AS (CAST(a AS BLOB) || 'x'));
                INSERT INTO bag(a,b) VALUES ('é',x'ff0080');''')
            self.assertEqual(artifacts.content_digests(conn, max_content_value_bytes=3), legacy_content(conn))
            conn.execute('DELETE FROM bag')
            for value in ["x'ff008078'", "CAST(x'ff008078' AS TEXT)", "'éé'", "CAST(x'61006263' AS TEXT)"]:
                conn.execute('INSERT INTO bag(a) VALUES (' + value + ')')
                sqls = []
                conn.set_trace_callback(sqls.append)
                with self.assertRaisesRegex(DietError, 'content value exceeds'):
                    artifacts.content_digests(conn, max_content_value_bytes=3)
                self.assertFalse(any(sql.startswith('SELECT *') for sql in sqls))
                conn.execute('DELETE FROM bag')
            # Only the generated column is oversized.
            conn.execute("INSERT INTO bag(a) VALUES ('abc')")
            with self.assertRaisesRegex(DietError, 'content value exceeds'):
                artifacts.content_digests(conn, max_content_value_bytes=3)

    def test_utf16_database_limit_counts_stored_bytes(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript("PRAGMA encoding='UTF-16le'; CREATE TABLE bag(a); INSERT INTO bag VALUES ('ab');")
            self.assertEqual(artifacts.content_digests(conn, max_content_value_bytes=4), legacy_content(conn))
            with self.assertRaisesRegex(DietError, 'content value exceeds'):
                artifacts.content_digests(conn, max_content_value_bytes=3)

    def test_structural_only_commands_do_not_add_content_scan(self):
        with patch('flowwrench.n8n_diet.artifacts._check_content_values', side_effect=AssertionError('no content scan')):
            saved = core.reclaim_plan(self.db, out=self.plan, max_content_value_bytes=0)
            self.assertEqual(saved['version'], 1)
            self.assertNotIn('content', saved)
            self.assertFalse(core.reclaim(self.db, out=self.out, plan_file=self.plan,
                                         max_content_value_bytes=0)['content_verified'])
            self.assertFalse(core.verify_copy(self.out, plan_file=self.plan,
                                             max_content_value_bytes=0)['content_verified'])
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_cli_validation_has_no_report(self):
        for command in ['reclaim-plan', 'reclaim', 'verify-copy']:
            flags = ['--plan', self.plan] if command == 'verify-copy' else ['--out', self.out]
            for limit in ['-1', 'true', '1.5', str(2**63)]:
                result = cli(command, self.db, *flags, '--max-content-value-bytes', limit, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse(self.out.exists())
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_staged_copy_guard_precedes_payload_and_publication(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("CREATE TABLE z_tail(value); INSERT INTO z_tail VALUES (zeroblob(64));")
        original = self.db.read_bytes()
        core.reclaim_plan(self.db, out=self.plan, content=True)
        before_plan = self.plan.read_bytes()
        self.out.write_bytes(b'existing output')
        real_verify = core.verify_structure
        sqls = []

        def tamper(conn, **kwargs):
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('UPDATE z_tail SET value=zeroblob(65)')
                writer.commit()
            conn.set_trace_callback(sqls.append)
            return real_verify(conn, **kwargs)

        with patch('flowwrench.n8n_diet.core.verify_structure', side_effect=tamper):
            with self.assertRaisesRegex(DietError, 'content value exceeds'):
                core.reclaim(self.db, out=self.out, overwrite=True, plan_file=self.plan,
                             max_content_value_bytes=64, row_hash_memory_bytes=32)
        self.assertFalse(any(sql == 'SELECT * FROM "z_tail"' for sql in sqls))
        self.assertEqual(self.out.read_bytes(), b'existing output')
        self.assertEqual(self.plan.read_bytes(), before_plan)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_refusal_errors_and_interrupt_close_spills_keep_locks_and_outputs(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''CREATE TABLE aa_spill(value);
                INSERT INTO aa_spill VALUES (1),(2),(3),(4);
                CREATE TABLE z_tail(value); INSERT INTO z_tail VALUES ('PAYLOAD-SECRET-' || hex(zeroblob(65)));''')
        original = self.db.read_bytes()
        core.reclaim_plan(self.db, out=self.plan, content=True)
        core.reclaim(self.db, out=self.out, plan_file=self.plan)
        before_plan, before_copy = self.plan.read_bytes(), self.out.read_bytes()
        real_check, real_temp = artifacts._check_content_values, tempfile.TemporaryFile
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

                    args = [operation, str(target), '--max-content-value-bytes', '64',
                            '--row-hash-memory-bytes', '32', '--json']
                    if operation == 'reclaim-plan':
                        args += ['--out', str(self.root / 'new.json'), '--content']
                    else:
                        args += ['--plan', str(self.plan)]  # no --content
                        if operation == 'reclaim':
                            args += ['--out', str(self.out), '--overwrite']
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=private_file), \
                            patch('flowwrench.n8n_diet.artifacts._check_content_values', side_effect=check), \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(cli_module.main(args), code)
                    self.assertTrue(checked)
                    self.assertTrue(all(stream.closed for stream in opened))
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertNotIn('PAYLOAD-SECRET', stderr.getvalue())
                    self.assertFalse((self.root / 'new.json').exists())
                    self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
                    self.assertEqual(self.db.read_bytes(), original)
                    self.assertEqual(self.plan.read_bytes(), before_plan)
                    self.assertEqual(self.out.read_bytes(), before_copy)


if __name__ == '__main__':
    unittest.main()
