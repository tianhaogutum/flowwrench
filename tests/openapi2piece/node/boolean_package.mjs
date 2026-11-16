// Node half of tests/openapi2piece/test_boolean_package.py.
//
// The body below is test/boolean-package.test.js lines 54-110, unchanged: it loads
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
    const piece = require(join(out, 'dist/index.js')).piece_booleans;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const metadata = PieceMetadata.safeParse({ ...piece.metadata(), name: 'booleans', version: '0.1.0' });
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
    for (const [id, body, path] of [
      ['sendtrue', true, 'sendTrue'], ['sendfalse', false, 'sendFalse'], ['zero', true, 'zero'],
      ['typeonly', true, 'typeOnly'], ['typeonly', false, 'typeOnly'],
    ]) {
      const send = piece.getAction(id);
      assert.equal(send.props.body.type, 'CHECKBOX');
      assert.equal(send.props.body.required, true);
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, `/v2/${path}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
      assert.equal(typeof JSON.parse(requests.at(-1).body), 'boolean');
    }
    const nested = { value: true, values: [true, false], lookup: { a: false }, composed: false };
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
    for (const id of ['sendtrue', 'sendfalse', 'typeonly']) {
      for (const body of ['true', 'false', 0, 1, null, undefined, {}, []]) await rejects(id, body);
      await assert.rejects(piece.getAction(id).run({ propsValue: { base_url } }), /^Error: Invalid or missing input: body$/);
      negatives++;
    }
    for (const [id, body] of [['sendtrue', false], ['sendfalse', true], ['zero', false], ['overlap', true], ['overlap', false]]) {
      await rejects(id, body);
    }
    for (const body of [
      { ...nested, value: false }, { ...nested, values: ['false'] },
      { ...nested, lookup: { a: true } }, { ...nested, composed: 'false' },
    ]) await rejects('nested', body);
    await rejects('nested', { ...nested, values: Array(26000).fill(false) }, /^Error: Input validation limit exceeded$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedBooleanEnumExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: negatives, contracts: 'wrapper enum, zero complete matches, duplicate true/false matches, wrong/missing types, nested failures and shared budget add zero requests' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
