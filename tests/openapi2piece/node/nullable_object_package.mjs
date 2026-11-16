// Node half of tests/openapi2piece/test_nullable_object_package.py.
//
// The body below is test/nullable-object-package.test.js lines 61-125, unchanged: it loads
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
    const piece = require(join(out, 'dist/index.js')).piece_objects;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const metadata = PieceMetadata.safeParse({ ...piece.metadata(), name: 'objects', version: '0.1.0' });
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
    const valid = { requestId: 'r1', email: 'a', extra: 1 };
    for (const [id, values] of [
      ['send31', [valid, null]], ['send30', [valid, null]], ['closed', [null]],
      ['composed', [valid, null]], ['overlap', [valid]], ['zero', [valid]],
    ]) for (const body of values) {
      const send = piece.getAction(id);
      assert.equal(send.props.body.type, 'OBJECT');
      assert.equal(send.props.body.required, true);
      const before = JSON.stringify(body);
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, `/v2/${id}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, before);
      assert.equal(JSON.stringify(body), before);
    }
    const nested = { wrapper: null, list: [null, valid], lookup: { a: null, b: valid } };
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
    const wrong = ['[0,1]', 'null', 'x', true, 0, [], undefined];
    for (const id of ['send31', 'send30']) {
      for (const body of [{ email: 'a' }, { requestId: 'r1' }, { requestId: '', email: 'a' }, { requestId: 'r1', email: '' }, {}, ...wrong]) {
        await rejects(id, body);
      }
    }
    // The closed branch rejects the wrapper requestId, so only null can match.
    for (const body of [valid, { email: 'a' }, { requestId: 'r1' }, {}]) await rejects('closed', body);
    for (const body of [{ email: 'a' }, { requestId: 'r1' }]) await rejects('composed', body);
    for (const body of [null, { requestId: 'r1' }]) await rejects('zero', body);
    for (const body of [null, { email: 'a' }]) await rejects('overlap', body);
    for (const body of [
      { ...nested, wrapper: { email: 'a' } }, { ...nested, list: [{ requestId: 'r1' }] },
      { ...nested, lookup: { a: { requestId: '', email: 'a' } } }, { ...nested, extra: true },
    ]) await rejects('nested', body);
    await rejects('nested', { ...nested, list: Array(26000).fill(null) }, /^Error: Input validation limit exceeded$/);
    let deep = { leaf: true };
    for (let i = 0; i < 41; i++) deep = { child: deep };
    await rejects('send31', { ...valid, extra: deep });
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedNullableObjectExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: negatives }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
