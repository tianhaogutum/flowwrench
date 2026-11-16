"""File-specific failures are navigable without guessing source regions or sides."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from flowwrench.n8n_refs.cli import main, sarif
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent


class FailureLocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = self.directory / 'checkout # 雪'
        self.root.mkdir()
        self.paths = {}
        for name, data in {
            'clean': workflow([node('Use')]),
            'hazard': workflow([node('Use', "={{ $items('Ghost') }}")]),
            'incomplete': workflow([node('Use', '={{ $(dynamic) }}')]),
            'identity': workflow([node('Use', id=None)]),
            'structure': [],
        }.items():
            path = self.root / (name + ' # %:é.json')
            path.write_text(json.dumps(data))
            self.paths[name] = path
        self.paths['missing'] = self.root / 'missing # %:雪.json'
        bad = self.root / 'bad.json'
        bad.write_text('{"secret": secret-source-text')
        self.paths['malformed'] = bad
        self.paths['directory'] = self.root

    def cli(self, arguments, root=None, cwd=ROOT):
        command = [sys.executable, '-m', 'flowwrench.n8n_refs', *map(str, arguments), '--format', 'sarif']
        if root is not None:
            command += ['--sarif-source-root', str(root)]
        result = subprocess.run(command, cwd=cwd, env=dict(os.environ, PYTHONPATH=str(ROOT.parents[1] / "src")),
                                capture_output=True, timeout=15)
        self.assertEqual(result.stderr, b'')
        run = json.loads(result.stdout)['runs'][0]
        self.assertNotIn('secret-source-text', result.stdout.decode())
        return result.returncode, run

    def assert_location(self, notification, file, root=None):
        # Independent shape/escaping oracle; equality excludes invented regions,
        # excerpts, logical pointers and role/identity metadata.
        path = Path(os.path.abspath(file)) if root is not None else Path(file).absolute()
        artifact = {'uri': path.as_uri()}
        if root is not None:
            try:
                relative = path.relative_to(root)
            except ValueError:
                pass
            else:
                artifact = {'uri': quote(relative.as_posix(), safe='/') if relative.parts else './',
                            'uriBaseId': 'SRCROOT'}
        self.assertEqual(notification['locations'], [{'physicalLocation': {'artifactLocation': artifact}}])
        self.assertEqual(set(notification), {'level', 'message', 'locations', 'properties'})
        self.assertEqual(notification['level'], 'error')

    def test_check_mixed_errors_keep_duplicate_argument_order_and_messages(self):
        p = self.paths
        names = ['missing', 'hazard', 'malformed', 'clean', 'structure', 'missing', 'directory']
        messages = ['No such file or directory', 'invalid UTF-8 JSON or excessive nesting',
                    'expected one workflow object with nodes array and connections object',
                    'No such file or directory', 'Is a directory']
        failures = [p[n] for n in names if n not in ('clean', 'hazard')]
        for root in (None, self.root):
            with self.subTest(root=root):
                code, run = self.cli(['check', *(p[n] for n in names)], root)
                self.assertEqual(code, 2)
                self.assertFalse(run['invocations'][0]['executionSuccessful'])
                notifications = run['invocations'][0]['toolExecutionNotifications']
                self.assertEqual([n['message']['text'] for n in notifications],
                                 [f'{file}: {message}' for file, message in zip(failures, messages)])
                self.assertEqual(len(run['results']), 1)
                self.assertIn('partialFingerprints', run['results'][0])
                for notification, file in zip(notifications, failures):
                    self.assert_location(notification, file, root)

    def test_compare_each_load_error_on_both_sides_and_same_path_roles(self):
        p = self.paths
        for name in ('missing', 'malformed', 'structure', 'directory'):
            for side in ('baseline', 'candidate'):
                for root in (None, self.root):
                    with self.subTest(name=name, side=side, root=root):
                        paths = [p[name], p['hazard']] if side == 'baseline' else [p['hazard'], p[name]]
                        code, run = self.cli(['compare', *paths, '--fail-on-new'], root)
                        self.assertEqual(code, 2)
                        notifications = run['invocations'][0]['toolExecutionNotifications']
                        self.assertEqual(len(notifications), 1)
                        self.assertTrue(notifications[0]['message']['text'].startswith(f'{side} {p[name]}: '))
                        self.assert_location(notifications[0], p[name], root)
                        sides = run['properties']['fingerprintEligibility']['sides']
                        self.assertEqual(sides[side]['reasonCodes'], ['load-error'])
                        self.assertTrue(sides['candidate' if side == 'baseline' else 'baseline']['eligible'])
        for left, right in ((p['missing'], p['malformed']), (p['missing'], p['missing'])):
            code, run = self.cli(['compare', left, right], self.root)
            self.assertEqual(code, 2)
            notifications = run['invocations'][0]['toolExecutionNotifications']
            self.assertEqual(len(notifications), 2)
            for n, side, file in zip(notifications, ('baseline', 'candidate'), (left, right)):
                self.assertTrue(n['message']['text'].startswith(f'{side} {file}: '))
                self.assert_location(n, file, self.root)

    def test_pathless_identity_and_incomplete_errors_never_guess_a_side(self):
        p = self.paths
        for name in ('identity', 'incomplete'):
            for left, right in ((p[name], p['hazard']), (p['hazard'], p[name]), (p[name], p[name])):
                for root in (None, self.root):
                    with self.subTest(name=name, left=left, right=right, root=root):
                        code, run = self.cli(['compare', left, right], root)
                        self.assertEqual(code, 2)
                        notification, = run['invocations'][0]['toolExecutionNotifications']
                        self.assertEqual(set(notification), {'level', 'message'})
                        self.assertTrue(all('partialFingerprints' not in r and 'baselineState' not in r
                                            for r in run['results']))
        document = sarif([], [{'message': 'pathless'}, {'side': 'baseline', 'message': 'still pathless'}])
        self.assertTrue(all('locations' not in n for n in
                            document['runs'][0]['invocations'][0]['toolExecutionNotifications']))

    def test_lexical_symlinks_outside_root_relative_and_escaped_paths(self):
        outside = self.directory / 'checkout # 雪-sibling' / 'outside # %:雪.json'
        outside.parent.mkdir()
        outside.write_text('[]')
        link = self.root / 'link # 雪.json'
        link.symlink_to(outside)
        dangling = self.root / 'dangling.json'
        dangling.symlink_to(self.directory / 'absent.json')
        alias = self.directory / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for root, file in ((self.root, outside), (self.root, link), (self.root, dangling),
                           (alias, alias / link.name), (self.root, alias / link.name), (self.root, self.root)):
            with self.subTest(root=root, file=file):
                code, run = self.cli(['check', file], root)
                self.assertEqual(code, 2)
                self.assert_location(run['invocations'][0]['toolExecutionNotifications'][0], file, root)
        for root in (None, self.root):
            raw_path = 'checkout # 雪/../checkout # 雪/missing # %:雪.json'
            code, run = self.cli(['check', raw_path], root, self.directory)
            self.assertEqual(code, 2)
            expected_path = str(self.directory) + '/' + raw_path
            self.assert_location(run['invocations'][0]['toolExecutionNotifications'][0], expected_path, root)
            self.assertTrue(run['invocations'][0]['toolExecutionNotifications'][0]['message']['text'].startswith(raw_path + ': '))

    def test_permission_error_has_real_input_location_without_raw_exception(self):
        # Deterministic even under root; the installed experiment also exercises
        # real filesystem unreadability as an unprivileged caller.
        file = str(self.paths['clean'])
        for command in (['check', file], ['compare', file, file]):
            output, error = StringIO(), StringIO()
            with patch('flowwrench.n8n_refs.core.Path.open', side_effect=PermissionError(13, 'Permission denied', 'secret-source-text')):
                with redirect_stdout(output), redirect_stderr(error):
                    code = main([*command, '--format', 'sarif', '--sarif-source-root', str(self.root)])
            self.assertEqual((code, error.getvalue()), (2, ''))
            self.assertNotIn('secret-source-text', output.getvalue())
            notifications = json.loads(output.getvalue())['runs'][0]['invocations'][0]['toolExecutionNotifications']
            self.assertEqual(len(notifications), 1 if command[0] == 'check' else 2)
            for n in notifications:
                self.assertTrue(n['message']['text'].endswith(': Permission denied'))
                self.assert_location(n, file, self.root)


if __name__ == '__main__':
    unittest.main()
