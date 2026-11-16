'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeCase } = require('../lib/schema');
const { evaluateAssertions } = require('../lib/assertions');
const { runWorker } = require('../lib/runner');
const flows = ['plain', 'outer', 'other', 'middle'].map(id => ({ id, type: 'debug', x: 1, y: 1 }));
const normalize = expectError => normalizeCase({ name: 'exact', expectError }, flows, 'exact.yaml', 0);
const exact = (message, count = 1, extra = {}) => ({ node: 'outer', message, messageMatch: 'exact', count, ...extra });

test('messageMatch accepts supplied exact/substring, preserves omission and rejects invalid modes', () => {
  assert.deepEqual(normalize([{ node: 'plain', message: 'x' }]).expectError, [{ node: 'plain', message: 'x', count: 1 }]);
  for (const messageMatch of ['exact', 'substring']) {
    for (const message of ['short', 'line\n雪🙂 <&', ' .* ', 'é', 'e\u0301']) {
      for (const count of [0, 1, 2, 10000]) {
        assert.deepEqual(normalize([{ node: 'plain', message, messageMatch, count }]).expectError[0],
          { node: 'plain', message, messageMatch, count });
      }
      assert.equal(normalize([{ node: 'plain', message, messageMatch }]).expectError[0].count, 1);
    }
  }
  for (const messageMatch of ['', null, 0, true, [], {}, 'EXACT', 'regex', ' exact']) {
    assert.throws(() => normalize([{ node: 'plain', message: 'x', messageMatch }]), /expectError.messageMatch must be exact or substring/);
  }
  for (const message of ['', null, 3]) assert.throws(() => normalize([exact(message)]), /expectError.message must be a nonempty literal message/);
  assert.throws(() => normalize([{ node: 'plain', message: '' }]), /expectError.message must be a nonempty literal substring/);
});

test('exact equality is literal and partitions candidates independently of message fidelity', () => {
  const messages = ['short', 'short tail', 'pre short', 'SHORT', 'line\n雪🙂 <&', 'line\r\n雪🙂 <&', 'é', 'e\u0301', '.*'];
  const errors = messages.map(message => ({ node: 'inner', instances: ['middle', 'outer'], message, messageTruncated: false }));
  errors.push({ node: 'inner', instances: ['outer'], message: 'x'.repeat(8000), messageTruncated: true });
  errors.push({ node: 'inner', instances: ['outer'], message: 'unknown' });
  errors.push({ node: 'different', instances: ['other'], message: 'short', messageTruncated: true });
  const original = structuredClone(errors);
  for (const [i, message] of messages.entries()) {
    const e = evaluateAssertions(normalize([exact(message)]), [], errors, false).errorExpectations[0];
    assert.deepEqual(e.errorIndexes, [i]);
    assert.deepEqual(e.candidateErrorIndexes, Array.from({ length: 11 }, (_, i) => i));
    assert.deepEqual(e.nonmatchingErrorIndexes, e.candidateErrorIndexes.filter(k => k !== i));
    assert.deepEqual(e.messageFidelity, { complete: false, truncatedErrorIndexes: [9] });
    assert.equal(e.captureComplete, false); assert.equal(e.status, 'passed');
  }
  for (const [message, indexes] of [['x'.repeat(8000), [9]], ['x'.repeat(8000) + 'TAIL', []], ['TAIL', []]]) {
    const e = evaluateAssertions(normalize([exact(message, indexes.length)]), [], errors).errorExpectations[0];
    assert.deepEqual(e.errorIndexes, indexes); assert.equal(e.status, 'passed');
    assert.equal(e.messageFidelity.complete, false);
  }
  const unknown = evaluateAssertions(normalize([exact('unknown')]), [], [errors[10]]).errorExpectations[0];
  assert.equal(unknown.messageFidelity.complete, null); assert.deepEqual(unknown.errorIndexes, [0]);
  assert.deepEqual(errors, original);
});

