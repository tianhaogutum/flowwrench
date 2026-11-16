'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { normalizeCase } = require('../lib/schema');
const { evaluateAssertions, evaluate } = require('../lib/assertions');
const { checkErrorMismatches } = require('../scripts/check-error-mismatches');
const root = path.resolve(__dirname, '..');
const flows = [{ id: 'n', type: 'function', name: 'Node', x: 1, y: 1 }];
const normalize = c => normalizeCase({ name: 'errors', timeout: 50, ...c }, flows, 'test', 0);
const base = { sequenceIndex: null, messageIndex: null, node: 'n', event: 'error', output: null, pointer: null };

test('error-count records retain declaration indexes, original error indexes and truthful zero counts', () => {
  const c = normalize({ expectError: [
    { node: 'Node', message: 'known', count: 1 },
    { node: 'n', message: 'known', count: 1 },
    { node: 'n', message: 'other', count: 2 },
  ] });
  const errors = [{ node: 'n', message: 'known first' }, { node: 'n', message: 'other' }, { node: 'n', message: 'known second' }];
  const result = evaluateAssertions(c, [], errors);
  assert.deepEqual(result.mismatches, [
    { ...base, kind: 'errorCount', previouslyConsumed: [], captureComplete: true, messageFidelity: { complete: null, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex: 0, errorMessage: 'known', errorIndexes: [0, 2], expected: { value: 1 }, actual: { value: 2 } },
    { ...base, kind: 'errorCount', previouslyConsumed: [{ errorIndex: 0, expectationIndex: 0 }, { errorIndex: 2, expectationIndex: 0 }], captureComplete: true, messageFidelity: { complete: null, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex: 1, errorMessage: 'known', errorIndexes: [], expected: { value: 1 }, actual: { value: 0 } },
    { ...base, kind: 'errorCount', previouslyConsumed: [], captureComplete: true, messageFidelity: { complete: null, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex: 2, errorMessage: 'other', errorIndexes: [1], expected: { value: 2 }, actual: { value: 1 } },
  ]);
  assert.deepEqual(result.failures, [
    "Error expectation at node n: expected 1 containing 'known', got 2",
    "Error expectation at node n: expected 1 containing 'known', got 0",
    "Error expectation at node n: expected 2 containing 'other', got 1",
  ]);
  assert.deepEqual(evaluate(c, [], errors), result.failures);
  assert.deepEqual(errors.map(e => e.message), ['known first', 'other', 'known second']);
});

test('unexpected errors retain concrete source, instance and runtime identities and suppress message checks', () => {
  const c = normalize({ expectError: [{ node: 'n', message: 'KNOWN' }], expect: [{ node: 'n' }] });
  const errors = [
    { node: 'n-inner', instances: ['n'], message: 'known <&' },
    { node: 'elsewhere', instances: [], message: 'KNOWN' },
    { node: null, instances: [], message: 'runtime failure' },
  ];
  const result = evaluateAssertions(c, [], errors);
  assert.deepEqual(result.mismatches, [
    { ...base, kind: 'errorCount', previouslyConsumed: [], captureComplete: true, messageFidelity: { complete: null, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex: 0, errorMessage: 'KNOWN', errorIndexes: [], expected: { value: 1 }, actual: { value: 0 } },
    ...errors.map((error, errorIndex) => ({ ...base, node: error.node, kind: 'unexpectedError', assertion: null, expectationIndex: null,
      errorIndex, expected: { missing: true }, actual: { value: error } })),
  ]);
  assert.deepEqual(result.failures, [
    "Error expectation at node n: expected 1 containing 'KNOWN', got 0",
    'Unexpected error at node n-inner: known <&',
    'Unexpected error at node elsewhere: KNOWN',
    'Unexpected error at node <runtime>: runtime failure',
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.mismatches)), result.mismatches);
  c.expectError[0].message = 'known';
  assert.equal(evaluateAssertions(c, [], [errors[0]]).mismatches[0].kind, 'count');
});

test('real caught subflow errors expose structured mismatches with unchanged human and parsed JUnit failures', t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-error-test-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkErrorMismatches({ executable: path.join(root, 'bin/nrflowtest.js'), fixtures: path.join(root, 'test/fixtures'), cwd });
});
