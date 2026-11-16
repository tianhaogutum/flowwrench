"""Local synthetic re-exports: verify references, not upstream execution."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flowwrench.ap_promote.core import Flow, mappings, remap
from flowwrench.ap_promote.jsondoc import Document

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'
LOOP_AUTH = '/flows/0/trigger/nextAction/children/0/firstLoopAction/settings/input/auth'
ROUTER_AUTH = '/flows/0/trigger/nextAction/children/1/settings/input/auth'
TOOLS = '/flows/0/trigger/nextAction/nextAction/settings/input/agentTools/'
TOOL_AUTH = '0/pieceMetadata/predefinedInput/auth'


class RoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.roundtrip-ap-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'source.json'
        self.destination = self.work / 'destination.json'
        self.mapping = self.work / 'map.json'
        self.source.write_bytes((FIXTURES / 'support-flow.json').read_bytes())
        self.mapping.write_bytes((FIXTURES / 'map.json').read_bytes())
        output, _ = remap(Flow.load(self.source), mappings([], [Document.load(self.mapping)]), strict=True)
        self.destination.write_text(output)

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, '-m', 'flowwrench.ap_promote', *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def verify(self, code=0, extra=()):
        return json.loads(self.cli('verify-roundtrip', self.source, self.destination,
                                  '--map-file', self.mapping, '--json', *extra, code=code).stdout)

    def mutate(self, path, edit):
        value = json.loads(path.read_text())
        edit(value)
        path.write_text(json.dumps(value))

    def test_restore_all_five_source_identities_without_writes(self):
        before = {p: p.read_bytes() for p in self.work.iterdir()}
        report = self.verify()
        self.assertTrue(report['ok'])
        self.assertEqual(report['sourceReferences'], 5)
        self.assertEqual(report['destinationReferences'], 5)
        self.assertEqual(report['verified'], 5)
        self.assertEqual(report['errors'], [])
        self.assertEqual([r['restoredExternalId'] for r in report['matches']],
                         ['gmail-dev', 'slack-dev', 'gmail-dev', 'slack-dev', 'gmail-dev'])
        self.assertTrue(all(r['destinationExternalId'].endswith('-prod') for r in report['matches']))
        self.assertEqual({p: p.read_bytes() for p in self.work.iterdir()}, before)

    def test_reports_lost_loop_router_and_both_tool_references_that_check_misses(self):
        def lose(value):
            router = value['flows'][0]['trigger']['nextAction']
            router['children'][0]['firstLoopAction'] = None  # Whole nested step disappeared.
            del router['children'][1]['settings']['input']['auth']
            tools = router['nextAction']['settings']['input']['agentTools']
            del tools[0]['pieceMetadata']['predefinedInput']['auth']
            tools[1]['pieceMetadata']['predefinedInput']['auth'] = None
        self.mutate(self.destination, lose)
        # Existing check sees only one valid remaining reference: prove the gap.
        checked = self.cli('check', self.destination, '--target-connections',
                           FIXTURES / 'target-connections.json', '--json')
        self.assertTrue(json.loads(checked.stdout)['ok'])
        report = self.verify(code=1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['verified'], 1)
        self.assertEqual([r['sourcePointer'] for r in report['errors']],
                         [LOOP_AUTH, ROUTER_AUTH, TOOLS + TOOL_AUTH,
                          TOOLS + '1/pieceMetadata/predefinedInput/auth'])
        self.assertTrue(all(r['problem'] == 'lost-reference' and r['destinationPointer'] is None
                            for r in report['errors']))

    def test_envelope_changes_and_router_relocation_keep_exact_pointers(self):
        value = json.loads(self.destination.read_text())['flows'][0]
        router = value['trigger']['nextAction']
        router['children'].reverse()
        self.destination.write_text(json.dumps({'version': value}))
        report = self.verify()
        loop = report['matches'][1]
        self.assertEqual(loop['sourcePointer'], LOOP_AUTH)
        self.assertEqual(loop['destinationPointer'],
                         '/version/trigger/nextAction/children/1/firstLoopAction/settings/input/auth')

    def test_many_to_one_restores_per_location_including_existing_destination(self):
        def step(name, auth, trigger=False):
            return dict(name=name, type='PIECE_TRIGGER' if trigger else 'PIECE',
                        settings=dict(pieceName='@activepieces/piece-slack', pieceVersion='1.2.3',
                                      input=dict(auth=auth)))
        source = {'trigger': step('trigger', 'dev-a', True)}
        source['trigger']['nextAction'] = step('second', 'dev-b')
        source['trigger']['nextAction']['nextAction'] = step('third', 'prod')
        self.source.write_text(json.dumps(source))
        self.mapping.write_text(json.dumps({'dev-a': 'prod', 'dev-b': 'prod'}))
        dest = json.dumps(source).replace('dev-a', 'prod').replace('dev-b', 'prod')
        self.destination.write_text(dest)
        report = self.verify()
        self.assertEqual([r['restoredExternalId'] for r in report['matches']], ['dev-a', 'dev-b', 'prod'])

    def test_wrong_destination_is_not_restored_even_if_it_is_another_known_id(self):
        self.mutate(self.destination, lambda v: v['flows'][0]['trigger']['settings']['input'].update(auth='slack-prod'))
        report = self.verify(code=1)
        error, = report['errors']
        self.assertEqual(error['problem'], 'wrong-connection')
        self.assertEqual(error['sourceExternalId'], 'gmail-dev')
        self.assertEqual(error['expectedDestinationExternalId'], 'gmail-prod')
        self.assertEqual(error['destinationExternalId'], 'slack-prod')
        self.assertEqual(error['sourcePointer'], '/flows/0/trigger/settings/input/auth')
        self.assertNotIn('restoredExternalId', error)

    def test_tool_piece_and_version_changes_are_reported(self):
        for field, value, problem in (('pieceName', '@activepieces/piece-other', 'wrong-piece'),
                                      ('pieceVersion', '99.0.0', 'wrong-version')):
            with self.subTest(field=field):
                original = self.destination.read_bytes()
                def edit(v):
                    meta = v['flows'][0]['trigger']['nextAction']['nextAction']['settings']['input']['agentTools'][0]['pieceMetadata']
                    meta[field] = value
                self.mutate(self.destination, edit)
                error, = self.verify(code=1)['errors']
                self.assertEqual(error['problem'], problem)
                self.assertEqual(error['sourcePointer'], TOOLS + TOOL_AUTH)
                self.assertEqual(error['destinationPointer'], TOOLS + TOOL_AUTH)
                self.destination.write_bytes(original)

    def test_repeated_occurrences_and_escaped_pointers(self):
        key = 'a~/b'
        def add(v, identifier, count):
            inputs = v['flows'][0]['trigger']['settings']['input']
            inputs[key] = ' '.join(["{{connections['" + identifier + "']}}"] * count)
        self.mutate(self.source, lambda v: add(v, 'gmail-dev', 2))
        self.mutate(self.destination, lambda v: add(v, 'gmail-prod', 1))
        error, = self.verify(code=1)['errors']
        self.assertEqual(error['problem'], 'lost-reference')
        self.assertEqual(error['sourcePointer'], '/flows/0/trigger/settings/input/a~0~1b')
        self.assertEqual(error['occurrence'], 1)
        self.assertEqual(self.verify(code=1)['verified'], 6)

    def test_unexpected_reference_is_reported_even_with_unknown_owner(self):
        self.mutate(self.destination, lambda v: v.update(note="{{connections['unexpected']}}"))
        error, = self.verify(code=1)['errors']
        self.assertEqual(error['problem'], 'unexpected-reference')
        self.assertIsNone(error['sourcePointer'])
        self.assertEqual(error['destinationPointer'], '/note')

    def test_multi_flow_names_do_not_mask_a_lost_reference(self):
        for path in (self.source, self.destination):
            self.mutate(path, lambda v: v['flows'].append(copy.deepcopy(v['flows'][0])))
        self.mutate(self.destination, lambda v: v['flows'][1]['trigger']['nextAction']['children'].__setitem__(0, None))
        report = self.verify(code=1)
        error, = report['errors']
        self.assertEqual(report['verified'], 9)
        self.assertEqual(error['flowIndex'], 1)
        self.assertEqual(error['sourcePointer'], LOOP_AUTH.replace('/flows/0/', '/flows/1/'))

    def test_plain_auth_and_template_formatting_are_equivalent(self):
        self.mutate(self.destination, lambda v: v['flows'][0]['trigger']['settings']['input'].update(auth='gmail-prod'))
        self.assertTrue(self.verify()['ok'])

    def multi_flow(self):
        for path, name in ((self.source, 'source'), (self.destination, 'reexport'), (self.mapping, 'map')):
            path.write_bytes((FIXTURES / f'roundtrip-multi-{name}.json').read_bytes())

    def test_explicit_reordered_pairing_restores_distinct_ids_and_exact_pointers(self):
        self.multi_flow()
        # Prior order-based behavior compares each repeated name to the wrong flow.
        default = self.verify(code=1)
        self.assertEqual(default['verified'], 0)
        self.assertEqual({e['problem'] for e in default['errors']}, {'wrong-connection'})
        before = {p: p.read_bytes() for p in self.work.iterdir()}
        report = self.verify(extra=('--flow-pair', '1=0', '--flow-pair', '0=1'))
        self.assertTrue(report['ok'])
        self.assertEqual(report['verified'], 8)
        self.assertEqual(report['flowPairs'], [
            dict(sourceFlowIndex=0, destinationFlowIndex=1,
                 sourceFlowPointer='/flows/0', destinationFlowPointer='/flows/1'),
            dict(sourceFlowIndex=1, destinationFlowIndex=0,
                 sourceFlowPointer='/flows/1', destinationFlowPointer='/flows/0')])
        for src, dst, prefix in ((0, 1, 'alpha'), (1, 0, 'beta')):
            records = report['matches'][src * 4:(src + 1) * 4]
            self.assertEqual([r['restoredExternalId'] for r in records],
                             [f'{prefix}-dev'] * 3 + [f'{prefix}-tool-dev'])
            self.assertTrue(all(r['flowIndex'] == src and r['sourceFlowIndex'] == src
                                and r['destinationFlowIndex'] == dst for r in records))
            self.assertEqual(records[0]['sourcePointer'], f'/flows/{src}/trigger/settings/input/auth')
            self.assertEqual(records[0]['destinationPointer'], f'/flows/{dst}/trigger/settings/input/auth')
            self.assertEqual([r['occurrence'] for r in records[1:3]], [0, 1])
            self.assertEqual(records[1]['sourcePointer'], f'/flows/{src}/trigger/settings/input/a~0~1b')
            self.assertEqual(records[1]['destinationPointer'], f'/flows/{dst}/trigger/settings/input/a~0~1b')
            self.assertEqual(records[3]['sourcePointer'], f'/flows/{src}/trigger/nextAction/settings/input/agentTools/{TOOL_AUTH}')
            self.assertEqual(records[3]['destinationPointer'], f'/flows/{dst}/trigger/nextAction/children/1/settings/input/agentTools/{TOOL_AUTH}')
        self.assertEqual({p: p.read_bytes() for p in self.work.iterdir()}, before)

    def test_pairing_does_not_hide_loss_wrong_id_or_unexpected_reference(self):
        self.multi_flow()
        def lose(v):
            inputs = v['flows'][1]['trigger']['settings']['input']
            inputs.pop('auth')
            inputs['a~/b'] = "{{connections['beta-prod']}} {{connections['alpha-prod']}}"
            v['flows'][0]['trigger']['settings']['input']['extra'] = "{{connections['beta-prod']}}"
        self.mutate(self.destination, lose)
        report = self.verify(code=1, extra=('--flow-pair', '0=1', '--flow-pair', '1=0'))
        self.assertEqual(report['verified'], 6)
        lost, wrong, extra = report['errors']
        self.assertEqual(lost['problem'], 'lost-reference')
        self.assertEqual(lost['sourcePointer'], '/flows/0/trigger/settings/input/auth')
        self.assertIsNone(lost['destinationPointer'])
        self.assertEqual((lost['sourceFlowIndex'], lost['destinationFlowIndex']), (0, 1))
        self.assertEqual(wrong['problem'], 'wrong-connection')
        self.assertEqual(wrong['sourcePointer'], '/flows/0/trigger/settings/input/a~0~1b')
        self.assertEqual(wrong['destinationPointer'], '/flows/1/trigger/settings/input/a~0~1b')
        self.assertEqual(wrong['occurrence'], 0)
        self.assertEqual(extra['problem'], 'unexpected-reference')
        self.assertIsNone(extra['sourcePointer'])
        self.assertEqual(extra['destinationPointer'], '/flows/0/trigger/settings/input/extra')
        self.assertEqual((extra['sourceFlowIndex'], extra['destinationFlowIndex']), (1, 0))
        self.assertNotIn('restoredExternalId', wrong)

    def test_invalid_flow_pairs_refuse_without_partial_report_or_input_changes(self):
        self.multi_flow()
        before = {p: p.read_bytes() for p in self.work.iterdir()}
        cases = [
            ('0=1',),  # Partial maps never silently fall back to array order.
            ('0=1', '0=0'), ('0=1', '1=1'), ('0=1', '1=0', '0=1'),
            ('2=1', '1=0'), ('0=2', '1=0'), ('-1=0', '1=1'),
            ('0',), ('0=1=0',), ('x=1',), ('0=1.0',), ('=1',), ('0=',),
            (' 0=1',), ('00=1',), ('0=+1',), ('0=١',), ('9' * 5000 + '=0',)]
        for pairs in cases:
            with self.subTest(pairs=tuple(p[:40] for p in pairs)):
                flags = [flag for pair in pairs for flag in ('--flow-pair', pair)]
                result = self.cli('verify-roundtrip', self.source, self.destination,
                                  '--map-file', self.mapping, '--json', *flags, code=2)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
        self.assertEqual({p: p.read_bytes() for p in self.work.iterdir()}, before)

    def test_explicit_wrong_bijection_is_a_comparison_failure(self):
        self.multi_flow()
        report = self.verify(code=1, extra=('--flow-pair', '0=0', '--flow-pair', '1=1'))
        self.assertEqual(report['verified'], 0)
        self.assertEqual(len(report['errors']), 8)
        self.assertEqual({e['problem'] for e in report['errors']}, {'wrong-connection'})

    def test_pairing_requires_reference_free_flows_and_equal_flow_counts(self):
        self.multi_flow()
        empty = dict(trigger=dict(name='trigger', type='EMPTY', settings={}))
        for path in (self.source, self.destination):
            self.mutate(path, lambda v: v['flows'].append(copy.deepcopy(empty)))
        args = ('verify-roundtrip', self.source, self.destination, '--map-file', self.mapping,
                '--json', '--flow-pair', '0=1', '--flow-pair', '1=0')
        self.assertEqual(self.cli(*args, code=2).stdout, '')
        self.assertTrue(self.verify(extra=('--flow-pair', '0=1', '--flow-pair', '1=0', '--flow-pair', '2=2'))['ok'])
        self.mutate(self.destination, lambda v: v['flows'].pop())
        self.assertEqual(self.cli(*args, code=2).stdout, '')

    def test_pairing_reports_unowned_extras_and_keeps_strict_mapping_guards(self):
        self.multi_flow()
        self.mutate(self.destination, lambda v: v.update(note="{{connections['extra']}}"))
        report = self.verify(code=1, extra=('--flow-pair', '0=1', '--flow-pair', '1=0'))
        error, = report['errors']
        self.assertEqual(error['destinationPointer'], '/note')
        self.assertIsNone(error['sourceFlowIndex'])
        self.assertIsNone(error['destinationFlowIndex'])
        self.mapping.write_text('{}')
        result = self.cli('verify-roundtrip', self.source, self.destination, '--map-file', self.mapping,
                          '--flow-pair', '0=1', '--flow-pair', '1=0', '--json', code=1)
        self.assertEqual(result.stdout, '')
        self.assertIn('unmapped', result.stderr)

    def test_explicit_pairing_text_report_lists_actual_flow_pointers(self):
        self.multi_flow()
        result = self.cli('verify-roundtrip', self.source, self.destination, '--map-file', self.mapping,
                          '--flow-pair', '0=1', '--flow-pair', '1=0')
        self.assertIn('sourceFlowIndex\tdestinationFlowIndex', result.stdout)
        self.assertIn('0\t1\t"/flows/0"\t"/flows/1"', result.stdout)
        self.assertIn('8/8 source references verified; 0 errors', result.stdout)

    def test_pairing_single_flow_across_all_envelopes(self):
        source_flow = json.loads(self.source.read_text())['flows'][0]
        dest_flow = json.loads(self.destination.read_text())['flows'][0]
        envelopes = [lambda f: f, lambda f: {'template': f},
                     lambda f: {'version': f}, lambda f: {'flows': [f]}]
        roots = ['', '/template', '/version', '/flows/0']
        for i, envelope in enumerate(envelopes):
            with self.subTest(sourceRoot=roots[i]):
                j = (i + 1) % len(envelopes)
                self.source.write_text(json.dumps(envelope(source_flow)))
                self.destination.write_text(json.dumps(envelopes[j](dest_flow)))
                report = self.verify(extra=('--flow-pair', '0=0'))
                self.assertEqual(report['verified'], 5)
                pair, = report['flowPairs']
                self.assertEqual(pair['sourceFlowPointer'], roots[i])
                self.assertEqual(pair['destinationFlowPointer'], roots[j])
                self.assertEqual(report['matches'][0]['sourcePointer'], roots[i] + '/trigger/settings/input/auth')
                self.assertEqual(report['matches'][0]['destinationPointer'], roots[j] + '/trigger/settings/input/auth')

    def test_nested_agent_in_batch_error_branch_reports_each_lost_tool(self):
        # Exercise tool ownership beneath all supported branch/loop mechanisms.
        for path in (self.source, self.destination):
            def nest(v):
                router = v['flows'][0]['trigger']['nextAction']
                router['type'] = 'AI_ROUTER'
                batch = router['children'][0]
                batch['type'] = 'PROCESS_IN_BATCHES'
                batch['firstLoopAction']['continueOnFailureBranches'] = {
                    'onFailure': router.pop('nextAction'), 'onSuccess': None}
            self.mutate(path, nest)
        def lose(v):
            loop = v['flows'][0]['trigger']['nextAction']['children'][0]['firstLoopAction']
            loop['continueOnFailureBranches']['onFailure']['settings']['input']['agentTools'] = []
        self.mutate(self.destination, lose)
        report = self.verify(code=1)
        prefix = ('/flows/0/trigger/nextAction/children/0/firstLoopAction/'
                  'continueOnFailureBranches/onFailure/settings/input/agentTools/')
        self.assertEqual(report['verified'], 3)
        self.assertEqual([e['sourcePointer'] for e in report['errors']],
                         [prefix + str(i) + '/pieceMetadata/predefinedInput/auth' for i in (0, 1)])

    def test_explicit_maps_required_and_cli_maps_combine(self):
        self.cli('verify-roundtrip', self.source, self.destination, code=2)
        result = self.cli('verify-roundtrip', self.source, self.destination,
                          '--map', 'slack-dev=slack-prod', '--map', 'gmail-dev=gmail-prod', '--json')
        self.assertEqual(json.loads(result.stdout)['verified'], 5)
        self.cli('verify-roundtrip', self.source, self.destination, '--map-file', self.mapping,
                 '--map', 'gmail-dev=conflicting', code=2)

    def test_identity_mapping_and_empty_reference_set(self):
        self.destination.write_bytes(self.source.read_bytes())
        self.mapping.write_text('{"gmail-dev":"gmail-dev", "slack-dev":"slack-dev"}')
        self.assertEqual(self.verify()['verified'], 5)
        value = '{"trigger":{"name":"t","type":"EMPTY","settings":{}}}'
        self.source.write_text(value)
        self.destination.write_text(value)
        self.mapping.write_text('{}')
        self.assertTrue(self.verify()['ok'])

    def test_mapping_guards_and_invalid_inputs_preserve_files(self):
        before = self.destination.read_bytes()
        self.mapping.write_text('{"slack-dev":"slack-prod"}')
        refused = self.cli('verify-roundtrip', self.source, self.destination,
                           '--map-file', self.mapping, '--json', code=1)
        self.assertIn('unmapped', refused.stderr)
        self.assertEqual(refused.stdout, '')
        self.cli('verify-roundtrip', self.source, self.destination, '--map', 'a=b', '--map', 'b=c', code=2)
        self.destination.write_text('{"token":"SECRET_SENTINEL", broken}')
        result = self.cli('verify-roundtrip', self.source, self.destination, '--map-file', self.mapping, code=2)
        self.assertNotIn('SECRET_SENTINEL', result.stdout + result.stderr)
        self.destination.write_bytes(before)

    def test_text_report_contains_loss_pointer(self):
        self.mutate(self.destination, lambda v: v['flows'][0]['trigger']['nextAction']['children'].__setitem__(0, None))
        result = self.cli('verify-roundtrip', self.source, self.destination,
                          '--map-file', self.mapping, code=1)
        self.assertIn('lost-reference', result.stdout)
        self.assertIn(LOOP_AUTH, result.stdout)


if __name__ == '__main__':
    unittest.main()
