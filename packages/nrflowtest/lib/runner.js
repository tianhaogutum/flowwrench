'use strict';
const path = require('node:path');
const fs = require('node:fs');
const { fork } = require('node:child_process');
const { performance } = require('node:perf_hooks');
const { prepare } = require('./prepare');
const { LoadError } = require('./errors');

function runWorker(testCase, prepared, userDir, { workerStartupTimeout = 10000, signal } = {}) {
  return new Promise(resolve => {
    const started = performance.now();
    const hardTimeout = testCase.hardTimeout ?? workerStartupTimeout + testCase.timeout + 2000;
    const termination = (reason, phase, exitCode = null, exitSignal = null) => ({ reason, phase,
      hardTimeout: reason === 'deadline' ? hardTimeout : null,
      signal: reason === 'interrupted' ? signal.reason : null, exitCode, exitSignal });
    const interrupted = (phase, detail = 'Child terminated.', code = null, exitSignal = null) => ({ status: 'failed', durationMs: performance.now() - started,
      termination: termination('interrupted', phase, code, exitSignal),
      failures: [`Run interrupted by ${signal.reason} during ${phase}. ${detail}`] });
    if (signal?.aborted) { resolve(interrupted('startup', 'Worker not started.')); return; }
    const child = fork(path.join(__dirname, 'worker.js'), [], {
      execArgv: ['--max-old-space-size=192'],
      // Do not pass host credentials, NODE_OPTIONS preload hooks or project environment to flows.
      env: { TZ: 'UTC', LANG: 'C.UTF-8' }, cwd: userDir,
      stdio: ['ignore', 'ignore', 'pipe', 'ipc'], serialization: 'json',
    });
    let result = null, stderr = '', deadlinePhase = null, phase = 'startup', loadError = null;
    let interruptedPhase = null;
    const cancel = () => {
      interruptedPhase = phase;
      clearTimeout(timer);
      // The worker may be blocked in trusted flow code and unable to handle IPC
      // disconnect or a graceful shutdown. The parent still waits for close.
      child.kill('SIGKILL');
    };
    child.stderr.on('data', b => { stderr = (stderr + b).slice(-8000); });
    // One absolute parent-owned deadline: phase messages never reset or extend it.
    const timer = setTimeout(() => { deadlinePhase = phase; child.kill('SIGKILL'); }, Math.max(0, Math.ceil(hardTimeout - (performance.now() - started))));
    child.on('message', value => {
      if (value?.type === 'phase' && ['observation', 'cleanup'].includes(value.phase)) phase = value.phase;
      else if (value?.type === 'result') result = value.result;
    });
    child.on('error', err => { loadError = `Cannot start worker: ${err.message}`; child.kill('SIGKILL'); });
    // Wait for process exit AND closed stdio before removing the runtime directory
    // or starting another case, including on startup/IPC failures.
    child.once('close', (code, exitSignal) => {
      clearTimeout(timer);
      // Remove only our listener: callers retain ownership of the AbortSignal.
      signal?.removeEventListener('abort', cancel);
      const durationMs = performance.now() - started;
      // Report the observed close status, never the signal we attempted to send.
      if (interruptedPhase) resolve(interrupted(interruptedPhase, 'Child terminated.', code, exitSignal));
      else if (deadlinePhase) resolve({ status: 'failed', durationMs, termination: termination('deadline', deadlinePhase, code, exitSignal),
        failures: [`Worker hard deadline exceeded after ${hardTimeout}ms during ${deadlinePhase}; possible infinite loop or stalled runtime. Child terminated.`] });
      else if (loadError) resolve({ status: 'load-error', message: loadError });
      else if (result && code === 0) resolve({ ...result, termination: null });
      else resolve({ status: 'failed', durationMs, termination: termination('workerExit', phase, code, exitSignal),
        failures: [`Worker exited unexpectedly (${exitSignal || code})${stderr ? `: ${stderr}` : ''}`] });
    });
    signal?.addEventListener('abort', cancel, { once: true });
    child.send({ testCase, prepared, userDir }, err => {
      if (err) { loadError = `Cannot send case to worker: ${err.message}`; child.kill('SIGKILL'); }
    });
  });
}
async function runSuite(flows, cases, options = {}) {
  // Preflight every case before any flow is executed.
  const plans = cases.map(c => prepare(flows, c, options));
  const started = performance.now();
  const warnings = [...new Set(plans.flatMap(p => p.warnings))];
  if (options.dryRun) return { dryRun: true, warnings, planned: cases.map(c => ({ file: c.file, name: c.name })), passed: 0, failed: 0, durationMs: 0, cases: [] };
  const results = [];
  for (const [i, testCase] of cases.entries()) {
    if (options.signal?.aborted) break;
    // Ephemeral runtime directory stays under the invocation directory and is always removed.
    const userDir = fs.mkdtempSync(path.resolve('.nrflowtest-'));
    let result;
    try { result = await runWorker(testCase, plans[i], userDir, options); }
    finally { fs.rmSync(userDir, { recursive: true, force: true }); }
    if (result.status === 'load-error') throw new LoadError(`${testCase.file} / ${testCase.name}: ${result.message}`);
    results.push({ file: testCase.file, name: testCase.name, mismatches: [], ...result });
  }
  return { ...(options.signal?.aborted ? { interrupted: options.signal.reason } : {}),
    warnings, cases: results, passed: results.filter(c => c.status === 'passed').length,
    failed: results.filter(c => c.status !== 'passed').length, durationMs: performance.now() - started };
}
module.exports = { runSuite, runWorker };
