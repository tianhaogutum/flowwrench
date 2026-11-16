'use strict';
const { inspect, isDeepStrictEqual } = require('node:util');
const { resolvePointer } = require('./pointer');
// Carry paths and raw values through the comparison. Never parse the human
// diagnostic: keys can themselves contain dots, slashes, tildes or brackets.
function partialMismatch(actual, expected, path = '$', pointer = '') {
  const mismatch = text => ({ text, pointer, expected: { value: expected }, actual: { value: actual } });
  if (expected !== null && typeof expected === 'object') {
    if (actual === null || typeof actual !== 'object' || Array.isArray(actual) !== Array.isArray(expected)) return mismatch(`${path}: expected ${show(expected)}, got ${show(actual)}`);
    if (Array.isArray(expected) && actual.length !== expected.length) return mismatch(`${path}.length: expected ${expected.length}, got ${actual.length}`);
    for (const key of Object.keys(expected)) {
      const childPointer = `${pointer}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`;
      if (!Object.hasOwn(actual, key)) return { text: `${path}.${key}: missing; expected ${show(expected[key])}`,
        pointer: childPointer, expected: { value: expected[key] }, actual: { missing: true } };
      const child = partialMismatch(actual[key], expected[key], `${path}.${key}`, childPointer);
      if (child) return child;
    }
    return null;
  }
  return Object.is(actual, expected) ? null : mismatch(`${path}: expected ${show(expected)}, got ${show(actual)}`);
}
function partialDiff(actual, expected, path = '$') { return partialMismatch(actual, expected, path)?.text ?? null; }
function show(v) { return inspect(v, { depth: 8, maxArrayLength: 20, maxStringLength: 1000, breakLength: 100 }).slice(0, 8000); }
function patternFailures(msg, pattern, location, context, failures, mismatches) {
  if (pattern.msg) {
    const mismatch = partialMismatch(msg, pattern.msg);
    if (mismatch) {
      const { text, ...values } = mismatch;
      failures.push(`Message mismatch at ${location}: ${text}`);
      mismatches.push({ ...context, kind: 'message', ...values });
    }
  }
  for (const [pointer, expected] of Object.entries(pattern.fields || {})) {
    const actual = resolvePointer(msg, pointer);
    if (!actual.found || !isDeepStrictEqual(actual.value, expected)) {
      failures.push(`Message mismatch at ${location}, JSON Pointer ${JSON.stringify(pointer)}: ${actual.found ? `expected ${show(expected)}, got ${show(actual.value)}` : `missing; expected ${show(expected)}`}`);
      mismatches.push({ ...context, kind: 'field', pointer, expected: { value: expected },
        actual: actual.found ? { value: actual.value } : { missing: true } });
    }
  }
}
function evaluateAssertions(testCase, events, errors, errorCaptureComplete = true) {
  const failures = [], mismatches = [], errorExpectations = [];
  const unmatched = new Set(errors.map((_, i) => i));
  const consumedBy = new Map();
  const errorContext = { sequenceIndex: null, messageIndex: null, event: 'error', output: null, pointer: null };
  for (const [expectationIndex, e] of testCase.expectError.entries()) {
    const hasSource = Object.hasOwn(e, 'source');
    const selectsError = i => (errors[i].node === e.node || errors[i].instances?.includes(e.node))
      && (!hasSource || errors[i].node === e.source);
    const matchesMessage = message => e.messageMatch === 'exact' ? message === e.message : message.includes(e.message);
    const candidates = [...unmatched].filter(selectsError);
    const matches = [], nonmatches = [];
    for (const i of candidates) (matchesMessage(errors[i].message) ? matches : nonmatches).push(i);
    // Include nonmatches: retained equality/containment and original-message
    // fidelity are independent, even for an exactly matching truncated prefix.
    // Consumed/unrelated entries cannot affect this declaration's fidelity.
    const truncatedErrorIndexes = candidates.filter(i => errors[i].messageTruncated === true);
    const complete = truncatedErrorIndexes.length ? false
      : candidates.some(i => typeof errors[i].messageTruncated !== 'boolean') ? null : true;
    // Report only witnessed prior consumers whose retained strings match this
    // selector. Scan capture order, independent of the order of consumption.
    const previouslyConsumed = errors.flatMap((error, errorIndex) =>
      consumedBy.has(errorIndex) && selectsError(errorIndex) && matchesMessage(error.message)
        ? [{ errorIndex, expectationIndex: consumedBy.get(errorIndex) }] : []);
    // One pre-consumption evaluation supplies both successful outcomes and
    // count mismatches. A pass describes retained counts, even after entry loss.
    const detail = { assertion: 'expectError', expectationIndex,
      node: e.node, ...(hasSource ? { source: e.source } : {}), errorMessage: e.message,
      ...(Object.hasOwn(e, 'messageMatch') ? { messageMatch: e.messageMatch } : {}),
      errorIndexes: matches, captureComplete: errorCaptureComplete,
      messageFidelity: { complete, truncatedErrorIndexes }, previouslyConsumed,
      expected: { value: e.count }, actual: { value: matches.length } };
    const passed = matches.length === e.count;
    errorExpectations.push({ ...detail, status: passed ? 'passed' : 'failed',
      candidateErrorIndexes: candidates, nonmatchingErrorIndexes: nonmatches });
    if (!passed) {
      failures.push(`Error expectation at node ${e.node}${hasSource ? `, source ${show(e.source)}` : ''}: expected ${e.count} ${e.messageMatch === 'exact' ? 'exactly matching' : 'containing'} ${show(e.message)}, got ${matches.length}`);
      mismatches.push({ ...errorContext, kind: 'errorCount', ...detail });
    }
    for (const i of matches) {
      unmatched.delete(i);
      consumedBy.set(i, expectationIndex);
    }
  }
  for (const i of unmatched) {
    failures.push(`Unexpected error at node ${errors[i].node || '<runtime>'}: ${errors[i].message}`);
    mismatches.push({ ...errorContext, kind: 'unexpectedError', assertion: null, expectationIndex: null,
      node: errors[i].node ?? null, errorIndex: i, expected: { missing: true }, actual: { value: errors[i] } });
  }
  // Surface real errors first, rather than obscuring them with consequent message timeouts.
  if (unmatched.size) return { failures, mismatches, errorExpectations };
  for (const e of testCase.expect) {
    const observed = events.filter(v => v.node === e.node && v.event === e.event && (e.output === undefined || v.output === e.output) && v.at <= e.timeout);
    const target = `node ${e.node} ${e.event}${e.output === undefined ? '' : ` output ${e.output}`}`;
    const context = { assertion: e.assertion, expectationIndex: e.expectationIndex,
      sequenceIndex: null, messageIndex: null, node: e.node, event: e.event,
      output: e.output ?? null, pointer: null };
    if (observed.length !== e.count) {
      failures.push(`${observed.length < e.count ? 'Timeout' : 'Count mismatch'} at ${target} after ${e.timeout}ms: expected ${e.count} message(s), got ${observed.length}`);
      mismatches.push({ ...context, kind: 'count', expected: { value: e.count }, actual: { value: observed.length } });
    }
    for (const [i, v] of observed.entries()) {
      const location = `${target}, message ${i + 1}`;
      const messageContext = { ...context, messageIndex: i, output: v.output ?? null };
      patternFailures(v.msg, e, location, messageContext, failures, mismatches);
      const entry = e.sequence?.[i];
      if (entry) {
        const sequenceContext = { ...messageContext, sequenceIndex: i };
        if (entry.output !== undefined && entry.output !== v.output) {
          failures.push(`Sequence mismatch at ${location}: expected output ${entry.output}, got ${v.output}`);
          mismatches.push({ ...sequenceContext, kind: 'output', expected: { value: entry.output }, actual: { value: v.output } });
        }
        patternFailures(v.msg, entry, location, sequenceContext, failures, mismatches);
      }
    }
  }
  return { failures, mismatches, errorExpectations };
}
// Retain the existing failures-only helper for callers/tests that use it.
function evaluate(testCase, events, errors) { return evaluateAssertions(testCase, events, errors).failures; }
module.exports = { partialDiff, evaluate, evaluateAssertions, show };
