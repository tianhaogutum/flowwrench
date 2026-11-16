'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { normalizeCase } = require('../lib/schema');
const { evaluateAssertions } = require('../lib/assertions');
const { runWorker } = require('../lib/runner');
const flows = ['plain', 'inner', 'middle', 'outer', 'other'].map(id => ({ id, name: `Named ${id}`, type: 'debug', x: 1, y: 1 }));
const normalize = expectError => normalizeCase({ name: 'zero', expectError }, flows, 'zero.yaml', 0);

test('error counts accept zero and retain default, maximum and invalid boundaries', () => {
  for (const count of [0, 1, 10000]) assert.equal(normalize([{ node: 'plain', message: 'x', count }]).expectError[0].count, count);
  assert.equal(normalize([{ node: 'plain', message: 'x' }]).expectError[0].count, 1);
  assert.equal(normalize([{ node: { name: 'Named outer' }, message: 'x', count: 0 }]).expectError[0].node, 'outer');
  for (const count of [-1, -0.5, 0.5, 10000.5, 10001, Number.MAX_SAFE_INTEGER, Infinity, NaN, '0', false]) {
    assert.throws(() => normalize([{ node: 'plain', message: 'x', count }]), /expectError.count: expected integer 0–10000/);
  }
  assert.throws(() => normalize([{ node: 'plain', message: '', count: 0 }]), /nonempty literal substring/);
  assert.throws(() => normalizeCase({ name: 'message', expect: [{ node: 'plain', count: 0 }] }, flows, 'zero.yaml', 0), /expected integer 1–10000/);
});

test('zero counts expose retained absence and independent candidate fidelity without consuming nonmatches', () => {
  const c = normalize([
    { node: 'outer', message: 'TAIL', count: 0 }, { node: 'plain', message: 'x', count: 0 },
    { node: 'middle', message: 'SHORT', count: 0 }, { node: 'inner', message: 'short' },
    { node: 'outer', message: 'x' }, { node: 'outer', message: 'absent', count: 0 },
  ]);
  const empty = evaluateAssertions(normalize([{ node: 'plain', message: 'x', count: 0 }]), [], []);
  assert.deepEqual(empty.failures, []); assert.deepEqual(empty.mismatches, []);
  assert.deepEqual(empty.errorExpectations[0], {
    assertion: 'expectError', expectationIndex: 0, node: 'plain', errorMessage: 'x', status: 'passed',
    expected: { value: 0 }, actual: { value: 0 }, errorIndexes: [], candidateErrorIndexes: [], nonmatchingErrorIndexes: [],
    captureComplete: true, messageFidelity: { complete: true, truncatedErrorIndexes: [] }, previouslyConsumed: [],
  });
  const errors = [
    { node: 'inner', instances: ['middle', 'outer'], message: 'short', messageTruncated: false },
    { node: 'inner', instances: ['middle', 'outer'], message: 'x'.repeat(8000), messageTruncated: true },
  ];
  const original = structuredClone(errors);
  const result = evaluateAssertions(c, [], errors, false);
  assert.deepEqual(result.failures, []); assert.deepEqual(result.mismatches, []);
  assert.deepEqual(result.errorExpectations.map(e => [e.status, e.actual.value, e.candidateErrorIndexes, e.nonmatchingErrorIndexes, e.errorIndexes]), [
    ['passed', 0, [0, 1], [0, 1], []], ['passed', 0, [], [], []], ['passed', 0, [0, 1], [0, 1], []],
    ['passed', 1, [0, 1], [1], [0]], ['passed', 1, [1], [], [1]], ['passed', 0, [], [], []],
  ]);
  assert.deepEqual(result.errorExpectations.map(e => e.messageFidelity), [
    { complete: false, truncatedErrorIndexes: [1] }, { complete: true, truncatedErrorIndexes: [] },
    { complete: false, truncatedErrorIndexes: [1] }, { complete: false, truncatedErrorIndexes: [1] },
    { complete: false, truncatedErrorIndexes: [1] }, { complete: true, truncatedErrorIndexes: [] },
  ]);
  assert.ok(result.errorExpectations.every(e => e.captureComplete === false));
  const unknown = evaluateAssertions(normalize([{ node: 'inner', message: 'absent', count: 0 }]), [], [{ node: 'inner', message: 'short' }]);
  assert.equal(unknown.errorExpectations[0].status, 'passed');
  assert.equal(unknown.errorExpectations[0].messageFidelity.complete, null);
  assert.deepEqual(unknown.mismatches.map(m => m.kind), ['unexpectedError']);
  assert.deepEqual(errors, original);
});

