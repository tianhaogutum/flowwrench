import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flowwrench.schedules.extract import extract, HUGINN
from flowwrench.schedules.model import InputError
from flowwrench.schedules.yaml_subset import load_schedule

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'fixtures'


def n8n(interval, **kwargs):
    return {'name': 'test', 'active': True, 'settings': {'timezone': 'UTC'},
            'nodes': [{'name': 'tick', 'type': 'n8n-nodes-base.scheduleTrigger',
                       'parameters': {'rule': {'interval': [interval]}}, **kwargs}]}


def ap(trigger, inputs, **kwargs):
    return {'status': 'ENABLED', 'version': {'displayName': 'test', 'trigger': {'name': 'trigger',
            'settings': {'pieceName': '@activepieces/piece-schedule', 'triggerName': trigger,
                         'input': inputs}}}, **kwargs}


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, value, name='input.json'):
        path = self.path / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def one(self, value):
        return extract([self.write(value)]).schedules[0]

    def test_six_format_fixture_inventory(self):
        inv = extract([FIXTURES])
        self.assertEqual(len(inv.schedules), 14)
        self.assertEqual({r.platform for r in inv.schedules}, {'n8n', 'node-red', 'huginn', 'windmill', 'activepieces', 'crontab'})
        self.assertEqual(sum(r.enabled for r in inv.schedules), 13)
        self.assertTrue(all(r.supported for r in inv.schedules))

    def test_n8n_rule_mappings(self):
        cases = [({'field': 'minutes', 'minutesInterval': 15}, '*/15 * * * *'),
                 ({'field': 'hours', 'triggerAtMinute': 7}, '7 * * * *'),
                 ({'field': 'days', 'triggerAtMinute': 30, 'triggerAtHour': 2}, '30 2 * * *'),
                 ({'field': 'weeks', 'triggerAtDay': [1, 3], 'triggerAtHour': 8, 'triggerAtMinute': 0}, '0 8 * * 1,3'),
                 ({'field': 'months', 'triggerAtDayOfMonth': 31, 'triggerAtMinute': 0, 'triggerAtHour': 0}, '0 0 31 * *'),
                 ({'field': 'cronExpression', 'expression': '12 5 3 * * *'}, '5 3 * * *')]
        for rule, expected in cases:
            with self.subTest(rule=rule):
                record = self.one(n8n(rule))
                self.assertTrue(record.supported, record.error)
                self.assertEqual(record.expr, expected)

    def test_n8n_stateful_and_randomized_rejected(self):
        for rule, kwargs in [({'field': 'minutes', 'minutesInterval': 7}, {}),
                             ({'field': 'hours', 'hoursInterval': 2}, {}),
                             ({'field': 'days', 'daysInterval': 2}, {}),
                             ({'field': 'weeks', 'weeksInterval': 2}, {}),
                             ({'field': 'months', 'monthsInterval': 2}, {}),
                             ({'field': 'seconds'}, {}),
                             ({'field': 'days'}, {'typeVersion': 1.3}),
                             ({'field': 'days', 'triggerAtHour': '={{$json.hour}}'}, {})]:
            with self.subTest(rule=rule, kwargs=kwargs):
                self.assertFalse(self.one(n8n(rule, **kwargs)).supported)

    def test_n8n_export_array_and_disabled(self):
        wf = n8n({'field': 'cronExpression', 'expression': '@hourly'})
        wf['active'] = False
        record = self.one([wf])
        self.assertFalse(record.enabled)
        self.assertEqual(record.expr, '0 * * * *')

    def test_n8n_legacy(self):
        wf = {'nodes': [{'type': 'n8n-nodes-base.cron', 'parameters': {'triggerTimes': {'item': [
              {'mode': 'everyWeek', 'weekday': 2, 'hour': 7, 'minute': 20}]}}}]}
        record = self.one(wf)
        self.assertEqual(record.expr, '20 7 * * 2')
        self.assertTrue(record.enabled_assumed)

    def test_node_red_cron_and_disabled_tab(self):
        record = self.one([{'type': 'tab', 'id': 'f', 'label': 'Ops', 'disabled': True},
                           {'type': 'inject', 'id': 'a', 'z': 'f', 'crontab': '5 * * * *', 'repeat': ''}])
        self.assertEqual(record.workflow, 'Ops')
        self.assertEqual(record.expr, '5 * * * *')
        self.assertFalse(record.enabled)
        self.assertTrue(record.timezone_assumed)

    def test_node_red_repeat_precedence_and_startup(self):
        inv = extract([self.write([{'type': 'inject', 'repeat': '60', 'crontab': '0 * * * *'},
                                  {'type': 'inject', 'once': True}])])
        self.assertEqual([r.kind for r in inv.schedules], ['interval', 'once'])
        self.assertTrue(all(not r.supported for r in inv.schedules))

    def test_huginn_enum_mapping(self):
        for enum, expr in HUGINN.items():
            with self.subTest(enum=enum):
                r = self.one({'name': 'Scenario', 'agents': [{'name': 'Agent', 'schedule': enum}]})
                self.assertEqual(r.expr, expr)
                self.assertEqual(r.enabled, enum != 'never')
                self.assertEqual(r.day_mode, 'and')
        self.assertFalse(self.one({'agents': [{'schedule': 'every_3h'}]}).supported)

    def test_windmill_yaml_real_top_level_and_opaque_args(self):
        p = self.path / 'demo.schedule.yaml'
        p.write_text('---\nschedule: "0 15 2 * * * *" # quoted\ntimezone: Europe/Berlin\nenabled: false\nscript_path: f/demo\nargs:\n  code: !!python/object/apply:os.system ["never"]\n')
        r = extract([p]).schedules[0]
        self.assertEqual(r.expr, '15 2 * * *')
        self.assertEqual(r.timezone, 'Europe/Berlin')
        self.assertFalse(r.enabled)

    def test_yaml_unsafe_or_complex_metadata_rejected(self):
        for value in ['schedule: !!python/object:foo x', 'schedule: *alias', 'schedule: |\n  0 * * * *',
                      'schedule: &anchor "0 * * * *"', 'schedule: [0, 1]',
                      'schedule: "0 * * * *"\nschedule: "1 * * * *"',
                      'schedule: "0 * * * *"\n---\nschedule: "1 * * * *"',
                      'schedule: "0 * * * *"\n...\nenabled: true', 'schedule:\n  foo: bar',
                      'schedule: "bad\\q"']:
            with self.subTest(value=value):
                with self.assertRaises(InputError):
                    load_schedule(value)

    def test_yaml_single_quoted_comments(self):
        d = load_schedule("schedule: '0 0 * * *' # note\ntimezone: UTC\nenabled: true\n")
        self.assertEqual(d['schedule'], '0 0 * * *')
        self.assertIs(d['enabled'], True)

    def test_activepieces_presets(self):
        cases = [('every_hour', {}, '0 * * * 1-5', 'UTC'),
                 ('every_hour', {'run_on_weekends': True, 'timezone': 'Europe/Berlin'}, '0 * * * *', 'UTC'),
                 ('every_day', {'hour_of_the_day': 5, 'run_on_weekends': True, 'timezone': 'Europe/Berlin'}, '0 5 * * *', 'Europe/Berlin'),
                 ('every_week', {'hour_of_the_day': 7, 'day_of_the_week': 0}, '0 7 * * 0', 'UTC'),
                 ('every_month', {'hour_of_the_day': 9, 'day_of_the_month': 31}, '0 9 31 * *', 'UTC'),
                 ('cron_expression', {'cronExpression': '0/5 * * * *', 'timezone': 'America/New_York'}, '0/5 * * * *', 'America/New_York')]
        for typ, inputs, expr, tz in cases:
            with self.subTest(trigger=typ):
                r = self.one(ap(typ, inputs))
                self.assertTrue(r.supported, r.error)
                self.assertEqual((r.expr, r.timezone), (expr, tz))

    def test_activepieces_interval_and_dynamic_values_flagged(self):
        for typ, inputs in [('every_x_minutes', {'minutes': 7}),
                             ('every_day', {'hour_of_the_day': '{{step.hour}}'}),
                             ('every_day', {'run_on_weekends': 'false'}), ('unknown', {})]:
            with self.subTest(trigger=typ, inputs=inputs):
                self.assertFalse(self.one(ap(typ, inputs)).supported)

    def test_activepieces_flowversion_and_disabled(self):
        export = ap('every_hour', {}, status='DISABLED')
        export['flowVersion'] = export.pop('version')
        self.assertFalse(self.one(export).enabled)

    def test_crontab_environment_and_never_executes_commands(self):
        p = self.path / 'jobs.crontab'
        marker = self.path / 'MUST_NOT_EXIST'
        p.write_text(f'API_TOKEN=do-not-print\nCRON_TZ="Europe/Berlin"\n@hourly touch {marker}\n30 2 * * * echo secret-command\n@reboot touch {marker}\n')
        inv = extract([p])
        self.assertEqual(len(inv.schedules), 3)
        self.assertTrue(all(r.timezone == 'Europe/Berlin' for r in inv.schedules))
        self.assertFalse(inv.schedules[2].supported)
        self.assertFalse(marker.exists())
        dumped = json.dumps([r.as_dict() for r in inv.schedules])
        self.assertNotIn('do-not-print', dumped)
        self.assertNotIn('secret-command', dumped)
        self.assertNotIn(str(marker), dumped)

    def test_unsupported_cron_is_record_local(self):
        obj = n8n({'field': 'cronExpression', 'expression': '0 0 L * *'})
        obj['nodes'].append({'name': 'good', 'type': 'n8n-nodes-base.scheduleTrigger', 'parameters': {'rule': {'interval': [{'field': 'cronExpression', 'expression': '@hourly'}]}}})
        inv = extract([self.write(obj)])
        self.assertEqual([r.supported for r in inv.schedules], [False, True])

    def test_unknown_timezone_is_fatal_even_if_disabled(self):
        value = n8n({'field': 'days', 'triggerAtMinute': 0, 'triggerAtHour': 0})
        value['settings']['timezone'] = 'Does/NotExist'
        value['active'] = False
        with self.assertRaisesRegex(InputError, 'unknown timezone'):
            self.one(value)

    def test_timezone_paths_rejected(self):
        for tz in ['/etc/passwd', '../UTC', 'Europe/../UTC', '', 123]:
            with self.subTest(tz=tz):
                with self.assertRaises(InputError):
                    extract([FIXTURES], tz)

    def test_unrecognized_warned_and_missing_or_empty_fails(self):
        (self.path / 'unknown.txt').write_text('hello')
        self.write({'not': 'a workflow'})
        with self.assertRaisesRegex(InputError, 'no schedules found'):
            extract([self.path])
        self.write(n8n({'field': 'days', 'triggerAtMinute': 0, 'triggerAtHour': 0}), 'valid.json')
        inv = extract([self.path])
        self.assertEqual(len(inv.warnings), 2)
        with self.assertRaisesRegex(InputError, 'does not exist'):
            extract([self.path / 'missing'])

    def test_malformed_inputs_do_not_leak_payloads(self):
        for text in ['{"secret":', '{"secret":NaN}', '{"nodes":[],"nodes":[]}', '[[' * 2000,
                     '{"agents":[1]}', '{"nodes":[{"type":"n8n-nodes-base.scheduleTrigger","parameters":null}]}']:
            with self.subTest(text=text[:50]):
                p = self.path / 'bad.json'
                p.write_text(text)
                if 'parameters' in text:
                    self.assertFalse(extract([p]).schedules[0].supported)
                else:
                    with self.assertRaises(InputError) as cm:
                        extract([p])
                    self.assertNotIn('secret', str(cm.exception))

    def test_symlinks_skipped_and_repeated_paths_deduplicated(self):
        p = self.write(n8n({'field': 'days', 'triggerAtMinute': 0, 'triggerAtHour': 0}))
        (self.path / 'link.json').symlink_to(p)
        inv = extract([self.path, p])
        self.assertEqual(len(inv.schedules), 1)
        self.assertTrue(any('symlink' in w for w in inv.warnings))

    def test_size_and_record_limits(self):
        p = self.write(n8n({'field': 'days', 'triggerAtMinute': 0, 'triggerAtHour': 0}))
        with patch('flowwrench.schedules.extract.MAX_FILE_BYTES', 10):
            with self.assertRaisesRegex(InputError, 'size limit'):
                extract([p])
        with patch('flowwrench.schedules.extract.MAX_RECORDS', 0):
            with self.assertRaisesRegex(InputError, 'schedules'):
                extract([p])

    def test_platform_day_modes_and_activepieces_combination_boundary(self):
        self.assertEqual(self.one(n8n({'field': 'cronExpression', 'expression': '0 0 */2 * MON'})).day_mode, 'or-values')
        self.assertEqual(self.one([{'type': 'inject', 'crontab': '0 0 */2 * MON'}]).day_mode, 'or-literal')
        r = self.one(ap('cron_expression', {'cronExpression': '0 0 1 * MON'}))
        self.assertFalse(r.supported)
        self.assertIn('combined day-of-month/week', r.error)

    def test_oversized_or_structured_labels_rejected_without_echoing_payload(self):
        for name in ['x' * 513, {'credential': 'DO_NOT_ECHO'}]:
            obj = n8n({'field': 'cronExpression', 'expression': '@hourly'})
            obj['nodes'][0]['name'] = name
            with self.subTest(name_type=type(name).__name__):
                with self.assertRaises(InputError) as cm:
                    self.one(obj)
                self.assertNotIn('DO_NOT_ECHO', str(cm.exception))
