#!/usr/bin/env python3
"""Opt-in real n8n import/execute/export acceptance. Exit 0 PASS, 77 SKIP, 2 ERROR.

Only the three committed credential-free, local Set fixtures are executed. Never
accept arbitrary workflows or user n8n configuration. npm ci is a separate step.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]  # tests/n8n_refs: fixtures/runtime/*.json
REPO = Path(__file__).resolve().parents[4]  # flowwrench checkout: git state and installable project
SOURCE = REPO / 'src/flowwrench/n8n_refs'
HERE = Path(__file__).resolve().parent
NODE_VERSION = 'v24.22.2'
N8N_VERSION = '2.47.4'
NEW_NAME = 'Customer / 雪'
EXPECTED = [{'result': 'fixed / 雪'}]
EXPRESSIONS = {
    'dollar': lambda name: f'$({json.dumps(name, ensure_ascii=False)}).first().json.value',
    'legacy': lambda name: f'$node[{json.dumps(name, ensure_ascii=False)}].json.value',
    'optional': lambda name: f'$node?.[{json.dumps(name, ensure_ascii=False)}]?.json.value',
}


def expression(key, name):
    # n8n preserves the original single quotes before rename, then refcheck
    # normalizes just the modified node-name literal to double quotes.
    source = EXPRESSIONS[key](name)
    if name == 'A':
        source = source.replace('"A"', "'A'")
    return '={{ ' + source + ' }}'


def runtime_env(state, path):
    """Allowlist: no inherited n8n/DB/Node/proxy/dotenv or home configuration."""
    for name in ['home', 'user', 'tmp']:
        (state / name).mkdir(parents=True, exist_ok=True)
    return {
        'PATH': path, 'HOME': str(state / 'home'), 'TMPDIR': str(state / 'tmp'),
        'LANG': 'C.UTF-8', 'TZ': 'UTC', 'NO_COLOR': '1',
        'N8N_USER_FOLDER': str(state / 'user'), 'DB_TYPE': 'sqlite',
        'DB_SQLITE_DATABASE': str(state / 'database.sqlite'),
        'N8N_ENCRYPTION_KEY': 'refcheck-synthetic-test-key-only-00000000',
        'N8N_DIAGNOSTICS_ENABLED': 'false', 'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false',
        'N8N_TEMPLATES_ENABLED': 'false', 'N8N_COMMUNITY_PACKAGES_ENABLED': 'false',
        'N8N_PERSONALIZATION_ENABLED': 'false', 'N8N_LICENSE_AUTO_RENEW_ENABLED': 'false',
        'N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS': 'true',
        'N8N_RUNNERS_BROKER_PORT': '0', 'N8N_PYTHON_ENABLED': 'false',
        'N8N_RUNNERS_MODE': 'external',
        'N8N_RUNNERS_AUTH_TOKEN': 'refcheck-local-synthetic-runner-token-only',
    }


def execution_result(stdout):
    """Read n8n --rawOutput resultData, never substitute static parser output.

    CLI migrations and startup messages can precede JSON. Require exactly one
    top-level execution envelope and a completed final node without engine error.
    """
    envelopes = []
    decoder = json.JSONDecoder()
    for offset, character in enumerate(stdout):
        if character != '{':
            continue
        try:
            value, _ = decoder.raw_decode(stdout, offset)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('data'), dict) and 'resultData' in value['data']:
            envelopes.append(value['data']['resultData'])
    if len(envelopes) != 1:
        raise ValueError('expected exactly one n8n execution envelope in raw output')
    return envelopes[0]


def business_output(stdout):
    result = execution_result(stdout)
    if result.get('error') or result.get('lastNodeExecuted') != 'Result':
        raise ValueError('n8n execution failed or did not finish Result')
    try:
        runs = result['runData']['Result']
        if len(runs) != 1 or runs[0].get('error'):
            raise ValueError('expected one successful Result run')
        outputs = runs[0]['data']['main']
        if len(outputs) != 1:
            raise ValueError('expected one Result output')
        return [item['json'] for item in outputs[0]]
    except (KeyError, TypeError) as exc:
        raise ValueError('missing n8n final business items') from exc


def negative_outcome(stdout):
    # An infrastructure crash, timeout or missing raw output is not proof that
    # the real engine rejected a reference. Keep those as ERROR, never PASS.
    result = execution_result(stdout)
    error = result.get('error')
    if error:
        require(error.get('name') == 'ExpressionError' and
                error.get('message') == "Referenced node doesn't exist",
                'negative control failed for an unrelated engine error')
        return {'engineReferenceError': error}
    business = business_output(stdout)
    require(business != EXPECTED, 'engine/gate disagreement: broken reference kept business output')
    return {'businessOutput': business}


def verify_export(exported, original, key, name):
    """Independent exact fixture oracle; no refcheck scanner/parser is used."""
    expected = deepcopy(original)
    expected['nodes'][1]['name'] = name
    expected['nodes'][2]['parameters']['values']['string'][0]['value'] = expression(key, name)
    if name != 'A':
        expected['connections']['Manual Trigger']['main'][0][0]['node'] = name
        expected['connections'][name] = expected['connections'].pop('A')
    actual_nodes = {node['id']: node for node in exported['nodes']}
    if len(actual_nodes) != 3 or set(actual_nodes) != {n['id'] for n in original['nodes']}:
        raise ValueError('re-export changed or duplicated node IDs')
    for node in expected['nodes']:
        actual = actual_nodes[node['id']]
        for field in ['name', 'type', 'typeVersion', 'parameters']:
            if actual[field] != node[field]:
                raise ValueError(f're-export mismatch: {key}/{node["id"]}/{field}')
    if exported['connections'] != expected['connections']:
        raise ValueError('re-export has stale or changed connections')


class Evidence:
    def __init__(self, directory):
        self.directory = directory
        self.commands = []

    def run(self, label, command, cwd, env, timeout=180):
        command = list(map(str, command))
        out = self.directory / (label + '.stdout')
        err = self.directory / (label + '.stderr')
        record = {'label': label, 'command': command, 'cwd': str(cwd),
                  'stdoutLog': str(out), 'stderrLog': str(err)}
        try:
            with out.open('w') as stdout, err.open('w') as stderr:
                result = subprocess.run(command, cwd=cwd, env=env, stdout=stdout,
                                        stderr=stderr, timeout=timeout)
            record['exitCode'] = result.returncode
        except subprocess.TimeoutExpired:
            record['exitCode'] = 124
        except OSError as exc:
            record['exitCode'] = 127
            err.write_text(str(exc), encoding='utf-8')
        self.commands.append(record)
        (self.directory / 'commands.json').write_text(json.dumps(self.commands, indent=2) + '\n')
        return record['exitCode'], out.read_text(encoding='utf-8')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path, help='new evidence directory; never overwritten')
    parser.add_argument('--refcheck-python', type=Path, default=REPO / '.venv/bin/python',
                        help='Python with a freshly installed flowwrench wheel; n8n_refs checked against source')
    args = parser.parse_args()
    evidence = args.output_dir.resolve()
    if evidence.exists():
        print('ERROR: evidence directory already exists', file=sys.stderr)
        return 2
    evidence.mkdir(parents=True)
    recorder = Evidence(evidence)
    summary = {'status': 'ERROR', 'fixtures': {}, 'expectedBusinessOutput': EXPECTED,
               'n8nVersion': N8N_VERSION, 'nodeVersion': NODE_VERSION,
               'packageLockSha256': hashlib.sha256((HERE / 'package-lock.json').read_bytes()).hexdigest()}
    code = 2
    try:
        summary['testedTree'] = subprocess.check_output(['git', 'write-tree'], cwd=REPO, text=True).strip()
        summary['indexMatchesWorkingTree'] = subprocess.run(['git', 'diff-files', '--quiet'], cwd=REPO).returncode == 0
        summary['testedHead'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
        # Hash tracked file contents including uncommitted changes without mutating the index.
        tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO).split(b'\0')
        summary['testedFilesSha256'] = {os.fsdecode(p): hashlib.sha256((REPO / os.fsdecode(p)).read_bytes()).hexdigest()
                                        for p in tracked if p and (REPO / os.fsdecode(p)).is_file()}
        node = shutil.which('node')
        n8n = HERE / 'node_modules/n8n/bin/n8n'
        python = args.refcheck_python.absolute()  # Keep venv symlink: resolving it loses the venv.
        missing = [label for label, ok in [('node', node), ('npm ci dependencies', n8n.is_file()),
                                          ('installed refcheck Python', python.is_file())] if not ok]
        if missing:
            summary.update(status='SKIP', reason='missing prerequisites: ' + ', '.join(missing))
            code = 77
        else:
            work = Path(tempfile.mkdtemp(prefix='isolated-', dir=evidence))
            env = runtime_env(work / 'preflight', str(Path(node).parent) + ':/usr/bin:/bin')
            exit_code, version = recorder.run('node-version', [node, '--version'], work, env)
            require(exit_code == 0 and version.strip() == NODE_VERSION, 'Node version mismatch')
            exit_code, version = recorder.run('n8n-version', [node, n8n, '--version'], work, env)
            require(exit_code == 0 and version.strip() == N8N_VERSION, 'n8n version mismatch')
            probe = ('import hashlib,json,pathlib,flowwrench.n8n_refs as m; p=pathlib.Path(m.__file__).parent; '
                     'print(json.dumps({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in p.glob("*.py")}))')
            exit_code, installed = recorder.run('installed-source', [python, '-I', '-c', probe], work, env)
            require(exit_code == 0, 'cannot inspect installed refcheck')
            source = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE.glob('*.py')}
            require(json.loads(installed) == source, 'installed refcheck differs from source')
            cli = [python, '-I', '-m', 'flowwrench.n8n_refs']
            for key in EXPRESSIONS:
                fixture = ROOT / f'fixtures/runtime/{key}.json'
                original = json.loads(fixture.read_text(encoding='utf-8'))
                require(len(original['nodes']) == 3 and not any(n.get('credentials') for n in original['nodes']),
                        'fixture must have three credential-free nodes')
                require([n['type'] for n in original['nodes']] == ['n8n-nodes-base.manualTrigger',
                        'n8n-nodes-base.set', 'n8n-nodes-base.set'], 'only local Manual Trigger/Set allowed')
                renamed = work / (key + '-renamed.json')
                exit_code, _ = recorder.run(key + '-rename', cli + ['rename', fixture, '--from', 'A', '--to', NEW_NAME,
                                                                  '-o', renamed], work, env)
                require(exit_code == 0, f'{key}: rename failed')
                broken = json.loads(renamed.read_text(encoding='utf-8'))
                broken['nodes'][2]['parameters']['values']['string'][0]['value'] = expression(key, 'Missing')
                broken_path = work / (key + '-broken.json')
                broken_path.write_text(json.dumps(broken, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                summary['fixtures'][key] = results = {}
                for phase, path, name in [('before', fixture, 'A'), ('after', renamed, NEW_NAME),
                                          ('broken', broken_path, 'Missing')]:
                    label = key + '-' + phase
                    state = Path(tempfile.mkdtemp(prefix=label + '-', dir=work))
                    phase_env = runtime_env(state, env['PATH'])
                    exit_code, output = recorder.run(label + '-gate', cli + ['check', path, '--format', 'json',
                                                                     '--fail-on', 'warning'], state, phase_env)
                    gate = json.loads(output)
                    require(not gate['errors'], label + ': gate load error')
                    findings = [f for r in gate['reports'] for f in r['findings']]
                    require((exit_code == 1 and [(f['rule'], f['reference']) for f in findings] == [('R001', 'Missing')]) if phase == 'broken'
                            else (exit_code == 0 and not findings), label + ': unexpected gate result')
                    exit_code, _ = recorder.run(label + '-import', [node, n8n, 'import:workflow', '--input', path],
                                               state, phase_env)
                    require(exit_code == 0, label + ': n8n import failed')
                    execution_exit, output = recorder.run(label + '-execute',
                        [node, n8n, 'execute', '--id', original['id'], '--rawOutput'], state, phase_env)
                    exported_path = evidence / (label + '-export.json')
                    exit_code, _ = recorder.run(label + '-export',
                        [node, n8n, 'export:workflow', '--id', original['id'], '--output', exported_path], state, phase_env)
                    require(exit_code == 0, label + ': n8n export failed')
                    exported = json.loads(exported_path.read_text(encoding='utf-8'))
                    require(isinstance(exported, list) and len(exported) == 1, label + ': unexpected export envelope')
                    entry = {'executionExitCode': execution_exit, 'export': str(exported_path),
                             'userFolder': phase_env['N8N_USER_FOLDER'], 'database': phase_env['DB_SQLITE_DATABASE']}
                    results[phase] = entry
                    try:
                        business = business_output(output)
                        entry['businessOutput'] = business
                    except ValueError as exc:
                        entry['engineOutputError'] = str(exc)
                        business = None
                    if phase == 'broken':
                        require(execution_exit not in [124, 127], label + ': execution infrastructure failure')
                        entry.update(negative_outcome(output))
                        entry['negativeControlDetected'] = True
                    else:
                        require(execution_exit == 0 and business == EXPECTED, label + ': real engine business result mismatch')
                        verify_export(exported[0], original, key, name)
                        entry['independentExportVerified'] = True
                require(results['before']['businessOutput'] == results['after']['businessOutput'],
                        key + ': changed business result')
            summary.update(status='PASS', isolatedUserFolders=True, credentialsImported=False,
                           sourceMatchesInstalled=True, commands=recorder.commands)
            code = 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        summary.update(status='ERROR', reason=str(exc), commands=recorder.commands)
    finally:
        (evidence / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': summary['status'], 'reason': summary.get('reason'),
                      'summary': str(evidence / 'summary.json')}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
