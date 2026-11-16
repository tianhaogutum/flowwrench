'use strict';
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { checkResourceCapture } = require('../scripts/check-resource-limits');
const root = path.resolve(__dirname, '..');
test('resource capture reports discarded errors, captured matching counts and retained UTF-8 prefix bytes', { timeout: 20000 }, t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-capture-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkResourceCapture({ executable: path.join(root, 'bin/nrflowtest.js'), fixtures: path.join(root, 'test/fixtures'), cwd });
});
