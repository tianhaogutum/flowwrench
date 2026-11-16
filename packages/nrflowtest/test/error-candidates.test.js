'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluateAssertions } = require('../lib/assertions');

test('candidate evidence partitions retained capture order before passing and failing consumers', () => {
  const inner = (message, messageTruncated) => ({ node: 'inner', instances: ['a', 'outer'], message,
    ...(messageTruncated === undefined ? {} : { messageTruncated }) });
  const errors = [inner('alpha shared', true), inner('beta shared', false),
    { node: 'elsewhere', message: 'shared', messageTruncated: true },
    inner('short', false), inner('x'.repeat(8000), true), inner('SHARED', false), inner('shared')];
  const original = structuredClone(errors);
  const c = { expectError: [
    { node: 'a', message: 'beta', count: 1 },
    { node: 'inner', message: 'alpha', count: 2 },
    { node: 'outer', message: 'shared', count: 1 },
    { node: 'a', message: 'shared', count: 1 },
    { node: 'a', message: 'TAIL', count: 1 }, // No inference from the discarded suffix.
    { node: 'outer', message: 'SHARED', count: 1 },
    { node: 'inner', message: 'x', count: 1 },
    { node: 'outer', message: 'absent', count: 1 },
    { node: 'a', message: 'short', count: 1 },
    { node: 'outer', message: 'shared', count: 1 },
    { node: 'missing', message: 'shared', count: 1 },
    { node: 'elsewhere', message: 'shared', count: 1 },
  ], expect: [] };
  const result = evaluateAssertions(c, [], errors, false);
  const evidence = result.errorExpectations.map(e => [e.status, e.candidateErrorIndexes, e.nonmatchingErrorIndexes, e.errorIndexes]);
  assert.deepEqual(evidence, [
    ['passed', [0, 1, 3, 4, 5, 6], [0, 3, 4, 5, 6], [1]],
    ['failed', [0, 3, 4, 5, 6], [3, 4, 5, 6], [0]],
    ['passed', [3, 4, 5, 6], [3, 4, 5], [6]],
    ['failed', [3, 4, 5], [3, 4, 5], []],
    ['failed', [3, 4, 5], [3, 4, 5], []],
    ['passed', [3, 4, 5], [3, 4], [5]],
    ['passed', [3, 4], [3], [4]],
    ['failed', [3], [3], []],
    ['passed', [3], [], [3]],
    ['failed', [], [], []], ['failed', [], [], []], ['passed', [2], [], [2]],
  ]);
  assert.deepEqual(result.errorExpectations.map(e => e.messageFidelity.complete),
    [false, false, false, false, false, false, false, true, true, true, true, false]);
  assert.deepEqual(result.errorExpectations[9].previouslyConsumed,
    [{ errorIndex: 0, expectationIndex: 1 }, { errorIndex: 1, expectationIndex: 0 }, { errorIndex: 6, expectationIndex: 2 }]);
  assert.ok(result.errorExpectations.every(e => e.captureComplete === false));
  for (const mismatch of result.mismatches) {
    const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = result.errorExpectations[mismatch.expectationIndex];
    const { kind, sequenceIndex, messageIndex, event, output, pointer, ...mismatchDetail } = mismatch;
    assert.equal(status, 'failed'); assert.equal(kind, 'errorCount');
    assert.deepEqual(detail, mismatchDetail, 'existing mismatch evidence is preserved');
    assert.equal(Object.hasOwn(mismatch, 'candidateErrorIndexes'), false);
    assert.equal(Object.hasOwn(mismatch, 'nonmatchingErrorIndexes'), false);
  }
  assert.deepEqual(errors, original);
  assert.deepEqual(JSON.parse(JSON.stringify(result.errorExpectations)), result.errorExpectations);
});

test('candidate evidence is available with unknown string fidelity and unexpected-error early return', () => {
  const errors = [{ node: 'n', message: 'short' }]; // Helper metadata is unavailable.
  const c = { expectError: [{ node: 'n', message: 'SHORT', count: 1 }, { node: 'absent', message: 'short', count: 1 }],
    expect: [{ node: 'sink', event: 'input', count: 999, timeout: 1 }] };
  const result = evaluateAssertions(c, [], errors, false);
  assert.deepEqual(result.errorExpectations.map(e => [e.actual.value, e.candidateErrorIndexes, e.nonmatchingErrorIndexes, e.messageFidelity.complete]),
    [[0, [0], [0], null], [0, [], [], true]]);
  assert.deepEqual(result.mismatches.map(m => m.kind), ['errorCount', 'errorCount', 'unexpectedError']);
  assert.deepEqual(result.errorExpectations.map(e => e.captureComplete), [false, false]);
});
