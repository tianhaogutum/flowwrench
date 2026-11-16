'use strict';
const { LoadError } = require('./errors');

// Node-RED rewires a subflow's internal outputs directly to its consumers. The
// instance's send() is its INPUT boundary, so it cannot represent output events.
// Resolve each exposed output to the concrete cloned source nodes instead.
function subflowMap(flows) {
  const byId = new Map(flows.map(n => [n.id, n]));
  const instances = new Set(), boundaries = {}, errorInstances = {};
  function definition(instance) { return byId.get(instance.type.slice(8)); }
  function outputSources(instance, runtimeId, port, stack) {
    const def = definition(instance);
    if (stack.includes(def.id) || stack.length > 32) throw new LoadError(`recursive/overdeep subflow ${def.id} is unsupported`);
    const wires = def.out?.[port]?.wires || [];
    return wires.flatMap(w => {
      // A subflow can expose its input directly as an output.
      if (w.id === def.id) return [{ node: runtimeId, output: w.port ?? 0 }];
      const source = byId.get(w.id), sourceId = `${runtimeId}-${w.id}`;
      if (source?.type.startsWith('subflow:')) return outputSources(source, sourceId, w.port ?? 0, [...stack, def.id]);
      return [{ node: sourceId, output: w.port ?? 0 }];
    });
  }
  function visit(instance, runtimeId, parents, stack) {
    const def = definition(instance);
    if (stack.includes(def.id) || stack.length >= 32) throw new LoadError(`recursive/overdeep subflow ${def.id} is unsupported`);
    instances.add(runtimeId);
    for (const [port] of (def.out || []).entries()) {
      for (const source of outputSources(instance, runtimeId, port, [])) {
        const mapping = { sourceOutput: source.output, instance: runtimeId, output: port };
        const entries = boundaries[source.node] ||= [];
        if (!entries.some(e => e.sourceOutput === mapping.sourceOutput && e.instance === runtimeId && e.output === port)) entries.push(mapping);
      }
    }
    for (const n of flows.filter(n => n.z === def.id)) {
      const cloneId = `${runtimeId}-${n.id}`;
      errorInstances[cloneId] = [runtimeId, ...parents];
      if (n.type.startsWith('subflow:')) visit(n, cloneId, [runtimeId, ...parents], [...stack, def.id]);
    }
  }
  for (const n of flows) if (n.type.startsWith('subflow:') && byId.get(n.z)?.type !== 'subflow') visit(n, n.id, [], []);
  return { instances: [...instances], boundaries, errorInstances };
}
module.exports = { subflowMap };
