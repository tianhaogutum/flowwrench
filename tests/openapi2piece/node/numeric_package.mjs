// Node half of tests/openapi2piece/test_numeric_package.py.
//
// The body below is test/numeric-package.test.js lines 54-103, unchanged: it loads
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
    const piece = require(join(out, 'dist/index.js')).piece_numbers;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const metadata = PieceMetadata.safeParse({ ...piece.metadata(), name: 'numbers', version: '0.1.0' });
    assert.equal(metadata.success, true, metadata.error?.message);
    const send = piece.getAction('sendnumber');
    assert.equal(send.props.body.type, 'NUMBER');
    assert.equal(send.props.body.required, true);
    assert.equal(piece.getAction('sendinteger').props.body.type, 'NUMBER');

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
    for (const body of [1, 2]) {
      assert.deepEqual(await send.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, '/v2/number');
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, String(body));
      assert.equal(typeof JSON.parse(requests.at(-1).body), 'number');
    }
    for (const body of [0.5, 1.5]) {
      await piece.getAction('fractional').run({ propsValue: { base_url, body } });
      assert.equal(requests.at(-1).url, '/v2/fractional');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
    }
    const nested = { value: 1, values: [1, 2], lookup: { a: 2 }, composed: 2 };
    await piece.getAction('nested').run({ propsValue: { base_url, body: nested } });
    assert.deepEqual(JSON.parse(requests.at(-1).body), nested);
    const count = requests.length;
    for (const body of [0, 3, 4, '1', null, undefined]) {
      await assert.rejects(send.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    await assert.rejects(piece.getAction('overlap').run({ propsValue: { base_url, body: 1 } }), /^Error: Invalid or missing input: body$/);
    await assert.rejects(piece.getAction('sendinteger').run({ propsValue: { base_url, body: 1.5 } }), /^Error: Invalid or missing input: body$/);
    for (const body of [0, 1, 2, 2.5, '0.5']) {
      await assert.rejects(piece.getAction('fractional').run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
    }
    await assert.rejects(piece.getAction('nested').run({ propsValue: { base_url, body: { ...nested, values: [0] } } }),
      /^Error: Invalid or missing input: body$/);
    assert.equal(requests.length, count);
    console.log(JSON.stringify({ installedNumericExample: 'passed', metadata: 'pinned real PieceMetadata accepted',
      requests: requests.map(({ url, body }) => ({ url, body })),
      negativeCases: '0/3 fail wrapper enum; 4 zero matches; 1 duplicate matches; wrong types, fractional integer, enum/bound intersections and nested wrapper failures before HTTP' }));
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
