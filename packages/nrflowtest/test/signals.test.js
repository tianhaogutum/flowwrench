'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { XMLParser, XMLValidator } = require('fast-xml-parser');
const { checkSignalCleanup } = require('../scripts/check-signal-cleanup');
const root = path.resolve(__dirname, '..');
for (const signal of ['SIGINT', 'SIGTERM']) {
  for (const phase of ['observation', 'cleanup', 'startup']) {
    test(`parent ${signal} during ${phase} reaps worker, removes runtime and reports interruption`, { timeout: 20000 }, async t => {
      const cwd = fs.mkdtempSync(path.join(root, '.nrflowtest-signal-'));
      t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
      const result = await checkSignalCleanup({ executable: path.join(root, 'bin/nrflowtest.js'),
        flows: path.join(root, 'test/fixtures/deadline-isolation.json'), cwd, signal, phase,
        reporter: 'json', existingReport: phase === 'cleanup' });
      assert.equal(XMLValidator.validate(result.xml), true);
      const suite = new XMLParser({ ignoreAttributes: false, trimValues: false }).parse(result.xml).testsuites;
      assert.equal(suite['@_tests'], '2'); assert.equal(suite['@_failures'], '1');
      const cases = suite.testsuite.testcase;
      assert.equal(cases[0]['@_name'], 'completed before cancellation');
      assert.equal(cases[0].failure, undefined);
      assert.equal(cases[1]['@_name'], 'cancelled active case');
      assert.match(cases[1].failure['#text'], new RegExp(`interrupted by ${signal} during ${phase}`));
      if (result.report) assert.equal(cases[1].failure['#text'], result.report.cases[1].failures.join('\n'));
      assert.deepEqual(fs.readdirSync(cwd).sort(), ['signal-junit.xml', 'signals.yaml']);
    });
  }
}
