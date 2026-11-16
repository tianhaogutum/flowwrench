"""Concrete graph export contracts; no Graphviz or Huginn dependency."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flowwrench.huginn.core import from_export, split_files
from flowwrench.huginn.formats import KitError, write_directory, yaml_text

ROOT = Path(__file__).resolve().parent


def scenario():
    # Unsorted GUIDs, a cycle, self-links, both kinds for a pair, and isolation.
    return dict(schema_version=1, guid="scenario", name="Graph", agents=[
        dict(guid="z", name="Isolated", type="Agents::ManualAgent", disabled=True, options={}),
        dict(guid="b", name="Sink", type="Agents::LogAgent", disabled=False, options={}),
        dict(guid="a", name="Source", type="Agents::ManualAgent", disabled=False, options={}),
    ], links=[dict(source=2, receiver=1), dict(source=1, receiver=2), dict(source=2, receiver=2)],
        control_links=[dict(controller=2, control_target=1), dict(controller=1, control_target=1)])


EXPECTED = '''digraph scenario {
  "a" [label=<Source<BR/>Agents::ManualAgent>];
  "b" [label=<Sink<BR/>Agents::LogAgent>];
  "z" [label=<Isolated<BR/>Agents::ManualAgent<BR/>(disabled)>, style="dashed"];
  "a" -> "a" [style="solid", label="event"];
  "a" -> "b" [style="solid", label="event"];
  "b" -> "a" [style="solid", label="event"];
  "a" -> "b" [style="dashed", label="control"];
  "b" -> "b" [style="dashed", label="control"];
}
'''


class GraphTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.source = self.path / 'input.json'
        self.source.write_text(json.dumps(scenario()))

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, '-m', 'flowwrench.huginn', *map(str, args)],
                              cwd=ROOT, env=env, text=True, capture_output=True)

    def snapshot(self):
        return {str(p.relative_to(self.path)): p.read_bytes() for p in self.path.rglob('*') if p.is_file()}

    def kit(self, raw=None, name='kit'):
        path = self.path / name
        write_directory(path, split_files(from_export(raw or scenario())))
        return path

    def refuse(self, source, reason):
        existing = self.path / 'existing.dot'
        existing.write_bytes(b'existing output\n')
        for output in (None, self.path/'absent.dot', existing):
            before = self.snapshot()
            args = ('graph', source) + (('-o', output) if output else ())
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertIn(reason, result.stderr)
            self.assertEqual(before, self.snapshot())

    def test_explicit_dot_for_cycles_self_links_both_kinds_and_isolation(self):
        before = self.snapshot()
        result = self.cli('graph', self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout, EXPECTED)
        self.assertEqual(self.snapshot(), before)

    def test_export_reordering_and_kit_filename_renames_are_byte_identical(self):
        raw = scenario()
        order = [2, 0, 1]
        new_index = {old: new for new, old in enumerate(order)}
        raw['agents'] = [raw['agents'][i] for i in order]
        for kind in ('links', 'control_links'):
            raw[kind] = [{k: new_index[v] for k, v in edge.items()} for edge in reversed(raw[kind])]
        self.source.write_text(json.dumps(raw, indent=4))
        kit = self.kit(raw)
        for i, p in enumerate(sorted((kit/'agents').iterdir())):
            p.rename(p.with_name(f'{9-i}.yaml'))
        before = self.snapshot()
        for source in (self.source, kit):
            result = self.cli('graph', source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, EXPECTED)
        self.assertEqual(before, self.snapshot())

    def test_name_changes_affect_only_display_and_duplicate_names_do_not_merge(self):
        raw = scenario()
        for agent in raw['agents']:
            agent['name'] = 'Same new name'
        self.source.write_text(json.dumps(raw))
        result = self.cli('graph', self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = EXPECTED.replace('Source<BR/>', 'Same new name<BR/>').replace('Sink<BR/>', 'Same new name<BR/>').replace('Isolated<BR/>', 'Same new name<BR/>')
        self.assertEqual(result.stdout, expected)

    def test_safe_display_text_quotes_backslash_controls_unicode_and_markup(self):
        raw = scenario()
        raw['agents'][2]['name'] = '"\\N\r\n\t\x00\x7f\x85\u2028\u2029<&>\'é😀'
        raw['agents'][2]['type'] = 'Agents::"Evil\\l</TD><IMG SRC="x">'
        self.source.write_text(json.dumps(raw))
        result = self.cli('graph', self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        name = '&#x22;&#x5c;N[U+000D][U+000A][U+0009][U+0000][U+007F][U+0085][U+2028][U+2029]&#x3c;&#x26;&#x3e;&#x27;&#xe9;&#x1f600;'
        kind = 'Agents::&#x22;Evil&#x5c;l&#x3c;/TD&#x3e;&#x3c;IMG SRC=&#x22;x&#x22;&#x3e;'
        self.assertIn(f'"a" [label=<{name}<BR/>{kind}>];\n', result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), len(EXPECTED.splitlines()))
        self.assertTrue(result.stdout.isascii())

    def test_unknown_schedule_unevaluated_and_opaque_data_omitted(self):
        raw = scenario()
        raw['agents'][2].update(schedule='unknown/custom', receivers=['z'], propagation=['z'])
        raw['agents'][2]['options'] = {'secret': 'DO_NOT_DISCLOSE', 'receivers': ['z'], 'script': 'touch /not/run',
                                     'text': '{{ env.SECRET }} {% credential user.name | downcase %}'}
        self.source.write_text(json.dumps(raw))
        kit = self.kit()  # Change a concrete kit without split's known-schedule check.
        agent = next(p for p in (kit/'agents').iterdir() if 'source' in p.name)
        agent.write_text(yaml_text(raw['agents'][2]))
        before = self.snapshot()
        env = dict(os.environ, SECRET='ENV_DO_NOT_DISCLOSE')
        for source in (self.source, kit):
            result = self.cli('graph', source, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, EXPECTED)
        self.assertEqual(before, self.snapshot())

    def test_templates_not_rendered_in_either_input_format(self):
        for value in ('${HSK:NAME}', '${HSK:bad space}'):
            for field in ('name', 'options'):
                raw = scenario()
                raw['agents'][2][field] = value if field == 'name' else {'nested': [value]}
                self.source.write_text(json.dumps(raw))
                self.refuse(self.source, 'placeholder' if ' ' in value else 'unresolved variables')
        kit = self.kit()
        agent = next(p for p in (kit/'agents').iterdir() if 'source' in p.name)
        raw = scenario()['agents'][2]
        raw['options'] = {'${HSK:KEY}': 'opaque'}
        agent.write_text(yaml_text(raw))
        self.refuse(kit, 'mapping keys')
        raw['options'] = '${HSK:OPTIONS}'
        agent.write_text(yaml_text(raw))
        self.refuse(kit, 'must be a mapping')

    def test_json_reference_guid_and_agent_failures_do_not_write(self):
        cases = []
        for kind, fields in (('links', ('source', 'receiver')), ('control_links', ('controller', 'control_target'))):
            for field in fields:
                for invalid in (-1, 100, True, 1.5, 'a', None):
                    raw = scenario(); raw[kind][0][field] = invalid
                    cases.append((raw, f'#/{kind}/0/{field}'))
            raw = scenario(); raw[kind].append(deepcopy(raw[kind][0])); cases.append((raw, 'duplicate edge'))
            raw = scenario(); raw[kind][0]['extra'] = 1; cases.append((raw, 'link must contain exactly'))
        raw = scenario(); raw['agents'][1]['guid'] = 'a'; cases.append((raw, 'duplicate agent guid'))
        raw = scenario(); raw['agents'][1]['guid'] = 'bad guid'; cases.append((raw, 'must be 1-128'))
        raw = scenario(); raw['agents'][1]['schedule'] = []; cases.append((raw, 'must be a string or null'))
        for raw, reason in cases:
            with self.subTest(reason=reason, raw=raw):
                self.source.write_text(json.dumps(raw))
                self.refuse(self.source, reason)

    def test_kit_dangling_duplicates_deleted_agent_and_invalid_guid(self):
        kit = self.kit()
        manifest = kit/'scenario.yaml'
        original = manifest.read_bytes()
        for kind, field in (('links', 'receiver'), ('control_links', 'controller')):
            from flowwrench.huginn.formats import load_data
            data = load_data(manifest); data[kind][0][field] = 'missing'
            manifest.write_text(yaml_text(data)); self.refuse(kit, f'#/{kind}/0/{field}: dangling guid')
            manifest.write_bytes(original)
            data = load_data(manifest); data[kind].append(deepcopy(data[kind][0]))
            manifest.write_text(yaml_text(data)); self.refuse(kit, 'duplicate edge')
            manifest.write_bytes(original)
        agent = next(p for p in (kit/'agents').iterdir() if 'source' in p.name)
        content = agent.read_bytes(); agent.unlink()
        self.refuse(kit, 'dangling guid')
        agent.write_bytes(content)
        (kit/'agents'/'copy.yaml').write_bytes(content)
        self.refuse(kit, 'duplicate agent guid')

    def test_private_exclusive_output_and_symlink_refusal(self):
        output = self.path/'new.dot'
        result = self.cli('graph', self.source, '-o', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(output.read_text(), EXPECTED)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        before = self.snapshot()
        result = self.cli('graph', self.source, '-o', output)
        self.assertEqual(result.returncode, 2); self.assertIn('overwrite', result.stderr)
        self.assertEqual(before, self.snapshot())
        link = self.path/'link.dot'; link.symlink_to(output)
        result = self.cli('graph', self.source, '-o', link)
        self.assertEqual(result.returncode, 2); self.assertIn('symlink', result.stderr)
        self.assertEqual(output.read_text(), EXPECTED)
        result = self.cli('graph', self.source, '-o', self.path/'missing'/'new.dot')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.path/'missing').exists())
        link = self.path/'input-link'; link.symlink_to(self.source)
        self.refuse(link, 'symlink')

    def test_empty_graph_and_absent_edge_arrays(self):
        raw = scenario(); raw.update(agents=[]); raw.pop('links'); raw.pop('control_links')
        self.source.write_text(json.dumps(raw))
        result = self.cli('graph', self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'digraph scenario {\n}\n')

    def test_shared_resource_bounds_and_generated_output_limit(self):
        from flowwrench.huginn.graph import dot
        raw = scenario(); raw['agents'] *= 334
        self.source.write_text(json.dumps(raw)); self.refuse(self.source, 'at most 1000')
        raw = scenario(); raw['links'] *= 3334
        self.source.write_text(json.dumps(raw)); self.refuse(self.source, 'at most 10000')
        doc = from_export(scenario()); before = deepcopy(doc)
        with patch('flowwrench.huginn.graph.MAX_FILE_BYTES', 100), self.assertRaisesRegex(KitError, 'generated DOT exceeds'):
            dot(doc)
        self.assertEqual(doc, before)
        # Exercise the real CLI size boundary: small input expands via safe entities.
        raw = scenario(); raw['agents'][2]['name'] = 'é' * 900000
        self.source.write_text(json.dumps(raw, ensure_ascii=False))
        self.refuse(self.source, 'generated DOT exceeds')

    def test_exact_guid_identity_and_library_input_immutability(self):
        from flowwrench.huginn.graph import dot
        raw = scenario()
        # Names can collide; GUID case, prefixes and punctuation remain exact.
        for agent, guid in zip(raw['agents'], ('A', 'a', 'a_-')):
            agent.update(guid=guid, name='Same')
        doc = from_export(raw)
        before = deepcopy(doc)
        output = dot(doc)
        self.assertEqual(doc, before)
        nodes = [line.split(' [')[0] for line in output.splitlines() if ' -> ' not in line and 'label=' in line]
        self.assertEqual(nodes, ['  "A"', '  "a"', '  "a_-"'])
        self.assertIn('"a_-" -> "a" [style="solid", label="event"]', output)
        self.assertIn('"a_-" -> "a" [style="dashed", label="control"]', output)

    def test_parser_and_document_bounds_reused(self):
        self.source.write_text('{"schema_version":1,"schema_version":1}')
        self.refuse(self.source, 'duplicate mapping key')
        self.source.write_text('[' * 70 + '0' + ']' * 70)
        self.refuse(self.source, 'depth/node')
        self.source.write_text('[' + ','.join(['0'] * 100001) + ']')
        self.refuse(self.source, 'depth/node')
        with self.source.open('wb') as stream:
            stream.truncate(5 * 1024 * 1024 + 1)
        self.refuse(self.source, '5 MiB')
        kit = self.kit()
        for i in range(5):
            with (kit/'agents'/f'large{i}.yaml').open('wb') as stream:
                stream.truncate(5 * 1024 * 1024)
        self.refuse(kit, '20 MiB')


if __name__ == '__main__':
    unittest.main()
