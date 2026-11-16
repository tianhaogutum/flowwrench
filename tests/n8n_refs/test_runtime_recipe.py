"""Real-engine recipe boundary contracts; these do not substitute for n8n runs."""
from copy import deepcopy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
(ROOT / '.cache/tests').mkdir(parents=True, exist_ok=True)
SPEC = importlib.util.spec_from_file_location('runtime_recipe', ROOT / 'integration/n8n/run.py')
recipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recipe)


class RuntimeRecipeTests(unittest.TestCase):
    def test_raw_output_uses_actual_engine_business_items(self):
        result = {'data': {'resultData': {'lastNodeExecuted': 'Result', 'runData': {
            'Result': [{'data': {'main': [[{'json': {'result': 'fixed / 雪'}, 'pairedItem': {'item': 0}}]]}}]}}}}
        raw = 'migration log\n' + json.dumps(result) + '\n'
        self.assertEqual(recipe.business_output(raw), [{'result': 'fixed / 雪'}])
        for mutation in ['error', 'lastNode', 'runCount']:
            changed = deepcopy(result)
            data = changed['data']['resultData']
            if mutation == 'error':
                data['error'] = {'message': 'bad reference'}
            elif mutation == 'lastNode':
                data['lastNodeExecuted'] = 'A'
            else:
                data['runData']['Result'] *= 2
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                recipe.business_output(json.dumps(changed))
        for raw in ['a log with {braces}', '{}', json.dumps(result) + json.dumps(result)]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                recipe.business_output(raw)

    def test_negative_control_requires_engine_evidence_not_infrastructure_failure(self):
        error = {'name': 'ExpressionError', 'message': "Referenced node doesn't exist"}
        raw = json.dumps({'data': {'resultData': {'error': error}}})
        self.assertEqual(recipe.negative_outcome(raw), {'engineReferenceError': error})
        for bad in ['server failed to start', '{}', raw.replace('ExpressionError', 'DatabaseError')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                recipe.negative_outcome(bad)

    def test_reexport_oracle_rejects_stale_reference_connection_or_id(self):
        for key in recipe.EXPRESSIONS:
            fixture = json.loads((ROOT / f'fixtures/runtime/{key}.json').read_text())
            recipe.verify_export(fixture, fixture, key, 'A')
            renamed = deepcopy(fixture)
            renamed['nodes'][1]['name'] = recipe.NEW_NAME
            renamed['nodes'][2]['parameters']['values']['string'][0]['value'] = recipe.expression(key, recipe.NEW_NAME)
            renamed['connections']['Manual Trigger']['main'][0][0]['node'] = recipe.NEW_NAME
            renamed['connections'][recipe.NEW_NAME] = renamed['connections'].pop('A')
            recipe.verify_export(renamed, fixture, key, recipe.NEW_NAME)
            for boundary in ['reference', 'connection', 'id']:
                bad = deepcopy(renamed)
                if boundary == 'reference':
                    bad['nodes'][2]['parameters'] = fixture['nodes'][2]['parameters']
                elif boundary == 'connection':
                    bad['connections'] = fixture['connections']
                else:
                    bad['nodes'][1]['id'] = 'replaced'
                with self.subTest(key=key, boundary=boundary), self.assertRaises(ValueError):
                    recipe.verify_export(bad, fixture, key, recipe.NEW_NAME)

    def test_runtime_environment_does_not_inherit_user_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.cache/tests') as temp:
            state = Path(temp)
            env = recipe.runtime_env(state, '/bin:/usr/bin')
            self.assertEqual(env['HOME'], str(state / 'home'))
            self.assertEqual(env['N8N_USER_FOLDER'], str(state / 'user'))
            self.assertEqual(env['DB_SQLITE_DATABASE'], str(state / 'database.sqlite'))
            self.assertEqual(env['DB_TYPE'], 'sqlite')
            self.assertEqual(env['N8N_RUNNERS_BROKER_PORT'], '0')
            for key in ['NODE_OPTIONS', 'NODE_PATH', 'N8N_CONFIG_FILES', 'DB_POSTGRESDB_PASSWORD', 'HTTP_PROXY']:
                self.assertNotIn(key, env)
            self.assertEqual(env['N8N_DIAGNOSTICS_ENABLED'], 'false')
            self.assertEqual(env['N8N_COMMUNITY_PACKAGES_ENABLED'], 'false')

    def test_ci_install_failure_is_error_with_raw_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.cache/tests') as temp:
            temp = Path(temp)
            node, npm = temp / 'node', temp / 'npm'
            node.write_text('#!/bin/sh\necho v24.22.2\n')
            npm.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo 11.19.1; '
                           'else echo "upstream unavailable" >&2; exit 17; fi\n')
            node.chmod(0o755)
            npm.chmod(0o755)
            output = temp / 'evidence'
            result = subprocess.run([sys.executable, str(ROOT / 'integration/n8n/ci.py'),
                                     '--output-dir', str(output)], env={'PATH': str(temp)},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'ERROR')
            command = summary['commands'][-1]
            self.assertEqual((command['label'], command['exitCode']), ('npm-ci', 17))
            self.assertIn('upstream unavailable', Path(command['stderrLog']).read_text())
            self.assertFalse((output / 'runtime').exists())

    def test_ci_missing_prerequisite_is_skip_not_pass(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.cache/tests') as temp:
            output = Path(temp) / 'evidence'
            result = subprocess.run([sys.executable, str(ROOT / 'integration/n8n/ci.py'),
                                     '--output-dir', str(output)], env={'PATH': temp},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertEqual(json.loads((output / 'summary.json').read_text())['status'], 'SKIP')

    def test_fixtures_are_three_node_local_workflows(self):
        fixtures = sorted((ROOT / 'fixtures/runtime').glob('*.json'))
        self.assertEqual(len(fixtures), 3)
        for path in fixtures:
            data = json.loads(path.read_text())
            self.assertEqual([n['type'] for n in data['nodes']],
                             ['n8n-nodes-base.manualTrigger', 'n8n-nodes-base.set', 'n8n-nodes-base.set'])
            self.assertEqual(len({n['id'] for n in data['nodes']}), 3)
            self.assertFalse(data['active'])
            self.assertFalse(any(n.get('credentials') for n in data['nodes']))
            self.assertEqual(data['nodes'][2]['parameters']['values']['string'][0]['value'],
                             recipe.expression(path.stem, 'A'))
