'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const YAML = require('yaml');
const { XMLParser, XMLValidator } = require('fast-xml-parser');
const { normalizeCase } = require('../lib/schema');
const { evaluate } = require('../lib/assertions');
const root = path.resolve(__dirname, '..');
const flowsFile = path.join(__dirname, 'fixtures/output-sequence.json');
const casesFile = path.join(__dirname, 'fixtures/output-sequence.test.yaml');
const flows = JSON.parse(fs.readFileSync(flowsFile, 'utf8'));
const example = () => YAML.parse(fs.readFileSync(casesFile, 'utf8')).cases[0];
function workspace(t) {
  const dir = fs.mkdtempSync(path.join(root, '.nrflowtest-sequence-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}
function invoke(dir, reporter = 'json', testFile = casesFile, extra = []) {
  const out = spawnSync(process.execPath, [path.join(root, 'bin/nrflowtest.js'), 'run', '--flows', flowsFile,
    '--tests', testFile, '--strict-types', '--reporter', reporter, ...extra], { cwd: dir, encoding: 'utf8', timeout: 20000 });
  assert.equal(out.error, undefined, out.error?.message);
  return out;
}
test('ordered multi-port sequence and JSON Pointer fixture passes through the real switch', t => {
  const dir = workspace(t);
  const out = invoke(dir);
  assert.equal(out.status, 0, out.stderr + out.stdout);
  const report = JSON.parse(out.stdout);
  assert.equal(report.passed, 1);
  assert.deepEqual(report.cases[0].mismatches, []);
  assert.deepEqual(fs.readdirSync(dir), []);
});
test('sequence count, order, port and missing-pointer mismatches agree in JSON and parsed JUnit', t => {
  const dir = workspace(t);
  const c = YAML.parse(fs.readFileSync(path.join(__dirname, 'fixtures/output-sequence.fail.yaml'), 'utf8')).cases[0];
  const file = path.join(dir, 'bad.yaml');
  fs.writeFileSync(file, YAML.stringify({ version: 1, cases: [c] }));
  const out = invoke(dir, 'json', file, ['--junit-file', 'junit.xml']);
  assert.equal(out.status, 1, out.stderr + out.stdout);
  const result = JSON.parse(out.stdout).cases[0];
  const failures = result.failures;
  const base = { assertion: 'expect', expectationIndex: 0, node: 'router', event: 'output' };
  assert.deepEqual(result.mismatches, [
    { ...base, kind: 'count', sequenceIndex: null, messageIndex: null, output: null, pointer: null, expected: { value: 4 }, actual: { value: 3 } },
    { ...base, kind: 'field', sequenceIndex: 0, messageIndex: 0, output: 0, pointer: '/payload/id', expected: { value: 'B<&' }, actual: { value: 'A' } },
    { ...base, kind: 'output', sequenceIndex: 1, messageIndex: 1, output: 0, pointer: null, expected: { value: 1 }, actual: { value: 0 } },
    { ...base, kind: 'field', sequenceIndex: 1, messageIndex: 1, output: 0, pointer: '/payload/id', expected: { value: 'A' }, actual: { value: 'B' } },
    { ...base, kind: 'field', sequenceIndex: 2, messageIndex: 2, output: 1, pointer: '/payload/missing', expected: { value: null }, actual: { missing: true } },
  ]);
  assert.ok(failures.some(f => /node router output.*expected 4 message\(s\), got 3/.test(f)));
  assert.ok(failures.some(f => /node router output, message 1.*JSON Pointer "\/payload\/id".*expected 'B<&', got 'A'/.test(f)));
  assert.ok(failures.some(f => /node router output, message 2.*expected output 1, got 0/.test(f)));
  assert.ok(failures.some(f => /message 2.*expected 'A', got 'B'/.test(f)));
  assert.ok(failures.some(f => /message 3.*JSON Pointer "\/payload\/missing".*missing; expected null/.test(f)));
  const xml = fs.readFileSync(path.join(dir, 'junit.xml'), 'utf8');
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml);
  assert.equal(parsed.testsuites['@_failures'], '1');
  assert.equal(parsed.testsuites.testsuite.testcase.failure['#text'], failures.join('\n'));
  // A same-count permutation must fail for order alone.
  c.expect[0].sequence = [
    { output: 0, msg: { payload: { id: 'B' } } },
    { output: 0, msg: { payload: { id: 'A' } } },
    { output: 1, msg: { payload: { id: 'C' } } },
  ];
  fs.writeFileSync(file, YAML.stringify({ version: 1, cases: [c] }));
  const wrongOrder = invoke(dir, 'json', file);
  assert.equal(wrongOrder.status, 1, wrongOrder.stderr);
  const orderResult = JSON.parse(wrongOrder.stdout).cases[0];
  const orderFailures = orderResult.failures;
  assert.equal(orderFailures.length, 2);
  assert.match(orderFailures[0], /message 1.*\$\.payload.id: expected 'B', got 'A'/);
  assert.match(orderFailures[1], /message 2.*\$\.payload.id: expected 'A', got 'B'/);
  assert.deepEqual(orderResult.mismatches, [
    { ...base, kind: 'message', sequenceIndex: 0, messageIndex: 0, output: 0, pointer: '/payload/id', expected: { value: 'B' }, actual: { value: 'A' } },
    { ...base, kind: 'message', sequenceIndex: 1, messageIndex: 1, output: 0, pointer: '/payload/id', expected: { value: 'A' }, actual: { value: 'B' } },
  ]);
  // An extra emission fails even when the expected prefix matches perfectly.
  c.expect[0].sequence = [{ output: 0, fields: { '/payload/id': 'A' } }];
  fs.writeFileSync(file, YAML.stringify({ version: 1, cases: [c] }));
  const extra = invoke(dir, 'junit', file);
  assert.equal(extra.status, 1, extra.stderr);
  assert.equal(XMLValidator.validate(extra.stdout), true);
  assert.match(extra.stdout, /Count mismatch.*expected 1 message\(s\), got 3/);
});
test('JSON Pointer selection handles escaped keys, root, arrays, null and own properties with exact values', () => {
  const msg = { payload: { 'a/b': { '~key': false }, '': 0, list: [null, { id: 'A', extra: true }], '~1': 'literal' } };
  const check = fields => evaluate(normalizeCase({ name: 'pointer', expect: [{ node: 'router', fields }] }, flows, 'test', 0),
    [{ node: 'router', event: 'input', at: 0, msg }], []);
  assert.deepEqual(check({ '': msg, '/payload/a~1b/~0key': false, '/payload/': 0, '/payload/list/0': null,
    '/payload/list/1': { extra: true, id: 'A' }, '/payload/~01': 'literal' }), []);
  for (const pointer of ['/payload/list/01', '/payload/list/-', '/payload/list/2', '/payload/list/0/id', '/payload/toString']) {
    assert.match(check({ [pointer]: null })[0], /missing; expected null/);
  }
  assert.match(check({ '/payload/list/1': { id: 'A' } })[0], /expected.*id: 'A'.*got/);
  assert.match(check({ '/payload/': '0' })[0], /expected '0', got 0/);
});
test('sequence uses the selected port and observation prefix, and applies common patterns to every message', () => {
  const c = normalizeCase({ name: 'window', timeout: 100, expect: [{ node: 'router', event: 'output', output: 1,
    timeout: 50, msg: { topic: 'records' }, fields: { '/payload/ok': true },
    sequence: [{ fields: { '/payload/id': 'C' } }, { fields: { '/payload/id': 'D' } }] }] }, flows, 'test', 0);
  const event = (output, at, id, topic = 'records') => ({ node: 'router', event: 'output', output, at, msg: { topic, payload: { id, ok: true } } });
  const events = [event(0, 1, 'ignore'), event(1, 2, 'C'), event(1, 50, 'D'), event(1, 51, 'late')];
  assert.deepEqual(evaluate(c, events, []), []);
  events[2].msg.topic = 'wrong';
  assert.match(evaluate(c, events, [])[0], /node router output output 1, message 2.*expected 'records', got 'wrong'/);
  events[2].msg.topic = 'records'; events[2].msg.payload.ok = false;
  assert.match(evaluate(c, events, [])[0], /message 2.*JSON Pointer "\/payload\/ok".*expected true, got false/);
});
test('invalid sequences and JSON Pointers fail preflight without executing or writing JUnit', t => {
  const dir = workspace(t);
  const file = path.join(dir, 'bad.yaml');
  const invalid = [
    { sequence: [] }, { sequence: null }, { sequence: [{}], count: 2 }, { sequence: Array(1001).fill({}) },
    { sequence: [{ typo: true }] }, { sequence: [{ msg: null }] }, { sequence: [{ fields: [] }] },
    { sequence: [{ output: 0 }] }, { event: 'output', sequence: [{ output: -1 }] },
    { fields: { 'payload/id': 'A' } }, { fields: { '/payload/~2': 'A' } }, { fields: { '/payload/~': 'A' } },
    { fields: null }, { fields: [] }, { fields: {} }, { sequence: [{ fields: { '#/payload': 'A' } }] },
  ];
  for (const e of invalid) {
    fs.writeFileSync(file, YAML.stringify({ version: 1, cases: [{ name: 'invalid', expect: [{ node: 'router', ...e }] }] }));
    const out = invoke(dir, 'json', file, ['--junit-file', 'must-not-exist.xml']);
    assert.equal(out.status, 2, JSON.stringify(e) + out.stdout);
    assert.equal(out.stdout, '');
    assert.equal(fs.existsSync(path.join(dir, 'must-not-exist.xml')), false);
    assert.deepEqual(fs.readdirSync(dir), ['bad.yaml']);
  }
  const c = example();
  fs.writeFileSync(file, YAML.stringify({ version: 1, cases: [c] }));
  const out = invoke(dir, 'json', file, ['--dry-run']);
  assert.equal(out.status, 0, out.stderr);
  assert.equal(JSON.parse(out.stdout).dryRun, true);
  assert.deepEqual(fs.readdirSync(dir), ['bad.yaml']);
});
