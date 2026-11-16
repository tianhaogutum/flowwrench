'use strict';
const { LoadError } = require('./errors');
const { subflowMap } = require('./subflows');
// Explicit allowlist: never discover/install nodes or load a user's settings.js.
const MODULES = {
  function: 'function/10-function.js', switch: 'function/10-switch.js', change: 'function/15-change.js',
  range: 'function/16-range.js', template: 'function/80-template.js', delay: 'function/89-delay.js',
  trigger: 'function/89-trigger.js', rbe: 'function/rbe.js', json: 'parsers/70-JSON.js',
  csv: 'parsers/70-CSV.js', split: 'sequence/17-split.js', join: 'sequence/17-split.js',
  sort: 'sequence/18-sort.js', batch: 'sequence/19-batch.js',
  'link in': 'common/60-link.js', 'link out': 'common/60-link.js', 'link call': 'common/60-link.js',
  catch: 'common/25-catch.js', status: 'common/25-status.js', complete: 'common/24-complete.js',
  junction: 'common/05-junction.js',
};
const STRUCTURAL = new Set(['tab', 'group', 'subflow', 'comment', 'global-config']);
function prepare(flows, testCase, options = {}) {
  const warnings = [], modules = new Set(), replacements = [];
  const stubs = new Map(testCase.stubs.map(s => [s.node, s]));
  const result = structuredClone(flows);
  for (const [i, n] of result.entries()) {
    if (n.type === 'comment') { modules.add('common/90-comment.js'); continue; }
    if (n.type === 'global-config') {
      if (n.modules && Object.keys(n.modules).length) throw new LoadError('global-config module declarations are unsupported; remove them from the synthetic export');
      modules.add('common/91-global-config.js'); continue;
    }
    if (STRUCTURAL.has(n.type)) continue;
    if (n.type.startsWith('subflow:')) {
      if (stubs.has(n.id)) throw new LoadError(`stubbing a subflow instance (${n.id}) is unsupported; stub its I/O nodes or test its boundary`);
      continue;
    }
    let stub = stubs.get(n.id);
    if (!stub && Object.hasOwn(MODULES, n.type)) {
      if (n.type === 'function') {
        if (!options.allowFunction) throw new LoadError(`function node ${n.id} requires --allow-function (trusted flow JavaScript only), or an explicit stub`);
        if (n.libs?.length) throw new LoadError(`function ${n.id}: external modules are not supported; stub this node`);
        // Node-RED expresses this timeout in seconds. The parent also enforces a hard deadline.
        n.timeout = Math.min(Number(n.timeout) || 1, 1);
      }
      modules.add(MODULES[n.type]);
      continue;
    }
    if (!stub && !['inject', 'debug'].includes(n.type)) {
      if (options.strictTypes) throw new LoadError(`unsupported node type "${n.type}" at ${n.id}; provide an explicit stub or omit --strict-types`);
      warnings.push(`Auto-stubbed ${n.id} (${n.type}): drops input; no I/O or config side effects. Add an explicit stub to model a response.`);
    }
    const originalType = n.type;
    const type = `nrflowtest-stub-${i}`;
    replacements.push({ type, originalType, stub: stub || null, passthrough: !stub && originalType === 'inject' });
    n.type = type;
    // Avoid inherited node constructor settings triggering work. Stubs only consume config id/name/wires.
  }
  return { subflows: subflowMap(result), flows: result, modules: [...modules], replacements, warnings };
}
module.exports = { prepare, MODULES };
