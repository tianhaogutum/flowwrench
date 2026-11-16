// Node half of tests/openapi2piece/test_array_package.py.
//
// The body below is test/array-package.test.js lines 54-115, unchanged: it loads
// the piece that the Python CLI generated and pytest compiled with `npx tsc`,
// validates real framework metadata and exercises it over local HTTP.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createRequire as createModuleRequire } from 'node:module';
import { join } from 'node:path';

const out = process.env.OPENAPI2PIECE_OUT;
// Resolve the pinned packages from the generated output, like the original
// resolved them from the repository root.
const createRequire = () => createModuleRequire(join(out, 'package.json'));
let server;
try {
    const require = createRequire(import.meta.url);
    const piece = require(join(out, 'dist/index.js')).piece_arrays;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const metadata = PieceMetadata.safeParse({ ...piece.metadata(), name: 'arrays', version: '0.1.0' });
    assert.equal(metadata.success, true, metadata.error?.message);
    const send = piece.getAction('sendunique');
    assert.equal(send.props.body.type, 'ARRAY');
    assert.deepEqual(send.props.body.properties, {});
    assert.equal(send.props.body.required, true);

    const requests = [];
    server = createServer(async (req, res) => {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      requests.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') });
      res.setHeader('Content-Type', 'application/json');
      res.end('{"ok":true}');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base_url = `http://127.0.0.1:${server.address().port}/v2`;
    for (const body of [[0, 1], [2, 3]]) {
      const snapshot = structuredClone(body);
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, '/v2/sendunique');
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
      assert.deepEqual(body, snapshot);
    }
    const jsonValues = piece.getAction('jsonvalues');
    for (const body of [
      [false, 0, true, 1, '1', null],
      [{ a: [1, 2], b: false }, { b: false, a: [2, 1] }, [1, 2], [2, 1]],
    ]) {
      const before = JSON.stringify(body);
      await jsonValues.run({ propsValue: { base_url, body } });
      assert.equal(requests.at(-1).url, '/v2/jsonvalues');
      assert.equal(requests.at(-1).body, before);
      assert.equal(JSON.stringify(body), before);
    }
    await piece.getAction('allowduplicates').run({ propsValue: { base_url, body: [0, 0] } });
    assert.equal(requests.at(-1).body, '[0,0]');
    await piece.getAction('branchunique').run({ propsValue: { base_url, body: [2, 2] } });
    assert.equal(requests.at(-1).body, '[2,2]');
    const nested = { value: [0, 1], values: [[2, 3]], lookup: { a: [0, 1] }, composed: [2, 3] };
    await piece.getAction('nested').run({ propsValue: { base_url, body: nested } });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.deepEqual(JSON.parse(requests.at(-1).body), nested);
    const count = requests.length;
    for (const body of [[0, 0], [2, 2], [0, 2], [2], [-1], [], ['1'], null, undefined]) {
      await assert.rejects(send.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    for (const body of [[true, true], ['x', 'x'], [null, null], [{ a: 1, b: [2] }, { b: [2], a: 1 }], [[1, 2], [1, 2]]]) {
      await assert.rejects(jsonValues.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    await assert.rejects(piece.getAction('overlap').run({ propsValue: { base_url, body: [0, 1] } }), /^Error: Invalid or missing input: body$/);
    await assert.rejects(piece.getAction('branchunique').run({ propsValue: { base_url, body: [0, 0] } }), /^Error: Invalid or missing input: body$/);
    await assert.rejects(piece.getAction('nested').run({ propsValue: { base_url, body: { ...nested, values: [[2, 2]] } } }),
      /^Error: Invalid or missing input: body$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedUniqueArrayExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: 'duplicate scalars, reordered objects and nested arrays; wrapper bounds/items, zero/multiple matches and branch uniqueness; no HTTP' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
