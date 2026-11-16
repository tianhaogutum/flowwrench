'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

// The same real Node-RED fixtures exercise source and offline-installed CLIs.
function checkErrorSelectorFidelity({ executable, fixtures, cwd, logDir }) {
  if (logDir) fs.mkdirSync(logDir, { recursive: true });
  function invoke(label, file, status, extra = [], allow = true) {
    const args = ['run', '--flows', path.join(fixtures, 'error-selector-fidelity.json'),
      '--tests', path.join(fixtures, file), '--strict-types', ...(allow ? ['--allow-function'] : []), ...extra];
    const out = spawnSync(executable, args, { cwd, encoding: 'utf8', timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
    if (logDir) {
      fs.writeFileSync(path.join(logDir, `${label}.stdout`), out.stdout || '');
      fs.writeFileSync(path.join(logDir, `${label}.stderr`), out.stderr || '');
      fs.writeFileSync(path.join(logDir, `${label}.json`), JSON.stringify({ argv: [executable, ...args], cwd,
        exitCode: out.status, signal: out.signal, error: out.error?.message ?? null,
        stdout: path.join(logDir, `${label}.stdout`), stderr: path.join(logDir, `${label}.stderr`) }, null, 2) + '\n');
    }
    assert.equal(out.error, undefined, out.error?.message);
    assert.equal(out.status, status, out.stderr + out.stdout);
    return out;
  }
  const positive = JSON.parse(invoke('positive', 'error-selector-fidelity.test.yaml', 0, ['--reporter', 'json']).stdout);
  const negative = JSON.parse(invoke('negative', 'error-selector-fidelity.fail.yaml', 1,
    ['--reporter', 'json', '--junit-file', 'selector-junit.xml']).stdout);
  const spec = invoke('spec', 'error-selector-fidelity.fail.yaml', 1).stdout;
  const usage = invoke('usage', 'error-selector-fidelity.test.yaml', 2, ['--junit-file', 'must-not-exist.xml'], false);
  const xml = fs.readFileSync(path.join(cwd, 'selector-junit.xml'), 'utf8');
  if (logDir) fs.writeFileSync(path.join(logDir, 'selector-junit.xml'), xml);
  assert.equal(positive.passed, 2); assert.equal(positive.failed, 0);
  assert.equal(negative.passed, 1); assert.equal(negative.failed, 8);
  const countFailure = (node, text, expected, actual) => `Error expectation at node ${node}: expected ${expected} containing '${text}', got ${actual}`;
  const resourceFailure = 'Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)';
  const failures = [
    [countFailure('plain', 'x', 2, 1), countFailure('plain', 'x', 1, 0), 'Timeout at node sink input after 80ms: expected 2 message(s), got 1'],
    [countFailure('a', 'TAIL', 1, 0), countFailure('a', 'OTHER', 1, 0), countFailure('a', 'TAIL', 1, 0)],
    [countFailure('a', 'short <&', 2, 1)],
    [countFailure('a', 'x', 2, 1), countFailure('a', 'x', 1, 0)],
    [countFailure('a', 'absent', 1, 0), 'Unexpected error at node a-inner: short <&'],
    [resourceFailure, countFailure('a', 'short', 101, 100), countFailure('a', 'absent', 1, 0), countFailure('b', 'absent', 1, 0)],
    [resourceFailure, countFailure('a', 'x', 101, 100), countFailure('a', 'x', 1, 0), countFailure('b', 'absent', 1, 0)],
    [countFailure('a', 'beta', 2, 1), countFailure('a', 'shared', 1, 0), countFailure('a', 'short', 2, 1), countFailure('a', 'short', 1, 0), countFailure('a', 'absent', 1, 0)], [],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  for (const f of failures.flat()) assert.ok(spec.includes(`  ${f}\n`));
  assert.ok(spec.endsWith('1 passed, 8 failed\n'));
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(parsed['@_tests'], '9'); assert.equal(parsed['@_failures'], '8');
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''), failures.map(f => f.join('\n')));
  assert.match(usage.stderr, /requires --allow-function/); assert.equal(usage.stdout, '');
  for (const c of [...positive.cases, ...negative.cases]) {
    assert.equal(c.termination, null);
    const incomplete = ['discarded entries do not taint retained short string fidelity', 'entry loss and retained truncation are independent'].includes(c.name);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: incomplete ? 101 : c.errors.length, complete: !incomplete });
    assert.deepEqual(c.resourceLimits, incomplete ? [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }] : []);
  }
  for (const c of [...positive.cases, negative.cases[8]]) assert.deepEqual(c.mismatches, []);
  assert.deepEqual(negative.cases[0].mismatches.map(m => m.kind), ['errorCount', 'errorCount', 'count']);
  assert.deepEqual(negative.cases[4].mismatches.map(m => m.kind), ['errorCount', 'unexpectedError']);
  assert.deepEqual(negative.cases[4].mismatches[1].actual, { value: negative.cases[4].errors[0] });
  assert.deepEqual(negative.cases[2].errors.map(e => [e.node, e.instances, e.messageTruncated]).sort((a, b) => a[0].localeCompare(b[0])),
    [['a-inner', ['a'], false], ['b-inner', ['b'], true], ['plain', [], true]]);
  // [declaration index, expected count, captured count, matching original indexes, fidelity, truncated candidate indexes]
  const all = Array.from({ length: 100 }, (_, i) => i);
  const expected = [
    [[0, 2, 1, [0], false, [0]], [1, 1, 0, [], true, []]],
    [[0, 1, 0, [], false, [0]], [1, 1, 0, [], false, [0]], [3, 1, 0, [], true, []]],
    [[0, 2, 1, [negative.cases[2].errors.findIndex(e => e.node === 'a-inner')], true, []]],
    [[0, 2, 1, [0], false, [0]], [1, 1, 0, [], true, []]],
    [[0, 1, 0, [], true, []]],
    [[0, 101, 100, all, true, []], [1, 1, 0, [], true, []], [2, 1, 0, [], true, []]],
    [[0, 101, 100, all, false, all], [1, 1, 0, [], true, []], [2, 1, 0, [], true, []]],
    [[1, 2, 1, [1], true, []], [2, 1, 0, [], true, []], [3, 2, 1, [2], true, []], [4, 1, 0, [], true, []], [5, 1, 0, [], true, []]], [],
  ];
  // Verify counts/consumption independently before requiring the additive JSON.
  const records = negative.cases.map(c => c.mismatches.filter(m => m.kind === 'errorCount'));
  assert.deepEqual(records.map(ms => ms.map(m => [m.expectationIndex, m.expected.value, m.actual.value, m.errorIndexes])),
    expected.map(es => es.map(e => e.slice(0, 4))));
  for (const [i, ms] of records.entries()) for (const m of ms) assert.equal(m.captureComplete, i !== 5 && i !== 6);
  assert.deepEqual(fs.readdirSync(cwd), ['selector-junit.xml'], 'workers and usage output cleaned up');
  assert.deepEqual(records.map(ms => ms.map(m => m.messageFidelity)), expected.map(es => es.map(e => ({ complete: e[4], truncatedErrorIndexes: e[5] }))));
  // Capture-order retained matches only, including passing declarations omitted from mismatches.
  const consumed = (errorIndex, expectationIndex) => ({ errorIndex, expectationIndex });
  assert.deepEqual(negative.cases[7].errors.map(e => [e.node, e.instances, e.message]),
    [['a-inner', ['a'], 'alpha shared'], ['a-inner', ['a'], 'beta shared'], ['a-inner', ['a'], 'short']]);
  assert.deepEqual(records.map(ms => ms.map(m => m.previouslyConsumed)), [
    [[], [consumed(0, 0)]],
    [[], [], []], // Never infer TAIL from a discarded suffix, before or after consumption.
    [[]],
    [[], [consumed(0, 0)]],
    [[]],
    [[], [], []], // Consumed short does not match absent; no discarded entries inferred.
    [[], all.map(i => consumed(i, 0)), []],
    [[], [consumed(0, 0), consumed(1, 1)], [], [consumed(2, 3)], []], [],
  ]);
  // Require outcomes even when mismatches is empty. Index tables are witnessed
  // captures, not inferred emitted totals or discarded-suffix matches.
  const indexAt = (c, node) => [c.errors.findIndex(e => e.node === node)];
  const matchIndexes = [
    [indexAt(positive.cases[0], 'a-inner'), indexAt(positive.cases[0], 'b-inner'), indexAt(positive.cases[0], 'plain')], [],
    [[0], []], [[], [], [0], []],
    [indexAt(negative.cases[2], 'a-inner'), indexAt(negative.cases[2], 'b-inner'), indexAt(negative.cases[2], 'plain')],
    [[0], [], [1]], [[]], [all, [], []], [all, [], []], [[0], [1], [], [2], [], []], [],
  ];
  // Explicit pre-consumption candidates, including retained substring misses.
  // Resolve cross-node scheduling only where the fixture injects distinct nodes.
  const candidateIndexes = [
    matchIndexes[0], [], [[0], []], [[0], [0], [0], []], matchIndexes[4],
    [[0, 1], [1], [1]], [[0]], [all, [], []], [all, [], []],
    [[0, 1, 2], [1, 2], [2], [2], [], []], [],
  ];
  const yaml = require('yaml');
  const declarations = ['error-selector-fidelity.test.yaml', 'error-selector-fidelity.fail.yaml']
    .flatMap(file => yaml.parse(fs.readFileSync(path.join(fixtures, file), 'utf8')).cases.map(c => c.expectError || []));
  for (const [i, c] of [...positive.cases, ...negative.cases].entries()) {
    assert.ok(Array.isArray(c.errorExpectations), c.name);
    assert.equal(c.errorExpectations.length, declarations[i].length);
    for (const [j, e] of c.errorExpectations.entries()) {
      const declared = declarations[i][j], indexes = matchIndexes[i][j];
      assert.deepEqual([e.assertion, e.expectationIndex, e.node, e.errorMessage, e.expected, e.actual, e.errorIndexes, e.status, e.captureComplete],
        ['expectError', j, declared.node, declared.message, { value: declared.count ?? 1 }, { value: indexes.length }, indexes,
          indexes.length === (declared.count ?? 1) ? 'passed' : 'failed', c.errorCapture.complete]);
      const candidates = candidateIndexes[i][j];
      assert.deepEqual(e.candidateErrorIndexes, candidates);
      assert.deepEqual(e.nonmatchingErrorIndexes, candidates.filter(k => !indexes.includes(k)));
      const mismatch = c.mismatches.find(m => m.kind === 'errorCount' && m.expectationIndex === j);
      const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = e;
      if (mismatch) {
        const { kind, sequenceIndex, messageIndex, event, output, pointer, ...expectedDetail } = mismatch;
        assert.deepEqual(detail, expectedDetail);
      } else {
        assert.equal(status, 'passed');
        assert.deepEqual(e.previouslyConsumed, []);
        const truncated = candidates.filter(k => c.errors[k].messageTruncated);
        assert.deepEqual(e.messageFidelity, { complete: truncated.length === 0, truncatedErrorIndexes: truncated });
      }
    }
  }
  const incomplete = JSON.parse(invoke('incomplete', 'error-expectations.fail.yaml', 1, ['--reporter', 'json']).stdout);
  assert.equal(incomplete.failed, 1); assert.equal(incomplete.passed, 0);
  const c = incomplete.cases[0];
  assert.deepEqual(c.errorCapture, { limit: 100, captured: 100, observedAtLeast: 101, complete: false });
  assert.deepEqual(c.resourceLimits, [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }]);
  assert.deepEqual(c.errorExpectations, [
    { assertion: 'expectError', expectationIndex: 0, node: 'a', errorMessage: 'x', status: 'passed',
      expected: { value: 100 }, actual: { value: 100 }, errorIndexes: all, captureComplete: false,
      candidateErrorIndexes: all, nonmatchingErrorIndexes: [],
      messageFidelity: { complete: false, truncatedErrorIndexes: all }, previouslyConsumed: [] },
    { assertion: 'expectError', expectationIndex: 1, node: 'a', errorMessage: 'x', status: 'failed',
      expected: { value: 1 }, actual: { value: 0 }, errorIndexes: [], captureComplete: false,
      candidateErrorIndexes: [], nonmatchingErrorIndexes: [],
      messageFidelity: { complete: true, truncatedErrorIndexes: [] }, previouslyConsumed: all.map(i => consumed(i, 0)) },
  ]);
  assert.deepEqual(c.failures, [resourceFailure, countFailure('a', 'x', 1, 0)]);
  assert.deepEqual(c.mismatches.map(m => m.expectationIndex), [1]);
  assert.deepEqual(fs.readdirSync(cwd), ['selector-junit.xml']);
  return { positivePasses: positive.passed, negativeHealthyPasses: negative.passed,
    intentionalFailures: negative.failed + incomplete.failed, incompletePassingDeclarations: 1, cliExitCodes: [0, 1, 2] };
}
module.exports = { checkErrorSelectorFidelity };
