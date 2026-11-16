'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeCase } = require('../lib/schema');
const { evaluate, evaluateAssertions, partialDiff } = require('../lib/assertions');
const flows = [{ id: 'router', type: 'switch', name: 'Router', x: 20, y: 20 }];
const normalize = c => normalizeCase({ name: 'diff', timeout: 100, ...c }, flows, 'test', 0);
const event = (msg, output, at = 0) => ({ node: 'router', event: output === undefined ? 'input' : 'output', output, at, msg });

test('partial-message records use lossless JSON Pointers and retain the first human-readable difference', () => {
  const examples = [
    [{ payload: { 'a/b': { '~key.with.dots': [false] } } }, { payload: { 'a/b': { '~key.with.dots': [0] } } }, '/payload/a~1b/~0key.with.dots/0', 0, { value: false }],
    [{ payload: {} }, { payload: { '': null } }, '/payload/', null, { missing: true }],
    [{ payload: null }, { payload: {} }, '/payload', {}, { value: null }],
    [{ payload: {} }, { payload: [] }, '/payload', [], { value: {} }],
    [{ payload: [1] }, { payload: [1, 2] }, '/payload', [1, 2], { value: [1] }],
    [{ payload: { '0': false } }, { payload: { '0': true } }, '/payload/0', true, { value: false }],
    [{ payload: { '~1': 'literal' } }, { payload: { '~1': 'other' } }, '/payload/~01', 'other', { value: 'literal' }],
    [{ payload: { missing: true } }, { payload: null }, '/payload', null, { value: { missing: true } }],
  ];
  for (const [actual, expected, pointer, expectedValue, actualValue] of examples) {
    const c = normalize({ expect: [{ node: 'Router', msg: expected }] });
    const result = evaluateAssertions(c, [event(actual)], []);
    assert.deepEqual(result.mismatches, [{ kind: 'message', assertion: 'expect', expectationIndex: 0,
      sequenceIndex: null, messageIndex: 0, node: 'router', event: 'input', output: null,
      pointer, expected: { value: expectedValue }, actual: actualValue }]);
    assert.deepEqual(result.failures, [`Message mismatch at node router input, message 1: ${partialDiff(actual, expected)}`]);
    assert.deepEqual(evaluate(c, [event(actual)], []), result.failures);
  }
});

test('common and sequence records retain filtered indexes, actual ports, root and untruncated exact values', () => {
  const longExpected = 'e'.repeat(1500), longActual = 'a'.repeat(1600);
  const c = normalize({ expect: [
    { node: 'router', event: 'output', output: 0, count: 1 },
    { node: 'router', event: 'output', output: 1, timeout: 50,
      msg: { topic: 'common' }, fields: { '/payload': longExpected },
      sequence: [{ output: 0, fields: { '': { payload: 'sequence' } } }] },
  ] });
  const msg = { topic: 'actual', payload: longActual };
  const events = [event({}, 0), event(msg, 1, 50), event({}, 1, 51)];
  const result = evaluateAssertions(c, events, []);
  const base = { assertion: 'expect', expectationIndex: 1, node: 'router', event: 'output', output: 1, messageIndex: 0 };
  assert.deepEqual(result.mismatches, [
    { ...base, kind: 'message', sequenceIndex: null, pointer: '/topic', expected: { value: 'common' }, actual: { value: 'actual' } },
    { ...base, kind: 'field', sequenceIndex: null, pointer: '/payload', expected: { value: longExpected }, actual: { value: longActual } },
    { ...base, kind: 'output', sequenceIndex: 0, pointer: null, expected: { value: 0 }, actual: { value: 1 } },
    { ...base, kind: 'field', sequenceIndex: 0, pointer: '', expected: { value: { payload: 'sequence' } }, actual: { value: msg } },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.mismatches)), result.mismatches);
  assert.equal(result.failures.length, 4);
});

