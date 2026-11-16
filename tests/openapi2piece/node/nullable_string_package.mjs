// Node half of tests/openapi2piece/test_nullable_string_package.py.
//
// The body below is test/nullable-string-package.test.js lines 61-122, unchanged: it loads
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
    const piece = require(join(out, 'dist/index.js')).piece_strings;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const metadata = PieceMetadata.safeParse({ ...piece.metadata(), name: 'strings', version: '0.1.0' });
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
      ['send31', 'abc', 'send31'], ['send31', null, 'send31'],
      ['send30', 'abc', 'send30'], ['send30', null, 'send30'],
      ['empty', '', 'empty'], ['empty', null, 'empty'],
      ['unicode', '😀😀', 'unicode'], ['unicode', 'e\u0301', 'unicode'], ['unicode', null, 'unicode'],
      ['independent', 'bb', 'independent'], ['independent', 'ccc', 'independent'],
      ['composed', null, 'composed'], ['overlap', 'abc', 'overlap'],
    ]) {
      const send = piece.getAction(id);
      assert.equal(send.props.body.type, 'SHORT_TEXT');
      assert.equal(send.props.body.required, true);
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, `/v2/${path}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
    }
    const nested = { value: null, values: ['abc', null], lookup: { a: null, b: 'abc' }, composed: null };
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
    for (const id of ['send31', 'send30', 'empty']) {
      for (const body of ['other', 'null', 0, true, undefined, {}, []]) await rejects(id, body);
      await assert.rejects(piece.getAction(id).run({ propsValue: { base_url } }), /^Error: Invalid or missing input: body$/);
      negatives++;
    }
    for (const [id, body] of [
      ['send31', 'ab'], ['send30', 'ab'], ['send31', ''], ['empty', 'a'], ['zero', null],
      ['overlap', null], ['enumexcludes', null], ['composed', 'ab'],
      ['independent', 'a'], ['independent', 'dddd'], ['unicode', '😀'], ['unicode', '😀😀😀😀'],
    ]) await rejects(id, body);
    for (const body of [
      { ...nested, value: 'ab' }, { ...nested, values: ['other'] },
      { ...nested, lookup: { a: 'ab' } }, { ...nested, composed: 'ab' },
    ]) await rejects('nested', body);
    await rejects('nested', { ...nested, values: Array(26000).fill(null) }, /^Error: Input validation limit exceeded$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedNullableStringExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: negatives, contracts: 'wrapper enum, zero complete null matches, duplicate nullable/null matches, enum excludes null, wrapper/branch length, Unicode, required empty, wrong/missing types, nested failures and shared budget add zero requests' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
