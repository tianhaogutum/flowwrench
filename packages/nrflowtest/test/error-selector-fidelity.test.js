'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { evaluateAssertions } = require('../lib/assertions');
const { checkErrorSelectorFidelity } = require('../scripts/check-error-selector-fidelity');
const root = path.resolve(__dirname, '..');

test('selector-scoped error string fidelity distinguishes candidates, consumption and entry loss in JSON', { timeout: 30000 }, t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-selector-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkErrorSelectorFidelity({ executable: path.join(root, 'bin/nrflowtest.js'), fixtures: path.join(root, 'test/fixtures'),
    cwd, logDir: process.env.NRFLOWTEST_SELECTOR_LOG_DIR });
});

test('selector fidelity handles unavailable metadata without inferring discarded strings', () => {
  const c = { expectError: [{ node: 'n', message: 'absent', count: 1 }], expect: [] };
  const fidelity = errors => evaluateAssertions(c, [], errors).mismatches[0].messageFidelity;
  const unknown = { node: 'n', message: 'x'.repeat(8000), messageOriginalLength: 9000 };
  const known = messageTruncated => ({ node: 'inner', instances: ['n'], message: 'x', messageTruncated });
  assert.deepEqual(fidelity([]), { complete: true, truncatedErrorIndexes: [] });
  assert.deepEqual(fidelity([unknown]), { complete: null, truncatedErrorIndexes: [] });
  assert.deepEqual(fidelity([known(false), unknown]), { complete: null, truncatedErrorIndexes: [] });
  assert.deepEqual(fidelity([unknown, known(true)]), { complete: false, truncatedErrorIndexes: [1] });
  assert.deepEqual(fidelity([{ ...unknown, node: 'unrelated' }, known(false)]), { complete: true, truncatedErrorIndexes: [] });
});
