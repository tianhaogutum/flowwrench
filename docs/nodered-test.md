# flowwrench nodered-test

`flowwrench nodered-test` runs offline, headless tests against real Node-RED flow
exports. It needs the actual Node-RED runtime, so it is not part of the Python
package: it ships as the Node.js companion **nrflowtest** in
[`packages/nrflowtest/`](../packages/nrflowtest/). Running `flowwrench nodered-test`
prints these instructions and exits 2.

Requires Node.js 22.9 or newer and npm. From a checkout of the repository:

```sh
cd packages/nrflowtest && npm ci && node bin/nrflowtest.js run \
  --flows fixtures/orders/flows.json --tests 'fixtures/orders/*.test.yaml'
```

The bundled order-pipeline example prints three named passes followed by
`3 passed, 0 failed`. To test your own export:

```sh
node bin/nrflowtest.js run --flows flows.json --tests 'tests/*.yaml'
```

Run its own test suite with `npm test` in `packages/nrflowtest/`. The full
reference (case format, stubs, assertions, reporters, limits) is in
[packages/nrflowtest/README.md](../packages/nrflowtest/README.md).

nrflowtest is Apache-2.0; see [THIRD_PARTY.md](../THIRD_PARTY.md) for its
dependencies (Node-RED, node-red-node-test-helper, yaml, glob).
