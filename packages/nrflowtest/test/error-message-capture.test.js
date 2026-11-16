'use strict';
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { checkErrorMessageCapture } = require('../scripts/check-error-message-capture');
const root = path.resolve(__dirname, '..');

test('error message capture reports code-unit truncation separately from entry completeness', { timeout: 30000 }, t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-error-strings-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkErrorMessageCapture({ executable: path.join(root, 'bin/nrflowtest.js'),
    fixtures: path.join(root, 'test/fixtures'), cwd, logDir: process.env.NRFLOWTEST_STRING_LOG_DIR });
});
