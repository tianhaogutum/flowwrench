"""Bounded hash runs must retain the established typed-row multiset contract."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import json
import io
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import artifacts, cli as cli_module, core
from flowwrench.n8n_diet.storage import DietError, connect
from .test_content_verification import ROOT, cli


def legacy_content(conn):
    """Pre-spill algorithm as an independent in-memory reference for tiny inputs."""
    class Text(bytes):
        pass
    tables = {}
    for table in artifacts.ordinary_tables(conn):
        conn.text_factory = Text
        rows = []
        try:
            for row in conn.execute('SELECT * FROM ' + artifacts.quote(table)):
                digest = hashlib.sha256(b'n8n-exec-diet/row/v1\0' + struct.pack('>Q', len(row)))
                for value in row:
                    if value is None:
                        tag, encoded = b'n', b''
                    elif isinstance(value, Text):
                        tag, encoded = b't', value
                    elif isinstance(value, bytes):
                        tag, encoded = b'b', value
                    elif isinstance(value, int):
                        tag, encoded = b'i', struct.pack('>q', value)
                    else:
                        tag, encoded = b'r', struct.pack('>d', value if value else 0.0)
                    digest.update(tag + struct.pack('>Q', len(encoded)) + encoded)
                rows.append(digest.digest())
        finally:
            conn.text_factory = str
        digest = hashlib.sha256(b'n8n-exec-diet/table/v1\0' + struct.pack('>Q', len(rows)))
        for row_hash in sorted(rows):
            digest.update(row_hash)
        tables[table] = digest.hexdigest()
    return {'algorithm': 'sha256-typed-rows-v1', 'tables': tables}


class ContentSpillingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.sqlite'
        self.plan = self.root / 'plan.json'
        self.out = self.root / 'copy.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.execute('CREATE TABLE bag(a,b)')
            conn.executemany('INSERT INTO bag(rowid,a,b) VALUES (?,?,?)',
                             [(i * 7 + 11, *[(None, b'\0\xff'), (-1, 'é'), (1.0, b'1')][i % 3])
                              for i in range(35)])
            conn.commit()
        self.original = self.db.read_bytes()
        with closing(connect(self.db, immutable=True)) as conn:
            self.reference = legacy_content(conn)

    def test_budget_validation_precedes_all_io(self):
        operations = [lambda budget: core.reclaim_plan('missing', out='missing', row_hash_memory_bytes=budget),
                      lambda budget: core.reclaim('missing', out='missing', plan_file='missing', row_hash_memory_bytes=budget),
                      lambda budget: core.verify_copy('missing', plan_file='missing', row_hash_memory_bytes=budget),
                      lambda budget: artifacts.content_digests(None, row_hash_memory_bytes=budget)]
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('no source access')), \
                patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('no plan access')):
            for operation in operations:
                for budget in [0, 31, -1, True, 1.5, '64', None]:
                    with self.subTest(operation=operation, budget=budget):
                        with self.assertRaisesRegex(DietError, 'row.hash.*at least 32'):
                            operation(budget)

    def test_cli_budget_validation_and_structural_compatibility(self):
        for command in ['reclaim-plan', 'reclaim', 'verify-copy']:
            extra = ['--plan', self.plan] if command == 'verify-copy' else ['--out', self.out]
            for budget in ['0', '31', '-1', '1.5', 'true']:
                result = cli(command, self.db, *extra, '--row-hash-memory-bytes', budget, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse(self.out.exists())
        saved = core.reclaim_plan(self.db, out=self.plan, row_hash_memory_bytes=32)
        self.assertEqual(saved['version'], 1)
        self.assertNotIn('content', saved)
        self.assertFalse(core.reclaim(self.db, out=self.out, plan_file=self.plan, row_hash_memory_bytes=32)['content_verified'])
        self.assertFalse(core.verify_copy(self.out, plan_file=self.plan, row_hash_memory_bytes=32)['content_verified'])
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_multiple_private_runs_match_legacy_and_preserve_duplicates(self):
        opened, sizes = [], []
        real_temp = tempfile.TemporaryFile
        real_write = artifacts._write_run

        def private_file(*args, **kwargs):
            stream = real_temp(*args, **kwargs)
            opened.append(stream)
            self.assertLessEqual(sum(not item.closed for item in opened), 2)
            info = os.fstat(stream.fileno())
            self.assertEqual(info.st_mode & 0o777, 0o600)
            self.assertEqual(info.st_nlink, 0)  # no pathname remains, including on SIGKILL
            return stream

        def write_run(stream, rows):
            sizes.append(len(rows))
            self.assertLessEqual(len(rows), 2)
            return real_write(stream, rows)

        with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=private_file), \
                patch('flowwrench.n8n_diet.artifacts._write_run', side_effect=write_run):
            saved = core.reclaim_plan(self.db, out=self.plan, content=True, row_hash_memory_bytes=65)
        self.assertGreater(len(sizes), 3)
        self.assertTrue(opened)
        self.assertTrue(all(stream.closed for stream in opened))
        self.assertEqual(saved['content'], self.reference)
        self.assertEqual(set(saved), {'format', 'version', 'operation', 'source', 'schema_sha256', 'row_counts', 'storage', 'content'})
        # Default and non-multiple budgets do not alter any artifact field.
        default = core.reclaim_plan(self.db, out=self.root / 'default.json', content=True)
        self.assertEqual(saved, default)
        core.reclaim(self.db, out=self.out, plan_file=self.plan, row_hash_memory_bytes=32)
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute('UPDATE bag SET rowid=1000-rowid')
            conn.commit()
        self.assertEqual(core.verify_copy(self.out, plan_file=self.plan, row_hash_memory_bytes=32)['content'], self.reference)
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute("UPDATE bag SET a=-1,b='é' WHERE rowid=(SELECT min(rowid) FROM bag WHERE a IS NULL)")
            conn.commit()
        before = self.out.read_bytes()
        with self.assertRaisesRegex(DietError, 'content verification failed'):
            core.verify_copy(self.out, plan_file=self.plan, row_hash_memory_bytes=32)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertEqual(json.loads(self.plan.read_text()), saved)

    def test_cleanup_on_spill_errors_interrupt_and_refusal(self):
        core.reclaim_plan(self.db, out=self.plan, content=True)
        plan_bytes = self.plan.read_bytes()
        core.reclaim(self.db, out=self.out, plan_file=self.plan)
        output_bytes = self.out.read_bytes()
        real_temp = tempfile.TemporaryFile
        for operation in ['plan', 'reclaim', 'verify', 'staged-copy']:
            for boundary in ['_write_run', '_merge_runs', '_read_hash']:
                for exception in [OSError('injected spill I/O failure'), KeyboardInterrupt()]:
                    with self.subTest(operation=operation, boundary=boundary, exception=type(exception).__name__):
                        opened = []

                        def private_file(*args, **kwargs):
                            stream = real_temp(*args, **kwargs)
                            opened.append(stream)
                            return stream

                        def fail(*args, **kwargs):
                            # Write some bytes before failure, including for interruption.
                            if boundary == '_write_run':
                                args[0].write(b'partial spill')
                            raise exception

                        def invoke():
                            if operation == 'plan':
                                return core.reclaim_plan(self.db, out=self.root / 'new.json', content=True, row_hash_memory_bytes=32)
                            if operation == 'verify':
                                return core.verify_copy(self.out, plan_file=self.plan, row_hash_memory_bytes=32)
                            return core.reclaim(self.db, out=self.out, plan_file=self.plan, overwrite=True, row_hash_memory_bytes=32)

                        with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=private_file):
                            if operation == 'staged-copy':
                                real_verify = core.verify_structure

                                def fail_in_copy(conn, **kwargs):
                                    with patch('flowwrench.n8n_diet.artifacts.' + boundary, side_effect=fail):
                                        return real_verify(conn, **kwargs)

                                context = patch('flowwrench.n8n_diet.core.verify_structure', side_effect=fail_in_copy)
                            else:
                                context = patch('flowwrench.n8n_diet.artifacts.' + boundary, side_effect=fail)
                            with context, self.assertRaises(type(exception)):
                                invoke()
                        self.assertTrue(opened)
                        self.assertTrue(all(stream.closed for stream in opened))
                        self.assertFalse((self.root / 'new.json').exists())
                        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
                        self.assertEqual(self.db.read_bytes(), self.original)
                        self.assertEqual(self.plan.read_bytes(), plan_bytes)
                        self.assertEqual(self.out.read_bytes(), output_bytes)
        # Failure creating the second scratch file closes the first one too.
        opened = []

        def fail_second(*args, **kwargs):
            if opened:
                raise OSError('injected spill create failure')
            stream = real_temp(*args, **kwargs)
            opened.append(stream)
            return stream

        with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'spill create'):
                core.reclaim_plan(self.db, out=self.root / 'new.json', content=True, row_hash_memory_bytes=32)
        self.assertTrue(opened[0].closed)

    def test_run_boundaries_and_short_io(self):
        # Exercise singleton, exact-full, odd-tail and multi-pass boundaries;
        # partial reads/writes must not silently omit bytes or duplicate hashes.
        with closing(connect(self.db, immutable=True)) as conn:
            reference = legacy_content(conn)
            real_pread = os.pread
            real_write = artifacts._write_hash

            class ShortWriter:
                def __init__(self, stream):
                    self.stream = stream

                def write(self, value):
                    return self.stream.write(value[:7])

            def short_write(stream, value):
                return real_write(ShortWriter(stream), value)

            with patch('flowwrench.n8n_diet.artifacts.os.pread', side_effect=lambda fd, size, offset: real_pread(fd, min(size, 5), offset)), \
                    patch('flowwrench.n8n_diet.artifacts._write_hash', side_effect=short_write):
                for capacity in [1, 2, 3, 5, 7, 10, 17, 34, 35, 36]:
                    self.assertEqual(artifacts.content_digests(conn, row_hash_memory_bytes=capacity * 32), reference)
            factory = conn.text_factory
            with patch('flowwrench.n8n_diet.artifacts.os.pread', return_value=b''):
                with self.assertRaisesRegex(DietError, 'truncated content hash spill'):
                    artifacts.content_digests(conn, row_hash_memory_bytes=32)
            self.assertIs(conn.text_factory, factory)
            self.assertEqual(conn.execute('SELECT count(*) FROM bag').fetchone()[0], 35)
            with patch('flowwrench.n8n_diet.artifacts._write_hash', side_effect=OSError('injected write')):
                with self.assertRaises(OSError):
                    artifacts.content_digests(conn, row_hash_memory_bytes=32)
            self.assertIs(conn.text_factory, factory)

    def test_cli_spill_failure_and_interrupt_have_no_stdout(self):
        for error, exit_code in [(OSError('injected spill I/O'), 2), (KeyboardInterrupt(), 130)]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('flowwrench.n8n_diet.artifacts._merge_runs', side_effect=error), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main(['reclaim-plan', str(self.db), '--out', str(self.plan),
                                        '--content', '--row-hash-memory-bytes', '32', '--json'])
            self.assertEqual(code, exit_code)
            self.assertEqual(stdout.getvalue(), '')
            self.assertTrue(stderr.getvalue())
            self.assertFalse(self.plan.exists())
            self.assertEqual(self.db.read_bytes(), self.original)

    def test_spilling_does_not_release_source_locks(self):
        real_merge = artifacts._merge_runs
        checked = []

        def check_locks(*args, **kwargs):
            attempt = subprocess.run([sys.executable, '-c',
                'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                str(self.db)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn('locked', attempt.stderr)
            checked.append(True)
            return real_merge(*args, **kwargs)

        with patch('flowwrench.n8n_diet.artifacts._merge_runs', side_effect=check_locks):
            core.reclaim_plan(self.db, out=self.plan, content=True, row_hash_memory_bytes=32)
        self.assertTrue(checked)
        self.assertEqual(self.db.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
