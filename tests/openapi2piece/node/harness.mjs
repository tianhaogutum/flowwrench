// Shared support for the Node halves of the Python test-suite.
//
// * Generator functions (parseSpec, normalizeSchema, renderPiece, writePiece)
//   are bridged to the Python port through bridge.py, so every generated file
//   and every normalized schema the original assertions inspect comes from
//   Python. JS `undefined` and nonfinite numbers survive the JSON hop.
// * `test`, `test.before` and `test.after` replace node:test with a small
//   sequential runner that records one result per test for pytest.
import { spawnSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { delimiter } from 'node:path';

const MARK = '$openapi2piece:js';

function replacer(key, value) {
  if (value === undefined) return { [MARK]: 'undefined' };
  if (typeof value === 'number' && !Number.isFinite(value)) return { [MARK]: String(value) };
  return value;
}

function reviver(key, value) {
  if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 1 && Object.hasOwn(value, MARK)) {
    const kind = value[MARK];
    return kind === 'undefined' ? undefined : Number(kind);
  }
  return value;
}

function call(op, payload) {
  const env = { ...process.env };
  env.PYTHONPATH = [process.env.OPENAPI2PIECE_SRC, process.env.PYTHONPATH].filter(Boolean).join(delimiter);
  const result = spawnSync(process.env.OPENAPI2PIECE_PYTHON, [process.env.OPENAPI2PIECE_BRIDGE, op], {
    input: JSON.stringify(payload, replacer), encoding: 'utf8', maxBuffer: 512 * 1024 * 1024, env,
  });
  if (result.status !== 0) throw new Error(`Python bridge ${op} failed (${result.status}): ${result.stderr}`);
  const response = JSON.parse(result.stdout, reviver);
  if (Object.hasOwn(response, 'error')) {
    // SpecError extends Error without renaming, so String(error) is "Error: <message>".
    const error = new Error(response.error);
    if (response.report) error.report = response.report;
    throw error;
  }
  return response.value;
}

export const parseSpec = text => call('parse', { text });
export const normalizeSchema = (input, label, depth, options) => call('normalize', { input, label, depth, options });
export async function renderPiece(spec, options) {
  const { files, model } = call('render', { spec, options });
  return { model, files: new Map(files) };
}
export async function writePiece(files, out, options) {
  call('write', { files: [...files], out, options });
}

// Same as test/helpers.js.
export const baseSpec = (overrides = {}) => ({ openapi: '3.1.0', info: { title: 'Fixture', version: '1' },
  servers: [{ url: 'https://api.example.test/v1' }], paths: { '/hello': { get: { operationId: 'hello', responses: { 200: { description: 'OK' } } } } }, ...overrides });

const registered = [];
const beforeHooks = [];
const afterHooks = [];

export function test(name, fn) {
  registered.push({ name, fn });
}
test.before = fn => { beforeHooks.push(fn); };
test.after = fn => { afterHooks.push(fn); };

const describe = error => (error && error.stack) ? `${error.stack}` : String(error);

export async function runRegisteredTests() {
  const results = {};
  let setupError;
  for (const hook of beforeHooks) {
    try { await hook(); } catch (error) { setupError = error; break; }
  }
  for (const { name, fn } of registered) {
    if (setupError) { results[name] = { ok: false, error: `before hook failed: ${describe(setupError)}` }; continue; }
    try {
      await fn();
      results[name] = { ok: true };
    } catch (error) {
      results[name] = { ok: false, error: describe(error) };
    }
    console.log(`${results[name].ok ? 'ok' : 'not ok'} - ${name}`);
  }
  for (const hook of afterHooks) {
    try { await hook(); } catch (error) { console.error(`after hook failed: ${describe(error)}`); }
  }
  writeFileSync(process.env.OPENAPI2PIECE_RESULTS, JSON.stringify(results));
}
