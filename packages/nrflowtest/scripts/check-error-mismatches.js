'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

// Used against both the source CLI and a separately installed package.
function checkErrorMismatches({ executable, fixtures, cwd }) {
  function invoke(file, expectedStatus, extra = []) {
    const out = spawnSync(executable, ['run', '--flows', path.join(fixtures, 'error-mismatches.json'),
      '--tests', path.join(fixtures, file), '--strict-types', '--allow-function', ...extra],
    { cwd, encoding: 'utf8', timeout: 30000 });
    assert.equal(out.error, undefined, out.error?.message);
    assert.equal(out.status, expectedStatus, out.stderr + out.stdout);
    return out.stdout;
  }
  const positive = JSON.parse(invoke('error-mismatches.test.yaml', 0, ['--reporter', 'json']));
  assert.equal(positive.passed, 1); assert.equal(positive.failed, 0);
  assert.deepEqual(positive.cases[0].mismatches, []);
  assert.equal(positive.cases[0].termination, null);
  assert.equal(positive.cases[0].observedMessages, 2);
  const error = { node: 'instance-inner', instances: ['instance'], message: 'problem <order> & service',
    messageTruncated: false, messageOriginalLength: 25 };
  assert.deepEqual(positive.cases[0].errors, [error, error]);
  const negative = JSON.parse(invoke('error-mismatches.fail.yaml', 1, ['--reporter', 'json', '--junit-file', 'errors-junit.xml']));
  assert.equal(negative.passed, 0); assert.equal(negative.failed, 5);
  for (const c of [...positive.cases, ...negative.cases]) {
    assert.equal(c.termination, null);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: c.errors.length, complete: true });
  }
  const base = { sequenceIndex: null, messageIndex: null, output: null, pointer: null, event: 'error' };
  const count = (expectationIndex, errorMessage, expected, actual, errorIndexes) => ({ ...base,
    kind: 'errorCount', previouslyConsumed: expectationIndex ? [{ errorIndex: 0, expectationIndex: 0 }] : [], captureComplete: true, messageFidelity: { complete: true, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex, node: 'instance', errorMessage, errorIndexes,
    expected: { value: expected }, actual: { value: actual } });
  const unexpected = { ...base, kind: 'unexpectedError', assertion: null, expectationIndex: null,
    node: 'instance-inner', errorIndex: 0, expected: { missing: true }, actual: { value: error } };
  assert.deepEqual(negative.cases.map(c => c.errors), [[], [error, error], [error], [error], [error]]);
  assert.deepEqual(negative.cases.map(c => c.observedMessages), [0, 2, 1, 1, 1]);
  assert.deepEqual(negative.cases.map(c => c.mismatches), [
    [count(0, 'problem', 1, 0, [])],
    [count(0, 'problem', 1, 2, [0, 1]), { kind: 'count', assertion: 'expect', expectationIndex: 0,
      sequenceIndex: null, messageIndex: null, node: 'sink', event: 'input', output: null, pointer: null,
      expected: { value: 1 }, actual: { value: 2 } }],
    [count(0, 'PROBLEM', 1, 0, []), unexpected],
    [count(1, 'problem', 1, 0, [])],
    [unexpected],
  ]);
  const expectedFailures = [
    ["Error expectation at node instance: expected 1 containing 'problem', got 0"],
    ["Error expectation at node instance: expected 1 containing 'problem', got 2", 'Count mismatch at node sink input after 80ms: expected 1 message(s), got 2'],
    ["Error expectation at node instance: expected 1 containing 'PROBLEM', got 0", 'Unexpected error at node instance-inner: problem <order> & service'],
    ["Error expectation at node instance: expected 1 containing 'problem', got 0"],
    ['Unexpected error at node instance-inner: problem <order> & service'],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), expectedFailures);
  const xml = fs.readFileSync(path.join(cwd, 'errors-junit.xml'), 'utf8');
  assert.match(xml, /problem &lt;order&gt; &amp; service/);
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(parsed['@_tests'], '5'); assert.equal(parsed['@_failures'], '5');
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure['#text']), expectedFailures.map(f => f.join('\n')));
  const spec = invoke('error-mismatches.fail.yaml', 1);
  for (const failures of expectedFailures) for (const failure of failures) assert.ok(spec.includes(`  ${failure}\n`), spec);
  assert.ok(spec.endsWith('0 passed, 5 failed\n'));
  // Opt-in failure remains a usage/load error with no report written.
  const usage = spawnSync(executable, ['run', '--flows', path.join(fixtures, 'error-mismatches.json'),
    '--tests', path.join(fixtures, 'error-mismatches.test.yaml'), '--junit-file', 'must-not-exist.xml'], { cwd, encoding: 'utf8', timeout: 10000 });
  assert.equal(usage.error, undefined); assert.equal(usage.status, 2, usage.stdout + usage.stderr);
  assert.match(usage.stderr, /requires --allow-function/); assert.equal(usage.stdout, '');
  assert.deepEqual(fs.readdirSync(cwd), ['errors-junit.xml']);
}
module.exports = { checkErrorMismatches };
