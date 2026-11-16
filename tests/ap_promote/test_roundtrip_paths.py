"""Empty roundtrip exports fail before dispatch; nonempty paths remain literal."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.ap_promote.cli import main
from flowwrench.ap_promote.core import Flow, mappings
from flowwrench.ap_promote.jsondoc import Document
from flowwrench.ap_promote.promotion import create_plan

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'


class RoundtripPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='roundtrip-path-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.source, self.destination, self.mapping = [self.work / n for n in
            ('source.json', 'destination.json', 'map.json')]
        for path, name in ((self.source, 'source'), (self.destination, 'reexport'),
                           (self.mapping, 'map')):
            path.write_bytes((FIXTURES / f'roundtrip-multi-{name}.json').read_bytes())
        _, plan = create_plan(Flow.load(FIXTURES / 'support-flow.json'),
                              mappings([], [Document.load(FIXTURES / 'map.json')]),
                              Document.load(FIXTURES / 'target-connections-versioned.json'))
        (self.work / 'reviewed.plan.json').write_text(json.dumps(plan))
        (self.work / 'prior.output.json').write_bytes(b'prior output CONTENT_SENTINEL')

    def snapshot(self):
        return {p.relative_to(self.work): (p.read_bytes() if p.is_file() else None,
                                         p.stat().st_mode) for p in self.work.rglob('*')}

    def cases(self):
        original = {p: p.read_bytes() for p in self.work.iterdir()}
        for empty in ('source', 'destination', 'both'):
            other = self.destination if empty == 'source' else self.source
            for export_state in (('valid',) if empty == 'both' else ('valid', 'missing', 'malformed')):
                for map_state in ('valid', 'missing', 'malformed', 'omitted', 'invalid-inline'):
                    for p, content in original.items():
                        p.write_bytes(content)
                    for state, path in ((export_state, other), (map_state, self.mapping)):
                        if state == 'missing':
                            path.unlink()
                        elif state == 'malformed':
                            path.write_bytes(b'invalid JSON CONTENT_SENTINEL')
                    paths = ['' if empty in ('source', 'both') else str(self.source),
                             '' if empty in ('destination', 'both') else str(self.destination)]
                    flags = ([] if map_state == 'omitted' else
                             ['--map', 'not-a-mapping'] if map_state == 'invalid-inline' else
                             ['--map-file', str(self.mapping)])
                    for paired in (False, True):
                        for report in ([], ['--json']):
                            pairs = ['--flow-pair=1=0', '--flow-pair', '0=1'] if paired else []
                            yield empty, export_state, map_state, ['verify-roundtrip', *paths, *flags, *pairs, *report]

    def assert_usage(self, out, err, empty):
        name = 'destination' if empty == 'destination' else 'source'
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('usage: flowwrench ap-promote verify-roundtrip '), err)
        self.assertIn(f'argument {name}: expected nonempty path', err)
        self.assertNotIn('CONTENT_SENTINEL', err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('refused:', err)

    def test_empty_exports_never_dispatch_read_write_or_report(self):
        for empty, export_state, map_state, argv in self.cases():
            before = self.snapshot()
            with self.subTest(empty=empty, export=export_state, mapping=map_state, argv=argv), \
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
                self.assert_usage(out.getvalue(), err.getvalue(), empty)
                self.assertEqual(self.snapshot(), before)

    def test_empty_exports_cli_preserves_inputs_artifacts_output_and_directories(self):
        for empty, export_state, map_state, argv in self.cases():
            before = self.snapshot()
            with self.subTest(empty=empty, export=export_state, mapping=map_state, argv=argv):
                result = self.cli(argv, 2)
                self.assert_usage(result.stdout, result.stderr, empty)
                self.assertEqual(self.snapshot(), before)

    def cli(self, argv, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *argv],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_nonempty_paths_are_literal_including_whitespace_filenames(self):
        for names in ((' source.json ', ' destination.json '), (' ', '  ')):
            source, destination = [self.work / name for name in names]
            source.write_bytes(self.source.read_bytes())
            destination.write_bytes(self.destination.read_bytes())
            # Relative filenames prove all-whitespace values reach the command verbatim.
            with patch('flowwrench.ap_promote.cli.run', return_value=0) as run:
                self.assertEqual(main(['verify-roundtrip', *names, '--map-file', str(self.mapping)]), 0)
                self.assertEqual((run.call_args.args[0].source, run.call_args.args[0].destination), names)
            before = self.snapshot()
            for report in ([], ['--json']):
                result = self.cli(['verify-roundtrip', str(source), str(destination),
                                   '--map-file', str(self.mapping), '--flow-pair=1=0',
                                   '--flow-pair=0=1', *report])
                if report:
                    self.assertEqual(json.loads(result.stdout)['verified'], 8)
                else:
                    self.assertIn('8/8 source references verified; 0 errors', result.stdout)
                self.assertEqual(self.snapshot(), before)

    def test_omitted_and_nonempty_invalid_inputs_keep_existing_diagnostics(self):
        for paths, required in (([], 'source, destination'), ([str(self.source)], 'destination')):
            result = self.cli(['verify-roundtrip', *paths, '--json'], 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('the following arguments are required: ' + required, result.stderr)
        for path in (self.source, self.destination, self.mapping):
            original = path.read_bytes()
            for state in ('missing', 'malformed'):
                if state == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(b'invalid JSON CONTENT_SENTINEL')
                before = self.snapshot()
                for report in ([], ['--json']):
                    result = self.cli(['verify-roundtrip', str(self.source), str(self.destination),
                                       '--map-file', str(self.mapping), *report], 2)
                    self.assertEqual(result.stdout, '')
                    expected = ('error: cannot read UTF-8 input (FileNotFoundError)' if state == 'missing'
                                else 'error: invalid JSON at offset 0')
                    self.assertEqual(result.stderr, expected + '\n')
                    self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
