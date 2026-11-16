'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { XMLParser, XMLValidator } = require('fast-xml-parser');

// Parent-only instrumentation substitutes three controlled, real child processes.
// The fourth worker runs Node-RED normally. No production worker override exists.
const probe = `
const cp = require('node:child_process'), fork = cp.fork;
const controlledWorker = process.argv.splice(2, 1)[0];
let index = 0;
cp.fork = (file, ...args) => fork(++index <= 3 ? controlledWorker : file, ...args);
require(process.argv[1]);
`;
const worker = `
process.once('message', ({ testCase }) => {
  const mode = testCase.inject[0].msg.payload;
  const finish = () => mode === 'signal' ? process.kill(process.pid, 'SIGTERM') : process.exit(mode === 'zero' ? 0 : 23);
  process.stderr.write('synthetic <worker> & exit');
  if (mode === 'zero') finish();
  else process.send({ type: 'phase', phase: mode === 'signal' ? 'cleanup' : 'observation' }, finish);
});
`;
function checkWorkerExits({ executable, flows, cwd }) {
  const workerFile = path.join(cwd, 'controlled-worker.cjs');
  const tests = path.join(cwd, 'exits.yaml');
  fs.writeFileSync(workerFile, worker);
  fs.writeFileSync(tests, JSON.stringify({ version: 1, cases: ['code', 'signal', 'zero', 'healthy'].map(mode => ({
    name: `${mode} worker`, timeout: 60, hardTimeout: 3000,
    inject: { node: 'function', msg: { payload: mode } }, expect: [{ node: 'sink', msg: { payload: 1 } }],
  })) }));
  const out = spawnSync(process.execPath, ['-e', probe, executable, workerFile, 'run', '--flows', flows,
    '--tests', tests, '--allow-function', '--reporter', 'json', '--junit-file', 'exits-junit.xml'],
  { cwd, encoding: 'utf8', timeout: 20000 });
  assert.equal(out.error, undefined, out.error?.message);
  assert.equal(out.status, 1, out.stderr + out.stdout);
  const report = JSON.parse(out.stdout);
  assert.equal(report.passed, 1); assert.equal(report.failed, 3);
  assert.deepEqual(report.cases.map(c => c.status), ['failed', 'failed', 'failed', 'passed']);
  assert.deepEqual(report.cases.map(c => c.termination), [
    { reason: 'workerExit', phase: 'observation', hardTimeout: null, signal: null, exitCode: 23, exitSignal: null },
    { reason: 'workerExit', phase: 'cleanup', hardTimeout: null, signal: null, exitCode: null, exitSignal: 'SIGTERM' },
    { reason: 'workerExit', phase: 'startup', hardTimeout: null, signal: null, exitCode: 0, exitSignal: null },
    null,
  ]);
  const failures = ['23', 'SIGTERM', '0'].map(code => [`Worker exited unexpectedly (${code}): synthetic <worker> & exit`]);
  assert.deepEqual(report.cases.map(c => c.failures), [...failures, []]);
  for (const c of report.cases) assert.deepEqual(c.mismatches, []);
  for (const c of report.cases.slice(0, 3)) assert.equal(Object.hasOwn(c, 'errorExpectations'), false);
  assert.deepEqual(report.cases[3].errorExpectations, []);
  const xml = fs.readFileSync(path.join(cwd, 'exits-junit.xml'), 'utf8');
  assert.equal(XMLValidator.validate(xml), true);
  const parsed = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(xml).testsuites;
  assert.equal(parsed['@_tests'], '4'); assert.equal(parsed['@_failures'], '3');
  assert.deepEqual(parsed.testsuite.testcase.slice(0, 3).map(c => c.failure['#text']), failures.map(f => f.join('\n')));
  assert.equal(parsed.testsuite.testcase[3].failure, undefined);
  assert.deepEqual(fs.readdirSync(cwd).sort(), ['controlled-worker.cjs', 'exits-junit.xml', 'exits.yaml'], 'runtime directories removed after worker close');
  return report;
}
module.exports = { checkWorkerExits };
