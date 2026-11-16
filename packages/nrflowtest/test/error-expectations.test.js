'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluateAssertions, evaluate } = require('../lib/assertions');
const { normalizeCase } = require('../lib/schema');
const { runWorker } = require('../lib/runner');

test('successful error declarations expose ordered outcomes even without mismatches', () => {
  const c = normalizeCase({ name: 'passing', expectError: [
    { node: 'Instance', message: 'beta' }, { node: 'a', message: 'alpha' },
  ] }, [{ id: 'a', type: 'debug', name: 'Instance', x: 1, y: 1 }], 'test', 0);
  const errors = ['alpha shared', 'beta shared'].map(message => ({ node: 'inner', instances: ['a'], message, messageTruncated: false }));
  const original = structuredClone(errors);
  const result = evaluateAssertions(c, [], errors);
  assert.deepEqual(result.failures, []);
  assert.deepEqual(result.mismatches, []);
  assert.deepEqual(result.errorExpectations, [
    { assertion: 'expectError', expectationIndex: 0, node: 'a', errorMessage: 'beta', status: 'passed',
      expected: { value: 1 }, actual: { value: 1 }, errorIndexes: [1], captureComplete: true,
      candidateErrorIndexes: [0, 1], nonmatchingErrorIndexes: [0],
      messageFidelity: { complete: true, truncatedErrorIndexes: [] }, previouslyConsumed: [] },
    { assertion: 'expectError', expectationIndex: 1, node: 'a', errorMessage: 'alpha', status: 'passed',
      expected: { value: 1 }, actual: { value: 1 }, errorIndexes: [0], captureComplete: true,
      candidateErrorIndexes: [0], nonmatchingErrorIndexes: [],
      messageFidelity: { complete: true, truncatedErrorIndexes: [] }, previouslyConsumed: [] },
  ]);
  assert.deepEqual(evaluate(c, [], errors), []);
  assert.deepEqual(errors, original);
  assert.deepEqual(JSON.parse(JSON.stringify(result.errorExpectations)), result.errorExpectations);
});

test('mixed outcomes retain passing and failing consumers, truthful zero and independent capture fidelity', () => {
  const errors = [
    { node: 'inner', instances: ['a', 'outer'], message: 'alpha shared', messageTruncated: true },
    { node: 'inner', instances: ['a', 'outer'], message: 'beta shared', messageTruncated: false },
    { node: 'plain', message: 'shared', messageTruncated: false },
  ];
  const c = { expectError: [
    { node: 'a', message: 'beta', count: 1 },
    { node: 'inner', message: 'alpha', count: 2 },
    { node: 'outer', message: 'shared', count: 1 },
    { node: 'plain', message: 'shared', count: 1 },
    { node: 'a', message: 'TAIL', count: 1 },
  ], expect: [] };
  const result = evaluateAssertions(c, [], errors, false);
  assert.deepEqual(result.errorExpectations.map(e => [e.expectationIndex, e.status, e.expected.value, e.actual.value, e.errorIndexes]),
    [[0, 'passed', 1, 1, [1]], [1, 'failed', 2, 1, [0]], [2, 'failed', 1, 0, []], [3, 'passed', 1, 1, [2]], [4, 'failed', 1, 0, []]]);
  assert.deepEqual(result.errorExpectations.map(e => e.previouslyConsumed), [[], [],
    [{ errorIndex: 0, expectationIndex: 1 }, { errorIndex: 1, expectationIndex: 0 }], [], []]);
  assert.deepEqual(result.errorExpectations.map(e => e.messageFidelity), [
    { complete: false, truncatedErrorIndexes: [0] }, { complete: false, truncatedErrorIndexes: [0] },
    { complete: true, truncatedErrorIndexes: [] }, { complete: true, truncatedErrorIndexes: [] }, { complete: true, truncatedErrorIndexes: [] },
  ]);
  assert.ok(result.errorExpectations.every(e => e.captureComplete === false));
  for (const m of result.mismatches) {
    const { kind, sequenceIndex, messageIndex, event, output, pointer, ...detail } = m;
    const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...outcome } = result.errorExpectations[m.expectationIndex];
    assert.equal(kind, 'errorCount'); assert.equal(status, 'failed'); assert.deepEqual(detail, outcome);
  }
});

test('empty declarations and unexpected errors retain outcomes without obscuring error priority', () => {
  const c = { expectError: [], expect: [{ node: 'sink', event: 'input', count: 999, timeout: 1 }] };
  const errors = [{ node: 'n', message: 'known' }];
  const result = evaluateAssertions(c, [], errors);
  assert.deepEqual(result.errorExpectations, []);
  assert.deepEqual(result.mismatches.map(m => m.kind), ['unexpectedError']);
  c.expectError = [{ node: 'n', message: 'absent', count: 1 }, { node: 'n', message: 'known', count: 1 }];
  const mixed = evaluateAssertions(c, [], errors);
  assert.deepEqual(mixed.errorExpectations.map(e => [e.status, e.messageFidelity.complete]), [['failed', null], ['passed', null]]);
  c.expectError.pop();
  const priority = evaluateAssertions(c, [], errors);
  assert.deepEqual(priority.errorExpectations.map(e => e.status), ['failed']);
  assert.deepEqual(priority.mismatches.map(m => m.kind), ['errorCount', 'unexpectedError']);
  assert.deepEqual(evaluateAssertions({ expectError: [], expect: [] }, [], []).errorExpectations, []);
});

test('unavailable worker evaluation omits outcomes on interruption and startup deadline', async () => {
  const c = { timeout: 10, hardTimeout: 1, expectError: [{ node: 'n', message: 'known', count: 1 }], expect: [] };
  const controller = new AbortController(); controller.abort('SIGINT');
  const interrupted = await runWorker(c, {}, process.cwd(), { signal: controller.signal });
  assert.equal(interrupted.termination.reason, 'interrupted');
  assert.equal(Object.hasOwn(interrupted, 'errorExpectations'), false);
  const deadline = await runWorker(c, {}, process.cwd());
  assert.equal(deadline.termination.reason, 'deadline');
  assert.equal(Object.hasOwn(deadline, 'errorExpectations'), false);
});
