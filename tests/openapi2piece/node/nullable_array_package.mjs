// Node half of tests/openapi2piece/test_nullable_array_package.py.
//
// The body below is test/nullable-array-package.test.js lines 61-132, unchanged: it loads
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
    for (const [id, values] of [
      ['send31', [[0, 1], null]], ['send30', [[0, 1], null]], ['empty', [[], null]],
      ['independent', [[0, 0], [1], null]], ['branchunique', [[0, 1], [2, 2], null]],
      ['composed', [[0, 1], null]], ['overlap', [[0, 1]]],
      ['jsonvalues', [null, [false, 0, true, 1, '1', null],
        [{ a: [1, 2], b: false }, { b: false, a: [2, 1] }, [1, 2], [2, 1]]]],
    ]) for (const body of values) {
      const send = piece.getAction(id);
      assert.equal(send.props.body.type, 'ARRAY');
      assert.deepEqual(send.props.body.properties, {});
      assert.equal(send.props.body.required, true);
      const before = JSON.stringify(body);
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, `/v2/${id}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, before);
      assert.equal(JSON.stringify(body), before);
    }
    const nested = { value: null, values: [[0, 1], null], lookup: { a: null, b: [0, 1] }, composed: null };
    const before = JSON.stringify(nested);
    await piece.getAction('nested').run({ propsValue: { base_url, body: nested } });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.equal(requests.at(-1).body, before);
    assert.equal(JSON.stringify(nested), before);
    const count = requests.length;
    let negatives = 0;
    const rejects = async (id, body, reason = /^Error: Invalid or missing input: body$/) => {
      await assert.rejects(piece.getAction(id).run({ propsValue: { base_url, body } }), reason);
      negatives++;
    };
    for (const id of ['send31', 'send30', 'empty', 'independent', 'branchunique', 'jsonvalues', 'composed']) {
      for (const body of ['[0,1]', 'null', true, false, 0, undefined, {}]) await rejects(id, body);
      await assert.rejects(piece.getAction(id).run({ propsValue: { base_url } }), /^Error: Invalid or missing input: body$/);
      negatives++;
    }
    for (const [id, values] of [
      ['send31', [[1], [0, 0], [-1, 0], [0, 2], [0, 1, 2, 3], [], ['0', '1']]],
      ['send30', [[1], [0, 0], [-1, 0], [0, 2]]], ['empty', [[0]]], ['zero', [null]],
      ['overlap', [null]], ['arrayoverlap', [[0, 1]]],
      ['independent', [[-1], [0], [1, 1], [0, 0, 0], [0, 0, 0, 0]]],
      ['branchunique', [[0, 0], [0, 2]]], ['composed', [[1], [0, 2]]],
      ['jsonvalues', [[true, true], ['x', 'x'], [null, null], [0, -0],
        [{ a: 1, b: [2] }, { b: [2], a: 1 }], [[1, 2], [1, 2]], [NaN], [Infinity], [undefined]]],
    ]) for (const body of values) await rejects(id, body);
    for (const body of [
      { ...nested, value: [1] }, { ...nested, values: [[0, 2]] },
      { ...nested, lookup: { a: [0, 0] } }, { ...nested, composed: [-1, 0] },
    ]) await rejects('nested', body);
    await rejects('nested', { ...nested, values: Array(26000).fill(null) }, /^Error: Input validation limit exceeded$/);
    let deep = { leaf: true };
    for (let i = 0; i < 41; i++) deep = { child: deep };
    await rejects('jsonvalues', [deep]);
    await rejects('jsonvalues', ['x'.repeat(100001)], /^Error: Input validation limit exceeded$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedNullableArrayExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: negatives, contracts: 'wrapper length/items/uniqueness, zero/duplicate complete null matches, complete branch items/bounds/uniqueness, structural equality, required empty, wrong/missing types, nested failures, depth and shared budget add zero requests' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
