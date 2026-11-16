"""Synthetic integration contracts for saved plans and publication recovery."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import core
from flowwrench.n8n_diet.storage import DietError, staged_output

ROOT = Path(__file__).resolve().parent


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'flowwrench.n8n_diet', *map(str, args)],
                          cwd=ROOT, text=True, capture_output=True, timeout=30)


class ReclamationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.sqlite'
        self.artifact = self.root / 'plan.json'
        self.out = self.root / 'copy.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            conn.executescript('CREATE TABLE auxiliary(value BLOB); INSERT INTO auxiliary VALUES (x\'00ff\');')
        self.original = self.db.read_bytes()

    def test_saved_plan_cli_roundtrip_and_recovery_check(self):
        result = cli('reclaim-plan', self.db, '--out', self.artifact, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(self.artifact.read_text())
        self.assertEqual(json.loads(result.stdout), plan)
        self.assertEqual(plan['source']['database']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(plan['source']['path'], str(self.db))
        self.assertEqual(plan['row_counts']['auxiliary'], 1)
        self.assertEqual(self.artifact.stat().st_mode & 0o777, 0o600)
        copied = cli('reclaim', self.db, '--plan', self.artifact, '--out', self.out, '--json')
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertTrue(json.loads(copied.stdout)['plan_verified'])
        verified = cli('verify-copy', self.out, '--plan', self.artifact, '--json')
        self.assertEqual(verified.returncode, 0, verified.stderr)
        report = json.loads(verified.stdout)
        self.assertTrue(report['verified'])
        self.assertEqual(report['output_sha256'], hashlib.sha256(self.out.read_bytes()).hexdigest())
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))

    def test_stale_data_or_schema_refused_before_staging(self):
        for change in ["UPDATE execution_data SET data='changed' WHERE executionId=1",
                       'CREATE INDEX extra_index ON auxiliary(value)']:
            with self.subTest(change=change):
                self.db.write_bytes(self.original)
                self.artifact.unlink(missing_ok=True)
                core.reclaim_plan(self.db, out=self.artifact)
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute(change)
                    conn.commit()
                before = self.db.read_bytes()
                self.out.write_bytes(b'previous output')
                with patch('flowwrench.n8n_diet.core.staged_output', side_effect=AssertionError('must not stage')):
                    with self.assertRaisesRegex(DietError, 'stale reclamation plan'):
                        core.reclaim(self.db, out=self.out, plan_file=self.artifact, overwrite=True)
                self.assertEqual(self.out.read_bytes(), b'previous output')
                self.assertEqual(self.db.read_bytes(), before)

    def test_schema_binding_is_checked_independently(self):
        core.reclaim_plan(self.db, out=self.artifact)
        plan = json.loads(self.artifact.read_text())
        plan['schema_sha256'] = '0' * 64
        self.artifact.write_text(json.dumps(plan))
        result = cli('reclaim', self.db, '--plan', self.artifact, '--out', self.out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('stale reclamation plan', result.stderr)
        self.assertFalse(self.out.exists())

    def test_identical_database_at_another_path_requires_new_plan(self):
        core.reclaim_plan(self.db, out=self.artifact)
        moved = self.root / 'moved.sqlite'
        moved.write_bytes(self.original)
        with self.assertRaisesRegex(DietError, 'stale reclamation plan'):
            core.reclaim(moved, out=self.out, plan_file=self.artifact)
        self.assertFalse(self.out.exists())
        self.assertEqual(moved.read_bytes(), self.original)

    def test_wal_fingerprint_and_locks_survive_hashing(self):
        # SQLite's writer lives in a separate process so test-side file hashing
        # cannot drop its process-owned SHM locks by closing another fd.
        writer = subprocess.Popen([sys.executable, '-c', """
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("UPDATE workflow_entity SET name='first' WHERE id='wf-a'")
c.commit()
print('ready', flush=True)
for line in sys.stdin:
    if line.strip() != 'change':
        break
    c.execute("UPDATE workflow_entity SET name='second' WHERE id='wf-a'")
    c.commit()
    print('changed', flush=True)
