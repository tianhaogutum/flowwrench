'use strict';
// XML 1.0 forbids most control characters, even inside escaped text.
function xml(value) {
  return String(value).replace(/[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\u{10000}-\u{10ffff}]/gu, '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}
function junit(report) {
  const lines = ['<?xml version="1.0" encoding="UTF-8"?>',
    `<testsuites tests="${report.cases.length}" failures="${report.failed}" errors="0" time="${(report.durationMs / 1000).toFixed(3)}">`,
    `<testsuite name="nrflowtest" tests="${report.cases.length}" failures="${report.failed}" errors="0" time="${(report.durationMs / 1000).toFixed(3)}">`];
  for (const c of report.cases) {
    lines.push(`  <testcase classname="${xml(c.file)}" name="${xml(c.name)}" time="${((c.durationMs || 0) / 1000).toFixed(3)}">`);
    if (c.status !== 'passed') lines.push(`    <failure message="${xml(c.failures?.[0] || 'Case failed')}">${xml((c.failures || []).join('\n'))}</failure>`);
    lines.push('  </testcase>');
  }
  if (report.warnings.length) lines.push(`  <system-err>${xml(report.warnings.join('\n'))}</system-err>`);
  lines.push('</testsuite>', '</testsuites>');
  return lines.join('\n') + '\n';
}
function spec(report) {
  if (report.dryRun) return `Validated ${report.planned.length} case(s); dry-run: no runtime started.\n`;
  return report.cases.map(c => `${c.status === 'passed' ? 'PASS' : 'FAIL'} ${c.file} :: ${c.name}\n${(c.failures || []).map(f => `  ${f}\n`).join('')}`).join('') + `${report.passed} passed, ${report.failed} failed\n`;
}
module.exports = { junit, spec, xml };
