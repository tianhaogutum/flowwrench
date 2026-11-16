"""Synthetic integration cases for reviewed, offline promotion artifacts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures'


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.plan-ap-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'flow.json'
        self.source.write_bytes((FIXTURE / 'support-flow.json').read_bytes())
        self.mapping = self.work / 'map.json'
        self.mapping.write_bytes((FIXTURE / 'map.json').read_bytes())
        self.catalog = self.work / 'catalog.json'
        self.catalog.write_bytes((FIXTURE / 'target-connections-versioned.json').read_bytes())
        self.piece_catalog = self.work / 'pieces.json'
        self.piece_catalog.write_bytes((FIXTURE / 'target-pieces.json').read_bytes())
        self.plan = self.work / 'plan.json'
        self.output = self.work / 'promoted.json'

    def reset_inputs(self):
        self.source.write_bytes((FIXTURE / 'support-flow.json').read_bytes())
        self.mapping.write_bytes((FIXTURE / 'map.json').read_bytes())
        self.catalog.write_bytes((FIXTURE / 'target-connections-versioned.json').read_bytes())
        self.plan.unlink(missing_ok=True)

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def config(self):
        return ('--map-file', self.mapping, '--target-connections', self.catalog)

    def make_plan(self, code=0, output=None, extra=()):
        return self.cli('plan', self.source, *self.config(), '-o', output or self.plan, '--json', *extra, code=code)

    def apply(self, code=0, output=None, extra=()):
        return self.cli('apply', self.plan, '--source', self.source, *self.config(),
                        '-o', output or self.output, '--json', *extra, code=code)

    def mutate(self, path, edit):
        value = json.loads(path.read_text())
        edit(value)
        path.write_text(json.dumps(value))

    def test_plan_and_apply_lossless_with_hashes_and_tool_versions(self):
        # Credential-like business data is opaque, and is never copied into the plan.
        source = self.source.read_bytes().replace(b'"flows":', b'"opaque": "SOURCE_SECRET_SENTINEL", "flows":')
        source = b'\xef\xbb\xbf' + source.replace(b'\n', b'\r\n')
        self.source.write_bytes(source)
        self.mutate(self.catalog, lambda v: v['data'][0].update(value={'access_token': 'CATALOG_SECRET_SENTINEL'}))
        catalog_before = self.catalog.read_bytes()
        plan = json.loads(self.make_plan().stdout)
        self.assertEqual(plan, json.loads(self.plan.read_text()))
        self.assertEqual(plan['format'], 'ap-flow-promote.plan/v1')
        self.assertEqual(plan['sourceSha256'], hashlib.sha256(source).hexdigest())
        self.assertRegex(plan['configSha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(plan['report']['rewritten'], 5)
        self.assertEqual(len(plan['compatibility']), 5)
        tools = [r for r in plan['compatibility'] if 'agentTools' in r['pointer']]
        self.assertEqual([r['connectionVersion'] for r in tools], ['0.10.0', '0.9.0'])
        self.assertTrue(all(r['status'] == 'exact' for r in plan['compatibility']))
        self.assertFalse(self.output.exists())
        self.assertNotIn('SECRET_SENTINEL', self.plan.read_text())
        report = json.loads(self.apply().stdout)
        self.assertTrue(report['planVerified'])
        expected = source.replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod')
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(plan['outputSha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.catalog.read_bytes(), catalog_before)

    def test_plan_is_deterministic_and_config_ignores_order_and_credentials(self):
        first = json.loads(self.make_plan().stdout)
        self.mutate(self.mapping, lambda v: None)  # Formatting changes are not config changes.
        self.mutate(self.catalog, lambda v: (v['data'].reverse(), v['data'][0].update(value={'token': 'NEW_SECRET'})))
        self.assertEqual(json.loads(self.make_plan().stdout), first)
        self.apply()

    def test_apply_refuses_stale_source_including_whitespace_and_preserves_output(self):
        self.make_plan()
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.output.write_bytes(b'previous output')
        result = self.apply(code=1)
        self.assertIn('stale source', result.stderr)
        self.assertEqual(self.output.read_bytes(), b'previous output')

    def test_apply_refuses_changed_mapping_and_catalog_metadata(self):
        for field in ('mapping', 'status', 'pieceVersion'):
            with self.subTest(field=field):
                self.reset_inputs()
                self.make_plan()
                if field == 'mapping':
                    self.mutate(self.mapping, lambda v: v.update({'gmail-dev': 'gmail-other'}))
                else:
                    value = 'ERROR' if field == 'status' else '0.9.1'
                    self.mutate(self.catalog, lambda v: v['data'][1].update({field: value}))
                self.assertIn('stale configuration', self.apply(code=1).stderr)
                self.assertFalse(self.output.exists())

    def test_plan_refuses_missing_or_mismatched_connection_versions(self):
        for version in (None, '0.11.0'):
            with self.subTest(version=version):
                self.reset_inputs()
                if version is None:
                    self.mutate(self.catalog, lambda v: v['data'][0].pop('pieceVersion'))
                else:
                    self.mutate(self.catalog, lambda v: v['data'][0].update(pieceVersion=version))
                self.assertIn('connection version', self.make_plan(code=1).stderr)
                self.assertFalse(self.plan.exists())

    def test_plan_refuses_unpinned_and_different_agent_tool_versions(self):
        for version in ('~0.10.0', '0.11.0'):
            with self.subTest(version=version):
                self.reset_inputs()
                def edit(value):
                    agent = value['flows'][0]['trigger']['nextAction']['nextAction']
                    agent['settings']['input']['agentTools'][0]['pieceMetadata']['pieceVersion'] = version
                self.mutate(self.source, edit)
                self.make_plan(code=1)
                self.assertFalse(self.plan.exists())

    def test_plan_refuses_conflicts_partial_mapping_and_bad_catalog(self):
        for mapping, code in (({'slack-dev': 'gmail-prod', 'gmail-dev': 'gmail-prod'}, 1),
                              ({'slack-dev': 'slack-prod'}, 1),
                              ({'slack-dev': 'gmail-dev', 'gmail-dev': 'gmail-prod'}, 2)):
            with self.subTest(mapping=mapping):
                self.mapping.write_text(json.dumps(mapping))
                self.make_plan(code=code)
                self.assertFalse(self.plan.exists())
        self.mapping.write_bytes((FIXTURE / 'map.json').read_bytes())
        self.mutate(self.catalog, lambda v: v.update(next='more'))
        self.make_plan(code=2)
        self.assertFalse(self.plan.exists())

    def test_apply_rejects_conflicting_map_entries(self):
        self.make_plan()
        self.apply(code=2, extra=('--map', 'gmail-dev=other'))
        self.assertFalse(self.output.exists())

    def test_invalid_connection_pin_is_input_error(self):
        self.mutate(self.catalog, lambda v: v['data'][0].update(pieceVersion='^0.10.0'))
        self.make_plan(code=2)
        self.assertFalse(self.plan.exists())

    def test_apply_revalidates_artifact_contents(self):
        original = json.loads(self.make_plan().stdout)
        for field in ('outputSha256', 'compatibility', 'report', 'unexpected'):
            with self.subTest(field=field):
                value = dict(original)
                value[field] = 'tampered'
                self.plan.write_text(json.dumps(value))
                self.apply(code=1)
                self.assertFalse(self.output.exists())
        self.plan.write_text('{"format":"future-version"}')
        self.apply(code=2)

    def test_apply_dry_run_validates_without_creating_parent(self):
        self.make_plan()
        out = self.work / 'missing-parent' / 'flow.json'
        report = json.loads(self.apply(output=out, extra=('--dry-run',)).stdout)
        self.assertTrue(report['dryRun'])
        self.assertFalse(out.parent.exists())
        self.source.write_bytes(self.source.read_bytes() + b' ')
        self.apply(code=1, output=out, extra=('--dry-run',))
        self.assertFalse(out.parent.exists())

    def test_plan_and_apply_protect_all_inputs_and_refuse_symlinks(self):
        inputs = (self.source, self.mapping, self.catalog)
        before = {p: p.read_bytes() for p in inputs}
        for path in inputs:
            self.make_plan(code=2, output=path)
        self.make_plan()
        before[self.plan] = self.plan.read_bytes()
        for path in before:
            self.apply(code=2, output=path)
            self.assertEqual(path.read_bytes(), before[path])
        self.output.symlink_to(self.source)
        self.apply(code=2)
        self.assertEqual(self.source.read_bytes(), before[self.source])

    def test_already_mapped_references_still_require_matching_versions(self):
        self.make_plan()
        self.apply()
        self.source.write_bytes(self.output.read_bytes())
        plan = json.loads(self.make_plan().stdout)
        self.assertEqual(plan['report']['rewritten'], 0)
        self.assertEqual(plan['report']['alreadyMapped'], 5)
        self.mutate(self.catalog, lambda v: v['data'][0].update(pieceVersion='0.10.1'))
        self.make_plan(code=1)

    def piece_config(self):
        return ('--target-pieces', self.piece_catalog)

    def test_plan_and_apply_gate_whole_flow_pieces_including_nonconnection_ai(self):
        # Contract: optional catalog adds a canonical metadata hash and every
        # usage record; the 6th usage (AI) has no connection to validate.
        before = self.source.read_bytes()
        plan = json.loads(self.make_plan(extra=self.piece_config()).stdout)
        normalized = sorted(json.loads(self.piece_catalog.read_text()), key=lambda r: r['name'])
        normalized = [dict(pieceName=r['name'], pieceVersion=r['version']) for r in normalized]
        canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(plan['targetPiecesSha256'], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(len(plan['pieceAvailability']), 6)
        self.assertEqual(len(plan['compatibility']), 5)
        ai, = [r for r in plan['pieceAvailability'] if r['pieceName'] == '@activepieces/piece-ai']
        self.assertEqual(ai['pointer'], '/flows/0/trigger/nextAction/nextAction')
        self.assertEqual(ai['status'], 'available')
        self.assertTrue(plan['report']['pieceCatalogVerified'])
        self.assertEqual(plan['report']['pieceUsagesVerified'], 6)
        report = json.loads(self.apply(extra=self.piece_config()).stdout)
        self.assertTrue(report['planVerified'])
        self.assertTrue(report['pieceCatalogVerified'])
        self.assertEqual(self.output.read_bytes(), before.replace(b'slack-dev', b'slack-prod').replace(b'gmail-dev', b'gmail-prod'))
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_nonconnection_ai_version_refuses_plan_without_overwrite(self):
        # Existing connection-only plan succeeds even without AI installed.
        self.mutate(self.piece_catalog, lambda v: v.pop())
        self.make_plan()
        before = self.plan.read_bytes()
        result = self.make_plan(code=1, extra=self.piece_config())
        self.assertIn('missing-version', result.stderr)
        self.assertIn('/flows/0/trigger/nextAction/nextAction', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_nonconnection_nested_tool_requires_its_own_exact_installed_pin(self):
        # A tool with no auth in a batch's error branch must still be gated.
        def edit(value):
            router = value['flows'][0]['trigger']['nextAction']
            batch = router['children'][0]
            batch['type'] = 'PROCESS_IN_BATCHES'
            batch['firstLoopAction']['continueOnFailureBranches'] = {'onFailure': {
                'name': 'nested_agent', 'type': 'PIECE',
                'settings': {'pieceName': '@activepieces/piece-ai', 'pieceVersion': '0.5.0',
                             'input': {'agentTools': [{'type': 'PIECE', 'pieceMetadata': {
                                 'pieceName': '@activepieces/piece-http', 'pieceVersion': '1.2.3-rc.1+build',
                                 'predefinedInput': {'url': 'https://example.invalid'}}}]}}}}
        self.mutate(self.source, edit)
        self.make_plan()  # Proves that no connection check covers this tool.
        result = self.make_plan(code=1, extra=self.piece_config())
        self.assertIn('agentTools/0', result.stderr)
        self.assertIn('missing-version', result.stderr)
        self.mutate(self.piece_catalog, lambda v: v.append({'name': '@activepieces/piece-http', 'version': '1.2.3-rc.1+build'}))
        plan = json.loads(self.make_plan(extra=self.piece_config()).stdout)
        self.assertEqual(len(plan['pieceAvailability']), 8)
        self.assertEqual(len(plan['compatibility']), 5)
        self.apply(extra=self.piece_config())

    def test_nonconnection_unpinned_step_or_tool_refuses_with_catalog(self):
        for location in ('step', 'tool'):
            for version in (None, '~0.5.0'):
                with self.subTest(location=location, version=version):
                    self.reset_inputs()
                    def edit(value):
                        agent = value['flows'][0]['trigger']['nextAction']['nextAction']['settings']
                        meta = agent
                        if location == 'tool':
                            meta = {'pieceName': '@activepieces/piece-ai', 'predefinedInput': {}}
                            agent['input']['agentTools'].append({'type': 'PIECE', 'pieceMetadata': meta})
                        if version is None:
                            meta.pop('pieceVersion', None)
                        else:
                            meta['pieceVersion'] = version
                    self.mutate(self.source, edit)
                    self.make_plan()  # Optional behavior preserves old no-catalog contract.
                    before = self.plan.read_bytes()
                    self.assertIn('unpinned', self.make_plan(code=1, extra=self.piece_config()).stderr)
                    self.assertEqual(self.plan.read_bytes(), before)

    def test_piece_catalog_hash_normalizes_order_aliases_duplicates_and_unknown_fields(self):
        first = json.loads(self.make_plan(extra=self.piece_config()).stdout)
        rows = json.loads(self.piece_catalog.read_text())
        rows = [dict(pieceName=r['name'], pieceVersion=r['version'], token='PIECE_SECRET_SENTINEL') for r in reversed(rows)]
        rows.append(dict(rows[0]))
        self.piece_catalog.write_text(json.dumps(dict(data=rows, next=None, nextCursor='')))
        self.assertEqual(json.loads(self.make_plan(extra=self.piece_config()).stdout), first)
        self.assertNotIn('PIECE_SECRET_SENTINEL', self.plan.read_text())
        self.apply(extra=self.piece_config())

    def test_apply_refuses_changed_omitted_or_added_piece_catalog_including_dry_run(self):
        self.make_plan(extra=self.piece_config())
        self.output.write_bytes(b'previous output')
        original = self.piece_catalog.read_bytes()
        for change in ('removed-version', 'added-unused-version', 'different-version', 'omit'):
            with self.subTest(change=change):
                rows = json.loads(original)
                if change == 'removed-version':
                    rows.pop()
                elif change == 'added-unused-version':
                    rows.append({'name': '@activepieces/piece-unused', 'version': '1.0.0'})
                elif change == 'different-version':
                    rows[-1]['version'] = '0.5.1'
                self.piece_catalog.write_text(json.dumps(rows))
                extra = () if change == 'omit' else self.piece_config()
                for dry_run in ((), ('--dry-run',)):
                    result = self.apply(code=1, extra=(*extra, *dry_run))
                    self.assertIn('stale configuration', result.stderr)
                    self.assertEqual(self.output.read_bytes(), b'previous output')
        self.piece_catalog.write_bytes(original)
        self.make_plan()  # Adding a catalog to a connection-only plan also changes policy inputs.
        self.assertIn('stale configuration', self.apply(code=1, extra=self.piece_config()).stderr)

    def test_piece_catalog_invalid_inputs_never_write_plan_or_output(self):
        self.make_plan(extra=self.piece_config())
        before = self.plan.read_bytes()
        self.output.write_bytes(b'previous output')
        invalid = ([{'name': 'p', 'version': '^1.0.0'}], [{'name': 'p'}],
                   [{'name': 'p', 'version': '1.0.0'}, 7],
                   {'data': [], 'next': 'more'}, {'data': [], 'nextCursor': 'more'})
        for value in invalid:
            with self.subTest(value=value):
                self.piece_catalog.write_text(json.dumps(value))
                self.make_plan(code=2, extra=self.piece_config())
                self.assertEqual(self.plan.read_bytes(), before)
                self.apply(code=2, extra=self.piece_config())
                self.assertEqual(self.output.read_bytes(), b'previous output')

    def test_piece_catalog_is_protected_input_and_plan_fields_are_revalidated(self):
        before = self.piece_catalog.read_bytes()
        self.make_plan(code=2, output=self.piece_catalog, extra=self.piece_config())
        self.assertEqual(self.piece_catalog.read_bytes(), before)
        original = json.loads(self.make_plan(extra=self.piece_config()).stdout)
        self.apply(code=2, output=self.piece_catalog, extra=self.piece_config())
        self.assertEqual(self.piece_catalog.read_bytes(), before)
        for field in ('targetPiecesSha256', 'pieceAvailability'):
            with self.subTest(field=field):
                value = dict(original)
                value[field] = 'tampered'
                self.plan.write_text(json.dumps(value))
                self.apply(code=1, extra=self.piece_config())
                self.assertFalse(self.output.exists())

    def test_piece_gate_with_no_connections_and_empty_catalog_is_not_optional(self):
        self.source.write_text(json.dumps({'trigger': {'name': 'trigger', 'type': 'EMPTY', 'settings': {},
            'nextAction': {'name': 'ai', 'type': 'PIECE', 'settings': {
                'pieceName': '@activepieces/piece-ai', 'pieceVersion': '0.5.0', 'input': {}}}}}))
        self.mapping.write_text('{}')
        self.catalog.write_text('[]')
        self.piece_catalog.write_text('[]')
        self.make_plan()  # No connection references at all.
        self.assertIn('missing-version', self.make_plan(code=1, extra=self.piece_config()).stderr)
        self.piece_catalog.write_text('[{"name":"@activepieces/piece-ai","version":"0.5.0"}]')
        plan = json.loads(self.make_plan(extra=self.piece_config()).stdout)
        self.assertEqual(plan['compatibility'], [])
        self.assertEqual(len(plan['pieceAvailability']), 1)
        missing_parent = self.work / 'does-not-exist' / 'out.json'
        self.apply(output=missing_parent, extra=(*self.piece_config(), '--dry-run'))
        self.assertFalse(missing_parent.parent.exists())
        self.apply(extra=self.piece_config())
        self.assertEqual(self.output.read_bytes(), self.source.read_bytes())

    def test_plan_too_large_to_reload_is_not_written(self):
        from unittest.mock import patch
        from flowwrench.ap_promote.cli import parser, run
        from flowwrench.ap_promote.jsondoc import InvalidInput
        # Source fits, but the plan repeats enough reference metadata to exceed
        # this lowered bound. The existing artifact must survive the refusal.
        self.plan.write_bytes(b'previous plan')
        args = parser().parse_args(['plan', str(self.source), *map(str, self.config()), '-o', str(self.plan)])
        with patch('flowwrench.ap_promote.jsondoc.MAX_BYTES', self.source.stat().st_size + 1):
            with self.assertRaisesRegex(InvalidInput, 'exceeds'):
                run(args)
        self.assertEqual(self.plan.read_bytes(), b'previous plan')


if __name__ == '__main__':
    unittest.main()
