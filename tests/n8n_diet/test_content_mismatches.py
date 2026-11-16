"""Opt-in table-only diagnostics must never turn other failures into reports."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import artifacts, cli as cli_module, core
from flowwrench.n8n_diet.storage import DietError, connect, quote
from .test_content_spilling import legacy_content
from .test_content_verification import cli
from . import test_content_verification as fixtures


class ContentMismatchesTest(unittest.TestCase):
    setUp = fixtures.ContentVerificationTest.setUp
    save_and_copy = fixtures.ContentVerificationTest.save_and_copy
    mutate_copy = fixtures.ContentVerificationTest.mutate_copy

    def diagnostic(self, **kwargs):
        return core.verify_copy(self.out, plan_file=self.artifact,
                                report_content_mismatches=True, **kwargs)

    def test_cli_previously_generic_mismatch_returns_structured_metadata(self):
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET b=x'ff0081' WHERE a='é'")
        plain = cli('verify-copy', self.out, '--plan', self.artifact, '--json')
        self.assertEqual((plain.returncode, plain.stdout, plain.stderr),
                         (2, '', 'flowwrench n8n-diet: copy content verification failed\n'))
        result = cli('verify-copy', self.out, '--plan', self.artifact,
                     '--report-content-mismatches', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertFalse(report['verified'])
        self.assertEqual([m['table'] for m in report['content_mismatches']], ['bag'])
        self.assertNotIn('é', result.stdout)

    def test_all_changed_tables_single_snapshot_single_payload_scan_and_spills(self):
        # Include control characters and quotes in metadata and invalid UTF-8
        # payloads; none of the payloads may appear in diagnostics.
        odd = 'z"\n\x1b[31m'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE '+quote(odd)+'(v, g AS (v) VIRTUAL)')
            conn.execute('INSERT INTO '+quote(odd)+"(v) VALUES(CAST(x'ff00534543524554' AS TEXT))")
            conn.commit()
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET a=1,b='1' WHERE rowid=(SELECT min(rowid) FROM bag WHERE a IS NULL)")
        self.mutate_copy('UPDATE "odd\""table" SET value=x\'008001\'')  # generated size changes
        self.mutate_copy('UPDATE '+quote(odd)+" SET v=CAST(x'fe00534543524554' AS TEXT)")
        with closing(connect(self.out, immutable=True)) as conn:
            expected_actual = legacy_content(conn)
        saved = json.loads(self.artifact.read_text())
        before = self.db.read_bytes(), self.artifact.read_bytes(), self.out.read_bytes()
        sqls, streams = [], []
        real_source, real_temp = core.source_database, tempfile.TemporaryFile
        from contextlib import contextmanager
        @contextmanager
        def traced(*args, **kwargs):
            with real_source(*args, **kwargs) as result:
                conn = result[0]
                conn.set_trace_callback(sqls.append)
                yield result
        def private_file(*args, **kwargs):
            stream = real_temp(*args, **kwargs)
            streams.append(stream)
            return stream
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=traced), \
                patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=private_file):
            report = self.diagnostic(row_hash_memory_bytes=32)
        names = sorted(['bag', 'odd"table', odd])
        mismatches = [{'table':t, 'expected_sha256':saved['content']['tables'][t],
                       'actual_sha256':expected_actual['tables'][t]} for t in names]
        self.assertEqual(report, {'output':str(self.out), 'verified':False, 'integrity_check':'ok',
                                 'schema_equal':True, 'row_counts_equal':True, 'content_verified':False,
                                 'content_algorithm':artifacts.CONTENT_ALGORITHM, 'content_mismatches':mismatches})
        payload_sql = [q for q in sqls if q.startswith('SELECT * FROM ')]
        self.assertEqual(sorted(payload_sql), sorted('SELECT * FROM '+quote(t) for t in saved['row_counts']))
        self.assertTrue(streams)
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(report, self.diagnostic())
        self.assertEqual(before, (self.db.read_bytes(), self.artifact.read_bytes(), self.out.read_bytes()))
        for flags in [[], ['--json']]:
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches', *flags)
            self.assertEqual((result.returncode, result.stderr), (2, ''))
            self.assertNotIn('\x1b', result.stdout)
            self.assertNotIn('SECRET', result.stdout)
            self.assertNotIn('Copy verified:', result.stdout)
            if flags:
                self.assertEqual(json.loads(result.stdout), report)
            else:
                self.assertIn('Copy content mismatch:', result.stdout)
                self.assertIn('\\n', result.stdout)
                self.assertIn('\\u001b', result.stdout)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_success_and_explicit_false_are_exactly_default(self):
        self.save_and_copy()
        default = core.verify_copy(self.out, plan_file=self.artifact)
        self.assertEqual(self.diagnostic(row_hash_memory_bytes=32), default)
        self.assertEqual(core.verify_copy(self.out, plan_file=self.artifact,
                                         report_content_mismatches=False), default)
        plain = cli('verify-copy', self.out, '--plan', self.artifact, '--json')
        for flags in [[], ['--content']]:
            result = cli('verify-copy', self.out, '--plan', self.artifact,
                         '--report-content-mismatches', '--json', *flags)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, plain.stdout, ''))

    def test_validation_and_v1_have_no_fabricated_comparison(self):
        with patch('flowwrench.n8n_diet.core.load_plan', side_effect=AssertionError('plan IO')):
            for value in [None, 0, 1, 'true', [], {}]:
                with self.subTest(value=value), self.assertRaisesRegex(DietError, 'report-content-mismatches must be a boolean'):
                    core.verify_copy('missing', plan_file='missing', report_content_mismatches=value)
            with self.assertRaisesRegex(DietError, 'max-content-bytes must'):
                core.verify_copy('missing', plan_file='missing', report_content_mismatches=1, max_content_bytes=-1)
        core.reclaim_plan(self.db, out=self.artifact)
        with patch('flowwrench.n8n_diet.core.source_database', side_effect=AssertionError('source IO')):
            with self.assertRaisesRegex(DietError, 'plan has no content digests'):
                self.diagnostic()
        result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches', '--json')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertIn('plan has no content digests', result.stderr)

    def test_structural_and_policy_failures_precede_mismatch_report(self):
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET b=x'ff0081' WHERE a='é'")
        before = self.out.read_bytes(), self.artifact.read_bytes()
        for kwargs, message in [({'max_content_rows':0, 'max_content_bytes':0}, 'content row count exceeds'),
                                ({'max_content_bytes':0, 'max_content_value_bytes':0}, 'content bytes exceed'),
                                ({'max_content_value_bytes':0, 'max_content_row_bytes':0}, 'content value exceeds'),
                                ({'max_content_row_bytes':0}, 'content row exceeds')]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(DietError, message):
                self.diagnostic(**kwargs)
        pristine = self.out.read_bytes()
        for sql, message in [('DELETE FROM bag WHERE rowid=(SELECT min(rowid) FROM bag)', 'row count'),
                             ('CREATE TABLE extra(x)', 'schema')]:
            self.out.write_bytes(pristine)
            self.mutate_copy(sql)
            with patch('flowwrench.n8n_diet.artifacts.content_digests', side_effect=AssertionError('payload scan')):
                with self.assertRaisesRegex(DietError, message):
                    self.diagnostic(max_content_rows=0)
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches', '--json')
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertIn(message, result.stderr)
        self.out.write_bytes(pristine)
        for flag in ['--max-content-rows', '--max-content-bytes', '--max-content-value-bytes', '--max-content-row-bytes']:
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches', flag, 0, '--json')
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertNotIn('content_mismatches', result.stderr)
        self.assertEqual(before, (self.out.read_bytes(), self.artifact.read_bytes()))

    def test_error_interruption_cleanup_and_no_partial_report(self):
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET a='SECRET-ROW-PAYLOAD' WHERE a IS NULL")
        before = self.out.read_bytes(), self.artifact.read_bytes(), self.db.read_bytes()
        for error, code, message in [(OSError('SECRET-ROW-PAYLOAD'), 2, 'copy verification failed; no report emitted'),
                                     (sqlite3.OperationalError('SECRET-ROW-PAYLOAD'), 2, 'copy verification failed; no report emitted'),
                                     (KeyboardInterrupt(), 130, 'interrupted')]:
            streams = []
            real_temp = tempfile.TemporaryFile
            def opened(*args, **kwargs):
                stream = real_temp(*args, **kwargs)
                streams.append(stream)
                return stream
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('flowwrench.n8n_diet.artifacts.tempfile.TemporaryFile', side_effect=opened), \
                    patch('flowwrench.n8n_diet.artifacts._merge_runs', side_effect=error), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_module.main(['verify-copy', str(self.out), '--plan', str(self.artifact),
                                          '--report-content-mismatches', '--row-hash-memory-bytes', '32', '--json'])
            self.assertEqual((result, stdout.getvalue()), (code, ''))
            self.assertIn(message, stderr.getvalue())
            self.assertNotIn('SECRET', stderr.getvalue())
            self.assertTrue(streams)
            self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(before, (self.out.read_bytes(), self.artifact.read_bytes(), self.db.read_bytes()))
        # Error unwind released OS locks: a separate writer can acquire immediately.
        result = subprocess.run([sys.executable, '-c',
                                 "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute('BEGIN IMMEDIATE'); c.rollback()",
                                 str(self.out)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_late_table_refusal_suppresses_earlier_mismatch(self):
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET b=x'ff0081' WHERE a='é'")
        # bag sorts before this table. All earlier mismatches must be withheld
        # until the complete hashing pass (including later byte checks) succeeds.
        self.mutate_copy("UPDATE workflow_entity SET name='" + 'x'*1000 + "'")
        seen = []
        real_hash = artifacts._table_content
        def hashed(cursor, capacity):
            seen.append(True)
            return real_hash(cursor, capacity)
        with patch('flowwrench.n8n_diet.artifacts._table_content', side_effect=hashed):
            with self.assertRaisesRegex(DietError, 'content value exceeds'):
                self.diagnostic(max_content_value_bytes=500, row_hash_memory_bytes=32)
        self.assertTrue(seen)
        result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches',
                     '--max-content-value-bytes', 500, '--json')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertEqual(result.stderr, 'flowwrench n8n-diet: content value exceeds --max-content-value-bytes; no output published\n')

    def test_snapshot_lock_stays_held_during_diagnostic_hash_pass(self):
        self.save_and_copy()
        self.mutate_copy("UPDATE bag SET b=x'ff0081' WHERE a='é'")
        real_hash = artifacts._table_content
        checked = []
        def hashed(cursor, capacity):
            if not checked:
                writer = subprocess.run([sys.executable, '-c',
                    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute('BEGIN IMMEDIATE')",
                    str(self.out)], capture_output=True)
                self.assertNotEqual(writer.returncode, 0)
                self.assertIn(b'locked', writer.stderr)
                checked.append(True)
            return real_hash(cursor, capacity)
        with patch('flowwrench.n8n_diet.artifacts._table_content', side_effect=hashed):
            report = self.diagnostic(row_hash_memory_bytes=32)
        self.assertTrue(checked)
        self.assertEqual([m['table'] for m in report['content_mismatches']], ['bag'])

    def test_safety_failures_and_writer_conflict_never_emit_report(self):
        self.save_and_copy()
        before = self.out.read_bytes()
        for suffix, message in [('-journal', 'rollback journal'), ('-wal', 'non-empty WAL')]:
            sidecar = self.out.with_name(self.out.name+suffix)
            sidecar.write_bytes(b'not-recovered')
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches',
                         '--max-content-rows', 0, '--json')
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertIn(message, result.stderr)
            self.assertEqual(sidecar.read_bytes(), b'not-recovered')
            sidecar.unlink()
        with closing(sqlite3.connect(self.out)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--report-content-mismatches', '--json')
            self.assertEqual((result.returncode, result.stdout), (2, ''))
            self.assertIn('locked', result.stderr)
            writer.rollback()
        self.assertEqual(self.out.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
