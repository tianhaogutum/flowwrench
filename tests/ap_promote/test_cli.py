import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.ap_promote.jsondoc import MAX_BYTES

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures'


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-ap-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.out = self.work / 'result.json'

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def source(self, text):
        p = self.work / 'input.json'
        p.write_text(text)
        return p

    def remap(self, *args, code=0):
        return self.cli('remap', FIXTURE / 'support-flow.json', '-o', self.out, *args, code=code)

    def test_inventory_e2e_multiple_files_json_and_table(self):
        result = self.cli('inventory', FIXTURE / 'support-flow.json', FIXTURE / 'support-flow.json', '--json')
        self.assertEqual(len(json.loads(result.stdout)), 10)
        self.assertEqual(sum('agentTools' in r['pointer'] for r in json.loads(result.stdout)), 4)
        text = self.cli('inventory', FIXTURE / 'support-flow.json').stdout
        self.assertIn('externalId', text.splitlines()[0])
        self.assertEqual(len(text.splitlines()), 6)

    def test_promotion_e2e_and_second_strict_pass(self):
        result = self.remap('--map', 'slack-dev=slack-prod', '--map', 'gmail-dev=gmail-prod', '--strict', '--json')
        self.assertEqual(json.loads(result.stdout)['rewritten'], 5)
        check = self.cli('check', self.out, '--target-connections', FIXTURE / 'target-connections.json', '--json')
        self.assertEqual(json.loads(check.stdout), {'references': 5, 'errors': [], 'ok': True})
        second = self.work / 'second.json'
        result = self.cli('remap', self.out, '--map-file', FIXTURE / 'map.json', '--strict', '-o', second,
                          '--target-connections', FIXTURE / 'target-connections.json', '--json')
        self.assertEqual(json.loads(result.stdout)['rewritten'], 0)
        self.assertEqual(self.out.read_bytes(), second.read_bytes())

    def test_verified_remap_file(self):
        result = self.remap('--map-file', FIXTURE / 'map.json', '--target-connections', FIXTURE / 'target-connections.json', '--strict', '--json')
        self.assertTrue(json.loads(result.stdout)['catalogVerified'])

    def test_strict_failure_preserves_existing_output(self):
        self.out.write_bytes(b'existing output')
        self.remap('--map', 'slack-dev=slack-prod', '--strict', code=1)
        self.assertEqual(self.out.read_bytes(), b'existing output')

    def test_strict_failure_no_new_output(self):
        self.remap('--map', 'slack-dev=slack-prod', '--strict', code=1)
        self.assertFalse(self.out.exists())

    def test_dry_run_never_creates_output_or_parent(self):
        output = self.work / 'does-not-exist' / 'flow.json'
        self.cli('remap', FIXTURE / 'support-flow.json', '--map-file', FIXTURE / 'map.json', '-o', output, '--strict', '--dry-run')
        self.assertFalse(output.parent.exists())
        self.cli('remap', FIXTURE / 'support-flow.json', '--map', 'slack-dev=slack-prod', '-o', output, '--strict', '--dry-run', code=1)
        self.assertFalse(output.parent.exists())

    def test_duplicate_entries_and_piece_failures_leave_no_file(self):
        for args, code in ((['--map', 'slack-dev=a', '--map', 'slack-dev=b'], 2),
                           (['--map', 'slack-dev=gmail-dev'], 1)):
            with self.subTest(args=args):
                self.remap(*args, code=code)
                self.assertFalse(self.out.exists())
        bad_map = self.source('{"slack-dev":"a","slack-dev":"b"}')
        self.remap('--map-file', bad_map, code=2)
        self.assertFalse(self.out.exists())

    def test_missing_connection_exit_one(self):
        result = self.cli('check', FIXTURE / 'support-flow.json', '--target-connections', FIXTURE / 'target-connections.json', '--json', code=1)
        self.assertEqual(len(json.loads(result.stdout)['errors']), 5)

    def test_piece_pin_and_gaps_e2e(self):
        result = self.cli('pieces', FIXTURE / 'support-flow.json', '--target-pieces', FIXTURE / 'target-pieces.json', '--json')
        self.assertEqual(len(json.loads(result.stdout)), 6)
        self.cli('pieces', FIXTURE / 'support-flow.json', '--target-pieces', self.source('[]'), code=1)
        value = json.loads((FIXTURE / 'support-flow.json').read_text())
        value['flows'][0]['trigger']['settings']['pieceVersion'] = '~0.9.0'
        result = self.cli('pieces', self.source(json.dumps(value)), '--json', code=1)
        self.assertEqual(json.loads(result.stdout)[0]['status'], 'unpinned')

    def test_scrub_stdout_output_and_in_place(self):
        result = self.cli('scrub', FIXTURE / 'support-flow.json')
        self.assertEqual(json.loads(result.stderr)['count'], 4)
        self.assertEqual(len(json.loads(result.stdout)['flows']), 1)
        self.cli('scrub', FIXTURE / 'support-flow.json', '-o', self.out)
        self.assertEqual(self.out.read_text(), result.stdout)
        before = self.out.read_bytes()
        self.cli('scrub', self.out, '--in-place')
        self.assertEqual(self.out.read_bytes(), before)

    def test_scrub_dry_run_leaves_source_and_destination_untouched(self):
        before = (FIXTURE / 'support-flow.json').read_bytes()
        result = self.cli('scrub', FIXTURE / 'support-flow.json', '-o', self.out, '--dry-run')
        self.assertEqual(result.stdout, '')
        self.assertEqual((FIXTURE / 'support-flow.json').read_bytes(), before)
        self.assertFalse(self.out.exists())

    def test_empty_map_retains_exact_bytes_cli(self):
        self.remap('--map-file', FIXTURE / 'empty-map.json')
        self.assertEqual(self.out.read_bytes(), (FIXTURE / 'support-flow.json').read_bytes())

    def test_invalid_json_shape_and_usage_exit_two(self):
        for source in ('{', '{}', '{"nodes":[]}'):
            with self.subTest(source=source):
                self.cli('inventory', self.source(source), '--json', code=2)
        self.cli('remap', FIXTURE / 'support-flow.json', '-o', self.out, code=2)
        self.cli('inventory', self.work / 'missing', code=2)
        self.cli('scrub', self.work, code=2)
        self.cli('not-a-command', code=2)
        self.assertFalse(self.out.exists())

    def test_input_size_limit_and_utf8(self):
        p = self.work / 'big.json'
        with p.open('wb') as f:
            f.truncate(MAX_BYTES + 1)
        self.cli('inventory', p, code=2)
        p.write_bytes(b'\xff\xfe')
        self.cli('inventory', p, code=2)

    def test_output_cannot_overwrite_source_map_or_target(self):
        source = self.source((FIXTURE / 'support-flow.json').read_text())
        original = source.read_bytes()
        self.cli('remap', source, '--map-file', FIXTURE / 'map.json', '-o', source, code=2)
        self.assertEqual(source.read_bytes(), original)
        mapping = self.work / 'map.json'
        mapping.write_text('{}')
        self.cli('remap', source, '--map-file', mapping, '-o', mapping, code=2)
        self.assertEqual(mapping.read_text(), '{}')
        target = self.work / 'target.json'
        target.write_bytes((FIXTURE / 'target-connections.json').read_bytes())
        self.cli('remap', source, '--map-file', FIXTURE / 'map.json', '--target-connections', target, '-o', target, code=2)
        self.assertEqual(target.read_bytes(), (FIXTURE / 'target-connections.json').read_bytes())

    def test_output_symlinks_and_parent_symlinks_refused(self):
        victim = self.work / 'victim.json'
        victim.write_text('unchanged')
        self.out.symlink_to(victim)
        self.remap('--map-file', FIXTURE / 'map.json', code=2)
        self.assertEqual(victim.read_text(), 'unchanged')
        self.cli('scrub', self.out, '--in-place', code=2)  # Invalid shape is also refused.
        link = self.work / 'linked-dir'
        link.symlink_to(self.work, target_is_directory=True)
        self.cli('remap', FIXTURE / 'support-flow.json', '--map-file', FIXTURE / 'map.json', '-o', link / 'other.json', code=2)
        self.assertFalse((self.work / 'other.json').exists())

    def test_external_id_is_never_used_as_a_path(self):
        self.remap('--map', 'slack-dev=../../escaped', '--map', 'gmail-dev=gmail-prod', '--strict')
        self.assertEqual(len(list(self.work.iterdir())), 1)
        self.assertIn('../../escaped', self.out.read_text())

    def test_diagnostics_do_not_echo_credentials(self):
        target = self.source('{"data":[],"value":{"access_token":"TOPSECRET"},"next":"cursor"}')
        result = self.cli('check', FIXTURE / 'support-flow.json', '--target-connections', target, code=2)
        self.assertNotIn('TOPSECRET', result.stdout + result.stderr)
        target = self.source('{"token":"TOPSECRET", broken}')
        result = self.cli('inventory', target, code=2)
        self.assertNotIn('TOPSECRET', result.stdout + result.stderr)

    def test_code_and_template_input_are_not_executed_even_in_dry_run(self):
        marker = self.work / 'MUST-NOT-EXIST'
        source = {'trigger': {'name': 'trigger', 'type': 'EMPTY', 'settings': {}, 'nextAction': {
            'name': 'code', 'type': 'CODE', 'settings': {'sourceCode': {'code': f"open({str(marker)!r}, 'w').write('unsafe')", 'packageJson': '{}'}, 'input': {}}}}}
        result = self.cli('inventory', self.source(json.dumps(source)), '--json')
        self.assertEqual(json.loads(result.stdout), [])
        self.cli('remap', self.work / 'input.json', '--map-file', FIXTURE / 'empty-map.json', '-o', self.out, '--dry-run')
        self.assertFalse(marker.exists())
        self.assertFalse(self.out.exists())

    def test_inventory_failure_is_not_partial_success(self):
        result = self.cli('inventory', FIXTURE / 'support-flow.json', self.source('{}'), '--json', code=2)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
