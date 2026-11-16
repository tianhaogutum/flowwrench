'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

// Shared source/installed CLI check; optional raw artifacts live outside the package.
function checkErrorMessageCapture({ executable, fixtures, cwd, logDir }) {
  if (logDir) fs.mkdirSync(logDir, { recursive: true });
  function invoke(label, file, expectedStatus, extra = [], allowFunction = true) {
    const args = ['run', '--flows', path.join(fixtures, 'error-message-capture.json'),
      '--tests', path.join(fixtures, file), '--strict-types', ...(allowFunction ? ['--allow-function'] : []), ...extra];
    const out = spawnSync(executable, args, { cwd, encoding: 'utf8', timeout: 30000, maxBuffer: 8 * 1024 * 1024 });
    if (logDir) {
      fs.writeFileSync(path.join(logDir, `${label}.stdout`), out.stdout || '');
      fs.writeFileSync(path.join(logDir, `${label}.stderr`), out.stderr || '');
      fs.writeFileSync(path.join(logDir, `${label}.json`), JSON.stringify({ argv: [executable, ...args], cwd,
        exitCode: out.status, signal: out.signal, error: out.error?.message ?? null,
        stdout: path.join(logDir, `${label}.stdout`), stderr: path.join(logDir, `${label}.stderr`) }, null, 2) + '\n');
    }
    assert.equal(out.error, undefined, out.error?.message);
    assert.equal(out.status, expectedStatus, out.stderr + out.stdout);
    return out;
  }
  const positive = JSON.parse(invoke('positive', 'error-message-capture.test.yaml', 0, ['--reporter', 'json']).stdout);
  const negative = JSON.parse(invoke('negative', 'error-message-capture.fail.yaml', 1,
    ['--reporter', 'json', '--junit-file', 'strings-junit.xml']).stdout);
  const spec = invoke('spec', 'error-message-capture.fail.yaml', 1).stdout;
  const usage = invoke('usage', 'error-message-capture.test.yaml', 2, ['--junit-file', 'must-not-exist.xml'], false);
  const xml = fs.readFileSync(path.join(cwd, 'strings-junit.xml'), 'utf8');
  if (logDir) fs.writeFileSync(path.join(logDir, 'strings-junit.xml'), xml);
  assert.equal(positive.passed, 2); assert.equal(positive.failed, 0);
  assert.equal(negative.passed, 1); assert.equal(negative.failed, 4);
  const originals = ['x'.repeat(7999), 'x'.repeat(8000), 'x'.repeat(8001), 'é'.repeat(8001),
    '😀'.repeat(4000), '😀'.repeat(4000) + 'z', 'x'.repeat(7999) + '😀', 'short <&'];
  const expectedErrors = strings => strings.map(message => ({ node: 'function', instances: [],
    message: message.slice(0, 8000), messageTruncated: message.length > 8000, messageOriginalLength: message.length }));
  // Check the existing strings independently before requiring the new metadata.
  assert.deepEqual(positive.cases[0].errors.map(e => e.message), originals.map(s => s.slice(0, 8000)));
  assert.deepEqual(negative.cases[0].errors.map(e => e.message), [...originals.map(s => s.slice(0, 8000)), '']);
  assert.equal(positive.cases[0].errors[6].message.charCodeAt(7999), 0xd83d);
  assert.equal(positive.cases[0].observedMessages, 16, 'original errors still reach real Catch nodes');
  assert.deepEqual(positive.cases[0].errors, expectedErrors(originals));
  assert.deepEqual(negative.cases.map(c => c.errors), [expectedErrors([...originals, '']),
    expectedErrors(['x'.repeat(8000) + 'TAIL']), expectedErrors(['x'.repeat(8001)]),
    expectedErrors(Array(100).fill('é'.repeat(8001))), []]);
  for (const c of [...positive.cases, ...negative.cases]) {
    assert.equal(c.termination, null);
    const incomplete = c.name === 'entry loss stays distinct from string truncation';
    assert.deepEqual(c.errorCapture, { limit: 100, captured: c.errors.length,
      observedAtLeast: incomplete ? 101 : c.errors.length, complete: !incomplete });
    assert.deepEqual(c.resourceLimits, incomplete ? [{ resource: 'errors', limit: 100, captured: 100, observedAtLeast: 101 }] : []);
  }
  for (const c of [...positive.cases, negative.cases[4]]) {
    assert.deepEqual(c.failures, []); assert.deepEqual(c.mismatches, []);
  }
  assert.deepEqual(positive.cases[1].errors, []);
  const unexpected = negative.cases[0].mismatches;
  assert.equal(unexpected.length, 9);
  for (const [i, record] of unexpected.entries()) {
    assert.equal(record.kind, 'unexpectedError'); assert.equal(record.errorIndex, i);
    assert.deepEqual(record.actual, { value: negative.cases[0].errors[i] });
  }
  const suffix = negative.cases[1].mismatches;
  assert.deepEqual(suffix.map(m => m.kind), ['errorCount', 'unexpectedError']);
  assert.deepEqual(suffix[0].actual, { value: 0 }); assert.deepEqual(suffix[0].errorIndexes, []);
  assert.equal(suffix[0].captureComplete, true);
  assert.deepEqual(suffix[1].actual, { value: negative.cases[1].errors[0] });
  const ordered = negative.cases[2].mismatches;
  assert.deepEqual(ordered.map(m => m.kind), ['errorCount', 'count']);
  assert.equal(ordered[0].expectationIndex, 1); assert.equal(ordered[0].captureComplete, true);
  assert.deepEqual(ordered[0].actual, { value: 0 }); assert.deepEqual(ordered[0].errorIndexes, []);
  assert.deepEqual(negative.cases[3].mismatches, []);
  const failures = [
    [...originals, ''].map(s => `Unexpected error at node function: ${s.slice(0, 8000)}`),
    ["Error expectation at node function: expected 1 containing 'TAIL', got 0", `Unexpected error at node function: ${'x'.repeat(8000)}`],
    ["Error expectation at node function: expected 1 containing 'x', got 0", 'Timeout at node sink input after 80ms: expected 2 message(s), got 1'],
    ['Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)'], [],
  ];
  assert.deepEqual(negative.cases.map(c => c.failures), failures);
  for (const f of failures.flat()) assert.ok(spec.includes(`  ${Buffer.from(f).toString()}\n`), 'unchanged UTF-8 spec failure');
  assert.ok(spec.endsWith('1 passed, 4 failed\n'));
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(parsed['@_tests'], '5'); assert.equal(parsed['@_failures'], '4');
  // Existing XML 1.0 sanitization removes the lone high surrogate; JSON keeps it.
  assert.deepEqual(parsed.testsuite.testcase.map(c => c.failure?.['#text'] ?? ''),
    failures.map(f => f.join('\n').replace(/\ud83d(?![\udc00-\udfff])/g, '')));
  assert.match(usage.stderr, /requires --allow-function/); assert.equal(usage.stdout, '');
  assert.deepEqual(fs.readdirSync(cwd), ['strings-junit.xml'], 'no worker directories or usage report survive');
  return { passed: positive.passed + negative.passed, intentionalFailures: negative.failed, cliExitCodes: [0, 1, 2] };
}
module.exports = { checkErrorMessageCapture };
