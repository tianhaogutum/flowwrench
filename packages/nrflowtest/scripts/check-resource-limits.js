'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');
const limitFailure = 'Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)';
const record = (resource, limit, observedAtLeast, captured = limit) => ({ resource, limit, captured, observedAtLeast });

// The helper's unused metric spy retains several calls per delivery and can
// exhaust the 192 MiB heap before 100000 deliveries. Test-only instrumentation
// drops that spy history, leaving actual runtime hooks/error capture untouched.
const probe = `
const cp = require('node:child_process'), fork = cp.fork;
const preload = process.argv.splice(2, 1)[0];
cp.fork = (file, args, options) => fork(file, args, {
  ...options, execArgv: [...options.execArgv, '--require=' + preload]
});
require(process.argv[1]);
`;
const preload = `
process.once('message', ({ testCase }) => {
  if (testCase.inject[0]?.msg.payload.mode !== 'deliveries') return;
  const path = require('node:path');
  const helper = require(require.resolve('node-red-node-test-helper', { paths: [path.dirname(process.argv[1])] }));
  setInterval(() => helper._logSpy?.resetHistory(), 10).unref();
});
`;

// The same real Node-RED fixtures run against the source and installed executable.
function checkResourceLimits({ executable, fixtures, cwd }) {
  const preloadFile = path.join(cwd, 'reset-unused-spy.cjs');
  fs.writeFileSync(preloadFile, preload);
  function invoke(file, expectedStatus, extra = [], delivery = false) {
    const out = spawnSync(delivery ? process.execPath : executable, [...(delivery ? ['-e', probe, executable, preloadFile] : []), 'run', '--flows', path.join(fixtures, 'resource-limits.json'),
      '--tests', path.join(fixtures, file), '--strict-types', '--allow-function', ...extra],
    { cwd, encoding: 'utf8', timeout: 30000 });
    assert.equal(out.error, undefined, out.error?.message);
    assert.equal(out.status, expectedStatus, out.stderr + out.stdout);
    return out.stdout;
  }
  const positive = JSON.parse(invoke('resource-limits.test.yaml', 0, ['--reporter', 'json']));
  assert.equal(positive.passed, 3); assert.equal(positive.failed, 0);
  for (const c of positive.cases) {
    assert.deepEqual(c.resourceLimits, []);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: c.errors.length, complete: true });
    assert.deepEqual(c.mismatches, []); assert.deepEqual(c.failures, []); assert.equal(c.termination, null);
  }
  assert.deepEqual(positive.cases.map(c => c.observedMessages), [10000, 1, 0]);
  assert.deepEqual(positive.cases.map(c => c.errors.length), [0, 0, 100]);
  const negative = JSON.parse(invoke('resource-limits.fail.yaml', 1, ['--reporter', 'json', '--junit-file', 'limits-junit.xml']));
  assert.equal(negative.passed, 1); assert.equal(negative.failed, 6);
  assert.deepEqual(negative.cases.map(c => c.resourceLimits), [
    [record('observations', 10000, 10001)], [record('observationBytes', 8388608, 8388609, 0)],
    [record('errors', 100, 101)],
    [record('observations', 10000, 10001), record('errors', 100, 101)],
    [record('errors', 100, 101)], [record('errors', 100, 101)], [],
  ]);
  assert.deepEqual(negative.cases.map(c => c.observedMessages), [10000, 0, 0, 10000, 0, 0, 1]);
  assert.deepEqual(negative.cases.map(c => c.errors.length), [0, 0, 100, 100, 100, 100, 0]);
  for (const c of negative.cases) {
    assert.equal(c.termination, null, 'resource breaches are ordinary worker results');
    const incomplete = c.errors.length === 100;
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: incomplete ? 101 : 0, complete: !incomplete });
  }
  const missing = negative.cases[1];
  assert.deepEqual(missing.mismatches, [{ kind: 'count', assertion: 'expect', expectationIndex: 0,
    sequenceIndex: null, messageIndex: null, node: 'function', event: 'output', output: null,
    pointer: null, expected: { value: 1 }, actual: { value: 0 } }]);
  for (const [i, c] of negative.cases.entries()) if (i !== 1) assert.deepEqual(c.mismatches, []);
  const failures = negative.cases.map((_, i) => i === 6 ? [] : i === 1 ? [limitFailure,
    'Timeout at node function output after 250ms: expected 1 message(s), got 0'] : [limitFailure]);
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  const xml = fs.readFileSync(path.join(cwd, 'limits-junit.xml'), 'utf8');
  assert.equal(XMLValidator.validate(xml), true);
  const suite = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(suite['@_tests'], '7'); assert.equal(suite['@_failures'], '6');
  assert.deepEqual(suite.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''), failures.map(f => f.join('\n')));
  for (const [file, exitCode] of [['test', 0], ['fail', 1]]) {
    const report = JSON.parse(invoke(`resource-deliveries.${file}.yaml`, exitCode, ['--reporter', 'json'], true));
    assert.equal(report.cases.length, 1);
    const c = report.cases[0];
    assert.equal(c.status, exitCode ? 'failed' : 'passed');
    assert.deepEqual(c.resourceLimits, exitCode ? [record('deliveries', 100000, 100001)] : []);
    assert.equal(c.observedMessages, 0); assert.equal(c.termination, null);
    assert.deepEqual(c.errors, []); assert.deepEqual(c.mismatches, []);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: 0, observedAtLeast: 0, complete: true });
    assert.deepEqual(c.failures, exitCode ? [limitFailure] : []);
  }
  const spec = invoke('resource-limits.fail.yaml', 1);
  for (const f of new Set(failures.flat())) assert.ok(spec.includes(`  ${f}\n`), spec);
  assert.ok(spec.endsWith('1 passed, 6 failed\n'));
  // Missing opt-in remains a preflight failure, with no case JSON or JUnit file.
  const usage = spawnSync(executable, ['run', '--flows', path.join(fixtures, 'resource-limits.json'),
    '--tests', path.join(fixtures, 'resource-limits.test.yaml'), '--junit-file', 'must-not-exist.xml'],
  { cwd, encoding: 'utf8', timeout: 10000 });
  assert.equal(usage.error, undefined); assert.equal(usage.status, 2, usage.stdout + usage.stderr);
  assert.equal(usage.stdout, ''); assert.match(usage.stderr, /requires --allow-function/);
  assert.deepEqual(fs.readdirSync(cwd), ['limits-junit.xml', 'reset-unused-spy.cjs'], 'all worker runtime directories removed');
}
module.exports = { checkResourceLimits };

