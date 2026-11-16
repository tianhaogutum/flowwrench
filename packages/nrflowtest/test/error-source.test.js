'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeCase } = require('../lib/schema');
const { evaluateAssertions } = require('../lib/assertions');
const { runWorker } = require('../lib/runner');
const flows = ['plain', 'outer', 'other', 'inner', 'middle'].map(id => ({ id, name: `Named ${id}`, type: 'debug', x: 1, y: 1 }));
const normalize = expectError => normalizeCase({ name: 'source', expectError }, flows, 'source.yaml', 0);

test('source is optional literal nonempty data, independent of exported node resolution', () => {
  const omitted = normalize([{ node: 'plain', message: 'x' }]).expectError[0];
  assert.deepEqual(omitted, { node: 'plain', message: 'x', count: 1 });
  for (const source of ['plain', 'outer-middle-inner', 'inner', 'Named plain', 'unknown', ' ', '.*', 'ü\n<&']) {
    for (const count of [0, 1, 2, 10000]) {
      assert.deepEqual(normalize([{ node: { name: 'Named outer' }, source, message: 'x', count }]).expectError[0],
        { node: 'outer', source, message: 'x', count });
    }
    assert.equal(normalize([{ node: 'outer', source, message: 'x' }]).expectError[0].count, 1);
  }
  for (const source of ['', null, 0, true, [], {}, { id: 'plain' }]) {
    assert.throws(() => normalize([{ node: 'plain', source, message: 'x' }]), /expectError.source must be a nonempty literal source id/);
  }
  assert.throws(() => normalize([{ node: 'unknown', source: 'plain', message: 'x' }]), /unknown node/);
});

test('source conjoins node/instance candidates, fidelity and literal matches without inferring missing data', () => {
  const errors = [
    { node: 'inner', instances: ['middle', 'outer'], message: 'shared', messageTruncated: false },
    { node: 'other', instances: ['middle', 'outer'], message: 'shared', messageTruncated: true },
    { node: 'inner', instances: ['middle', 'outer'], message: 'x'.repeat(8000), messageTruncated: true },
    { node: 'inner', instances: ['elsewhere'], message: 'shared', messageTruncated: false },
    { node: 'inner', instances: ['middle', 'outer'], message: 'SHARED' },
  ];
  const original = structuredClone(errors);
  const c = normalize([
    { node: 'outer', source: 'inner', message: 'TAIL', count: 0 },
    { node: 'middle', source: 'inner', message: 'shared' },
    { node: 'outer', source: 'other', message: 'shared' },
    { node: 'inner', source: 'inner', message: 'shared' }, // Direct source also sees other ancestry.
    { node: 'outer', source: 'inner', message: 'x' },
    { node: 'middle', source: 'inner', message: 'SHARED' },
    { node: 'outer', source: 'inner', message: 'shared', count: 0 },
  ]);
  const r = evaluateAssertions(c, [], errors, false);
  assert.deepEqual(r.failures, []);
  assert.deepEqual(r.errorExpectations.map(e => [e.candidateErrorIndexes, e.errorIndexes, e.nonmatchingErrorIndexes, e.messageFidelity.complete]), [
    [[0, 2, 4], [], [0, 2, 4], false], [[0, 2, 4], [0], [2, 4], false],
    [[1], [1], [], false], [[2, 3, 4], [3], [2, 4], false], [[2, 4], [2], [4], false],
    [[4], [4], [], null], [[], [], [], true],
  ]);
  assert.deepEqual(r.errorExpectations[6].previouslyConsumed, [{ errorIndex: 0, expectationIndex: 1 }]);
  assert.ok(r.errorExpectations.every(e => e.captureComplete === false));
  for (const source of ['INNER', 'ner', 'Named inner', 'missing', '.*']) {
    const e = evaluateAssertions(normalize([{ node: 'outer', source, message: 'shared', count: 0 }]), [], errors).errorExpectations[0];
    assert.deepEqual([e.source, e.candidateErrorIndexes, e.errorIndexes, e.previouslyConsumed, e.messageFidelity.complete], [source, [], [], [], true]);
  }
  assert.deepEqual(errors, original);
});

test('source scopes passing/failing all-match consumption and capture-order prior consumers', () => {
  const errors = ['inner', 'other', 'inner'].map(node => ({ node, instances: ['middle', 'outer'], message: 'shared', messageTruncated: false }));
  const c = normalize([
    { node: 'middle', source: 'other', message: 'shared' },
    { node: 'outer', source: 'inner', message: 'shared', count: 0 }, // Fails and consumes both.
    { node: 'outer', source: 'inner', message: 'shared', count: 0 },
    { node: 'outer', source: 'other', message: 'shared', count: 2 },
    { node: 'inner', source: 'other', message: 'shared' }, // Source equality cannot bypass node selection.
    { node: 'outer', message: 'shared' },
  ]);
  const r = evaluateAssertions(c, [], errors);
  const inner = [{ errorIndex: 0, expectationIndex: 1 }, { errorIndex: 2, expectationIndex: 1 }];
  assert.deepEqual(r.errorExpectations.map(e => [e.status, e.errorIndexes, e.previouslyConsumed]), [
    ['passed', [1], []], ['failed', [0, 2], []], ['passed', [], inner],
    ['failed', [], [{ errorIndex: 1, expectationIndex: 0 }]], ['failed', [], []],
    ['failed', [], [inner[0], { errorIndex: 1, expectationIndex: 0 }, inner[1]]],
  ]);
  assert.deepEqual(r.failures, ["Error expectation at node outer, source 'inner': expected 0 containing 'shared', got 2",
    "Error expectation at node outer, source 'other': expected 2 containing 'shared', got 0",
    "Error expectation at node inner, source 'other': expected 1 containing 'shared', got 0",
    "Error expectation at node outer: expected 1 containing 'shared', got 0"]);
  assert.equal(Object.hasOwn(r.errorExpectations[5], 'source'), false);
  for (const m of r.mismatches) {
    const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = r.errorExpectations[m.expectationIndex];
    const { kind, sequenceIndex, messageIndex, event, output, pointer, ...mismatchDetail } = m;
    assert.equal(status, 'failed'); assert.deepEqual(detail, mismatchDetail);
  }
});

test('unmatched source retains error priority and unavailable evaluations omit outcomes', async () => {
  const c = normalize([{ node: 'outer', source: 'inner', message: 'shared', count: 0 }]);
  c.expect = [{ node: 'plain', event: 'input', count: 999, timeout: 1 }];
  const r = evaluateAssertions(c, [], [{ node: 'other', instances: ['outer'], message: 'shared' }]);
  assert.deepEqual(r.mismatches.map(m => m.kind), ['unexpectedError']);
  assert.equal(r.errorExpectations[0].status, 'passed');
  c.hardTimeout = 1;
  const controller = new AbortController(); controller.abort('SIGINT');
  for (const options of [{ signal: controller.signal }, {}]) {
    const result = await runWorker(c, {}, process.cwd(), options);
    assert.ok(['interrupted', 'deadline'].includes(result.termination.reason));
    assert.equal(Object.hasOwn(result, 'errorExpectations'), false);
  }
});

test('source CLI covers two real nested sources, counts, literal misses, reporters and cleanup', { timeout: 30000 }, t => {
  const fs = require('node:fs'), path = require('node:path');
  const root = path.resolve(__dirname, '..');
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-source-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  require('../scripts/check-error-source').checkErrorSource({ executable: path.join(root, 'bin/nrflowtest.js'),
    fixtures: path.join(root, 'test/fixtures'), cwd, logDir: process.env.NRFLOWTEST_SOURCE_LOG_DIR });
});
