"""Load failure codes describe originating boundaries, never message text."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.n8n_refs.cli import main, sarif
from flowwrench.n8n_refs.core import MAX_BYTES, load_workflow, WorkflowError
from .helpers import TEST_TMP, node, workflow


class LoadCategoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = workflow([node('Use')])
        self.paths = {}
        self.codes = {}
        cases = {
            'clean': (self.data, None),
            'hazard': (workflow([node('Use', "={{ $items('Ghost') }}")]), None),
            'incomplete': (workflow([node('Use', '={{ $(dynamic) }}')]), None),
            'identity': (workflow([node('Use', id=None)]), None),
            'shape': ([], 'invalid-workflow'),
            'empty': (workflow([]), 'invalid-workflow'),
            'node': ({**self.data, 'nodes': [{}]}, 'invalid-workflow'),
            'nodes-limit': ({**self.data, 'nodes': [node('Use')] * 2001}, 'input-limit'),
        }
        edge = {'node': 'Use', 'type': 'main', 'index': 0}
        cases['edges-limit'] = ({**self.data, 'connections': {'Use': {'main': [[edge] * 20001]}}}, 'input-limit')
        deep = deepcopy(self.data)
        value = 0
        for _ in range(101):
            value = [value]
        deep['extra'] = value
        cases['depth'] = (deep, 'input-limit')
        for name, (data, code) in cases.items():
            self.write(name, json.dumps(data).encode(), code)
        for name, raw, code in [
            ('utf8', b'{"private":"secret-source-text\xff"}', 'invalid-utf8'),
            ('json', b'{"private": secret-source-text', 'invalid-json'),
            ('duplicate', b'{"private":1,"private":2}', 'invalid-json'),
            ('nonfinite', b'{"private":NaN}', 'invalid-json'),
            ('recursion', json.dumps(self.data)[:-1].encode() + b',"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}', 'input-limit'),
            ('size', b'\xff' * (MAX_BYTES + 1), 'input-limit'),
        ]:
            self.write(name, raw, code)
        self.paths['missing'] = self.root / 'missing # 雪.json'
        self.codes['missing'] = 'file-not-found'
        self.paths['directory'] = self.root
        self.codes['directory'] = 'file-read'

    def write(self, name, raw, code):
        path = self.root / (name + ' # 雪.json')
        path.write_bytes(raw)
        self.paths[name], self.codes[name] = path, code

    def cli(self, args, format):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*map(str, args), '--format', format])
        self.assertEqual(err.getvalue(), '')
        self.assertNotIn('secret-source-text', out.getvalue())
        return code, json.loads(out.getvalue())

    def test_mixed_check_codes_and_duplicate_order(self):
        names = ['missing', 'hazard', 'utf8', 'clean', 'json', 'shape', 'missing',
                 'empty', 'node', 'duplicate', 'nonfinite', 'nodes-limit',
                 'edges-limit', 'depth', 'recursion', 'size', 'directory']
        failures = [n for n in names if self.codes[n]]
        code, document = self.cli(['check', *(self.paths[n] for n in names)], 'json')
        self.assertEqual(code, 2)
        self.assertEqual([e['category'] for e in document['errors']], [self.codes[n] for n in failures])
        self.assertEqual([e['file'] for e in document['errors']], [str(self.paths[n]) for n in failures])
        self.assertTrue(all(set(e) == {'file', 'message', 'category'} for e in document['errors']))
        self.assertEqual(len(document['reports']), 2)
        code, sarif_doc = self.cli(['check', *(self.paths[n] for n in names),
                                  '--sarif-source-root', self.root], 'sarif')
        self.assertEqual(code, 2)
        run = sarif_doc['runs'][0]
        notices = run['invocations'][0]['toolExecutionNotifications']
        for n, error, notice in zip(failures, document['errors'], notices):
            self.assertEqual(notice['properties'], {'category': self.codes[n]})
            self.assertEqual(notice['message']['text'], error['file'] + ': ' + error['message'])
            self.assertEqual(set(notice['locations'][0]['physicalLocation']), {'artifactLocation'})
        self.assertEqual(len(notices), len(failures))
        self.assertIn('partialFingerprints', run['results'][0])
        self.assertFalse(run['invocations'][0]['executionSuccessful'])

    def test_every_failure_both_comparison_sides_and_same_path(self):
        for name, category in self.codes.items():
            if category is None:
                continue
            for names in ((name, 'clean'), ('clean', name), (name, name)):
                with self.subTest(names=names):
                    expected_sides = [s for s, n in zip(('baseline', 'candidate'), names) if self.codes[n]]
                    for format in ('json', 'sarif'):
                        code, doc = self.cli(['compare', *(self.paths[n] for n in names), '--fail-on-new'], format)
                        self.assertEqual(code, 2)
                        if format == 'json':
                            self.assertIsNone(doc['comparison'])
                            self.assertEqual([e['side'] for e in doc['errors']], expected_sides)
                            self.assertEqual([e['category'] for e in doc['errors']], [category] * len(expected_sides))
                        else:
                            run = doc['runs'][0]
                            notices = run['invocations'][0]['toolExecutionNotifications']
                            self.assertEqual([n['properties'] for n in notices], [{'category': category}] * len(expected_sides))
                            self.assertEqual([n['message']['text'].split(' ', 1)[0] for n in notices], expected_sides)
                            for side in expected_sides:
                                self.assertEqual(run['properties']['fingerprintEligibility']['sides'][side]['reasonCodes'], ['load-error'])

    def test_precedence_and_original_messages_at_load_boundary(self):
        # Shape precedes depth; node count precedes depth; depth precedes node
        # fields; the exceeding edge limit precedes invalid edge shape.
        deep = json.loads(self.paths['depth'].read_text())['extra']
        edge = {'node': 'Use', 'type': 'main', 'index': 0}
        for data, expected in [
            ({'extra': deep}, 'invalid-workflow'),
            ({**self.data, 'nodes': [node('Use')] * 2001, 'extra': deep}, 'input-limit'),
            ({**self.data, 'nodes': [{}], 'extra': deep}, 'input-limit'),
            ({**self.data, 'connections': {'Use': {'main': [[edge] * 20000 + [None]]}}}, 'input-limit'),
        ]:
            self.write('precedence', json.dumps(data).encode(), expected)
            with self.assertRaises(WorkflowError) as caught:
                load_workflow(self.paths['precedence'])
            self.assertEqual(caught.exception.category, expected)
        for name in ('utf8', 'json'):
            with self.assertRaises(WorkflowError) as caught:
                load_workflow(self.paths[name])
            self.assertEqual(str(caught.exception), 'invalid UTF-8 JSON or excessive nesting')
            self.assertEqual(caught.exception.category, self.codes[name])

    def test_parser_recursion_category_preserves_original_message(self):
        # Python 3.14 can parse deeper arrays than 3.12; force the exception at
        # its originating boundary rather than assuming a platform stack size.
        with patch('flowwrench.n8n_refs.core.json.loads', side_effect=RecursionError):
            with self.assertRaises(WorkflowError) as caught:
                load_workflow(self.paths['clean'])
        self.assertEqual(caught.exception.category, 'input-limit')
        self.assertEqual(str(caught.exception), 'invalid UTF-8 JSON or excessive nesting')

    def test_read_errors_classified_by_type_not_localized_message(self):
        for exception, expected in [(FileNotFoundError, 'file-not-found'),
                                    (PermissionError, 'permission-denied'), (OSError, 'file-read')]:
            with patch('flowwrench.n8n_refs.core.Path.open', side_effect=exception(5, 'same localized text', 'secret-source-text')):
                for command in (['check', self.paths['clean']], ['compare', self.paths['clean'], self.paths['clean']]):
                    code, doc = self.cli(command, 'json')
                    self.assertEqual(code, 2)
                    self.assertTrue(all(e['category'] == expected and e['message'] == 'same localized text' for e in doc['errors']))

    def test_pathless_and_healthy_controls_have_no_category(self):
        for name in ('identity', 'incomplete'):
            for names in ((name, 'clean'), ('clean', name), (name, name)):
                for format in ('json', 'sarif'):
                    code, doc = self.cli(['compare', *(self.paths[n] for n in names)], format)
                    self.assertEqual(code, 2)
                    errors = doc['errors'] if format == 'json' else doc['runs'][0]['invocations'][0]['toolExecutionNotifications']
                    self.assertEqual(len(errors), 1)
                    self.assertNotIn('category', errors[0])
                    self.assertNotIn('properties', errors[0])
        for command, expected in [(['check', self.paths['clean']], 0), (['check', self.paths['hazard']], 1),
                                  (['compare', self.paths['clean'], self.paths['hazard']], 0),
                                  (['compare', self.paths['clean'], self.paths['hazard'], '--fail-on-new'], 1)]:
            for format in ('json', 'sarif'):
                code, doc = self.cli(command, format)
                self.assertEqual(code, expected)
                errors = doc['errors'] if format == 'json' else doc['runs'][0]['invocations'][0]['toolExecutionNotifications']
                self.assertEqual(errors, [])
        notices = sarif([], [{'file': 'untyped', 'message': 'unknown'}, {'message': 'pathless'}])['runs'][0]['invocations'][0]['toolExecutionNotifications']
        self.assertTrue(all('properties' not in n for n in notices))
        # An analysis failure after a successful load must not masquerade as a
        # categorized load failure, even when it mentions a CLI file.
        with patch('flowwrench.n8n_refs.cli.analyze', side_effect=ValueError('analysis error')):
            code, doc = self.cli(['check', self.paths['clean']], 'json')
            self.assertEqual(code, 2)
            self.assertNotIn('category', doc['errors'][0])


if __name__ == '__main__':
    unittest.main()
