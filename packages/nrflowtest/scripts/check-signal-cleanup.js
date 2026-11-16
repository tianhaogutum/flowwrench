'use strict';
// Linux integration check, shared by the source tests and installed-package check.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { setTimeout: delay } = require('node:timers/promises');

// Observe worker lifecycle without changing the runner or inheriting hooks into
// the worker. fd 3 belongs only to the CLI parent, separate from report stdout.
const probe = `
const fs = require('node:fs'), cp = require('node:child_process');
const emit = value => fs.writeSync(3, JSON.stringify(value) + '\\n');
const fork = cp.fork;
let index = 0;
cp.fork = (...args) => {
  const child = fork(...args), worker = ++index;
  emit({ worker, pid: child.pid, type: 'start' });
  child.on('message', message => {
    if (message?.type === 'phase') emit({ worker, pid: child.pid, ...message });
  });
  child.on('close', (code, signal) => emit({ worker, pid: child.pid, type: 'close', code, signal }));
  return child;
};
require(process.argv[1]);
`;
async function until(predicate, description, timeout = 10000) {
  const end = Date.now() + timeout;
  while (!predicate()) {
    assert.ok(Date.now() < end, `Timed out waiting for ${description}`);
    await delay(10);
  }
}
async function checkSignalCleanup({ executable, flows, cwd, signal, phase, reporter = 'junit', existingReport = false }) {
  const healthy = { name: 'completed before cancellation', timeout: 60, hardTimeout: 60000,
    inject: { node: 'function', msg: { payload: 'healthy' } }, expect: [{ node: 'sink', msg: { payload: 1 } }] };
  const hanging = { ...healthy, name: 'cancelled active case',
    timeout: phase === 'cleanup' ? 60 : 30000,
    inject: { node: 'function', msg: { payload: phase === 'cleanup' ? 'hang cleanup' : 'hang' } } };
  const tests = path.join(cwd, 'signals.yaml');
  fs.writeFileSync(tests, JSON.stringify({ version: 1, cases: [healthy, hanging, { ...healthy, name: 'must not start' }] }));
  const reportFile = path.join(cwd, 'signal-junit.xml');
  if (existingReport) fs.writeFileSync(reportFile, '<old-passing-report/>');
  const child = spawn(process.execPath, ['-e', probe, executable, 'run', '--flows', flows,
    '--tests', tests, '--allow-function', '--reporter', reporter, '--junit-file', 'signal-junit.xml'],
  { cwd, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] });
  let stdout = '', stderr = '', telemetry = '', closed = false, outcome, spawnError;
  const events = [];
  child.stdout.on('data', b => { stdout += b; });
  child.stderr.on('data', b => { stderr += b; });
  child.stdio[3].on('data', b => {
    telemetry += b;
    let end;
    while ((end = telemetry.indexOf('\n')) !== -1) {
      events.push(JSON.parse(telemetry.slice(0, end)));
      telemetry = telemetry.slice(end + 1);
    }
  });
  child.on('error', error => { spawnError = error; });
  child.on('close', (code, signal) => { closed = true; outcome = { code, signal }; });
  try {
    await until(() => events.some(e => e.worker === 2 && (phase === 'startup' ? e.type === 'start' : e.phase === phase)) || closed,
      `second worker ${phase}`);
    assert.equal(closed, false, `${spawnError || ''}\n${stderr}\n${stdout}`);
    const pid = events.find(e => e.worker === 2).pid;
    const runtimeDirs = () => fs.readdirSync(cwd).filter(f => f.startsWith('.nrflowtest-'));
    assert.equal(runtimeDirs().length, 1, 'active worker owns one runtime directory');
    // Let the scheduled infinite loop enter before sending a parent-only signal.
    if (phase === 'observation') await delay(100);
    assert.equal(child.kill(signal), true);
    // Repeated cancellation must not restore default signal exit before cleanup.
    child.kill(signal);
    await until(() => closed, 'CLI exit after cancellation', 5000);
    assert.deepEqual({ ...outcome, workerExists: fs.existsSync(`/proc/${pid}`),
      runtimeDirs: runtimeDirs(), workerReaped: events.some(e => e.worker === 2 && e.type === 'close'),
      startedWorkers: events.filter(e => e.type === 'start').length },
    { code: signal === 'SIGINT' ? 130 : 143, signal: null, workerExists: false,
      runtimeDirs: [], workerReaped: true, startedWorkers: 2 },
    `Cancellation must reap the worker and clean up before parent exit\n${stderr}`);
    assert.match(stderr, new RegExp(`interrupted by ${signal}`));
    const xml = fs.readFileSync(reportFile, 'utf8');
    let report;
    if (reporter === 'json') {
      report = JSON.parse(stdout);
      assert.equal(report.interrupted, signal);
      assert.equal(report.passed, 1); assert.equal(report.failed, 1);
      assert.deepEqual(report.cases.map(c => c.status), ['passed', 'failed']);
      assert.match(report.cases[1].failures[0], new RegExp(`interrupted by ${signal} during ${phase}`));
      assert.ok(report.cases[1].durationMs > 0);
      assert.equal(report.cases[0].termination, null);
      const close = events.find(e => e.worker === 2 && e.type === 'close');
      assert.deepEqual(report.cases[1].termination, { reason: 'interrupted', phase, hardTimeout: null,
        signal, exitCode: close.code, exitSignal: close.signal });
      assert.equal(close.code, null); assert.equal(close.signal, 'SIGKILL');
    } else assert.equal(stdout, xml, 'stdout and saved JUnit are identical');
    return { xml, report, stdout, stderr, events };
  } finally {
    if (!closed) child.kill('SIGKILL');
    // Also clean up workers left by the pre-fix regression, including an orphan
    // with a blocked event loop that cannot handle IPC disconnect.
    for (const e of events.filter(e => e.type === 'start' && !events.some(done => done.pid === e.pid && done.type === 'close'))) {
      try { process.kill(e.pid, 'SIGKILL'); } catch (err) { if (err.code !== 'ESRCH') throw err; }
    }
    await until(() => closed && events.filter(e => e.type === 'start').every(e => !fs.existsSync(`/proc/${e.pid}`)),
      'test process cleanup', 5000);
  }
}
module.exports = { checkSignalCleanup };
