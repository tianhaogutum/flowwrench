import copy
import json
from pathlib import Path
import unittest

from flowwrench.ap_promote.core import (Flow, Mapping, check_references, connection_catalog,
                                  mappings, pieces, remap, scrub)
from flowwrench.ap_promote.jsondoc import Document, InvalidInput, Refused, MAX_DEPTH

FIXTURE = Path(__file__).resolve().parent / 'fixtures'
SLACK = '@activepieces/piece-slack'
GMAIL = '@activepieces/piece-gmail'


def parsed(value):
    return Flow(Document(json.dumps(value, ensure_ascii=False)))


def minimal(auth="{{connections['old']}}", piece=SLACK):
    return {'trigger': {'name': 'trigger', 'type': 'PIECE_TRIGGER',
                        'settings': {'pieceName': piece, 'pieceVersion': '1.2.3',
                                     'input': {'auth': auth}}}}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.source = (FIXTURE / 'support-flow.json').read_text()
        self.flow = Flow(Document(self.source))
        self.mapping = mappings([], [Document.load(FIXTURE / 'map.json')])
        self.target = connection_catalog(Document.load(FIXTURE / 'target-connections.json'))

    def test_real_format_router_loop_agent_inventory_and_piece_ownership(self):
        refs = [r.record() for r in self.flow.references]
        self.assertEqual(len(refs), 5)
        self.assertEqual([r['step'] for r in refs], ['trigger', 'notify_slack', 'email_summary', 'triage_agent', 'triage_agent'])
        self.assertEqual([r['pieceName'] for r in refs], [GMAIL, SLACK, GMAIL, SLACK, GMAIL])
        self.assertIn('/children/0/firstLoopAction/', refs[1]['pointer'])
        self.assertIn('/agentTools/1/pieceMetadata/', refs[-1]['pointer'])
        self.assertEqual(len(self.flow.pieces), 6)

    def test_minimal_diff_and_verified_output_preserves_dynamic_properties(self):
        output, report = remap(self.flow, self.mapping, strict=True, target=self.target)
        self.assertEqual(report['rewritten'], 5)
        self.assertEqual(report['unmapped'], [])
        self.assertEqual(output, self.source.replace('slack-dev', 'slack-prod').replace('gmail-dev', 'gmail-prod'))
        self.assertEqual(check_references(Flow(Document(output)), self.target), [])
        original = json.loads(self.source)
        result = json.loads(output)
        for branch in (0, 1):
            s = original['flows'][0]['trigger']['nextAction']['children'][branch]
            r = result['flows'][0]['trigger']['nextAction']['children'][branch]
            if branch == 0:
                s, r = s['firstLoopAction'], r['firstLoopAction']
            self.assertEqual(s['settings']['input']['dynamicProperties'], r['settings']['input']['dynamicProperties'])

    def test_strict_idempotency(self):
        once, _ = remap(self.flow, self.mapping, strict=True, target=self.target)
        twice, report = remap(Flow(Document(once)), self.mapping, strict=True, target=self.target)
        self.assertEqual(once, twice)
        self.assertEqual(report['rewritten'], 0)
        self.assertEqual(report['alreadyMapped'], 5)

    def test_empty_map_is_byte_stable_even_nonstandard_whitespace(self):
        source = '\ufeff\r\n\t' + self.source.replace('\n', '\r\n') + ' \t'
        output, report = remap(Flow(Document(source)), {})
        self.assertEqual(output.encode(), source.encode())
        self.assertEqual(len(report['unmapped']), 5)

    def test_strict_unmapped(self):
        with self.assertRaisesRegex(Refused, '3 unmapped'):
            remap(self.flow, {'slack-dev': Mapping('slack-prod')}, strict=True)

    def test_piece_guard_with_assertion(self):
        with self.assertRaisesRegex(Refused, 'piece guard'):
            remap(self.flow, {'slack-dev': Mapping('gmail-prod', GMAIL)})

    def test_piece_guard_from_existing_reference(self):
        with self.assertRaisesRegex(Refused, 'piece guard'):
            remap(self.flow, {'slack-dev': Mapping('gmail-dev')})

    def test_piece_guard_from_many_to_one_mapping(self):
        with self.assertRaisesRegex(Refused, 'piece guard'):
            remap(self.flow, {'slack-dev': Mapping('shared'), 'gmail-dev': Mapping('shared')})

    def test_piece_guard_from_target_and_missing_target(self):
        for target in ({}, {'new': {'pieceName': GMAIL, 'status': 'ACTIVE'}},
                       {'new': {'pieceName': SLACK, 'status': 'MISSING'}}):
            with self.subTest(target=target), self.assertRaises(Refused):
                remap(parsed(minimal()), {'old': Mapping('new')}, target=target)

    def test_target_validates_unmapped_references_also(self):
        with self.assertRaisesRegex(Refused, 'target check failed'):
            remap(self.flow, {'slack-dev': Mapping('slack-prod')}, target=self.target)

    def test_check_missing_wrong_piece_inactive_and_unknown(self):
        self.assertEqual(len(check_references(self.flow, self.target)), 5)
        output, _ = remap(self.flow, self.mapping)
        target = copy.deepcopy(self.target)
        target['slack-prod']['pieceName'] = GMAIL
        errors = check_references(Flow(Document(output)), target)
        self.assertEqual([e['problem'] for e in errors], ['wrong-piece', 'wrong-piece'])
        target['slack-prod'] = {'pieceName': SLACK, 'status': 'ERROR'}
        self.assertEqual(len([e for e in check_references(Flow(Document(output)), target) if e['problem'] == 'inactive']), 2)
        value = minimal()
        value['description'] = "{{connections['orphan']}}"
        flow = parsed(value)
        catalog = {'old': {'pieceName': SLACK, 'status': 'ACTIVE'}, 'orphan': {'pieceName': SLACK, 'status': 'ACTIVE'}}
        self.assertEqual(check_references(flow, catalog)[0]['problem'], 'unknown-piece')
        with self.assertRaisesRegex(Refused, 'unknown owning piece'):
            remap(flow, {'orphan': Mapping('new')})

    def test_all_supported_wrappers_and_multi_flow(self):
        base = minimal()
        versions = [base, {'name': 'legacy', 'template': base}, {'version': base}, {'flows': [base, copy.deepcopy(base)]}]
        for i, value in enumerate(versions):
            with self.subTest(i=i):
                self.assertEqual(len(parsed(value).references), 2 if i == 3 else 1)

    def test_error_branches_batch_and_null_router_children(self):
        value = minimal()
        action = {'name': 'process', 'type': 'PIECE', 'settings': copy.deepcopy(value['trigger']['settings'])}
        action['continueOnFailureBranches'] = {
            'onSuccess': {'name': 'success', 'type': 'CODE', 'settings': {'input': {'x': "{{connections['old']}}"}}},
            'onFailure': {'name': 'fail', 'type': 'PROCESS_IN_BATCHES', 'settings': {'items': '[]'}, 'firstLoopAction': {'name': 'child', 'type': 'PIECE', 'settings': copy.deepcopy(value['trigger']['settings'])}}}
        value['trigger']['nextAction'] = {'name': 'router', 'type': 'AI_ROUTER', 'settings': {}, 'children': [None, action]}
        flow = parsed(value)
        self.assertEqual(len(flow.references), 4)
        self.assertEqual(flow.references[2].context.step, 'success')
        self.assertIsNone(flow.references[2].context.piece)  # Never inherit parent piece into CODE.

    def test_literal_agent_auth_and_generic_nested_templates(self):
        value = json.loads(self.source)
        tools = value['flows'][0]['trigger']['nextAction']['nextAction']['settings']['input']['agentTools']
        tools[0]['pieceMetadata']['predefinedInput']['auth'] = 'slack-dev'
        tools[1]['pieceMetadata']['predefinedInput']['fields']['extra'] = {'mode': 'choose-yourself', 'value': 'Use {{ connections [ "gmail-dev" ] }} twice {{connections[\'gmail-dev\']}}'}
        output, report = remap(parsed(value), self.mapping, strict=True, target=self.target)
        self.assertEqual(report['rewritten'], 7)
        self.assertIn('"auth": "slack-prod"', output)
        self.assertEqual(len(Flow(Document(output)).references), 7)

    def test_json_pointer_escape_and_no_fake_piece_context(self):
        value = minimal()
        value['trigger']['settings']['input']['a~/b'] = {'pieceName': GMAIL, 'value': "{{connections['old']}}"}
        r = parsed(value).references[-1]
        self.assertEqual(r.record()['pointer'], '/trigger/settings/input/a~0~1b/value')
        self.assertEqual(r.context.piece, SLACK)

    def test_scrub_only_step_sample_metadata(self):
        output, removed = scrub(self.flow)
        self.assertEqual(len(removed), 4)
        expected = json.loads(self.source)
        expected['flows'][0]['trigger']['settings'].pop('sampleData')
        router = expected['flows'][0]['trigger']['nextAction']
        router['children'][0]['firstLoopAction']['settings'].pop('sampleData')
        router['children'][1]['settings'].pop('sampleData')
        router['nextAction']['settings'].pop('sampleData')
        self.assertEqual(json.loads(output), expected)
        self.assertIn('"sampleData": {\n', output)  # A business input field remains.
        self.assertEqual(scrub(Flow(Document(output))), (output, []))

    def test_scrub_first_middle_last_and_only_member(self):
        for settings in ({'sampleData': {}, 'x': 1}, {'x': 1, 'sampleData': {}, 'y': 2},
                         {'x': 1, 'sampleData': {}}, {'sampleData': {}}, {'sampleData': None}):
            value = {'trigger': {'name': 'trigger', 'type': 'EMPTY', 'settings': settings}}
            for indent in (None, 2):
                with self.subTest(settings=settings, indent=indent):
                    output, removed = scrub(Flow(Document(json.dumps(value, indent=indent))))
                    self.assertEqual(len(removed), 1)
                    self.assertEqual(json.loads(output)['trigger']['settings'], {k: v for k, v in settings.items() if k != 'sampleData'})

    def test_exact_semver_pins_and_invalid_versions(self):
        for version in ('1.2.3', '0.0.0', '1.2.3-beta.1+build.02'):
            value = minimal()
            value['trigger']['settings']['pieceVersion'] = version
            self.assertEqual(pieces(parsed(value))[0]['status'], 'pinned')
        for version in ('01.2.3', '1.2.3-.', '1.2.3-beta..x', '1.2.3-01', '١.٢.٣'):
            value = minimal()
            value['trigger']['settings']['pieceVersion'] = version
            self.assertEqual(pieces(parsed(value))[0]['status'], 'unpinned')
        with self.assertRaises(InvalidInput):
            pieces(self.flow, Document('[{"name":"p","version":"~1.0.0"}]'))

    def test_scrub_refuses_unknown_sample_fields(self):
        value = minimal()
        value['trigger']['settings']['sampleData'] = {'futureData': 'valuable'}
        with self.assertRaisesRegex(InvalidInput, 'unknown sampleData'):
            scrub(parsed(value))

    def test_pieces_pins_and_exact_gaps_including_agent_tools(self):
        records = pieces(self.flow, Document.load(FIXTURE / 'target-pieces.json'))
        self.assertEqual(len(records), 6)
        self.assertTrue(all(r['status'] == 'available' for r in records))
        self.assertEqual(sum(r['status'] == 'missing-version' for r in pieces(self.flow, Document('[]'))), 6)
        for version in ('~1.2.3', '^1.2.3', '*', None):
            value = minimal()
            if version is None:
                value['trigger']['settings'].pop('pieceVersion')
            else:
                value['trigger']['settings']['pieceVersion'] = version
            self.assertEqual(pieces(parsed(value))[0]['status'], 'unpinned')


