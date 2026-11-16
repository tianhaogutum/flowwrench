'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const YAML = require('yaml');
const { XMLParser, XMLValidator } = require('fast-xml-parser');
const { partialDiff } = require('../lib/assertions');
const { loadFlows, loadTests, normalizeCase } = require('../lib/schema');
const { prepare } = require('../lib/prepare');
const { runWorker } = require('../lib/runner');
const { junit } = require('../lib/reporters');
const root = path.resolve(__dirname, '..');
const cli = path.join(root, 'bin/nrflowtest.js');
const orders = path.join(root, 'fixtures/orders/flows.json');
const orderTests = path.join(root, 'fixtures/orders/*.test.yaml');
function workspace(t) {
  const dir = fs.mkdtempSync(path.join(root, '.nrflowtest-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}
function flow(type = 'change', extra = {}) {
  return [
    { id: 'tab', type: 'tab', label: 'Synthetic' },
    { id: 'n', type, z: 'tab', name: 'Transform', x: 100, y: 80, wires: [['sink']], ...extra },
    { id: 'sink', type: 'debug', z: 'tab', name: 'Sink', x: 260, y: 80, wires: [] },
  ];
}
function base(overrides = {}) {
  return { name: 'case', timeout: 60, inject: { node: 'n', msg: { payload: { a: 1 } } }, expect: [{ node: 'sink', msg: { payload: { a: 1 } } }], ...overrides };
}
function setup(t, flows, cases) {
  const dir = workspace(t);
  fs.writeFileSync(path.join(dir, 'flows.json'), JSON.stringify(flows));
  fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases }));
  return dir;
}
function invoke(dir, extra = [], custom) {
  const result = spawnSync(process.execPath, [cli, ...(custom || ['run', '--flows', 'flows.json', '--tests', 'cases.yaml', '--reporter', 'json', ...extra])], {
    cwd: dir, encoding: 'utf8', timeout: 20000,
    env: { ...process.env, NRFLOWTEST_TEST_SECRET: 'DO_NOT_INHERIT_THIS_SYNTHETIC_SECRET' },
  });
  assert.equal(result.error, undefined, result.error?.message);
  return result;
}
function report(result, exit = 0) {
  assert.equal(result.status, exit, result.stderr + result.stdout);
  return JSON.parse(result.stdout);
}

