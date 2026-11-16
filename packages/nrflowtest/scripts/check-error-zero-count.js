'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const YAML = require('yaml');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

// Exercise the same zero-count contract against source and offline-installed CLIs.
function checkErrorZeroCount({ executable, fixtures, cwd, logDir }) {
  if (logDir) fs.mkdirSync(logDir, { recursive: true });
  function invoke(label, file, exitCode, extra = [], allow = true) {
    const argv = [executable, 'run', '--flows', path.join(fixtures, 'error-zero-count.json'),
      '--tests', file, '--strict-types', ...(allow ? ['--allow-function'] : []), ...extra];
    const out = spawnSync(argv[0], argv.slice(1), { cwd, encoding: 'utf8', timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
    if (logDir) {
      const stdout = path.join(logDir, `${label}.stdout`), stderr = path.join(logDir, `${label}.stderr`);
      fs.writeFileSync(stdout, out.stdout || ''); fs.writeFileSync(stderr, out.stderr || '');
      fs.writeFileSync(path.join(logDir, `${label}.json`), JSON.stringify({ argv, cwd, exitCode: out.status,
        signal: out.signal, error: out.error?.message ?? null, stdout, stderr,
        testedTree: process.env.NRFLOWTEST_TESTED_TREE ?? null, testedHead: process.env.NRFLOWTEST_TESTED_HEAD ?? null }, null, 2) + '\n');
    }
    assert.equal(out.error, undefined, out.error?.message);
    assert.equal(out.status, exitCode, out.stderr + out.stdout);
    return out;
  }
  const positiveFile = path.join(fixtures, 'error-zero-count.test.yaml');
  const negativeFile = path.join(fixtures, 'error-zero-count.fail.yaml');
  const positive = JSON.parse(invoke('positive', positiveFile, 0, ['--reporter', 'json']).stdout);
  const negative = JSON.parse(invoke('negative', negativeFile, 1, ['--reporter', 'json', '--junit-file', 'zero-junit.xml']).stdout);
  assert.deepEqual([positive.passed, positive.failed, negative.passed, negative.failed], [3, 0, 1, 4]);
  const countFailure = (node, message, expected, actual) => `Error expectation at node ${node}: expected ${expected} containing '${message}', got ${actual}`;
  const resourceFailure = 'Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)';
  const failures = [
    [countFailure('plain', 'short <&', 0, 2), countFailure('plain', 'short <&', 1, 0), 'Timeout at node sink input after 80ms: expected 3 message(s), got 2'],
    [countFailure('outer', 'alpha', 2, 1)],
    ['Unexpected error at node plain: short <&'],
    [resourceFailure], [],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  assert.deepEqual(positive.cases.map(c => c.failures), [[], [], []]);
  const spec = invoke('spec', negativeFile, 1).stdout;
  for (const failure of failures.flat()) assert.ok(spec.includes(`  ${failure}\n`));
  assert.ok(spec.endsWith('1 passed, 4 failed\n'));
  const xml = fs.readFileSync(path.join(cwd, 'zero-junit.xml'), 'utf8');
  if (logDir) fs.writeFileSync(path.join(logDir, 'zero-junit.xml'), xml);
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.deepEqual([parsed['@_tests'], parsed['@_failures']], ['5', '4']);
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''), failures.map(f => f.join('\n')));

  const all = Array.from({ length: 100 }, (_, i) => i);
  const matches = [[[], []], [[], [], [0], [1], []], [[1], [], [0], []],
    [[0, 1], [], []], [[0], []], [[]], [[], all, []], [[]]];
  const candidates = [[[], []], [[0, 1], [0, 1], [0, 1], [1], []], [[0, 1], [0], [0], []],
    [[0, 1], [], []], [[0], []], [[0]], [all, all, []], [[]]];
  const consumed = (errorIndex, expectationIndex) => ({ errorIndex, expectationIndex });
  const pair = [consumed(0, 0), consumed(1, 0)];
  const provenance = [[[], []], [[], [], [], [], [consumed(1, 3)]],
    [[], [consumed(1, 0)], [], [consumed(0, 2), consumed(1, 0)]],
    [[], pair, pair], [[], [consumed(0, 0)]], [[]], [[], [], all.map(i => consumed(i, 1))], [[]]];
  const declarations = [positiveFile, negativeFile].flatMap(file => YAML.parse(fs.readFileSync(file, 'utf8')).cases.map(c => c.expectError));
  const cases = [...positive.cases, ...negative.cases];
  for (const [i, c] of cases.entries()) {
    assert.equal(c.termination, null);
    assert.equal(c.errorExpectations.length, declarations[i].length);
    const incomplete = i === 6;
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length,
      observedAtLeast: incomplete ? 101 : c.errors.length, complete: !incomplete });
    assert.deepEqual(c.resourceLimits, incomplete ? [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }] : []);
    for (const [j, e] of c.errorExpectations.entries()) {
      const declared = declarations[i][j], count = declared.count ?? 1, indexes = matches[i][j];
      assert.deepEqual([e.assertion, e.expectationIndex, e.node, e.errorMessage, e.expected, e.actual, e.status, e.errorIndexes,
        e.candidateErrorIndexes, e.nonmatchingErrorIndexes, e.previouslyConsumed, e.captureComplete],
      ['expectError', j, i === 2 || i === 4 || i === 6 ? 'outer' : declared.node, declared.message,
        { value: count }, { value: indexes.length }, count === indexes.length ? 'passed' : 'failed', indexes,
        candidates[i][j], candidates[i][j].filter(k => !indexes.includes(k)), provenance[i][j], !incomplete]);
      const truncated = i === 1 ? candidates[i][j].filter(k => k === 1) : incomplete ? candidates[i][j] : [];
      assert.deepEqual(e.messageFidelity, { complete: truncated.length === 0, truncatedErrorIndexes: truncated });
      if (e.status === 'failed') {
        const mismatch = c.mismatches.find(m => m.kind === 'errorCount' && m.expectationIndex === j);
        const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = e;
        const { kind, sequenceIndex, messageIndex, event, output, pointer, ...mismatchDetail } = mismatch;
        assert.deepEqual(detail, mismatchDetail);
      }
    }
  }
  assert.deepEqual(cases.map(c => c.errors.length), [0, 2, 2, 2, 1, 1, 100, 0]);
  assert.deepEqual(cases.map(c => c.mismatches.map(m => m.kind)),
    [[], [], [], ['errorCount', 'errorCount', 'count'], ['errorCount'], ['unexpectedError'], [], []]);
  for (const i of [2, 4, 6]) for (const error of cases[i].errors) {
    assert.equal(error.node, 'outer-middle-inner');
    assert.deepEqual(error.instances, ['outer-middle', 'outer']);
  }
  assert.deepEqual(cases[1].errors.map(e => [e.node, e.messageTruncated, e.messageOriginalLength]),
    [['plain', false, 8], ['plain', true, 8004]]);
  for (const error of cases[6].errors) {
    assert.equal(error.message, 'x'.repeat(8000));
    assert.equal(error.messageOriginalLength, 8004); assert.equal(error.messageTruncated, true);
  }

  const dry = JSON.parse(invoke('dry-run', positiveFile, 0, ['--dry-run', '--reporter', 'json']).stdout);
  assert.deepEqual(dry.cases, []);
  const usage = invoke('opt-in', positiveFile, 2, ['--junit-file', 'must-not-exist.xml'], false);
  assert.equal(usage.stdout, ''); assert.match(usage.stderr, /requires --allow-function/);
  for (const count of [-1, 0.5, 10001]) {
    const file = path.join(cwd, `invalid-${count}.yaml`);
    const yaml = YAML.stringify({ version: 1, cases: [{ name: 'invalid', expectError: [{ node: 'plain', message: 'x', count }] }] });
    fs.writeFileSync(file, yaml);
    if (logDir) fs.writeFileSync(path.join(logDir, path.basename(file)), yaml);
    try {
      const out = invoke(`invalid-${count}`, file, 2, ['--reporter', 'json', '--junit-file', 'must-not-exist.xml']);
      assert.equal(out.stdout, ''); assert.match(out.stderr, /expectError.count: expected integer 0–10000/);
    } finally { fs.rmSync(file, { force: true }); }
  }
  assert.deepEqual(fs.readdirSync(cwd), ['zero-junit.xml'], 'runtime directories and rejected outputs are absent');
  return { positivePasses: 3, negativeHealthyPasses: 1, intentionalFailures: 4, cliExitCodes: [0, 1, 2],
    nestedInstanceAttribution: true, retainedZeroWithIncompleteAndTruncatedCapture: true, parsedJunitAndSpecAgree: true };
}
module.exports = { checkErrorZeroCount };
