#!/usr/bin/env python3
"""Local CI example: install locked n8n + flowwrench, then run real-engine n8n-refs acceptance.

Use Python with venv/ensurepip. Exit 77 means missing Node/npm, 2 means ERROR
(including failed installation/upstream unavailability), and 0 alone means PASS.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from run import Evidence, HERE, NODE_VERSION, REPO, runtime_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    evidence = args.output_dir.resolve()
    if evidence.exists():
        print('ERROR: output directory already exists', file=sys.stderr)
        return 2
    evidence.mkdir(parents=True)
    recorder = Evidence(evidence)
    summary = {'status': 'ERROR'}
    code = 2
    try:
        node, npm = shutil.which('node'), shutil.which('npm')
        if not node or not npm:
            summary.update(status='SKIP', reason='Node/npm unavailable')
            code = 77
        else:
            env = runtime_env(evidence / 'install-home', os.environ.get('PATH', '/usr/bin:/bin'))

            def step(label, command, timeout=1200):
                exit_code, output = recorder.run(label, command, REPO, env, timeout)
                if exit_code:
                    raise ValueError(f'{label} failed (exit {exit_code}); see commands.json/raw logs')
                return output.strip()

            if step('node-version', [node, '--version']) != NODE_VERSION:
                raise ValueError('requires Node ' + NODE_VERSION)
            if step('npm-version', [npm, '--version']) != '11.19.1':
                raise ValueError('requires npm 11.19.1')
            step('npm-ci', [npm, 'ci', '--prefix', HERE, '--cache', evidence / 'npm-cache',
                            '--userconfig', '/dev/null', '--globalconfig', '/dev/null', '--no-audit', '--no-fund'])
            venv = evidence / 'venv'
            step('venv', [sys.executable, '-m', 'venv', venv])
            python = venv / 'bin/python'
            step('install-refcheck', [python, '-m', 'pip', '--isolated', 'install', '--no-deps', REPO])
            exit_code, _ = recorder.run('runtime', [sys.executable, HERE / 'run.py', '--refcheck-python', python,
                                                   '--output-dir', evidence / 'runtime'], REPO, env, 1200)
            if exit_code not in [0, 77]:
                raise ValueError('runtime acceptance failed; see runtime/summary.json and raw logs')
            summary.update(status='PASS' if exit_code == 0 else 'SKIP', runtimeSummary=str(evidence / 'runtime/summary.json'))
            code = exit_code
    except (OSError, ValueError) as exc:
        summary.update(status='ERROR', reason=str(exc))
    summary['commands'] = recorder.commands
    (evidence / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({'status': summary['status'], 'summary': str(evidence / 'summary.json')}))
    return code


if __name__ == '__main__':
    sys.exit(main())