test('exact and substring prior evidence uses the current predicate after passing and failing consumers', () => {
  const errors = ['short', 'short tail', 'short', 'pre short'].map(message => ({ node: 'inner', instances: ['middle', 'outer'], message, messageTruncated: false }));
  const c = normalize([
    exact('short tail', 1, { node: 'middle', source: 'inner' }),
    exact('short', 0, { source: 'inner' }), // Fails and consumes both exact matches.
    exact('short', 1),
    exact('pre short', 0), // Fails and consumes.
    exact('short tail', 0),
    { node: 'outer', message: 'short', messageMatch: 'substring', count: 0 },
    { node: 'outer', message: 'short', count: 0 },
    exact('short', 0, { source: 'different' }),
    exact('short', 0, { node: 'plain', source: 'inner' }),
  ]);
  const r = evaluateAssertions(c, [], errors);
  const p = (errorIndex, expectationIndex) => ({ errorIndex, expectationIndex });
  assert.deepEqual(r.errorExpectations.map(e => e.errorIndexes), [[1], [0, 2], [], [3], [], [], [], [], []]);
  assert.deepEqual(r.errorExpectations.map(e => e.previouslyConsumed), [[], [], [p(0, 1), p(2, 1)], [], [p(1, 0)],
    [p(0, 1), p(1, 0), p(2, 1), p(3, 3)], [p(0, 1), p(1, 0), p(2, 1), p(3, 3)], [], []]);
  assert.deepEqual(r.failures, ["Error expectation at node outer, source 'inner': expected 0 exactly matching 'short', got 2",
    "Error expectation at node outer: expected 1 exactly matching 'short', got 0",
    "Error expectation at node outer: expected 0 exactly matching 'pre short', got 1"]);
  assert.equal(Object.hasOwn(r.errorExpectations[6], 'messageMatch'), false);
  assert.equal(r.errorExpectations[5].messageMatch, 'substring');
  for (const m of r.mismatches) {
    const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = r.errorExpectations[m.expectationIndex];
    const { kind, sequenceIndex, messageIndex, event, output, pointer, ...actual } = m;
    assert.equal(status, 'failed'); assert.deepEqual(detail, actual);
  }
});

test('exact source/node conjunction, retained zero, error priority and unavailable outcome omission', async () => {
  const errors = [
    { node: 'inner', instances: ['middle', 'outer'], message: 'short', messageTruncated: false },
    { node: 'right', instances: ['middle', 'outer'], message: 'short tail', messageTruncated: true },
    { node: 'inner', instances: ['other'], message: 'short', messageTruncated: false },
  ];
  const c = normalize([exact('short', 0, { source: 'right' }), exact('short', 1, { source: 'inner' }),
    exact('short', 1, { node: 'other', source: 'inner' }), exact('short tail', 1, { source: 'right' })]);
  const r = evaluateAssertions(c, [], errors);
  assert.deepEqual(r.failures, []);
  assert.deepEqual(r.errorExpectations.map(e => [e.candidateErrorIndexes, e.errorIndexes, e.messageFidelity.complete]),
    [[[1], [], false], [[0], [0], true], [[2], [2], true], [[1], [1], false]]);
  assert.deepEqual(evaluateAssertions(normalize([exact('short', 0)]), [], []).failures, []);
  const priority = normalize([exact('short', 0)]);
  priority.expect = [{ node: 'plain', event: 'input', count: 999, timeout: 1 }];
  const unmatched = evaluateAssertions(priority, [], [errors[1]]);
  assert.deepEqual(unmatched.mismatches.map(m => m.kind), ['unexpectedError']);
  assert.equal(unmatched.errorExpectations[0].status, 'passed');
  c.hardTimeout = 1;
  const controller = new AbortController(); controller.abort('SIGINT');
  for (const options of [{ signal: controller.signal }, {}]) {
    const result = await runWorker(c, {}, process.cwd(), options);
    assert.ok(['interrupted', 'deadline'].includes(result.termination.reason));
    assert.equal(Object.hasOwn(result, 'errorExpectations'), false);
  }
});

test('exact CLI preserves retained evidence, nested sources, reporters, validation and cleanup', { timeout: 30000 }, t => {
  const fs = require('node:fs'), path = require('node:path');
  const root = path.resolve(__dirname, '..');
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-exact-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  require('../scripts/check-error-exact').checkErrorExact({ executable: path.join(root, 'bin/nrflowtest.js'),
    fixtures: path.join(root, 'test/fixtures'), cwd, logDir: process.env.NRFLOWTEST_EXACT_LOG_DIR });
});
