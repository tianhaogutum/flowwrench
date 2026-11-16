"""Empty pairing occurrences fail before dispatch; nonempty pairs stay literal."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.ap_promote.cli import main, parser
from flowwrench.ap_promote.core import Flow, mappings
from flowwrench.ap_promote.jsondoc import Document
from flowwrench.ap_promote.promotion import create_plan

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'


class FlowPairOptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='pair-options-ap-')
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.source, self.destination, self.mapping = [self.work / n for n in
            ('source.json', 'destination.json', 'map.json')]
        for path, name in ((self.source, 'source'), (self.destination, 'reexport'),
                           (self.mapping, 'map')):
            path.write_bytes((FIXTURES / f'roundtrip-multi-{name}.json').read_bytes())
        # Keep a genuine reviewed plan and prior output next to the inputs.
        _, plan = create_plan(Flow.load(FIXTURES / 'support-flow.json'),
                              mappings([], [Document.load(FIXTURES / 'map.json')]),
                              Document.load(FIXTURES / 'target-connections-versioned.json'))
        (self.work / 'reviewed.plan.json').write_text(json.dumps(plan))
        (self.work / 'prior.output.json').write_bytes(b'prior output CONTENT_SENTINEL')

    def snapshot(self):
        return {p: (p.read_bytes(), p.stat().st_mode) for p in self.work.rglob('*')}

    def cases(self):
        original = self.snapshot()
        variants = [('valid', None), ('no-mapping', None)] + [
            (kind, path) for path in (self.source, self.destination, self.mapping)
            for kind in ('missing', 'malformed')]
        for kind, path in variants:
            for p, (content, _) in original.items():
                p.write_bytes(content)
            if kind == 'missing':
                path.unlink()
            elif kind == 'malformed':
                path.write_bytes(b'invalid JSON CONTENT_SENTINEL')
            valid = ['--flow-pair', '1=0', '--flow-pair=0=1']
            for empty in (['--flow-pair', ''], ['--flow-pair=']):
                for flags in (empty, [*empty, *valid], [*valid, *empty]):
                    for report in ([], ['--json']):
                        argv = ['verify-roundtrip', str(self.source), str(self.destination)]
                        if kind != 'no-mapping':
                            argv += ['--map-file', str(self.mapping)]
                        yield kind, path, [*argv, *flags, *report]

    def assert_usage(self, out, err):
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('usage: flowwrench ap-promote verify-roundtrip '), err)
        self.assertIn('argument --flow-pair: expected nonempty pairing', err)
        self.assertNotIn('CONTENT_SENTINEL', err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('refused:', err)

    def test_empty_pairs_never_dispatch_read_write_or_report(self):
        for kind, path, argv in self.cases():
            before = self.snapshot()
            with self.subTest(kind=kind, path=path, argv=argv), \
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

    def test_empty_pairs_cli_preserves_inputs_reviewed_artifact_and_output(self):
        for kind, path, argv in self.cases():
            before = self.snapshot()
            with self.subTest(kind=kind, path=path, argv=argv):
                result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *argv],
                                        cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assert_usage(result.stdout, result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_nonempty_pairs_dispatch_literal_repeated_values_and_default_none(self):
        base = ['verify-roundtrip', str(self.source), str(self.destination),
                '--map-file', str(self.mapping)]
        self.assertIsNone(parser().parse_args(base).flow_pair)
        values = ['1=0', '0=1', '1=0', ' ', ' 0=1', '0=1 ', '00=1', '0=١', '0=', '=1']
        flags = [arg for i, value in enumerate(values) for arg in
                 (['--flow-pair', value] if i % 2 else ['--flow-pair=' + value])]
        with patch('flowwrench.ap_promote.cli.run', return_value=0) as run:
            self.assertEqual(main([*base, *flags]), 0)
            self.assertEqual(run.call_args.args[0].flow_pair, values)


if __name__ == '__main__':
    unittest.main()
