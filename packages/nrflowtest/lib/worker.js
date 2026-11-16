'use strict';
const { performance } = require('node:perf_hooks');
const { EventEmitter } = require('node:events');
const { evaluateAssertions } = require('./assertions');

// Defense in depth against accidental sockets. This is not a security sandbox for hostile JS.
function blockNetwork() {
  const deny = function () { throw new Error('nrflowtest: network access/listeners are disabled; use a stub'); };
  const net = require('node:net');
  net.Socket.prototype.connect = deny;
  net.Server.prototype.listen = deny;
  const dgram = require('node:dgram');
  dgram.Socket.prototype.bind = deny;
  dgram.Socket.prototype.send = deny;
  require('node:tls').connect = deny;
  for (const name of ['lookup', 'resolve', 'resolve4', 'resolve6', 'reverse']) {
    require('node:dns')[name] = deny;
    require('node:dns').promises[name] = deny;
  }
}
function messageOf(err) { return typeof err?.message === 'string' ? err.message : typeof err === 'string' ? err : require('node:util').inspect(err); }
function reportPhase(phase) {
  if (process.connected) process.send({ type: 'phase', phase }, () => {});
}
async function execute({ testCase, prepared, userDir }) {
  blockNetwork();
  // The helper's constructor searches cwd for package.json. Use our installed package,
  // including when the CLI is invoked from a directory with no package.json.
  process.chdir(require('node:path').join(__dirname, '..'));
  const helper = require('node-red-node-test-helper');
  process.chdir(userDir);
  helper.init(require.resolve('node-red'));
  const RED = require('node-red');
  helper.settings({
    userDir, logging: { console: { level: 'off' } }, contextStorage: { default: { module: 'memory' } },
    functionExternalModules: false, functionGlobalContext: {}, functionTimeout: 1,
    externalModules: { autoInstall: false, modules: { allowInstall: false }, palette: { allowInstall: false } },
    credentialSecret: false, httpAdminRoot: false, httpNodeRoot: false,
  });
  const started = performance.now();
  let origin = started, armed = false, eventBytes = 0, loadFinished = false, overflow = false;
  const events = [], errors = [], resourceLimits = [];
  // Capture the first witnessed breach, not a total after capture has stopped.
  // More than one independent resource may overflow (errors still record after
  // message capture stops). Keep the existing shared observation-stop behavior.
  const exceed = (resource, limit, observedAtLeast, captured) => {
    overflow = true;
    if (!resourceLimits.some(r => r.resource === resource)) resourceLimits.push({ resource, limit, captured, observedAtLeast });
  };
  const subflowInstances = new Set(prepared.subflows.instances);
  const watched = new Set(testCase.expect.map(e => `${e.node}:${e.event}`));
  const recordError = (node, err) => {
    if (errors.length < 100) {
      const message = messageOf(err);
      // String fidelity is separate from entry-count completeness. Measure the
      // normalized string before slicing, in JavaScript UTF-16 code units.
      errors.push({ node, instances: prepared.subflows.errorInstances[node] || [], message: message.slice(0, 8000),
        messageTruncated: message.length > 8000, messageOriginalLength: message.length });
    } else exceed('errors', 100, errors.length + 1, errors.length);
  };
  const uncaught = err => recordError(null, err);
  process.on('uncaughtException', uncaught);
  process.on('unhandledRejection', uncaught);
  function record(node, event, msg, output) {
    if (!armed || !watched.has(`${node}:${event}`)) return;
    if (overflow) return;
    if (events.length >= 10000) { exceed('observations', 10000, events.length + 1, events.length); return; }
    try {
      const text = JSON.stringify(msg);
      if (!text) throw new Error('message is not JSON serializable');
      const attemptedBytes = eventBytes + Buffer.byteLength(text);
      if (attemptedBytes > 8 * 1024 * 1024) { exceed('observationBytes', 8 * 1024 * 1024, attemptedBytes, eventBytes); return; }
      events.push({ node, event, output, at: performance.now() - origin, msg: JSON.parse(text) });
      eventBytes = attemptedBytes;
    } catch (err) { recordError(node, `Cannot observe non-JSON message: ${messageOf(err)}`); }
  }
  const proto = helper._NodePrototype;
  const originals = { updateWires: proto.updateWires, error: proto.error };
  proto.updateWires = function (...args) {
    originals.updateWires.apply(this, args);
    const send = this.send;
    this.send = function (messages, ...rest) {
      const outputs = Array.isArray(messages) ? messages : [messages];
      outputs.forEach((out, i) => {
        for (const msg of Array.isArray(out) ? out : [out]) if (msg !== null && msg !== undefined) {
          if (!subflowInstances.has(this.id)) record(this.id, 'output', msg, i);
          for (const boundary of prepared.subflows.boundaries[this.id] || []) {
            if (boundary.sourceOutput === i) record(boundary.instance, 'output', msg, boundary.output);
          }
        }
      });
      return send.call(this, messages, ...rest);
    };
  };
  proto.error = function (err, ...rest) {
    recordError(this.id, err);
    // Suppress the matching log event only; catch nodes still receive the original error.
    suppressLog++;
    try { return originals.error.call(this, err, ...rest); }
    finally { suppressLog--; }
  };
  let suppressLog = 0;
  const logHandler = new EventEmitter();
  logHandler.on('log', event => {
    if (!suppressLog && event.level <= RED.log.ERROR) recordError(event.id || null, event.msg);
  });
  RED.log.addHandler(logHandler);
  const hooks = require('@node-red/util').hooks;
  let deliveries = 0;
  hooks.add('onReceive.nrflowtest', e => {
    if (++deliveries > 100000) { exceed('deliveries', 100000, deliveries, 100000); return false; }
    record(e.destination.id, 'input', e.msg);
  });
  const timers = new Set();
  const schedule = (fn, delay) => {
    const timer = setTimeout(() => { timers.delete(timer); fn(); }, delay);
    timers.add(timer); return timer;
  };
  const registerStubs = red => {
    for (const replacement of prepared.replacements) {
      red.nodes.registerType(replacement.type, function (config) {
        red.nodes.createNode(this, config);
        this.on('input', (msg, send, done) => {
          const reply = () => {
            if (replacement.stub?.error) this.error(replacement.stub.error, msg);
            else if (replacement.stub?.outputs) send(structuredClone(replacement.stub.outputs));
            else if (replacement.passthrough) send(msg);
            done();
          };
          if (replacement.stub?.delay) schedule(reply, replacement.stub.delay);
          else reply();
        });
      });
    }
  };
  let result;
  try {
    await helper.load([...prepared.modules.map(m => require(`@node-red/nodes/core/${m}`)), registerStubs], prepared.flows);
    loadFinished = true;
    const targets = [...new Set([...testCase.inject, ...testCase.expect, ...testCase.expectError].map(x => x.node))];
    for (const id of targets) if (!helper.getNode(id)) throw new Error(`node ${id} is not active (disabled node/tab or invalid configuration)`);
    // Observe only after deployment, so setup emits do not race the first injection.
    origin = performance.now(); armed = true;
    reportPhase('observation');
    for (const injection of testCase.inject) {
      const run = () => helper.getNode(injection.node).receive(structuredClone(injection.msg));
      if (injection.after) schedule(run, injection.after); else run();
    }
    await new Promise(resolve => setTimeout(resolve, testCase.timeout));
    armed = false;
    result = { status: 'completed' };
  } catch (err) {
    result = { status: 'load-error', message: messageOf(err) };
  } finally {
    armed = false;
    for (const timer of timers) clearTimeout(timer);
    reportPhase('cleanup');
    try { await helper.unload(); }
    catch (err) { recordError(null, `Runtime cleanup failed: ${messageOf(err)}`); }
    hooks.remove('*.nrflowtest');
    proto.updateWires = originals.updateWires;
    proto.error = originals.error;
    RED.log.removeHandler(logHandler);
    process.off('uncaughtException', uncaught);
    process.off('unhandledRejection', uncaught);
  }
  if (result.status === 'completed') {
    const errorLimit = resourceLimits.find(r => r.resource === 'errors');
    // Completeness describes retained entries, not the separate message-string cap.
    const errorCapture = { limit: 100, captured: errors.length,
      observedAtLeast: errorLimit?.observedAtLeast ?? errors.length, complete: !errorLimit };
    const { failures, mismatches, errorExpectations } = evaluateAssertions(testCase, events, errors, errorCapture.complete);
    if (overflow) failures.unshift('Observation limit exceeded (10000 observations / 100000 deliveries / 8 MiB / 100 errors)');
    result = { status: failures.length ? 'failed' : 'passed', failures, mismatches, errorExpectations, errors, errorCapture, resourceLimits, observedMessages: events.length };
  } else if (!loadFinished && errors.length) {
    result.message += `; ${errors.map(e => e.message).join('; ')}`;
  }
  // No server is ever started. A listener would indicate a violation, including in future integrations.
  const listeners = process._getActiveHandles().filter(h => h.constructor?.name === 'Server' && h.listening);
  if (listeners.length) return { status: 'failed', failures: ['Runtime leaked a listening server'] };
  return { ...result, durationMs: performance.now() - started };
}
// Fallback for IPC loss when the event loop can run. The parent handles signal
// cancellation itself, including workers blocked in flow code.
process.once('disconnect', () => process.exit(2));
process.once('message', async payload => {
  let result;
  try { result = await execute(payload); }
  catch (err) { result = { status: 'load-error', message: messageOf(err) }; }
  // Wait for IPC delivery, then exit explicitly: no flow timers or handles survive a case.
  if (process.connected) process.send({ type: 'result', result }, () => process.exit(0));
  else process.exit(2);
});
