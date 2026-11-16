'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const YAML = require('yaml');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

function checkErrorSource({ executable, fixtures, cwd, logDir }) {
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
  const positiveFile = path.join(fixtures, 'error-source.test.yaml');
  const negativeFile = path.join(fixtures, 'error-source.fail.yaml');
  const positive = JSON.parse(invoke('positive', positiveFile, 0, ['--reporter', 'json']).stdout);
  const negative = JSON.parse(invoke('negative', negativeFile, 1, ['--reporter', 'json', '--junit-file', 'source-junit.xml']).stdout);
  assert.deepEqual([positive.passed, positive.failed, negative.passed, negative.failed], [4, 0, 1, 3]);
  const failures = [
    ["Error expectation at node outer, source 'outer-middle-left': expected 0 containing 'shared', got 2",
      "Error expectation at node outer, source 'outer-middle-left': expected 1 containing 'shared', got 0",
      "Error expectation at node outer: expected 1 containing 'shared', got 0"],
    ['Unexpected error at node outer-middle-right: shared <&'],
    ["Error expectation at node outer, source 'left': expected 1 containing 'shared', got 0"], [],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  const spec = invoke('spec', negativeFile, 1).stdout;
  for (const f of failures.flat()) assert.ok(spec.includes(`  ${f}\n`));
  assert.ok(spec.endsWith('1 passed, 3 failed\n'));
  const xml = fs.readFileSync(path.join(cwd, 'source-junit.xml'), 'utf8');
  if (logDir) fs.writeFileSync(path.join(logDir, 'source-junit.xml'), xml);
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.deepEqual([parsed['@_tests'], parsed['@_failures']], ['4', '3']);
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''), failures.map(f => f.join('\n')));

  const candidates = [[[0], [1], [], []], [[], [], [], [], [], [2], [2], [0, 1], [3]],
    [[], [0, 1], []], [[0], [1], [0]], [[0, 1], [], [2], [], []], [[1], [0]], [[], [0, 1]], [[]]];
  const matches = [[[0], [1], [], []], [[], [], [], [], [], [], [2], [0, 1], [3]],
    [[], [0, 1], []], [[], [1], [0]], [[0, 1], [], [2], [], []], [[], [0]], [[], [0, 1]], [[]]];
  const consumed = (errorIndex, expectationIndex) => ({ errorIndex, expectationIndex });
  const left = [consumed(0, 0), consumed(1, 0)];
  const prior = [[[], [], [consumed(0, 0)], [consumed(0, 0), consumed(1, 1)]],
    [[], [], [], [], [], [], [], [], []], [[], [], [consumed(0, 1), consumed(1, 1)]], [[], [], []],
    [[], left, [], left, [...left, consumed(2, 2)]], [[], []], [[], []], [[]]];
  const declarations = [positiveFile, negativeFile].flatMap(file => YAML.parse(fs.readFileSync(file, 'utf8')).cases.map(c => c.expectError));
  const cases = [...positive.cases, ...negative.cases];
  assert.deepEqual(cases.map(c => c.errors.map(e => e.node)), [
    ['outer-middle-left', 'outer-middle-right'], ['outer-middle-left', 'outer-middle-left', 'outer-middle-right', 'other-middle-left'],
    ['plain', 'plain'], ['outer-middle-left', 'outer-middle-right'],
    ['outer-middle-left', 'outer-middle-left', 'outer-middle-right'], ['outer-middle-left', 'outer-middle-right'],
    ['outer-middle-left', 'outer-middle-right'], [],
  ]);
  for (const [i, c] of cases.entries()) {
    assert.equal(c.termination, null); assert.deepEqual(c.resourceLimits, []);
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length, observedAtLeast: c.errors.length, complete: true });
    assert.equal(c.errorExpectations.length, declarations[i].length);
    for (const error of c.errors) {
      if (error.node !== 'plain') assert.deepEqual(error.instances, [error.node.startsWith('other-') ? 'other-middle' : 'outer-middle', error.node.startsWith('other-') ? 'other' : 'outer']);
    }
    for (const [j, e] of c.errorExpectations.entries()) {
      const declared = declarations[i][j], count = declared.count ?? 1, indexes = matches[i][j];
      assert.equal(Object.hasOwn(e, 'source'), Object.hasOwn(declared, 'source'));
      assert.equal(e.source, declared.source);
      assert.deepEqual([e.node, e.errorMessage, e.expected, e.actual, e.status, e.errorIndexes, e.candidateErrorIndexes, e.nonmatchingErrorIndexes,
        e.previouslyConsumed, e.captureComplete],
      [declared.node?.name ? 'outer' : declared.node, declared.message, { value: count }, { value: indexes.length }, count === indexes.length ? 'passed' : 'failed', indexes,
        candidates[i][j], candidates[i][j].filter(k => !indexes.includes(k)), prior[i][j], true]);
      const truncated = i === 3 && j !== 1 ? [0] : [];
      assert.deepEqual(e.messageFidelity, { complete: truncated.length === 0, truncatedErrorIndexes: truncated });
      if (e.status === 'failed') {
        const mismatch = c.mismatches.find(m => m.kind === 'errorCount' && m.expectationIndex === j);
        const { status, candidateErrorIndexes, nonmatchingErrorIndexes, ...detail } = e;
        const { kind, sequenceIndex, messageIndex, event, output, pointer, ...mismatchDetail } = mismatch;
        assert.deepEqual(detail, mismatchDetail);
      }
    }
  }
  assert.deepEqual(cases.map(c => c.mismatches.map(m => m.kind)), [[], [], [], [], ['errorCount', 'errorCount', 'errorCount'], ['unexpectedError'], ['errorCount'], []]);
  assert.equal(cases[3].errors[0].message, 'x'.repeat(8000));
  assert.equal(cases[3].errors[0].messageOriginalLength, 8004); assert.equal(cases[3].errors[0].messageTruncated, true);
  const dry = JSON.parse(invoke('dry-run', positiveFile, 0, ['--dry-run', '--reporter', 'json']).stdout);
  assert.deepEqual(dry.cases, []);
  const usage = invoke('opt-in', positiveFile, 2, ['--junit-file', 'must-not-exist.xml'], false);
  assert.equal(usage.stdout, ''); assert.match(usage.stderr, /requires --allow-function/);
  for (const [i, source] of ['', null, 42, {}, []].entries()) {
    const file = path.join(cwd, `invalid-${i}.yaml`);
    const yaml = YAML.stringify({ version: 1, cases: [{ name: 'invalid', expectError: [{ node: 'outer', source, message: 'shared' }] }] });
    fs.writeFileSync(file, yaml);
    if (logDir) fs.writeFileSync(path.join(logDir, path.basename(file)), yaml);
    try {
      const out = invoke(`invalid-${i}`, file, 2, ['--reporter', 'json', '--junit-file', 'must-not-exist.xml']);
      assert.equal(out.stdout, ''); assert.match(out.stderr, /expectError.source must be a nonempty literal source id/);
    } finally { fs.rmSync(file, { force: true }); }
  }
  assert.deepEqual(fs.readdirSync(cwd), ['source-junit.xml'], 'runtime directories and rejected outputs absent');
  return { positivePasses: 4, negativeHealthyPasses: 1, intentionalFailures: 3, cliExitCodes: [0, 1, 2],
    realNestedTwoSources: true, sourceScopedFidelityAndConsumption: true, parsedJunitAndSpecAgree: true };
}
module.exports = { checkErrorSource };
