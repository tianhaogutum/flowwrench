#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');
const { checkSignalCleanup } = require('./check-signal-cleanup');
const { checkErrorMismatches } = require('./check-error-mismatches');
const { checkErrorMessageCapture } = require('./check-error-message-capture');
const { checkErrorSelectorFidelity } = require('./check-error-selector-fidelity');
const { checkErrorZeroCount } = require('./check-error-zero-count');
const { checkErrorSource } = require('./check-error-source');
const { checkErrorExact } = require('./check-error-exact');
const { checkWorkerExits } = require('./check-worker-exits');
const { checkResourceLimits, checkResourceCapture } = require('./check-resource-limits');
const root = path.resolve(__dirname, '..');
const scratch = fs.mkdtempSync(path.join(root, '.nrflowtest-package-'));
function command(executable, args, cwd, expectedStatus = 0) {
  const result = spawnSync(executable, args, { cwd, encoding: 'utf8', timeout: 120000, maxBuffer: 1024 * 1024 });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, expectedStatus, `${executable} ${args.join(' ')}\n${result.stderr}\n${result.stdout}`);
  return result.stdout;
}
async function verifyPackage() {
  try {
    const packed = JSON.parse(command('npm', ['pack', '--json', '--pack-destination', scratch, '--ignore-scripts'], root))[0];
    const prefix = path.join(scratch, 'installed');
    fs.mkdirSync(prefix);
    // Extract only our own freshly built archive, not a user-supplied tarball.
    command('tar', ['-xzf', path.join(scratch, packed.filename), '-C', prefix], scratch);
    const installed = path.join(prefix, 'package');
    command('npm', ['ci', '--offline', '--ignore-scripts', '--omit=dev', '--no-audit', '--no-fund'], installed);
    for (const file of ['LICENSE', 'NOTICE', 'THIRD_PARTY.md', 'STATUS.json', 'npm-shrinkwrap.json', 'research/evidence-index.md', 'research/dependency-audit.json']) assert.ok(fs.existsSync(path.join(installed, file)), `missing packaged ${file}`);
    const bare = path.join(scratch, 'bare'); fs.mkdirSync(bare);
    const executable = path.join(installed, 'bin/nrflowtest.js');
    assert.equal(command(executable, ['--version'], bare).trim(), '0.1.0');
    const report = JSON.parse(command(executable, ['run', '--flows', path.join(installed, 'fixtures/orders/flows.json'), '--tests', path.join(installed, 'fixtures/orders/*.test.yaml'), '--strict-types', '--reporter', 'json'], bare));
    assert.equal(report.passed, 3); assert.equal(report.failed, 0);
    for (const c of report.cases) { assert.deepEqual(c.mismatches, []); assert.equal(c.termination, null); }
    const deadlines = JSON.parse(command(executable, ['run', '--flows', path.join(installed, 'test/fixtures/deadline-isolation.json'),
      '--tests', path.join(installed, 'test/fixtures/deadline-isolation.test.yaml'), '--allow-function', '--reporter', 'json',
      '--junit-file', 'deadline-junit.xml'], bare, 1));
    assert.equal(deadlines.passed, 1); assert.equal(deadlines.failed, 2);
    assert.deepEqual(deadlines.cases.map(c => c.status), ['failed', 'failed', 'passed']);
    for (const [i, phase] of ['observation', 'cleanup'].entries()) {
      assert.deepEqual(deadlines.cases[i].termination, { reason: 'deadline', phase, hardTimeout: 1500,
        signal: null, exitCode: null, exitSignal: 'SIGKILL' });
      assert.deepEqual(deadlines.cases[i].mismatches, []);
      assert.match(deadlines.cases[i].failures[0], new RegExp(`hard deadline exceeded.*1500ms.*${phase}`));
      assert.ok(deadlines.cases[i].durationMs >= 1500);
    }
    const deadlineXml = fs.readFileSync(path.join(bare, 'deadline-junit.xml'), 'utf8');
    assert.equal(XMLValidator.validate(deadlineXml), true);
    const parsed = new XMLParser({ ignoreAttributes: false }).parse(deadlineXml);
    assert.equal(parsed.testsuites['@_tests'], '3'); assert.equal(parsed.testsuites['@_failures'], '2');
    assert.equal(deadlines.cases[2].termination, null);
    for (const i of [0, 1]) assert.equal(parsed.testsuites.testsuite.testcase[i].failure['#text'], deadlines.cases[i].failures.join('\n'));
    const sequenceFlows = path.join(installed, 'test/fixtures/output-sequence.json');
    const sequence = JSON.parse(command(executable, ['run', '--flows', sequenceFlows,
      '--tests', path.join(installed, 'test/fixtures/output-sequence.test.yaml'), '--strict-types', '--reporter', 'json'], bare));
    assert.equal(sequence.passed, 1); assert.equal(sequence.failed, 0);
    assert.deepEqual(sequence.cases[0].mismatches, []);
    const mismatch = JSON.parse(command(executable, ['run', '--flows', sequenceFlows,
      '--tests', path.join(installed, 'test/fixtures/output-sequence.fail.yaml'), '--strict-types', '--reporter', 'json',
      '--junit-file', 'sequence-junit.xml'], bare, 1));
    assert.equal(mismatch.failed, 1); assert.equal(mismatch.passed, 0);
    const records = mismatch.cases[0].mismatches;
    assert.deepEqual(records.map(m => [m.kind, m.assertion, m.expectationIndex, m.sequenceIndex, m.messageIndex, m.node, m.event, m.output, m.pointer]), [
      ['count', 'expect', 0, null, null, 'router', 'output', null, null],
      ['field', 'expect', 0, 0, 0, 'router', 'output', 0, '/payload/id'],
      ['output', 'expect', 0, 1, 1, 'router', 'output', 0, null],
      ['field', 'expect', 0, 1, 1, 'router', 'output', 0, '/payload/id'],
      ['field', 'expect', 0, 2, 2, 'router', 'output', 1, '/payload/missing'],
    ]);
    assert.deepEqual(records.map(m => [m.expected, m.actual]), [
      [{ value: 4 }, { value: 3 }], [{ value: 'B<&' }, { value: 'A' }],
      [{ value: 1 }, { value: 0 }], [{ value: 'A' }, { value: 'B' }],
      [{ value: null }, { missing: true }],
    ]);
    const failures = mismatch.cases[0].failures;
    assert.ok(failures.some(f => /expected 4 message\(s\), got 3/.test(f)));
    assert.ok(failures.some(f => /message 1.*JSON Pointer "\/payload\/id".*expected 'B<&', got 'A'/.test(f)));
    assert.ok(failures.some(f => /message 2.*expected output 1, got 0/.test(f)));
    assert.ok(failures.some(f => /message 3.*JSON Pointer "\/payload\/missing".*missing; expected null/.test(f)));
    const sequenceXml = fs.readFileSync(path.join(bare, 'sequence-junit.xml'), 'utf8');
    assert.equal(XMLValidator.validate(sequenceXml), true);
    const sequenceParsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(sequenceXml);
    assert.equal(sequenceParsed.testsuites['@_tests'], '1'); assert.equal(sequenceParsed.testsuites['@_failures'], '1');
    assert.equal(sequenceParsed.testsuites.testsuite.testcase.failure['#text'], failures.join('\n'));
    const errorDir = path.join(bare, 'errors'); fs.mkdirSync(errorDir);
    checkErrorMismatches({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: errorDir });
    fs.rmSync(errorDir, { recursive: true });
    const stringsDir = path.join(bare, 'strings'); fs.mkdirSync(stringsDir);
    checkErrorMessageCapture({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: stringsDir });
    fs.rmSync(stringsDir, { recursive: true });
    const selectorDir = path.join(bare, 'selector'); fs.mkdirSync(selectorDir);
    checkErrorSelectorFidelity({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: selectorDir });
    fs.rmSync(selectorDir, { recursive: true });
    const sourceDir = path.join(bare, 'source'); fs.mkdirSync(sourceDir);
    checkErrorSource({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: sourceDir });
    fs.rmSync(sourceDir, { recursive: true });
    const exactDir = path.join(bare, 'exact'); fs.mkdirSync(exactDir);
    checkErrorExact({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: exactDir });
    fs.rmSync(exactDir, { recursive: true });
    const zeroDir = path.join(bare, 'zero'); fs.mkdirSync(zeroDir);
    checkErrorZeroCount({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: zeroDir });
    fs.rmSync(zeroDir, { recursive: true });
    const exitsDir = path.join(bare, 'exits'); fs.mkdirSync(exitsDir);
    checkWorkerExits({ executable, flows: path.join(installed, 'test/fixtures/deadline-isolation.json'), cwd: exitsDir });
    fs.rmSync(exitsDir, { recursive: true });
    const limitsDir = path.join(bare, 'limits'); fs.mkdirSync(limitsDir);
    checkResourceLimits({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: limitsDir });
    fs.rmSync(limitsDir, { recursive: true });
    const captureDir = path.join(bare, 'capture'); fs.mkdirSync(captureDir);
    checkResourceCapture({ executable, fixtures: path.join(installed, 'test/fixtures'), cwd: captureDir });
    fs.rmSync(captureDir, { recursive: true });
    for (const [signal, phase] of [['SIGINT', 'observation'], ['SIGTERM', 'cleanup']]) {
      const signalDir = path.join(bare, signal); fs.mkdirSync(signalDir);
      const result = await checkSignalCleanup({ executable, flows: path.join(installed, 'test/fixtures/deadline-isolation.json'),
        cwd: signalDir, signal, phase, reporter: 'json', existingReport: true });
      assert.equal(XMLValidator.validate(result.xml), true);
      const interrupted = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(result.xml).testsuites;
      for (const c of result.report.cases) assert.deepEqual(c.mismatches, []);
      assert.equal(interrupted['@_tests'], '2'); assert.equal(interrupted['@_failures'], '1');
      assert.equal(interrupted.testsuite.testcase[0].failure, undefined);
      assert.equal(interrupted.testsuite.testcase[1].failure['#text'], result.report.cases[1].failures.join('\n'));
      assert.deepEqual(fs.readdirSync(signalDir).sort(), ['signal-junit.xml', 'signals.yaml']);
      fs.rmSync(signalDir, { recursive: true });
    }
    // A locked root installation must retain all patched versions. npm ignores
    // overrides when this CLI is installed as another package's dependency.
    const { createRequire } = require('node:module');
    const installedRequire = createRequire(path.join(installed, 'package.json'));
    const redRequire = createRequire(installedRequire.resolve('@node-red/editor-api/package.json'));
    assert.equal(redRequire('body-parser/package.json').version, '1.20.9');
    assert.equal(redRequire('multer/package.json').version, '2.5.0');
    const registryRequire = createRequire(installedRequire.resolve('@node-red/registry/package.json'));
    assert.equal(registryRequire('npm/package.json').version, '11.21.2');
    assert.deepEqual(fs.readdirSync(bare).sort(), ['deadline-junit.xml', 'sequence-junit.xml']);
    process.stdout.write('Package verification: 3 orders, 1 later deadline case, 1 multi-port sequence, 1 declared caught-error and 1 later worker-exit case passed; 2 intentional deadline failures, 1 intentional sequence failure, 5 intentional error cases and 3 intentional worker-exit failures via installed executable; five structured count/field/port/missing-value records plus error-count and unexpected-error records retain source/instance attribution and unchanged spec/parsed JUnit text; structured terminal reason/phase/budget/received-signal/actual-child-status verified; resource-limit lower bounds, exact caps, UTF-8 bytes and setup/cleanup errors verified; exits 0/1/2 and SIGINT/SIGTERM 130/143 verified; active workers reaped, runtime directories removed; version, licenses, provenance and patch pins verified.\n');
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}
verifyPackage().catch(err => { console.error(err); process.exitCode = 1; });
