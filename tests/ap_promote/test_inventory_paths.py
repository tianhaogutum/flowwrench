"""Inventory validates every empty path before dispatch or any file reads."""
import contextlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.ap_promote.cli import main
from flowwrench.ap_promote.core import Flow

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'
FIELDS = ('file', 'step', 'pieceName', 'pieceVersion', 'pointer', 'externalId', 'kind')


class InventoryPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='inventory-path-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.exports = [self.work / n for n in ('first.json', 'second.json')]
        self.contents = [(FIXTURES / n).read_bytes() for n in
                         ('support-flow.json', 'roundtrip-multi-source.json')]
        for path, content in zip(self.exports, self.contents):
            path.write_bytes(content)
            path.chmod(0o640)
        artifacts = self.work / 'artifacts'
        artifacts.mkdir(mode=0o750)
        for name, content, mode in (('reviewed.plan.json', b'{"prior":"CONTENT_SENTINEL"}', 0o600),
                                    ('prior.output.json', b'prior output CONTENT_SENTINEL', 0o644)):
            path = artifacts / name
            path.write_bytes(content)
            path.chmod(mode)
        (artifacts / 'empty-directory').mkdir(mode=0o700)

    def snapshot(self):
        return {p.relative_to(self.work): (p.read_bytes() if p.is_file() else None,
                                          p.stat().st_mode)
                for p in (self.work, *self.work.rglob('*'))}

    def empty_cases(self):
        for states in itertools.product(('valid', 'missing', 'malformed'), repeat=2):
            for path, content, state in zip(self.exports, self.contents, states):
                if state == 'missing':
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content if state == 'valid' else b'invalid JSON CONTENT_SENTINEL')
            first, second = map(str, self.exports)
            for position, paths in (('first', ['', first, second]),
                                    ('middle', [first, '', second]),
                                    ('last', [first, second, '']),
                                    ('multiple', [first, '', second, ''])):
                for report in ([], ['--json']):
                    yield (position, states), ['inventory', *paths, *report]
        for paths in ([''], ['', '']):
            for report in ([], ['--json']):
                yield ('only-empty', paths), ['inventory', *paths, *report]

    def assert_usage(self, out, err):
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('usage: flowwrench ap-promote inventory '), err)
        self.assertIn('argument files: expected nonempty path', err)
        for forbidden in ('CONTENT_SENTINEL', 'Traceback', 'refused:', 'error: cannot read'):
            self.assertNotIn(forbidden, err)

    def test_empty_entries_never_dispatch_read_write_or_report(self):
        for case, argv in self.empty_cases():
            before = self.snapshot()
            with self.subTest(case=case, argv=argv), \
                    patch('flowwrench.ap_promote.cli.run', return_value=0) as run, \
                    patch('flowwrench.ap_promote.cli.Flow.load') as flow_load, \
                    patch('flowwrench.ap_promote.cli.Document.load') as doc_load, \
                    patch('flowwrench.ap_promote.cli.atomic_write') as write, \
                    patch('flowwrench.ap_promote.cli.dump') as dump, \
                    patch('flowwrench.ap_promote.cli.table') as table:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as error:
                        main(argv)
                self.assertEqual(error.exception.code, 2)
                for mocked in (run, flow_load, doc_load, write, dump, table):
                    mocked.assert_not_called()
                self.assert_usage(out.getvalue(), err.getvalue())
                self.assertEqual(self.snapshot(), before)

    def cli(self, argv, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *argv],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_empty_entries_cli_preserves_artifact_bytes_modes_and_entries(self):
        for case, argv in self.empty_cases():
            before = self.snapshot()
            with self.subTest(case=case, argv=argv):
                result = self.cli(argv, 2)
                self.assert_usage(result.stdout, result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_nonempty_literal_whitespace_repeated_paths_and_record_order(self):
        for names in ((' first.json ', ' second.json '), (' ', '  ')):
            paths = [self.work / n for n in names]
            for path, content in zip(paths, self.contents):
                path.write_bytes(content)
            with patch('flowwrench.ap_promote.cli.run', return_value=0) as run:
                self.assertEqual(main(['inventory', *names, names[0]]), 0)
                self.assertEqual(run.call_args.args[0].files, [*names, names[0]])
            files = [str(paths[1]), str(paths[0]), str(paths[1])]
            expected = [dict(file=file, **r.record()) for file in files
                        for r in Flow.load(file).references]
            self.assertTrue(expected)
            before = self.snapshot()
            for report in ([], ['--json']):
                result = self.cli(['inventory', *files, *report])
                self.assertEqual(result.stderr, '')
                if report:
                    self.assertEqual(json.loads(result.stdout), expected)
                else:
                    lines = result.stdout.splitlines()
                    self.assertEqual(lines[0], '\t'.join(FIELDS))
                    self.assertEqual([[json.loads(cell) for cell in line.split('\t')]
                                      for line in lines[1:]],
                                     [[record.get(field) for field in FIELDS] for record in expected])
                self.assertEqual(self.snapshot(), before)

    def test_omitted_missing_and_malformed_paths_keep_safe_no_partial_diagnostics(self):
        for report in ([], ['--json']):
            result = self.cli(['inventory', *report], 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('the following arguments are required: files', result.stderr)
        invalid = self.work / 'invalid.json'
        for state in ('missing', 'malformed'):
            if state == 'malformed':
                invalid.write_bytes(b'invalid JSON CONTENT_SENTINEL')
            expected = ('error: cannot read UTF-8 input (FileNotFoundError)' if state == 'missing'
                        else 'error: invalid JSON at offset 0')
            for files in ([str(invalid), str(self.exports[0])],
                          [str(self.exports[0]), str(invalid)]):
                before = self.snapshot()
                for report in ([], ['--json']):
                    result = self.cli(['inventory', *files, *report], 2)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, expected + '\n')
                    self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
