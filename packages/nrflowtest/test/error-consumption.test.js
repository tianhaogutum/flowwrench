'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluateAssertions } = require('../lib/assertions');

test('consumption provenance uses original capture and declaration indexes across concrete and instance selectors', () => {
  // Consume out of capture order to require capture-order provenance. Missing
  // truncation metadata does not make a witnessed retained substring unknown.
  const errors = [
    { node: 'inner', instances: ['a', 'outer'], message: 'alpha shared' },
    { node: 'inner', instances: ['a', 'outer'], message: 'beta shared' },
    { node: 'elsewhere', instances: ['b'], message: 'shared' },
    { node: 'inner', instances: ['a', 'outer'], message: 'nonmatch' },
    { node: 'inner', instances: ['a', 'outer'], message: 'shared remaining' },
  ];
  const original = structuredClone(errors);
  const c = { expectError: [
    { node: 'a', message: 'beta', count: 1 }, // Passing declaration index 0 consumes index 1.
    { node: 'b', message: 'shared', count: 1 },
    { node: 'inner', message: 'alpha', count: 2 }, // Failing declaration 2 consumes index 0.
    { node: 'outer', message: 'shared', count: 2 }, // Only index 4 remains; consume despite failure.
    { node: 'a', message: 'shared', count: 1 },
    { node: 'a', message: 'SHARED', count: 1 },
    { node: 'a', message: 'nonmatch', count: 1 },
    { node: 'b', message: 'alpha', count: 1 },
  ], expect: [] };
  const result = evaluateAssertions(c, [], errors, false);
  const records = result.mismatches;
  assert.deepEqual(records.map(m => [m.expectationIndex, m.actual.value, m.errorIndexes]),
    [[2, 1, [0]], [3, 1, [4]], [4, 0, []], [5, 0, []], [7, 0, []]]);
  assert.deepEqual(records.map(m => m.previouslyConsumed), [[],
    [{ errorIndex: 0, expectationIndex: 2 }, { errorIndex: 1, expectationIndex: 0 }],
    [{ errorIndex: 0, expectationIndex: 2 }, { errorIndex: 1, expectationIndex: 0 }, { errorIndex: 4, expectationIndex: 3 }], [], []]);
  assert.ok(records.every(m => m.captureComplete === false));
  assert.deepEqual(records.map(m => m.messageFidelity), [
    { complete: null, truncatedErrorIndexes: [] }, { complete: null, truncatedErrorIndexes: [] },
    { complete: null, truncatedErrorIndexes: [] }, { complete: null, truncatedErrorIndexes: [] },
    { complete: true, truncatedErrorIndexes: [] },
  ]);
  assert.deepEqual(errors, original, 'provenance does not annotate or mutate captures');
  assert.deepEqual(JSON.parse(JSON.stringify(records)), records);
  const empty = evaluateAssertions({ expectError: [{ node: 'a', message: 'shared', count: 1 }], expect: [] }, [], [], false).mismatches[0];
  assert.deepEqual(empty.previouslyConsumed, []);
  assert.deepEqual(empty.actual, { value: 0 });
  assert.equal(empty.captureComplete, false);
});