test('count records identify original expectation arrays and absent/extra sequence emissions without invented values', () => {
  const c = normalize({ expect: [
    { node: 'router', sequence: [{}, {}] },
    { node: 'router', event: 'output', output: 1, sequence: [{}] },
  ], expectNoMessage: [
    { node: 'router', event: 'output', output: 0 },
    { node: 'router' },
  ] });
  const events = [event({}), event({}, 0), event({}, 1), event({}, 1)];
  const result = evaluateAssertions(c, events, []);
  const base = { node: 'router', kind: 'count', sequenceIndex: null, messageIndex: null, pointer: null };
  assert.deepEqual(result.mismatches, [
    { ...base, assertion: 'expect', expectationIndex: 0, event: 'input', output: null, expected: { value: 2 }, actual: { value: 1 } },
    { ...base, assertion: 'expect', expectationIndex: 1, event: 'output', output: 1, expected: { value: 1 }, actual: { value: 2 } },
    { ...base, assertion: 'expectNoMessage', expectationIndex: 0, event: 'output', output: 0, expected: { value: 0 }, actual: { value: 1 } },
    { ...base, assertion: 'expectNoMessage', expectationIndex: 1, event: 'input', output: null, expected: { value: 0 }, actual: { value: 1 } },
  ]);
  assert.deepEqual(result.failures, [
    'Timeout at node router input after 100ms: expected 2 message(s), got 1',
    'Count mismatch at node router output output 1 after 100ms: expected 1 message(s), got 2',
    'Count mismatch at node router output output 0 after 100ms: expected 0 message(s), got 1',
    'Count mismatch at node router input after 100ms: expected 0 message(s), got 1',
  ]);
});

test('passing results have no mismatches; error-only records preserve unexpected-error priority', () => {
  const c = normalize({ expect: [{ node: 'router', fields: { '/payload': null } }] });
  assert.deepEqual(evaluateAssertions(c, [event({ payload: null })], []), { failures: [], mismatches: [], errorExpectations: [] });
  const error = { node: 'router', message: 'broken' };
  assert.deepEqual(evaluateAssertions(c, [], [error]), { failures: ['Unexpected error at node router: broken'], mismatches: [
    { kind: 'unexpectedError', assertion: null, expectationIndex: null, sequenceIndex: null, messageIndex: null,
      node: 'router', event: 'error', output: null, pointer: null, errorIndex: 0, expected: { missing: true }, actual: { value: error } },
  ], errorExpectations: [] });
  const errorsOnly = normalize({ expectError: [{ node: 'router', message: 'broken' }] });
  assert.deepEqual(evaluateAssertions(errorsOnly, [], []), {
    failures: ["Error expectation at node router: expected 1 containing 'broken', got 0"], mismatches: [
      { kind: 'errorCount', previouslyConsumed: [], captureComplete: true, messageFidelity: { complete: true, truncatedErrorIndexes: [] }, assertion: 'expectError', expectationIndex: 0, sequenceIndex: null, messageIndex: null,
        node: 'router', event: 'error', output: null, pointer: null, errorMessage: 'broken', errorIndexes: [],
        expected: { value: 1 }, actual: { value: 0 } },
    ],
    errorExpectations: [{ assertion: 'expectError', expectationIndex: 0, node: 'router', errorMessage: 'broken',
      status: 'failed', expected: { value: 1 }, actual: { value: 0 }, errorIndexes: [], captureComplete: true,
      candidateErrorIndexes: [], nonmatchingErrorIndexes: [],
      messageFidelity: { complete: true, truncatedErrorIndexes: [] }, previouslyConsumed: [] }],
  });
  assert.deepEqual(evaluateAssertions(errorsOnly, [], [error]), { failures: [], mismatches: [],
    errorExpectations: [{ assertion: 'expectError', expectationIndex: 0, node: 'router', errorMessage: 'broken',
      status: 'passed', expected: { value: 1 }, actual: { value: 1 }, errorIndexes: [0], captureComplete: true,
      candidateErrorIndexes: [0], nonmatchingErrorIndexes: [],
      messageFidelity: { complete: null, truncatedErrorIndexes: [] }, previouslyConsumed: [] }],
  });
  c.expectError = errorsOnly.expectError;
  assert.equal(evaluateAssertions(c, [], [error]).mismatches[0].kind, 'count');
});