test('deep partial match recurses objects, keeps array order/length and reports a path', () => {
  assert.equal(partialDiff({ a: { b: 1, c: 2 }, extra: 3 }, { a: { b: 1 } }), null);
  assert.match(partialDiff({ a: [1] }, { a: [1, 2] }), /\$\.a.length/);
  assert.match(partialDiff({ a: { b: 2 } }, { a: { b: 1 } }), /\$\.a.b: expected 1, got 2/);
  assert.match(partialDiff({}, { a: null }), /missing/);
  assert.ok(partialDiff({ a: 1 }, { a: '1' }));
});
test('real exported orders fixture passes all three CLI cases, including stub and no-message', t => {
  const dir = workspace(t);
  const r = invoke(dir, [], ['run', '--flows', orders, '--tests', orderTests, '--strict-types', '--reporter', 'json']);
  const data = report(r);
  assert.equal(data.passed, 3); assert.equal(data.failed, 0); assert.deepEqual(data.warnings, []);
  assert.deepEqual(fs.readdirSync(dir), [], 'runtime directories are cleaned; no helper server files');
});
test('partial mismatch returns 1 with the property path and actual value', t => {
  const dir = setup(t, flow(), [base({ expect: [{ node: 'sink', msg: { payload: { a: 9 } } }] })]);
  const data = report(invoke(dir), 1);
  assert.match(data.cases[0].failures[0], /\$\.payload.a: expected 9, got 1/);
});
test('timeout returns 1 and identifies the node', t => {
  const dir = setup(t, flow('switch', { property: 'payload.a', propertyType: 'msg', rules: [{ t: 'eq', v: '10', vt: 'num' }], outputs: 1 }), [base()]);
  assert.match(report(invoke(dir), 1).cases[0].failures[0], /Timeout at node sink input after 60ms/);
});
test('expectNoMessage fails on an actual message', t => {
  const dir = setup(t, flow(), [base({ expect: [], expectNoMessage: [{ node: 'sink' }] })]);
  assert.match(report(invoke(dir), 1).cases[0].failures[0], /expected 0 message\(s\), got 1/);
});
test('counts include extra messages even when one message matches', t => {
  const dir = setup(t, flow(), [base({ inject: [{ node: 'n', msg: { payload: 1 } }, { node: 'n', msg: { payload: 2 } }], expect: [{ node: 'sink', msg: { payload: 1 } }] })]);
  const failures = report(invoke(dir), 1).cases[0].failures;
  assert.match(failures[0], /Count mismatch.*expected 1.*got 2/);
  assert.ok(failures.some(f => /expected 1, got 2/.test(f)));
});
test('stub output indexes, null outputs, batches and deep cloning are preserved', t => {
  const flows = flow('http request');
  flows[1].wires = [['sink'], ['second']];
  flows.push({ id: 'second', type: 'debug', z: 'tab', x: 300, y: 200, wires: [] });
  const cases = [base({
    stubs: [{ node: 'n', outputs: [null, [{ payload: { x: 1 } }, { payload: { x: 2 } }]] }],
    expect: [{ node: 'n', event: 'output', output: 1, count: 2, msg: { payload: {} } }, { node: 'second', count: 2 }],
    expectNoMessage: [{ node: 'sink' }, { node: 'n', event: 'output', output: 0 }],
  })];
  assert.equal(report(invoke(setup(t, flows, cases), ['--strict-types'])).passed, 1);
});
test('unwired node emissions are observed', t => {
  const flows = flow(); flows[1].wires = [];
  const dir = setup(t, flows, [base({ expect: [{ node: 'n', event: 'output', output: 0, msg: { payload: { a: 1 } } }] })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('input snapshots are taken before downstream mutations', t => {
  const flows = flow('change', { rules: [{ t: 'set', p: 'payload.a', pt: 'msg', to: '99', tot: 'num' }] });
  const dir = setup(t, flows, [base({ expect: [{ node: 'n', msg: { payload: { a: 1 } } }, { node: 'sink', msg: { payload: { a: 99 } } }] })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('multiple timed injections, delayed stub responses and per-expectation windows', t => {
  const dir = setup(t, flow('http request'), [base({ timeout: 120,
    inject: [{ node: 'n', msg: { payload: 1 } }, { node: 'n', msg: { payload: 2 }, after: 40 }],
    stubs: [{ node: 'n', delay: 10, outputs: [{ payload: 'reply' }] }],
    expect: [{ node: 'sink', count: 2, msg: { payload: 'reply' } }],
    expectNoMessage: [{ node: 'sink', timeout: 5 }],
  })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('unknown type auto-stubs with warning and drops input', t => {
  const dir = setup(t, flow('synthetic-unknown'), [base({ expect: [], expectNoMessage: [{ node: 'sink' }] })]);
  const r = invoke(dir); const data = report(r);
  assert.equal(data.warnings.length, 1); assert.match(r.stderr, /Auto-stubbed n.*synthetic-unknown/);
});
test('strict-types returns 2 for an unknown type, but explicit stub maps to real wiring', t => {
  const flows = flow('synthetic-unknown');
  const dir = setup(t, flows, [base()]);
  const failed = invoke(dir, ['--strict-types']); assert.equal(failed.status, 2); assert.match(failed.stderr, /unsupported node type/);
  fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases: [base({ stubs: [{ node: 'n', outputs: [{ payload: { a: 1 } }] }] })] }));
  assert.equal(report(invoke(dir, ['--strict-types'])).passed, 1);
});
test('node.error fails even when a catch node handles it', t => {
  const flows = flow('function', { func: 'node.error("real upstream problem", msg); return null;', outputs: 1 });
  flows.push({ id: 'catcher', type: 'catch', z: 'tab', x: 200, y: 200, scope: ['n'], uncaught: false, wires: [['sink']] });
  const dir = setup(t, flows, [base({ expect: [{ node: 'sink', msg: { error: { message: 'real upstream problem' } } }] })]);
  const failures = report(invoke(dir, ['--allow-function']), 1).cases[0].failures;
  assert.match(failures[0], /Unexpected error at node n: real upstream problem/);
  assert.ok(!failures.some(f => /Timeout/.test(f)));
});
test('synchronous throw is reported with the real message, not a timeout', t => {
  const dir = setup(t, flow('function', { func: 'throw new Error("broken <order> & service");', outputs: 1 }), [base()]);
  const failures = report(invoke(dir, ['--allow-function']), 1).cases[0].failures;
  assert.match(failures[0], /broken <order> & service/); assert.ok(!failures.some(f => /Timeout/.test(f)));
});
test('expected node.error passes; errors with different messages remain failures', t => {
  const dir = setup(t, flow('function', { func: 'node.error("known problem",msg); return null;', outputs: 1 }), [base({ expect: [], expectError: [{ node: 'n', message: 'known problem' }] })]);
  assert.equal(report(invoke(dir, ['--allow-function'])).passed, 1);
  fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases: [base({ expect: [], expectError: [{ node: 'n', message: 'different problem' }] })] }));
  assert.equal(report(invoke(dir, ['--allow-function']), 1).failed, 1);
});
test('missing expected error fails', t => {
  const dir = setup(t, flow(), [base({ expect: [], expectError: [{ node: 'n', message: 'not raised' }] })]);
  assert.match(report(invoke(dir), 1).cases[0].failures[0], /Error expectation.*got 0/);
});
test('asynchronous function errors surface', t => {
  const dir = setup(t, flow('function', { func: 'setTimeout(() => { throw new Error("async fault"); }, 5); return null;', outputs: 1 }), [base()]);
  assert.match(report(invoke(dir, ['--allow-function']), 1).cases[0].failures[0], /async fault/);
});
test('unhandled rejection fails with its real message', t => {
  const dir = setup(t, flow('function', { func: 'Promise.reject(new Error("rejected promise")); return null;', outputs: 1 }), [base()]);
  assert.match(report(invoke(dir, ['--allow-function']), 1).cases[0].failures[0], /rejected promise/);
});
test('function execution requires explicit opt-in, including dry-run; stubbing avoids execution', t => {
  const dir = setup(t, flow('function', { func: 'throw new Error("must not run");', outputs: 1 }), [base()]);
  const r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /requires --allow-function/);
  assert.equal(invoke(dir, ['--dry-run']).status, 2);
  fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases: [base({ stubs: [{ node: 'n', outputs: [{ payload: { a: 1 } }] }] })] }));
  assert.equal(report(invoke(dir)).passed, 1);
});
test('external function modules are rejected rather than installed', t => {
  const dir = setup(t, flow('function', { func: 'return msg;', libs: [{ var: 'secret', module: 'arbitrary-package' }] }), [base()]);
  const r = invoke(dir, ['--allow-function']); assert.equal(r.status, 2); assert.match(r.stderr, /external modules are not supported/);
});
test('flow code cannot read inherited host credentials through env.get', t => {
  const dir = setup(t, flow('function', { func: 'msg.payload = typeof env.get("NRFLOWTEST_TEST_SECRET"); return msg;', outputs: 1 }), [base({ expect: [{ node: 'sink', msg: { payload: 'undefined' } }] })]);
  assert.equal(report(invoke(dir, ['--allow-function'])).passed, 1);
});
test('flow context resets between cases', t => {
  const dir = setup(t, flow('function', { func: 'let n = context.get("n") || 0; context.set("n", ++n); msg.payload = n; return msg;', outputs: 1 }), [
    base({ name: 'first', expect: [{ node: 'sink', msg: { payload: 1 } }] }),
    base({ name: 'second', expect: [{ node: 'sink', msg: { payload: 1 } }] }),
  ]);
  assert.equal(report(invoke(dir, ['--allow-function'])).passed, 2);
});
test('dry-run never executes function initialization or runtime steps', t => {
  const dir = setup(t, flow('function', { initialize: 'throw new Error("setup executed");', func: 'throw new Error("executed");', outputs: 1 }), [base()]);
  const data = report(invoke(dir, ['--allow-function', '--dry-run']));
  assert.equal(data.dryRun, true); assert.equal(data.planned.length, 1); assert.deepEqual(data.cases, []);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['cases.yaml', 'flows.json']);
});
test('subflow instance input/output and literal subflow environment work end to end', t => {
  const dir = workspace(t);
  const r = invoke(dir, [], ['run', '--flows', path.join(root, 'test/fixtures/subflow.json'), '--tests', path.join(root, 'test/fixtures/subflow.test.yaml'), '--reporter', 'json']);
  assert.equal(report(r).passed, 1);
});
test('nested subflow output boundaries are resolved through internal sources', t => {
  const flows = JSON.parse(fs.readFileSync(path.join(root, 'test/fixtures/subflow.json')));
  flows.push({ id: 'outer', type: 'subflow', name: 'Outer', in: [{ wires: [{ id: 'nested' }] }], out: [{ wires: [{ id: 'nested', port: 0 }] }] });
  flows.push({ id: 'nested', type: 'subflow:sf', z: 'outer', x: 100, y: 100, wires: [[]] });
  flows.find(n => n.id === 'instance').type = 'subflow:outer';
  const dir = setup(t, flows, [base({ inject: { node: 'instance', msg: { payload: 1 } }, expect: [{ node: 'instance', event: 'output', msg: { source: 'fixture', payload: 1 } }, { node: 'sink', msg: { source: 'fixture' } }] })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('stubs inside a subflow definition apply to the concrete instance and errors can target its boundary', t => {
  const flows = JSON.parse(fs.readFileSync(path.join(root, 'test/fixtures/subflow.json')));
  flows.find(n => n.id === 'inner').type = 'http request';
  const dir = setup(t, flows, [base({ inject: { node: 'instance', msg: {} }, stubs: [{ node: 'inner', error: 'subflow service failed' }], expect: [], expectError: [{ node: 'instance', message: 'subflow service failed' }] })]);
  assert.equal(report(invoke(dir, ['--strict-types'])).passed, 1);
});
test('config node stubs and tab/group/comment metadata load without side effects', t => {
  const flows = flow('http request', { server: 'broker', g: 'group' });
  flows.push({ id: 'broker', type: 'mqtt-broker', name: 'Config' }, { id: 'group', type: 'group', z: 'tab', nodes: ['n'] }, { id: 'note', type: 'comment', z: 'tab', x: 100, y: 200, name: 'fixture' });
  const dir = setup(t, flows, [base({ stubs: [{ node: 'n', outputs: [{ payload: { a: 1 } }] }, { node: 'broker' }] })]);
  assert.equal(report(invoke(dir, ['--strict-types'])).passed, 1);
});
test('auto inject repeat and once are inert, even on a real exported flow', t => {
  const flows = flow('inject', { once: true, onceDelay: 0.01, repeat: '0.01' });
  const dir = setup(t, flows, [base({ inject: [], expect: [], expectNoMessage: [{ node: 'sink' }] })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('I/O, exec, file and server types are never loaded by auto-stubs', t => {
  const flows = flow('exec', { command: 'touch marker-must-not-exist', addpay: false });
  for (const [i, type] of ['http in', 'tcp in', 'mqtt in', 'file'].entries()) flows.push({ id: `io${i}`, type, z: 'tab', x: 100, y: i * 40, filename: 'marker-must-not-exist', wires: [] });
  const dir = setup(t, flows, [base({ expect: [], expectNoMessage: [{ node: 'sink' }] })]);
  assert.equal(report(invoke(dir)).warnings.length, 5);
  assert.equal(fs.existsSync(path.join(dir, 'marker-must-not-exist')), false);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['cases.yaml', 'flows.json']);
});
test('hard deadline kills stuck asynchronous code and reaps worker', async t => {
  const dir = setup(t, flow('function', { func: 'setTimeout(() => { while (true) {} }, 0); return null;', outputs: 1 }), [base({ timeout: 20 })]);
  const flows = loadFlows(path.join(dir, 'flows.json'));
  const c = loadTests([path.join(dir, 'cases.yaml')], flows)[0];
  const result = await runWorker(c, prepare(flows, c, { allowFunction: true }), dir, { workerStartupTimeout: 50 });
  assert.equal(result.status, 'failed'); assert.match(result.failures[0], /hard deadline exceeded/);
  assert.deepEqual(result.termination, { reason: 'deadline', phase: 'observation', hardTimeout: 2070,
    signal: null, exitCode: null, exitSignal: 'SIGKILL' });
});
test('per-case hardTimeout isolates hanging input and cleanup, reports JUnit deadlines and runs later cases', t => {
  const dir = workspace(t);
  const out = invoke(dir, [], ['run', '--flows', path.join(root, 'test/fixtures/deadline-isolation.json'),
    '--tests', path.join(root, 'test/fixtures/deadline-isolation.test.yaml'), '--allow-function',
    '--reporter', 'json', '--junit-file', 'junit.xml']);
  assert.equal(out.status, 1, out.stderr);
  const data = JSON.parse(out.stdout);
  const text = fs.readFileSync(path.join(dir, 'junit.xml'), 'utf8');
  assert.equal(XMLValidator.validate(text), true);
  const xml = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(text);
  assert.equal(xml.testsuites['@_tests'], '3');
  assert.equal(xml.testsuites['@_failures'], '2');
  const cases = xml.testsuites.testsuite.testcase;
  for (const [i, phase] of ['observation', 'cleanup'].entries()) {
    assert.deepEqual(data.cases[i].termination, { reason: 'deadline', phase, hardTimeout: 1500,
      signal: null, exitCode: null, exitSignal: 'SIGKILL' });
    assert.deepEqual(data.cases[i].mismatches, []);
    assert.equal(cases[i].failure['#text'], data.cases[i].failures.join('\n'));
    assert.match(cases[i].failure['#text'], new RegExp(`hard deadline exceeded.*1500ms.*${phase}`));
    assert.ok(Number(cases[i]['@_time']) >= 1.5, 'deadline failures include elapsed wall time');
    assert.ok(Number(cases[i]['@_time']) < 10, 'case uses its own hard budget rather than the default 12s allowance');
  }
  assert.equal(cases[2]['@_name'], 'later case has fresh context and no leaked timers');
  assert.equal(cases[2].failure, undefined);
  assert.equal(data.cases[2].termination, null);
  assert.deepEqual(fs.readdirSync(dir), ['junit.xml'], 'all case runtime directories removed after worker exit');
});
test('hardTimeout validates an explicit whole-worker budget without changing the default', t => {
  const flows = flow();
  assert.equal(normalizeCase(base(), flows, 'cases.yaml', 0).hardTimeout, undefined);
  for (const value of [10, 1500, 60000]) {
    assert.equal(normalizeCase(base({ hardTimeout: value }), flows, 'cases.yaml', 0).hardTimeout, value);
  }
  const dir = setup(t, flows, [base()]);
  for (const value of [0, 9, 60001, 1.5, '1500', null]) {
    fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases: [base({ hardTimeout: value })] }));
    const out = invoke(dir, ['--junit-file', 'junit.xml']);
    assert.equal(out.status, 2, JSON.stringify(value));
    assert.match(out.stderr, /\.hardTimeout: expected integer 10–60000/);
    assert.equal(fs.existsSync(path.join(dir, 'junit.xml')), false);
  }
  fs.writeFileSync(path.join(dir, 'cases.yaml'), YAML.stringify({ version: 1, cases: [base({ hardTimeout: 10 })] }));
  assert.equal(report(invoke(dir, ['--dry-run'])).dryRun, true);
  const data = report(invoke(dir), 1);
  assert.match(data.cases[0].failures[0], /hard deadline exceeded.*10ms.*startup/);
  assert.deepEqual(data.cases[0].termination, { reason: 'deadline', phase: 'startup', hardTimeout: 10,
    signal: null, exitCode: null, exitSignal: 'SIGKILL' });
  assert.ok(data.cases[0].durationMs >= 10);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['cases.yaml', 'flows.json']);
});
test('JUnit success/failure XML parses, escapes special chars, strips XML-invalid controls and writes a nested report', t => {
  const dir = setup(t, flow(), [base({ name: 'bad <order> & "case"\u0001', expect: [{ node: 'sink', msg: { payload: { a: '<wrong>&' } } }] })]);
  const r = invoke(dir, ['--reporter', 'junit']);
  // Duplicate reporter options must be rejected. Request JUnit through an explicit argv instead.
  assert.equal(r.status, 2);
  const out = invoke(dir, [], ['run', '--flows', 'flows.json', '--tests', 'cases.yaml', '--reporter', 'junit', '--junit-file', 'out/junit.xml']);
  assert.equal(out.status, 1, out.stderr);
  assert.equal(XMLValidator.validate(out.stdout), true);
  const xml = new XMLParser({ ignoreAttributes: false }).parse(out.stdout);
  assert.equal(xml.testsuites['@_failures'], '1');
  assert.match(xml.testsuites.testsuite.testcase.failure['#text'], /<wrong>&/);
  assert.equal(fs.readFileSync(path.join(dir, 'out/junit.xml'), 'utf8'), out.stdout);
});
test('JUnit passing fixture has correct test counts and no failures', t => {
  const dir = workspace(t);
  const r = invoke(dir, [], ['run', '--flows', orders, '--tests', orderTests, '--reporter', 'junit']);
  assert.equal(r.status, 0, r.stderr); assert.equal(XMLValidator.validate(r.stdout), true);
  const xml = new XMLParser({ ignoreAttributes: false }).parse(r.stdout);
  assert.equal(xml.testsuites['@_tests'], '3'); assert.equal(xml.testsuites['@_failures'], '0');
  assert.equal(xml.testsuites.testsuite.testcase.length, 3);
});
test('JUnit XML escapes warning output too', () => {
  const output = junit({ cases: [], failed: 0, durationMs: 0, warnings: ['<unsafe>&"\u0000'] });
  assert.equal(XMLValidator.validate(output), true); assert.match(output, /&lt;unsafe&gt;&amp;/);
});
test('report paths reject traversal, absolute paths, symlinks and input-file overwrites', t => {
  const dir = setup(t, flow(), [base()]);
  fs.symlinkSync(root, path.join(dir, 'escape'));
  for (const target of ['../outside.xml', '/tmp/outside.xml', 'escape/out.xml', 'flows.json', 'cases.yaml']) {
    const r = invoke(dir, ['--junit-file', target]); assert.equal(r.status, 2, target); assert.equal(r.stdout, '');
  }
});
test('invalid flow JSON and missing node both produce exit 2 before running cases', t => {
  const dir = setup(t, flow(), [base({ inject: { node: 'missing', msg: {} } })]);
  let r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /unknown node.*missing/);
  fs.writeFileSync(path.join(dir, 'flows.json'), '{');
  r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /invalid flows JSON/);
});
test('disabled target produces load error 2, not timeout', t => {
  const flows = flow(); flows[0].disabled = true;
  const r = invoke(setup(t, flows, [base()])); assert.equal(r.status, 2); assert.match(r.stderr, /not active/);
});
test('missing wires, duplicate ids and unsafe object keys are rejected', t => {
  for (const mutate of [f => f[1].wires = [['nope']], f => f.push({ ...f[1] }), f => f[1].func = JSON.parse('{"__proto__":{"polluted":true}}')]) {
    const flows = flow(); mutate(flows);
    assert.equal(invoke(setup(t, flows, [base()])).status, 2);
  }
  assert.equal({}.polluted, undefined);
});
test('YAML aliases, duplicate keys, unknown fields and unsupported schemas are rejected', t => {
  const dir = setup(t, flow(), [base()]);
  for (const text of [
    'version: 1\ncases: &cases []\nalias: *cases\n',
    'version: 1\ncases:\n - name: custom tag\n   inject: {node: n, msg: {payload: !script arbitrary}}\n   expect: [{node: sink}]\n',
    'version: 1\ncases:\n - name: alias\n   inject: {node: n, msg: &msg {payload: 1}}\n   expect: [{node: sink, msg: *msg}]\n',
    'version: 1\nversion: 1\ncases: []\n',
    YAML.stringify({ version: 1, cases: [base({ typo: true })] }),
    YAML.stringify({ version: 2, cases: [base()] }),
    YAML.stringify({ version: 1, cases: [base({ expect: [], expectError: [] })] }),
    YAML.stringify({ version: 1, cases: [base({ timeout: 0 })] }),
    YAML.stringify({ version: 1, cases: [base({ stubs: [{ node: 'n', outputs: [{ payload: 1 }], error: 'both' }] })] }),
  ]) {
    fs.writeFileSync(path.join(dir, 'cases.yaml'), text);
    assert.equal(invoke(dir).status, 2, text);
  }
});
test('name ambiguity is rejected but explicit ids remain unambiguous', t => {
  const flows = flow(); flows[1].name = 'Same'; flows[2].name = 'Same';
  const dir = setup(t, flows, [base({ inject: { node: { name: 'Same' }, msg: {} } })]);
  const r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /ambiguous node/);
});
test('empty test glob and unknown CLI options return 2; help/version return 0', t => {
  const dir = workspace(t);
  for (const argv of [[], ['run'], ['run', '--bogus'], ['run', '--flows', orders, '--tests', 'nothing-*.yaml']]) assert.equal(invoke(dir, [], argv).status, 2);
  assert.match(invoke(dir, [], ['--help']).stdout, /Usage:/);
  assert.equal(invoke(dir, [], ['--version']).stdout.trim(), '0.1.0');
});
test('the spec reporter shows a usable summary', t => {
  const dir = setup(t, flow(), [base()]);
  const r = invoke(dir, [], ['run', '--flows', 'flows.json', '--tests', 'cases.yaml']);
  assert.equal(r.status, 0); assert.match(r.stdout, /PASS.*case/); assert.match(r.stdout, /1 passed, 0 failed/);
});
test('prototype-like type names remain unknown types; reserved ids fail safely', t => {
  const dir = setup(t, flow('constructor'), [base({ expect: [], expectNoMessage: [{ node: 'sink' }] })]);
  assert.equal(report(invoke(dir)).warnings.length, 1);
  const flows = flow(); flows[1].id = '__proto__';
  fs.writeFileSync(path.join(dir, 'flows.json'), JSON.stringify(flows));
  const r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /reserved.*node id/);
});
test('shell syntax in glob input is treated as data, never as a command', t => {
  const dir = workspace(t);
  const r = invoke(dir, [], ['run', '--flows', orders, '--tests', '$(touch hacked).yaml']);
  assert.equal(r.status, 2); assert.equal(fs.existsSync(path.join(dir, 'hacked')), false);
});
test('unsupported subflow selectors and recursive subflows fail explicitly', t => {
  const flows = JSON.parse(fs.readFileSync(path.join(root, 'test/fixtures/subflow.json')));
  let dir = setup(t, flows, [base({ inject: { node: 'inner', msg: {} }, expect: [{ node: 'sink' }] })]);
  let r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /inside a subflow/);
  const inner = flows.find(n => n.id === 'inner'); inner.type = 'subflow:sf';
  dir = setup(t, flows, [base({ inject: { node: 'instance', msg: {} }, expect: [{ node: 'sink' }] })]);
  r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /recursive\/overdeep subflow/);
});
test('large input files and non-regular input files fail before deployment', t => {
  const dir = setup(t, flow(), [base()]);
  fs.writeFileSync(path.join(dir, 'cases.yaml'), ' '.repeat(1024 * 1024 + 1));
  let r = invoke(dir); assert.equal(r.status, 2); assert.match(r.stderr, /file exceeds/);
  r = invoke(dir, [], ['run', '--flows', '.', '--tests', 'cases.yaml']);
  assert.equal(r.status, 2); assert.match(r.stderr, /regular file/);
});
test('subflow direct-input output exposes the actual output boundary exactly once', t => {
  const flows = [
    { id: 'tab', type: 'tab' },
    { id: 'sf', type: 'subflow', in: [{ wires: [] }], out: [{ wires: [{ id: 'sf', port: 0 }] }] },
    { id: 'instance', type: 'subflow:sf', z: 'tab', x: 100, y: 100, wires: [['sink']] },
    { id: 'sink', type: 'debug', z: 'tab', x: 300, y: 100, wires: [] },
  ];
  const dir = setup(t, flows, [base({ inject: { node: 'instance', msg: { payload: 1 } }, expect: [{ node: 'instance', event: 'output', output: 0, msg: { payload: 1 } }, { node: 'sink', msg: { payload: 1 } }] })]);
  assert.equal(report(invoke(dir)).passed, 1);
});
test('exceeding the error recording cap fails even when the first 100 errors are expected', t => {
  const dir = setup(t, flow('function', { func: 'for (let i=0; i<101; i++) node.error("known", msg); return null;', outputs: 1 }), [base({ expect: [], expectError: [{ node: 'n', message: 'known', count: 100 }] })]);
  const data = report(invoke(dir, ['--allow-function']), 1);
  assert.match(data.cases[0].failures[0], /Observation limit exceeded/);
});
