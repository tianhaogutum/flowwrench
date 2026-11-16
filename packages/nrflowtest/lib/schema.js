'use strict';
const fs = require('node:fs');
const YAML = require('yaml');
const { globIterateSync } = require('glob');
const { LoadError } = require('./errors');
const { pointerTokens } = require('./pointer');

const own = (o, k) => Object.hasOwn(o, k);
const object = o => o !== null && typeof o === 'object' && !Array.isArray(o);
function fail(where, message) { throw new LoadError(`${where}: ${message}`); }
function keys(o, allowed, where) {
  if (!object(o)) fail(where, 'expected an object');
  for (const k of Object.keys(o)) if (!allowed.includes(k)) fail(where, `unknown field ${k}`);
}
function data(value, where, depth = 0) {
  if (depth > 64) fail(where, 'maximum nesting depth is 64');
  if (value === null || ['string', 'boolean'].includes(typeof value)) return;
  if (typeof value === 'number' && Number.isFinite(value)) return;
  if (typeof value !== 'object') fail(where, 'only JSON-compatible data is supported');
  for (const [k, v] of Object.entries(value)) {
    if (['__proto__', 'constructor', 'prototype'].includes(k)) fail(where, `unsafe key ${k}`);
    data(v, where, depth + 1);
  }
}
function read(file, max) {
  try {
    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
    try {
      if (!fs.fstatSync(fd).isFile()) fail(file, 'expected a regular file');
      const buffer = Buffer.alloc(max + 1);
      let size = 0, n;
      while (size < buffer.length && (n = fs.readSync(fd, buffer, size, buffer.length - size, null))) size += n;
      if (size > max) fail(file, `file exceeds ${max} bytes`);
      return buffer.subarray(0, size).toString('utf8');
    } finally { fs.closeSync(fd); }
  } catch (err) { if (err instanceof LoadError) throw err; fail(file, err.message); }
}
function loadFlows(file) {
  let flows;
  try { flows = JSON.parse(read(file, 5 * 1024 * 1024)); }
  catch (err) { fail(file, `invalid flows JSON: ${err.message}`); }
  data(flows, file);
  if (!Array.isArray(flows) || !flows.length || flows.length > 10000) fail(file, 'expected 1–10000 nodes in a Node-RED export array');
  const ids = new Set();
  for (const n of flows) {
    if (!object(n) || typeof n.id !== 'string' || !n.id || typeof n.type !== 'string' || !n.type) fail(file, 'each node needs nonempty string id and type');
    if (ids.has(n.id)) fail(file, `duplicate node id ${n.id}`);
    if (own(Object.prototype, n.id) || n.id === 'global' || n.id.length > 256) fail(file, `reserved or overlong node id ${n.id.slice(0, 256)}`);
    ids.add(n.id);
    if (own(n, 'credentials')) fail(file, `inline credentials on ${n.id} are not supported; use a synthetic fixture`);
    if (own(n, 'wires') && (!Array.isArray(n.wires) || n.wires.some(w => !Array.isArray(w) || w.some(id => typeof id !== 'string')))) fail(file, `invalid wires on ${n.id}`);
    if (own(n, 'env') && (!Array.isArray(n.env) || n.env.some(e => !object(e) || typeof e.name !== 'string' || !['str', 'num', 'bool', 'json'].includes(e.type)))) fail(file, `node ${n.id}: only literal str/num/bool/json environment values are supported`);
  }
  for (const n of flows) {
    if (n.z && !flows.some(p => p.id === n.z && ['tab', 'subflow'].includes(p.type))) fail(file, `node ${n.id}: missing tab/subflow ${n.z}`);
    if (n.type.startsWith('subflow:') && !flows.some(p => p.id === n.type.slice(8) && p.type === 'subflow')) fail(file, `missing subflow definition for ${n.id}`);
    for (const id of (n.wires || []).flat()) if (!ids.has(id)) fail(file, `wire from ${n.id} points to missing node ${id}`);
    if (n.type === 'subflow') {
      for (const direction of ['in', 'out']) {
        if (!Array.isArray(n[direction])) fail(file, `subflow ${n.id} needs ${direction} ports`);
        if (direction === 'in' && n.in.length > 1) fail(file, `subflow ${n.id}: only one input port is supported`);
        for (const port of n[direction]) {
          if (!object(port) || !Array.isArray(port.wires)) fail(file, `invalid ${direction} port on ${n.id}`);
          for (const w of port.wires) {
            const source = object(w) && flows.find(s => s.id === w.id);
            if (!object(w) || !source || (source.z !== n.id && source.id !== n.id)) fail(file, `subflow ${n.id}: invalid port wire`);
            if (direction === 'out' && (!Number.isInteger(w.port) || w.port < 0)) fail(file, `subflow ${n.id}: output source needs a nonnegative port`);
          }
        }
      }
    }
  }
  return flows;
}
function resolveNode(selector, flows, where, stub = false) {
  let matches;
  if (typeof selector === 'string' && selector) {
    matches = flows.filter(n => n.id === selector);
    if (!matches.length) matches = flows.filter(n => n.name === selector);
  } else if (object(selector)) {
    keys(selector, ['id', 'name'], where);
    if (Object.keys(selector).length !== 1) fail(where, 'selector must contain exactly id or name');
    const [key] = Object.keys(selector);
    if (typeof selector[key] !== 'string' || !selector[key]) fail(where, 'selector must be a nonempty string');
    matches = flows.filter(n => n[key] === selector[key]);
  } else fail(where, 'node selector must be an id/name string or {id: ...}/{name: ...}');
  if (matches.length !== 1) fail(where, `${matches.length ? 'ambiguous' : 'unknown'} node ${JSON.stringify(selector)}`);
  const n = matches[0];
  if (['tab', 'group', 'subflow', 'comment', 'global-config'].includes(n.type)) fail(where, `${n.id} is structural, not a message node`);
  // Internal subflow ids are cloned per instance; callers must address the instance boundary.
  if (!stub && flows.some(p => p.id === n.z && p.type === 'subflow')) fail(where, `${n.id} is inside a subflow; select its instance instead`);
  if (!stub && (!own(n, 'x') || !own(n, 'y'))) fail(where, `${n.id} is a config node (no x/y); only stubs can address config nodes`);
  return n.id;
}
function integer(v, min, max, where) { if (!Number.isInteger(v) || v < min || v > max) fail(where, `expected integer ${min}–${max}`); return v; }
function list(v, where, required = false) {
  if (v === undefined && !required) return [];
  if (!Array.isArray(v) || (required && !v.length) || v.length > 1000) fail(where, 'expected an array (maximum 1000 items)');
  return v;
}
function normalizeCase(c, flows, file, index) {
  const where = `${file}, case ${index + 1}`;
  keys(c, ['name', 'timeout', 'hardTimeout', 'inject', 'stubs', 'expect', 'expectNoMessage', 'expectError'], where);
  if (typeof c.name !== 'string' || !c.name.trim()) fail(where, 'name is required');
  const timeout = integer(c.timeout ?? 200, 10, 30000, where + '.timeout');
  const hardBudget = own(c, 'hardTimeout') ? { hardTimeout: integer(c.hardTimeout, 10, 60000, where + '.hardTimeout') } : {};
  const selector = (v, loc, stub = false) => resolveNode(v, flows, `${where}.${loc}`, stub);
  const inject = list(Array.isArray(c.inject) ? c.inject : c.inject === undefined ? [] : [c.inject], where + '.inject').map((i, k) => {
    keys(i, ['node', 'msg', 'after'], where + '.inject');
    if (!object(i.msg)) fail(where, 'inject.msg must be an object');
    return { node: selector(i.node, `inject[${k}]`), msg: i.msg, after: integer(i.after ?? 0, 0, timeout - 1, where + '.inject.after') };
  });
  const stubs = list(c.stubs, where + '.stubs').map(s => {
    keys(s, ['node', 'outputs', 'error', 'delay'], where + '.stubs');
    if (own(s, 'error') && (typeof s.error !== 'string' || !s.error)) fail(where, 'stub.error must be a nonempty string');
    if (own(s, 'outputs') && own(s, 'error')) fail(where, 'a stub cannot have both outputs and error');
    if (own(s, 'outputs') && (!Array.isArray(s.outputs) || s.outputs.some(o => o !== null && !object(o) && !(Array.isArray(o) && o.every(object))))) fail(where, 'stub.outputs must be Node-RED per-output messages: [object|null|object[]]');
    return { ...s, node: selector(s.node, 'stub.node', true), delay: integer(s.delay ?? 0, 0, 30000, where + '.stub.delay') };
  });
  if (new Set(stubs.map(s => s.node)).size !== stubs.length) fail(where, 'duplicate stubs for a node');
  function messagePattern(pattern, loc) {
    if (own(pattern, 'msg') && !object(pattern.msg)) fail(loc, 'msg must be an object');
    if (own(pattern, 'fields')) {
      if (!object(pattern.fields) || !Object.keys(pattern.fields).length) fail(loc, 'fields must be a nonempty object of JSON Pointer/value pairs');
      for (const pointer of Object.keys(pattern.fields)) {
        if (!pointerTokens(pointer)) fail(loc, `invalid JSON Pointer ${JSON.stringify(pointer)}; use an empty string or /-prefixed path with ~0/~1 escapes`);
      }
    }
  }
  function observations(items, none) {
    return list(items, where).map((e, k) => {
      const loc = `${where}.${none ? 'expectNoMessage' : 'expect'}[${k}]`;
      keys(e, none ? ['node', 'event', 'output', 'timeout'] : ['node', 'event', 'output', 'timeout', 'count', 'msg', 'fields', 'sequence'], loc);
      const event = e.event ?? 'input';
      if (!['input', 'output'].includes(event)) fail(where, 'event must be input or output');
      if (own(e, 'output') && event !== 'output') fail(where, 'output index requires event: output');
      messagePattern(e, loc);
      if (own(e, 'sequence')) {
        for (const [i, entry] of list(e.sequence, loc + '.sequence', true).entries()) {
          const entryLoc = `${loc}.sequence[${i}]`;
          keys(entry, ['output', 'msg', 'fields'], entryLoc);
          messagePattern(entry, entryLoc);
          if (own(entry, 'output')) {
            if (event !== 'output') fail(entryLoc, 'output index requires event: output');
            integer(entry.output, 0, 999, entryLoc + '.output');
          }
        }
        if (own(e, 'count') && e.count !== e.sequence.length) fail(loc, 'count must equal sequence length');
      }
      return { ...e, assertion: none ? 'expectNoMessage' : 'expect', expectationIndex: k,
        node: selector(e.node, 'expect.node'), event,
        ...(own(e, 'output') ? { output: integer(e.output, 0, 999, where + '.output') } : {}),
        count: none ? 0 : integer(e.count ?? e.sequence?.length ?? 1, 1, 10000, where + '.count'),
        timeout: integer(e.timeout ?? timeout, 1, timeout, where + '.expect.timeout') };
    });
  }
  const expect = [...observations(c.expect, false), ...observations(c.expectNoMessage, true)];
  const expectError = list(c.expectError, where + '.expectError').map(e => {
    keys(e, ['node', 'message', 'count', 'source', 'messageMatch'], where + '.expectError');
    if (own(e, 'messageMatch') && !['exact', 'substring'].includes(e.messageMatch)) fail(where, 'expectError.messageMatch must be exact or substring');
    if (typeof e.message !== 'string' || !e.message) fail(where, `expectError.message must be a nonempty literal ${e.messageMatch === 'exact' ? 'message' : 'substring'}`);
    if (own(e, 'source') && (typeof e.source !== 'string' || !e.source)) fail(where, 'expectError.source must be a nonempty literal source id');
    return { node: selector(e.node, 'expectError.node'), ...(own(e, 'source') ? { source: e.source } : {}),
      message: e.message, ...(own(e, 'messageMatch') ? { messageMatch: e.messageMatch } : {}),
      count: integer(e.count ?? 1, 0, 10000, where + '.expectError.count') };
  });
  if (!expect.length && !expectError.length) fail(where, 'at least one expectation is required');
  return { name: c.name, file, timeout, ...hardBudget, inject, stubs, expect, expectError };
}
function loadTests(patterns, flows) {
  const found = new Set();
  if (patterns.length > 100) fail('tests', 'maximum 100 patterns');
  for (const pattern of patterns) {
    if (pattern.length > 2048) fail('tests', 'pattern length exceeds 2048');
    for (const file of globIterateSync(pattern, { nodir: true, follow: false, nobrace: true, noext: true, maxDepth: 32 })) {
      found.add(file);
      if (found.size > 1000) fail('tests', 'maximum 1000 files');
    }
  }
  const files = [...found].sort();
  if (!files.length) fail('tests', `no files matched ${patterns.join(', ')}`);
  const cases = [];
  let totalBytes = 0;
  for (const file of files) {
    let suite;
    try {
      const source = read(file, 1024 * 1024);
      totalBytes += Buffer.byteLength(source);
      if (totalBytes > 10 * 1024 * 1024) fail('tests', 'test files exceed 10 MiB in total');
      const document = YAML.parseDocument(source, { uniqueKeys: true, strict: true, stringKeys: true });
      if (document.errors.length || document.warnings.length) throw new Error([...document.errors, ...document.warnings].map(e => e.message).join('; '));
      suite = document.toJS({ maxAliasCount: 0 });
    }
    catch (err) { fail(file, `invalid YAML: ${err.message}`); }
    data(suite, file);
    keys(suite, ['version', 'cases'], file);
    if (suite.version !== 1) fail(file, 'version must be 1');
    const names = new Set();
    for (const [i, c] of list(suite.cases, file + '.cases', true).entries()) {
      const normalized = normalizeCase(c, flows, file, i);
      if (names.has(normalized.name)) fail(file, `duplicate case name ${normalized.name}`);
      names.add(normalized.name); cases.push(normalized);
      if (cases.length > 1000) fail('tests', 'maximum 1000 cases');
    }
  }
  return cases;
}
module.exports = { loadFlows, loadTests, resolveNode, normalizeCase };
