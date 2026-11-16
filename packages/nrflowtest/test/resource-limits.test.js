'use strict';
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { checkResourceLimits } = require('../scripts/check-resource-limits');
const root = path.resolve(__dirname, '..');
test('structured resource limits distinguish retained captures from witnessed lower bounds, including exact caps and setup/cleanup errors', { timeout: 60000 }, t => {
  const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-limits-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  checkResourceLimits({ executable: path.join(root, 'bin/nrflowtest.js'), fixtures: path.join(root, 'test/fixtures'), cwd });
});