class InputBoundaryTests(unittest.TestCase):
    def test_invalid_json_and_duplicate_members(self):
        for source in ('', '{', '{"x":1,}', '[1,]', '{"x":1,"x":2}', 'null null', '{"secret":"TOPSECRET", bad}', 'NaN', 'Infinity', '1e999', '"\\ud800"'):
            with self.subTest(source=source), self.assertRaises(InvalidInput) as cm:
                Document(source)
            self.assertNotIn('TOPSECRET', str(cm.exception))

    def test_saved_agent_ids_are_explicitly_unsupported(self):
        value = minimal()
        value['agentIds'] = ['saved-agent']
        with self.assertRaisesRegex(InvalidInput, 'saved agentIds'):
            parsed(value)

    def test_saved_run_agent_and_unknown_tool_types(self):
        value = minimal('', '@activepieces/piece-ai')
        value['trigger']['settings']['actionName'] = 'run_agent'
        value['trigger']['settings']['input']['agentId'] = 'saved-agent'
        with self.assertRaisesRegex(InvalidInput, 'saved run_agent'):
            parsed(value)
        value['trigger']['settings']['input'].pop('agentId')
        value['trigger']['settings']['input']['agentTools'] = [{'type': 'FUTURE'}]
        with self.assertRaisesRegex(InvalidInput, 'unsupported agent tool type'):
            parsed(value)

    def test_node_and_reference_limits(self):
        from unittest.mock import patch
        with patch('flowwrench.ap_promote.jsondoc.MAX_NODES', 3), self.assertRaisesRegex(InvalidInput, 'node limit'):
            Document('[0, 1, 2]')
        with patch('flowwrench.ap_promote.core.MAX_REFERENCES', 2), self.assertRaisesRegex(InvalidInput, 'reference limit'):
            Flow.load(FIXTURE / 'support-flow.json')

    def test_depth_limit(self):
        with self.assertRaisesRegex(InvalidInput, 'depth/node'):
            Document('[' * (MAX_DEPTH + 2) + '0' + ']' * (MAX_DEPTH + 2))

    def test_invalid_shape_and_unsupported_steps(self):
        for value in ([], {}, {'nodes': []}, {'flows': []}, {'trigger': {}}, {'template': minimal(), 'flows': [minimal()]},
                      {'trigger': {'name': 'x', 'type': 'FUTURE', 'settings': {}}}):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                parsed(value)

    def test_expression_and_inline_auth_boundaries(self):
        for auth in ("{{connections[variable]}}", "{{connections['old'].token}}", "{{connections['old'] + 'x'}}",
                     "{{connections['old']", "{{trigger.auth}}", "prefix {{connections['old']}}", {'token': 'TOPSECRET'}):
            with self.subTest(auth=auth), self.assertRaises(InvalidInput):
                parsed(minimal(auth))

    def test_mapping_conflicts_and_chains(self):
        for entries in (['a=b', 'a=c'], ['a=b', 'b=c'], ['a=b', 'b=a'], ['bad'], ['a='], ["a=b'c"], ['a=../../x y']):
            with self.subTest(entries=entries), self.assertRaises(InvalidInput):
                mappings(entries, [])
        self.assertEqual(mappings(['a=b', 'a=b'], []), {'a': Mapping('b')})
        with self.assertRaises(InvalidInput):
            mappings(['a=b'], [Document('{"a":"c"}')])
        with self.assertRaises(InvalidInput):
            mappings([], [Document('{"a":{"externalId":"b","pieceName":"p","script":"x"}}')])

    def test_catalog_rejects_partial_pages_and_duplicates_without_echoing_secrets(self):
        for value in ({'data': [], 'next': 'cursor'}, {'data': [], 'nextCursor': 'cursor'},
                      [{'externalId': 'a', 'pieceName': 'p'}, {'externalId': 'a', 'pieceName': 'p'}],
                      [{'externalId': 'a', 'pieceName': 'p', 'status': 'INVALID'}], {'unexpected': 'TOPSECRET'}):
            with self.subTest(value=value), self.assertRaises(InvalidInput) as cm:
                connection_catalog(Document(json.dumps(value)))
            self.assertNotIn('TOPSECRET', str(cm.exception))
        catalog = connection_catalog(Document('[{"externalId":"a","pieceName":"p","value":{"access_token":"TOPSECRET"}}]'))
        self.assertNotIn('TOPSECRET', str(catalog))

    def test_raw_escaped_string_edits_preserve_every_other_byte(self):
        source = json.dumps(minimal())
        source = source.replace('old', '\\u006fld')
        source = source.replace('"auth": "', '"auth": "\\ud83d\\ude80 ')  # Generic field below, not auth.
        source = source.replace('"auth":', '"note":')
        flow = Flow(Document(source))
        output, report = remap(flow, {'old': Mapping('new')})
        self.assertEqual(report['rewritten'], 1)
        self.assertEqual(output, source.replace('\\u006fld', 'new'))
        self.assertIn('\\ud83d\\ude80', output)

    def test_string_offsets_after_json_escapes(self):
        value = minimal('')
        value['trigger']['settings']['input']['note'] = '\n🚀 "quoted" {{connections["old"]}} and {{ connections [ \'old\' ] }}'
        source = json.dumps(value)
        output, report = remap(Flow(Document(source)), {'old': Mapping('new')})
        self.assertEqual(report['rewritten'], 2)
        self.assertEqual(output, source.replace('old', 'new'))


if __name__ == '__main__':
    unittest.main()
