// windmill-dryrun expression sandbox (package data of flowwrench.windmill_dryrun).
//
// Adapted from the child program embedded in openflow-dryrun lib/expression.js
// (Copyright 2026 tianhaogutum, Apache License 2.0). The
// evaluation logic is unchanged; only this header was added.
//
// Python never runs this file by path: it reads it and passes its source to
// `node --permission|--experimental-permission --disable-proto=throw
// --max-old-space-size=64 -e <source>`, in a fresh process per expression,
// with the environment reduced to NODE_NO_WARNINGS=1 and TZ=UTC.
//
// Protocol (one request per process):
//   stdin  (UTF-8): {"expr": string, "scope": {flow_input, results,
//                    previous_result, result, error}, "ids": [module ids],
//                    "timeout": milliseconds}
//   stdout (UTF-8): {"ok": true, "value": <finite JSON>} or
//                   {"ok": false, "message": <at most 2000 characters>}
//
// No host functions/objects are injected into the VM. All input is parsed
// INSIDE the context; the expression result is serialized inside the context
// under the same VM timeout, so hostile toJSON/getters never reach the host.
const vm = require('node:vm');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', data => input += data);
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(input);
    const context = vm.createContext(Object.create(null), {
      codeGeneration: { strings: false, wasm: false }, microtaskMode: 'afterEvaluate'
    });
    const dataLiteral = JSON.stringify(JSON.stringify(request.scope));
    const idsLiteral = JSON.stringify(JSON.stringify(request.ids));
    const setup = '\"use strict\"; const {results, flow_input, previous_result, result, error} = ((raw, ids) => {' +
      'const scope = JSON.parse(raw), knownIds = new Set(JSON.parse(ids));' +
      'return { ...scope, results: new Proxy(scope.results, { get(target, key) {' +
      'if (typeof key === \"string\" && !knownIds.has(key)) throw new Error(\"Unknown result id: \" + key);' +
      'return Object.hasOwn(target, key) ? target[key] : undefined;' +
      '}, has(target,key) { if (typeof key === \"string\" && !knownIds.has(key)) throw new Error(\"Unknown result id: \" + key); return Object.hasOwn(target,key); }}) };' +
      '})(' + dataLiteral + ',' + idsLiteral + ');';
    // Serialize within the timeout too: hostile toJSON/getters never reach host.
    const code = setup + '\nconst answer = (\n' + request.expr + '\n);' +
      '\nif (answer && typeof answer.then === "function") throw new Error("Async expressions are unsupported");' +
      '\nconst encoded = JSON.stringify(answer, (key,value) => {' +
      'if (typeof value === "undefined" || typeof value === "function" || typeof value === "symbol" || (typeof value === "number" && !Number.isFinite(value))) throw new Error("Expression must return finite JSON values"); return value; });' +
      '\nif (encoded.length > 1048576) throw new Error("Expression output exceeds 1 MiB"); encoded;';
    const value = new vm.Script(code).runInContext(context, { timeout: request.timeout });
    process.stdout.write(JSON.stringify({ok: true, value: JSON.parse(value)}));
  } catch (e) { process.stdout.write(JSON.stringify({ok:false, message:String(e.message).slice(0,2000)})); }
});
