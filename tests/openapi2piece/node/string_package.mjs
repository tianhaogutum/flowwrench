// Node half of tests/openapi2piece/test_string_package.py.
//
// The body below is test/string-package.test.js lines 54-108, unchanged: it loads
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
    const send = piece.getAction('sendstring');
    assert.equal(send.props.body.type, 'SHORT_TEXT');
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
    for (const body of ['abc', 'xyz']) {
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, '/v2/string');
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
      assert.equal(typeof JSON.parse(requests.at(-1).body), 'string');
    }
    for (const body of ['😀😀', '😀😀😀', 'e\u0301', 'ok']) {
      await piece.getAction('unicode').run({ propsValue: { base_url, body } });
      assert.equal(requests.at(-1).url, '/v2/unicode');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
    }
    const nested = { value: 'abc', values: ['abc', 'xyz'], lookup: { a: 'xyz' }, composed: 'abc' };
    const before = JSON.stringify(nested);
    await piece.getAction('nested').run({ propsValue: { base_url, body: nested } });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.equal(requests.at(-1).body, before);
    assert.equal(JSON.stringify(nested), before);
    const count = requests.length;
    for (const body of ['ab', 'other', 'none', '', 'abc ', 123, false, ['abc'], null, undefined]) {
      await assert.rejects(send.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    await assert.rejects(piece.getAction('overlap').run({ propsValue: { base_url, body: 'abc' } }), /^Error: Invalid or missing input: body$/);
    for (const body of ['😀', '😀😀😀😀', 'no', 'é']) {
      await assert.rejects(piece.getAction('unicode').run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    for (const body of [
      { ...nested, value: 'ab' }, { ...nested, values: ['other'] },
      { ...nested, lookup: { a: 'none' } }, { ...nested, composed: 'other' },
    ]) await assert.rejects(piece.getAction('nested').run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    await assert.rejects(piece.getAction('nested').run({ propsValue: { base_url, body: { ...nested, values: Array(26000).fill('abc') } } }),
      /^Error: Input validation limit exceeded$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedStringEnumExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })), requestsBeforeNegatives: count, requestsAfterNegatives: requests.length,
      negativeCases: 'ab wrapper length, other wrapper enum, none zero matches, abc duplicate matches; Unicode min/max, wrong types, nested failures and shared work bound: 20 cases add zero requests' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
