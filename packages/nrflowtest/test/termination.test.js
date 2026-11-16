'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { runWorker } = require('../lib/runner');
const { checkWorkerExits } = require('../scripts/check-worker-exits');
const root = path.resolve(__dirname, '..');

test('unexpected worker exits retain actual code/signal, last phase and unchanged parsed JUnit text', t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-exits-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkWorkerExits({ executable: path.join(root, 'bin/nrflowtest.js'),
    flows: path.join(root, 'test/fixtures/deadline-isolation.json'), cwd });
});

test('pre-aborted worker reports unknown child identity without starting a child', async () => {
  const controller = new AbortController();
  controller.abort('SIGINT');
  // An invalid cwd proves fork was never reached.
  const result = await runWorker({}, {}, '/nrflowtest-no-such-directory', { signal: controller.signal });
  assert.deepEqual(result.termination, { reason: 'interrupted', phase: 'startup', hardTimeout: null,
    signal: 'SIGINT', exitCode: null, exitSignal: null });
  assert.deepEqual(result.failures, ['Run interrupted by SIGINT during startup. Worker not started.']);
});