c.close()
""", str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), 'ready')
            paths = [self.db, Path(str(self.db) + '-wal'), Path(str(self.db) + '-shm')]
            before = [p.read_bytes() for p in paths]
            result = cli('reclaim-plan', self.db, '--out', self.artifact)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.artifact.exists())
            result = cli('reclaim-plan', self.db, '--out', self.artifact, '--force', '--content', '--row-hash-memory-bytes', '32')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, [p.read_bytes() for p in paths])
            plan = json.loads(self.artifact.read_text())
            self.assertEqual(plan['source']['wal']['sha256'], hashlib.sha256(before[1]).hexdigest())
            # Opening/closing a second DB fd for hashing would drop POSIX locks.
            from flowwrench.n8n_diet.storage import source_database
            with source_database(self.db, force=True, fingerprint={}):
                attempt = subprocess.run([sys.executable, '-c',
                    'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                    str(self.db)], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(attempt.returncode, 0)
                self.assertIn('locked', attempt.stderr)
            result = cli('reclaim', self.db, '--out', self.out, '--plan', self.artifact, '--force', '--row-hash-memory-bytes', '32')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(core.verify_copy(self.out, plan_file=self.artifact, content=True)['content_verified'])
            self.assertEqual(before, [p.read_bytes() for p in paths])
            writer.stdin.write('change\n')
            writer.stdin.flush()
            self.assertEqual(writer.stdout.readline().strip(), 'changed')
            self.assertEqual(self.db.read_bytes(), before[0])  # Only the WAL changed.
            self.out.unlink()
            result = cli('reclaim', self.db, '--plan', self.artifact, '--out', self.out, '--force')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('stale reclamation plan', result.stderr)
            self.assertFalse(self.out.exists())
        finally:
            _, stderr = writer.communicate('stop\n', timeout=5)
            self.assertEqual(writer.returncode, 0, stderr)

    def test_rollback_writer_still_blocked_after_fingerprinting(self):
        from flowwrench.n8n_diet.storage import source_database
        with source_database(self.db, fingerprint={}):
            attempt = subprocess.run([sys.executable, '-c',
                'import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); c.execute("BEGIN IMMEDIATE")',
                str(self.db)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn('locked', attempt.stderr)

    def test_plan_refuses_source_alias_and_existing_artifact(self):
        for path in [self.db, Path(str(self.db) + '-wal')]:
            with self.assertRaises(DietError):
                core.reclaim_plan(self.db, out=path)
        core.reclaim_plan(self.db, out=self.artifact)
        original_plan = self.artifact.read_bytes()
        with self.assertRaisesRegex(DietError, 'output exists'):
            core.reclaim_plan(self.db, out=self.artifact)
        with self.assertRaises(DietError):
            core.reclaim(self.db, out=self.artifact, plan_file=self.artifact, overwrite=True)
        self.assertEqual(self.artifact.read_bytes(), original_plan)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_bad_artifact_refused_without_output(self):
        for value in [b'not json', b'[]', b'{"format":"n8n-exec-diet/reclaim-plan","version":2}',
                      b'x' * (1048576 + 1)]:
            self.artifact.write_bytes(value)
            result = cli('reclaim', self.db, '--plan', self.artifact, '--out', self.out)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(self.out.exists())
        core.reclaim_plan(self.db, out=self.root / 'valid.json')
        valid = json.loads((self.root / 'valid.json').read_text())
        valid['version'] = True  # bool must not pass as integer version 1.
        self.artifact.write_text(json.dumps(valid))
        with self.assertRaisesRegex(DietError, 'invalid reclamation plan'):
            core.reclaim(self.db, out=self.out, plan_file=self.artifact)

    def test_recovery_check_rejects_schema_and_count_mismatch(self):
        core.reclaim_plan(self.db, out=self.artifact)
        core.reclaim(self.db, out=self.out, plan_file=self.artifact)
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute('DELETE FROM auxiliary')
            conn.commit()
        with self.assertRaisesRegex(DietError, 'row count'):
            core.verify_copy(self.out, plan_file=self.artifact)
        self.out.unlink()
        core.reclaim(self.db, out=self.out, plan_file=self.artifact)
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute('CREATE INDEX unexpected ON auxiliary(value)')
            conn.commit()
        with self.assertRaisesRegex(DietError, 'schema'):
            core.verify_copy(self.out, plan_file=self.artifact)

    def test_publication_fsync_failure_reports_visible_output_for_recovery(self):
        core.reclaim_plan(self.db, out=self.artifact)
        real_fsync = os.fsync

        def fail_directory(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('injected directory fsync failure')
            return real_fsync(fd)

        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                if overwrite:
                    self.out.write_bytes(b'previous output')
                with patch('flowwrench.n8n_diet.storage.os.fsync', side_effect=fail_directory):
                    with self.assertRaisesRegex(DietError, 'published.*durability.*verify-copy'):
                        core.reclaim(self.db, out=self.out, plan_file=self.artifact, overwrite=overwrite)
                self.assertTrue(core.verify_copy(self.out, plan_file=self.artifact)['verified'])
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_prepublication_fsync_failure_preserves_previous_output(self):
        self.out.write_bytes(b'previous output')
        with patch('flowwrench.n8n_diet.storage.os.fsync', side_effect=OSError('injected file fsync failure')):
            with self.assertRaisesRegex(OSError, 'injected file'):
                with staged_output(self.out, overwrite=True) as staging:
                    staging.write_bytes(b'new output')
        self.assertEqual(self.out.read_bytes(), b'previous output')
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))


if __name__ == '__main__':
    unittest.main()