test('failed zero and earlier passing or failing consumers preserve all-match consumption and provenance', () => {
  const c = normalize([
    { node: 'outer', message: 'alpha', count: 0 }, // Fails and consumes both matches, not zero entries.
    { node: 'inner', message: 'alpha', count: 0 },
    { node: 'outer', message: 'beta' },
    { node: 'middle', message: 'shared', count: 0 },
    { node: 'outer', message: 'shared' }, // Existing positive-count mismatch remains.
  ]);
  const errors = ['alpha shared', 'beta shared', 'alpha shared'].map(message =>
    ({ node: 'inner', instances: ['middle', 'outer'], message, messageTruncated: false }));
  const result = evaluateAssertions(c, [], errors);
  assert.deepEqual(result.errorExpectations.map(e => [e.status, e.actual.value, e.errorIndexes]),
    [['failed', 2, [0, 2]], ['passed', 0, []], ['passed', 1, [1]], ['passed', 0, []], ['failed', 0, []]]);
  const alpha = [{ errorIndex: 0, expectationIndex: 0 }, { errorIndex: 2, expectationIndex: 0 }];
  const shared = [alpha[0], { errorIndex: 1, expectationIndex: 2 }, alpha[1]];
  assert.deepEqual(result.errorExpectations.map(e => e.previouslyConsumed), [[], alpha, [], shared, shared]);
  assert.deepEqual(result.failures, ["Error expectation at node outer: expected 0 containing 'alpha', got 2",
    "Error expectation at node outer: expected 1 containing 'shared', got 0"]);
  assert.deepEqual(result.mismatches.map(m => [m.kind, m.expectationIndex, m.expected.value, m.actual.value]),
    [['errorCount', 0, 0, 2], ['errorCount', 4, 1, 0]]);
  for (const m of result.mismatches) {
    const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = result.errorExpectations[m.expectationIndex];
    const { kind, sequenceIndex, messageIndex, event, output, pointer, ...mismatchDetail } = m;
    assert.equal(status, 'failed'); assert.deepEqual(detail, mismatchDetail);
  }
  c.expectError = c.expectError.slice(1, 2); // An unrelated error still suppresses message timeouts.
  c.expect = [{ node: 'plain', event: 'input', count: 999, timeout: 1 }];
  assert.deepEqual(evaluateAssertions(c, [], [{ node: 'other', message: 'alpha' }]).mismatches.map(m => m.kind), ['unexpectedError']);
});

test('zero declarations do not invent outcomes when worker evaluation is unavailable', async () => {
  const c = normalize([{ node: 'plain', message: 'absent', count: 0 }]);
  c.hardTimeout = 1;
  const controller = new AbortController(); controller.abort('SIGINT');
  for (const options of [{ signal: controller.signal }, {}]) {
    const result = await runWorker(c, {}, process.cwd(), options);
    assert.ok(['interrupted', 'deadline'].includes(result.termination.reason));
    assert.equal(Object.hasOwn(result, 'errorExpectations'), false);
  }
});

test('zero-count CLI covers retained matches, nonmatches, nested instances, entry loss and invalid counts', { timeout: 30000 }, t => {
  const root = path.resolve(__dirname, '..');
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-zero-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  require('../scripts/check-error-zero-count').checkErrorZeroCount({ executable: path.join(root, 'bin/nrflowtest.js'),
    fixtures: path.join(root, 'test/fixtures'), cwd, logDir: process.env.NRFLOWTEST_ZERO_LOG_DIR });
});
