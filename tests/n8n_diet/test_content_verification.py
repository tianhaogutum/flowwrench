"""Content-copy contracts: typed row multisets, independent of physical rowids."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_diet import core
from flowwrench.n8n_diet.storage import DietError

ROOT = Path(__file__).resolve().parent


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'flowwrench.n8n_diet', *map(str, args)],
                          cwd=ROOT, text=True, capture_output=True, timeout=30)


class ContentVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.sqlite'
        self.artifact = self.root / 'plan.json'
        self.out = self.root / 'copy.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / 'fixtures/split-schema.sql').read_text())
            # No column affinities: integer, real, text and blob remain distinct.
            # Gapped implicit rowids may change on VACUUM; they are not content.
            conn.executescript('''
                CREATE TABLE bag(a, b);
                INSERT INTO bag(rowid,a,b) VALUES
                    (11, NULL, x'00ff'), (23, NULL, x'00ff'),
                    (40, 'é', x'ff0080'), (60, 1, '1'), (70, 1.0, x'31');
                CREATE TABLE empty(value);
                CREATE TABLE keyed(id INTEGER PRIMARY KEY, value BLOB);
                INSERT INTO keyed VALUES (91, x'00ff');
                CREATE TABLE "odd\""table"("odd\""column" TEXT, value BLOB,
                    size INT GENERATED ALWAYS AS (length(value)) STORED);
                INSERT INTO "odd\""table" VALUES ('text', x'0080');
                CREATE TABLE ordered(k TEXT PRIMARY KEY, value) WITHOUT ROWID;
                INSERT INTO ordered VALUES ('z', 2), ('a', 1);
            ''')
        self.original = self.db.read_bytes()

    def save_and_copy(self):
        core.reclaim_plan(self.db, out=self.artifact, content=True)
        return core.reclaim(self.db, out=self.out, plan_file=self.artifact)

    def mutate_copy(self, sql):
        with closing(sqlite3.connect(self.out)) as conn:
            conn.execute(sql)
            conn.commit()

    def test_cli_content_roundtrip_survives_vacuum_rowids(self):
        saved = cli('reclaim-plan', self.db, '--out', self.artifact, '--content', '--json')
        self.assertEqual(saved.returncode, 0, saved.stderr)
        artifact = json.loads(saved.stdout)
        self.assertEqual(artifact['version'], 2)
        self.assertEqual(artifact['content']['algorithm'], 'sha256-typed-rows-v1')
        self.assertEqual(set(artifact['content']['tables']), set(artifact['row_counts']))
        copied = cli('reclaim', self.db, '--out', self.out, '--plan', self.artifact, '--json')
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertTrue(json.loads(copied.stdout)['content_verified'])
        with closing(sqlite3.connect(self.out)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM bag').fetchone()[0], 5)
            self.assertEqual(conn.execute('SELECT id FROM keyed').fetchone()[0], 91)
        # Verification no longer needs the original DB, only the trusted plan.
        self.db.unlink()
        for flags in [[], ['--content']]:
            result = cli('verify-copy', self.out, '--plan', self.artifact, '--json', *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['content_verified'])
            self.assertEqual(report['content'], artifact['content'])
            self.assertEqual(report['output_sha256'], hashlib.sha256(self.out.read_bytes()).hexdigest())
            self.assertNotIn('é', result.stdout)
        human = cli('verify-copy', self.out, '--plan', self.artifact, '--content')
        self.assertIn('row content', human.stdout)

    def test_same_count_content_changes_are_rejected_without_flag(self):
        self.save_and_copy()
        pristine = self.out.read_bytes()
        changes = [
            "UPDATE bag SET b=x'ff0081' WHERE a='é'",  # BLOB byte change
            "UPDATE bag SET a='different' WHERE a='é'",  # TEXT change
            "UPDATE bag SET a='1' WHERE typeof(a)='integer'",  # type only
            "UPDATE bag SET a=1.0 WHERE typeof(a)='integer'",  # INTEGER vs REAL
            "UPDATE bag SET a=x'31' WHERE typeof(a)='integer'",  # TEXT/BLOB distinct
            "UPDATE keyed SET id=92",  # explicit INTEGER PRIMARY KEY is content
            "UPDATE bag SET a='' WHERE a IS NULL",  # NULL vs empty
        ]
        for change in changes:
            with self.subTest(change=change):
                self.out.write_bytes(pristine)
                self.mutate_copy(change)
                before = self.out.read_bytes()
                result = cli('verify-copy', self.out, '--plan', self.artifact, '--json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('content verification failed', result.stderr)
                self.assertNotIn('different', result.stderr)
                self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_duplicate_multiplicity_changes_with_same_set_and_count_fail(self):
        self.save_and_copy()
        # A,A,B -> A,B,B leaves the set of distinct rows and total count equal.
        self.mutate_copy("UPDATE bag SET a='é', b=x'ff0080' WHERE rowid=(SELECT min(rowid) FROM bag WHERE a IS NULL)")
        with self.assertRaisesRegex(DietError, 'content verification failed'):
            core.verify_copy(self.out, plan_file=self.artifact)

    def test_row_order_and_implicit_rowid_changes_are_ignored(self):
        self.save_and_copy()
        self.mutate_copy('UPDATE bag SET rowid=rowid+100')
        self.mutate_copy('UPDATE bag SET rowid=200-rowid')
        self.assertTrue(core.verify_copy(self.out, plan_file=self.artifact, content=True)['content_verified'])

    def test_known_digest_vectors_and_empty_table(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''DELETE FROM bag;
                INSERT INTO bag VALUES (NULL, x'00ff'), (NULL, x'00ff'), (-1, 'é');''')
        artifact = core.reclaim_plan(self.db, out=self.artifact, content=True, row_hash_memory_bytes=32)
        # Independent vectors encoded from the documented byte format, including
        # the duplicate row hash twice. Not generated through the code under test.
        self.assertEqual(artifact['content']['tables']['bag'],
                         '240c800407dd29250026764feaa98de30cfbacf59d090b4ca6f8ecac61675358')
        self.assertEqual(artifact['content']['tables']['empty'],
                         'b7b0ec667b1e86d7dd2f959beebca47bf6edba53ece706bad24c5bb5bca44bd9')

    def test_raw_text_binary_boundaries_and_numeric_precision(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''INSERT INTO bag VALUES
                ('ab', 'c'), ('a', 'bc'), ('', x''), (NULL, NULL),
                (CAST(x'ff0080' AS TEXT), x'ff0080'),
                (-9223372036854775808, 9223372036854775807),
                (1.0000000000000002, 1e999);''')
        self.save_and_copy()
        self.assertTrue(core.verify_copy(self.out, plan_file=self.artifact)['content_verified'])
        pristine = self.out.read_bytes()
        for change in ["UPDATE bag SET a='a', b='bc' WHERE a='ab'",
                       "UPDATE bag SET a=NULL WHERE a=''",
                       "UPDATE bag SET a=x'ff0080' WHERE a=CAST(x'ff0080' AS TEXT)",
                       "UPDATE bag SET a=1.0 WHERE a=1.0000000000000002",
                       "UPDATE bag SET b=9223372036854775806 WHERE b=9223372036854775807"]:
            with self.subTest(change=change):
                self.out.write_bytes(pristine)
                self.mutate_copy(change)
                with self.assertRaisesRegex(DietError, 'content verification failed'):
                    core.verify_copy(self.out, plan_file=self.artifact)

    def test_v1_plan_compatibility_and_explicit_content_requirement(self):
        saved = core.reclaim_plan(self.db, out=self.artifact)
        self.assertEqual(saved['version'], 1)
        self.assertNotIn('content', saved)
        copied = core.reclaim(self.db, out=self.out, plan_file=self.artifact)
        self.assertFalse(copied['content_verified'])
        self.mutate_copy("UPDATE bag SET b=x'01' WHERE a IS NULL")
        self.assertFalse(core.verify_copy(self.out, plan_file=self.artifact)['content_verified'])
        result = cli('verify-copy', self.out, '--plan', self.artifact, '--content', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('plan has no content digests', result.stderr)

    def test_invalid_content_artifacts_are_refused(self):
        core.reclaim_plan(self.db, out=self.artifact, content=True)
        valid = json.loads(self.artifact.read_text())
        invalid = []
        for replacement in [None, {}, {'algorithm': 'unknown', 'tables': valid['content']['tables']},
                            {'algorithm': 'sha256-typed-rows-v1', 'tables': {'bag': '0' * 64}}]:
            candidate = dict(valid, content=replacement)
            invalid.append(candidate)
        bad_digest = json.loads(json.dumps(valid))
        bad_digest['content']['tables']['bag'] = 'not a digest'
        invalid.append(bad_digest)
        invalid.append(dict(valid, version=1))
        missing = dict(valid)
        del missing['content']
        invalid.append(missing)
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                self.artifact.write_text(json.dumps(candidate))
                result = cli('reclaim', self.db, '--out', self.out, '--plan', self.artifact)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid reclamation plan', result.stderr)
                self.assertFalse(self.out.exists())

    def test_wrong_digest_plan_refused_before_staging(self):
        core.reclaim_plan(self.db, out=self.artifact, content=True)
        artifact = json.loads(self.artifact.read_text())
        artifact['content']['tables']['bag'] = '0' * 64
        self.artifact.write_text(json.dumps(artifact))
        self.out.write_bytes(b'previous output')
        with patch('flowwrench.n8n_diet.core.staged_output', side_effect=AssertionError('must not stage')):
            with self.assertRaisesRegex(DietError, 'stale reclamation plan'):
                core.reclaim(self.db, out=self.out, plan_file=self.artifact, overwrite=True)
        self.assertEqual(self.out.read_bytes(), b'previous output')

    def test_content_checked_before_publication(self):
        core.reclaim_plan(self.db, out=self.artifact, content=True)
        self.out.write_bytes(b'previous output')
        real_verify = core.verify_structure

        def tamper_then_verify(conn, **kwargs):
            # Alter the private staging DB using another connection before checks.
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("UPDATE bag SET b=x'01' WHERE a IS NULL")
                writer.commit()
            return real_verify(conn, **kwargs)

        with patch('flowwrench.n8n_diet.core.verify_structure', side_effect=tamper_then_verify):
            with self.assertRaisesRegex(DietError, 'content verification failed'):
                core.reclaim(self.db, out=self.out, plan_file=self.artifact, overwrite=True)
        self.assertEqual(self.out.read_bytes(), b'previous output')
        self.assertFalse(list(self.root.glob('.n8n-exec-diet-*')))


if __name__ == '__main__':
    unittest.main()