// Small entry-capture regression; also used against the offline installed CLI.
function checkResourceCapture({ executable, fixtures, cwd }) {
  const out = spawnSync(executable, ['run', '--flows', path.join(fixtures, 'resource-limits.json'),
    '--tests', path.join(fixtures, 'resource-capture.fail.yaml'), '--allow-function',
    '--reporter', 'json', '--junit-file', 'capture-junit.xml'],
  { cwd, encoding: 'utf8', timeout: 15000 });
  assert.equal(out.error, undefined); assert.equal(out.status, 1, out.stdout + out.stderr);
  const report = JSON.parse(out.stdout);
  assert.equal(report.failed, 3); assert.equal(report.passed, 0);
  const [declared, mismatched, bytes] = report.cases;
  // Prove existing generic failure/count behavior before requiring new metadata.
  const failures = [[limitFailure], [limitFailure,
    "Error expectation at node function: expected 101 containing 'known', got 100",
    "Error expectation at node function: expected 1 containing 'known', got 0"], [limitFailure]];
  assert.deepEqual(report.cases.map(c => c.failures), failures);
  assert.deepEqual(declared.mismatches, []);
  assert.equal(declared.errors.length, 100);
  assert.deepEqual(declared.errorCapture, { limit: 100, captured: 100, observedAtLeast: 101, complete: false });
  assert.deepEqual(mismatched.errorCapture, declared.errorCapture);
  for (const c of [declared, mismatched]) {
    assert.deepEqual(c.resourceLimits, [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }]);
  }
  assert.deepEqual(mismatched.mismatches, [101, 1].map((expected, expectationIndex) => ({
    sequenceIndex: null, messageIndex: null, event: 'error', output: null, pointer: null,
    kind: 'errorCount', assertion: 'expectError', expectationIndex, node: 'function', errorMessage: 'known',
    errorIndexes: expectationIndex ? [] : Array.from({ length: 100 }, (_, i) => i), captureComplete: false,
    messageFidelity: { complete: true, truncatedErrorIndexes: [] },
    previouslyConsumed: expectationIndex ? Array.from({ length: 100 }, (_, errorIndex) => ({ errorIndex, expectationIndex: 0 })) : [],
    expected: { value: expected }, actual: { value: expectationIndex ? 0 : 100 },
  })));
  assert.deepEqual(bytes.resourceLimits, [{ resource: 'observationBytes', limit: 8388608, captured: 100, observedAtLeast: 8388700 }]);
  assert.equal(bytes.observedMessages, 1);
  assert.deepEqual(bytes.mismatches, []);
  assert.deepEqual(bytes.errorCapture, { limit: 100, captured: 0, observedAtLeast: 0, complete: true });
  for (const c of report.cases) assert.equal(c.termination, null);
  const xml = fs.readFileSync(path.join(cwd, 'capture-junit.xml'), 'utf8');
  assert.equal(XMLValidator.validate(xml), true);
  const suite = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(suite['@_tests'], '3'); assert.equal(suite['@_failures'], '3');
  assert.deepEqual(suite.testsuite.testcase.map(c => c.failure['#text']), failures.map(f => f.join('\n')));
  assert.deepEqual(fs.readdirSync(cwd), ['capture-junit.xml'], 'all worker runtime directories removed');
}
module.exports.checkResourceCapture = checkResourceCapture;
