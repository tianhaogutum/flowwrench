'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const YAML = require('yaml');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

function checkErrorExact({ executable, fixtures, cwd, logDir }) {
  if (logDir) fs.mkdirSync(logDir, { recursive: true });
  function invoke(label, file, exitCode, extra = [], allow = true) {
    const argv = [executable, 'run', '--flows', path.join(fixtures, 'error-source.json'),
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
  const positiveFile = path.join(fixtures, 'error-exact.test.yaml');
  const negativeFile = path.join(fixtures, 'error-exact.fail.yaml');
  const positive = JSON.parse(invoke('positive', positiveFile, 0, ['--reporter', 'json']).stdout);
  const negative = JSON.parse(invoke('negative', negativeFile, 1, ['--reporter', 'json', '--junit-file', 'exact-junit.xml']).stdout);
  assert.deepEqual([positive.passed, positive.failed, negative.passed, negative.failed], [4, 0, 1, 4]);
  const failures = [
    ["Error expectation at node outer, source 'outer-middle-left': expected 0 exactly matching 'short', got 2",
      "Error expectation at node outer, source 'outer-middle-left': expected 1 exactly matching 'short', got 0"],
    ['Unexpected error at node plain: short tail'],
    ["Error expectation at node plain: expected 1 exactly matching 'short', got 0"],
    ['Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)'], [],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  const spec = invoke('spec', negativeFile, 1).stdout;
  for (const f of failures.flat()) assert.ok(spec.includes(`  ${f}\n`));
  assert.ok(spec.endsWith('1 passed, 4 failed\n'));
  const xml = fs.readFileSync(path.join(cwd, 'exact-junit.xml'), 'utf8');
  if (logDir) fs.writeFileSync(path.join(logDir, 'exact-junit.xml'), xml);
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.deepEqual([parsed['@_tests'], parsed['@_failures']], ['5', '4']);
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''), failures.map(f => f.join('\n')));

  const range = n => Array.from({ length: n }, (_, i) => i);
  const candidates = [
    [range(8), range(8), [1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 5, 6, 7], [1, 2, 3, 6, 7], [1, 2, 3, 7], [1, 2, 3], [1, 2], [], []],
    [[3], [0, 1, 2], [2], [2], [4]], [[0], [0], [1], [], []], [[]],
    [[0, 1, 2], [2], [2], [], [3]], [[0]], [[0, 1], [0, 1]], [range(100)], [[0]],
  ];
  const matches = [[[ ], [0], [4], [5], [6], [7], [3], [1, 2], [], []],
    [[3], [0, 1], [], [2], [4]], [[], [0], [1], [], []], [[]],
    [[0, 1], [], [2], [], [3]], [[]], [[], [0, 1]], [range(100)], [[0]]];
  const p = (errorIndex, expectationIndex) => ({ errorIndex, expectationIndex });
  const prior = [
    [[], [], [], [], [], [], [], [p(0, 1)], [p(0, 1)], [p(0, 1), p(1, 7), p(2, 7)]],
    [[], [], [p(0, 1), p(1, 1), p(3, 0)], [p(0, 1), p(1, 1)], []],
    [[], [], [], [], [p(0, 1)]], [[]],
    [[], [p(0, 0), p(1, 0)], [p(0, 0), p(1, 0)], [p(2, 2)], []], [[]], [[], []], [[]], [[]],
  ];
  const declarations = [positiveFile, negativeFile].flatMap(file => YAML.parse(fs.readFileSync(file, 'utf8')).cases.map(c => c.expectError));
  const cases = [...positive.cases, ...negative.cases];
  for (const [i, c] of cases.entries()) {
    assert.equal(c.termination, null);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: i === 7 ? 101 : c.errors.length, complete: i !== 7 });
    assert.deepEqual(c.resourceLimits, i === 7 ? [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }] : []);
    for (const error of c.errors) {
      if (error.node !== 'plain') assert.deepEqual(error.instances, [error.node.startsWith('other-') ? 'other-middle' : 'outer-middle', error.node.startsWith('other-') ? 'other' : 'outer']);
    }
    assert.equal(c.errorExpectations.length, declarations[i].length);
    for (const [j, e] of c.errorExpectations.entries()) {
      const declared = declarations[i][j], count = declared.count ?? 1, indexes = matches[i][j];
      for (const key of ['source', 'messageMatch']) {
        assert.equal(Object.hasOwn(e, key), Object.hasOwn(declared, key)); assert.equal(e[key], declared[key]);
      }
      assert.deepEqual([e.node, e.errorMessage, e.expected, e.actual, e.status, e.errorIndexes, e.candidateErrorIndexes, e.nonmatchingErrorIndexes,
        e.previouslyConsumed, e.captureComplete],
      [declared.node, declared.message, { value: count }, { value: indexes.length }, count === indexes.length ? 'passed' : 'failed', indexes,
        candidates[i][j], candidates[i][j].filter(k => !indexes.includes(k)), prior[i][j], i !== 7]);
      const truncated = i === 2 && j < 2 ? [0] : i === 7 ? range(100) : [];
      assert.deepEqual(e.messageFidelity, { complete: truncated.length === 0, truncatedErrorIndexes: truncated });
      if (e.status === 'failed') {
        const mismatch = c.mismatches.find(m => m.kind === 'errorCount' && m.expectationIndex === j);
        const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = e;
        const { kind, sequenceIndex, messageIndex, event, output, pointer, ...actual } = mismatch;
        assert.deepEqual(detail, actual);
      }
    }
  }
  assert.deepEqual(cases.map(c => c.mismatches.map(m => m.kind)), [[], [], [], [], ['errorCount', 'errorCount'], ['unexpectedError'], ['errorCount'], [], []]);
  assert.deepEqual(cases[1].errors.map(e => e.node), ['outer-middle-left', 'outer-middle-left', 'outer-middle-left', 'outer-middle-right', 'other-middle-left']);
  assert.deepEqual([cases[2].errors[0].message, cases[2].errors[0].messageOriginalLength, cases[2].errors[0].messageTruncated], ['x'.repeat(8000), 8004, true]);
  assert.deepEqual(cases[0].errors.map(e => e.message), ['short', 'short tail', 'pre short', 'SHORT', 'line\n雪🙂 <&', 'é', 'e\u0301', '.*']);
  const dry = JSON.parse(invoke('dry-run', positiveFile, 0, ['--dry-run', '--reporter', 'json']).stdout);
  assert.deepEqual(dry.cases, []); assert.equal(dry.planned.length, 4);
  const usage = invoke('opt-in', positiveFile, 2, ['--junit-file', 'must-not-exist.xml'], false);
  assert.equal(usage.stdout, ''); assert.match(usage.stderr, /requires --allow-function/);
  for (const [i, messageMatch] of ['', null, 42, false, {}, [], 'EXACT', 'regex'].entries()) {
    const file = path.join(cwd, `invalid-${i}.yaml`);
    const yaml = YAML.stringify({ version: 1, cases: [{ name: 'invalid', expectError: [{ node: 'outer', message: 'short', messageMatch }] }] });
    fs.writeFileSync(file, yaml);
    if (logDir) fs.writeFileSync(path.join(logDir, path.basename(file)), yaml);
    try {
      const out = invoke(`invalid-${i}`, file, 2, ['--reporter', 'json', '--junit-file', 'must-not-exist.xml']);
      assert.equal(out.stdout, ''); assert.match(out.stderr, /expectError.messageMatch must be exact or substring/);
    } finally { fs.rmSync(file, { force: true }); }
  }
  assert.deepEqual(fs.readdirSync(cwd), ['exact-junit.xml'], 'runtime directories and rejected outputs absent');
  return { positivePasses: 4, negativeHealthyPasses: 1, intentionalFailures: 4, cliExitCodes: [0, 1, 2],
    retainedExactTruncatedEquality: true, independentIncompleteCapture: true,
    realNestedSourceAttribution: true, exactPriorConsumerEvidence: true, parsedJunitAndSpecAgree: true };
}
module.exports = { checkErrorExact };
