// Node half of tests/openapi2piece/test_package.py.
//
// The statements below are test/package.test.js lines 139-143, 148-150,
// 158-161 and 169-300, unchanged. pytest has already generated the pieces with
// the Python CLI and compiled them with `npx tsc` in the original order; this
// script loads the compiled output, validates real framework metadata and
// exercises the actions over local HTTP.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createRequire as createModuleRequire } from 'node:module';
import { join } from 'node:path';

const { OPENAPI2PIECE_AUTH_OUT: authOut, OPENAPI2PIECE_CONSTRAINED_OUT: constrainedOut,
  OPENAPI2PIECE_STRINGS_OUT: stringsOut, OPENAPI2PIECE_WIDGETS_OUT: widgetsOut } = process.env;
// Resolve the pinned packages from the generated output, like the original
// resolved them from the repository root.
const createRequire = () => createModuleRequire(join(authOut, 'package.json'));
{
    const require = createRequire(import.meta.url);
    const piece = require(join(authOut, 'dist/index.js')).piece_authchoices;
    const { PieceMetadata } = require('@activepieces/pieces-framework');
    const validation = PieceMetadata.safeParse({ ...piece.metadata(), name: 'authchoices', version: '0.1.0' });
    assert.equal(validation.success, true, validation.error?.message);
    const constrained = require(join(constrainedOut, 'dist/index.js')).piece_constrained;
    const constrainedMetadata = PieceMetadata.safeParse({ ...constrained.metadata(), name: 'constrained', version: '0.1.0' });
    assert.equal(constrainedMetadata.success, true, constrainedMetadata.error?.message);
    const strings = require(join(stringsOut, 'dist/index.js')).piece_strings;
    const stringsMetadata = PieceMetadata.safeParse({ ...strings.metadata(), name: 'strings', version: '0.1.0' });
    assert.equal(stringsMetadata.success, true, stringsMetadata.error?.message);
    assert.equal(strings.getAction('sendstring').props.body.type, 'SHORT_TEXT');
    const widgets = require(join(widgetsOut, 'dist/index.js')).piece_widgets;
    const widgetMetadata = PieceMetadata.safeParse({ ...widgets.metadata(), name: 'widgets', version: '0.1.0' });
    assert.equal(widgetMetadata.success, true, widgetMetadata.error?.message);
    assert.equal(widgets.getAction('createwidget').props.body.type, 'OBJECT');
    const requests = [];
    const server = createServer(async (req, res) => {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      requests.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') });
      res.setHeader('Content-Type', 'application/json');
      if (req.url.includes('/fail')) res.statusCode = 422;
      res.end('{"ok":true}');
    });
    try {
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
      const base_url = `http://127.0.0.1:${server.address().port}/v1`;
      const auth = { type: 'CUSTOM_AUTH', props: { auth_headerkey: 'local-key', auth_querykey: 'q &+=', auth_bearer: 'local-bearer' } };
      const action = piece.getAction('items');
      for (const auth_scheme of ['HeaderKey', 'QueryKey', 'Bearer']) {
        assert.deepEqual(await action.run({ propsValue: { base_url, auth_scheme }, auth }), { ok: true });
      }
      assert.deepEqual(requests.map(r => ({ url: r.url, key: r.headers['x-api-key'], authorization: r.headers.authorization })), [
        { url: '/v1/items', key: 'local-key', authorization: undefined },
        { url: '/v1/items?api_key=q+%26%2B%3D', key: undefined, authorization: undefined },
        { url: '/v1/items', key: undefined, authorization: 'Bearer local-bearer' },
      ]);
      await assert.rejects(action.run({ propsValue: { base_url }, auth }), /Invalid or missing input: auth_scheme/);
      assert.equal(requests.length, 3);
      const compound = piece.getAction('compound');
      for (const auth_requirement of ['["HeaderKey","QueryKey"]', '["Bearer"]']) {
        assert.deepEqual(await compound.run({ propsValue: { base_url, auth_requirement }, auth }), { ok: true });
      }
      assert.deepEqual(requests.slice(-2).map(r => ({ url: r.url, key: r.headers['x-api-key'], authorization: r.headers.authorization })), [
        { url: '/v1/compound-alternative?api_key=q+%26%2B%3D', key: 'local-key', authorization: undefined },
        { url: '/v1/compound-alternative', key: undefined, authorization: 'Bearer local-bearer' },
      ]);
      await assert.rejects(compound.run({ propsValue: { base_url }, auth }), /Invalid or missing input: auth_requirement/);
      await assert.rejects(compound.run({ propsValue: { base_url, auth_requirement: '["HeaderKey","QueryKey"]' },
        auth: { props: { auth_headerkey: 'local-key', auth_bearer: 'valid-fallback' } } }), /Missing credentials for this operation/);
      assert.equal(requests.length, 5);
      await assert.rejects(compound.run({ propsValue: { base_url: base_url + '/fail', auth_requirement: '["HeaderKey","QueryKey"]' }, auth }),
        /^Error: HTTP request failed with status 422$/);
      assert.equal(requests.length, 6);
      assert.equal(requests.at(-1).headers.authorization, undefined);
      assert.equal(requests.at(-1).headers['x-api-key'], 'local-key');
      const notify = constrained.getAction('notify');
      const body = { requestId: 'r1', email: 'a' };
      assert.deepEqual(await notify.run({ propsValue: { base_url, body } }), { ok: true });
      assert.equal(requests.at(-1).url, '/v1/notify');
      assert.deepEqual(JSON.parse(requests.at(-1).body), body);
      for (const body of [{ email: 'a' }, { requestId: '', email: 'a' },
        { requestId: 'r1', email: 'a', sms: '123' }, { requestId: 'r1' }, null]) {
        await assert.rejects(notify.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
      }
      assert.equal(requests.length, 7);
      const sendString = strings.getAction('sendstring');
      for (const body of ['abc', 'xyz']) {
        assert.deepEqual(await sendString.run({ propsValue: { base_url, body } }), { ok: true });
        assert.equal(requests.at(-1).url, '/v1/string');
        assert.equal(requests.at(-1).headers['content-type'], 'application/json');
        assert.equal(requests.at(-1).body, JSON.stringify(body));
      }
      const count = requests.length;
      for (const body of ['ab', 'def', '', null, undefined, 123]) {
        await assert.rejects(sendString.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
      }
      await assert.rejects(strings.getAction('overlap').run({ propsValue: { base_url, body: 'abc' } }), /^Error: Invalid or missing input: body$/);
      assert.equal(requests.length, count);

      const optional = piece.getAction('optional');
      assert.equal(optional.requireAuth, false);
      assert.equal(optional.props.auth_requirement.required, true);
      assert.equal(optional.props.auth_requirement.defaultValue, undefined);
      assert.deepEqual(optional.props.auth_requirement.options.options, [
        { label: '"HeaderKey" + "QueryKey"', value: '["HeaderKey","QueryKey"]' },
        { label: 'Anonymous', value: '[]' },
      ]);
      for (const connection of [undefined, auth]) {
        assert.deepEqual(await optional.run({ propsValue: { base_url, auth_requirement: '[]' }, auth: connection }), { ok: true });
        assert.equal(requests.at(-1).url, '/v1/anonymous-alternative');
        assert.equal(requests.at(-1).headers['x-api-key'], undefined);
        assert.equal(requests.at(-1).headers.authorization, undefined);
      }
      assert.deepEqual(await optional.run({ propsValue: { base_url, auth_requirement: '["HeaderKey","QueryKey"]' }, auth }), { ok: true });
      assert.equal(requests.at(-1).url, '/v1/anonymous-alternative?api_key=q+%26%2B%3D');
      assert.equal(requests.at(-1).headers['x-api-key'], 'local-key');
      assert.equal(requests.at(-1).headers.authorization, undefined);
      const optionalCount = requests.length;
      for (const auth_requirement of [undefined, 'Anonymous', [], '["HeaderKey"]']) {
        await assert.rejects(optional.run({ propsValue: { base_url, auth_requirement }, auth }), /^Error: Invalid or missing input: auth_requirement$/);
      }
      for (const connection of [undefined, { props: { auth_headerkey: 'local-key' } }, { props: { auth_querykey: 'query' } }]) {
        await assert.rejects(optional.run({ propsValue: { base_url, auth_requirement: '["HeaderKey","QueryKey"]' }, auth: connection }),
          /^Error: Missing credentials for this operation$/);
      }
      assert.equal(requests.length, optionalCount);
      for (const auth_requirement of ['[]', '["HeaderKey","QueryKey"]']) {
        const before = requests.length;
        await assert.rejects(optional.run({ propsValue: { base_url: base_url + '/fail', auth_requirement }, auth }),
          /^Error: HTTP request failed with status 422$/);
        assert.equal(requests.length, before + 1);
        assert.equal(requests.at(-1).headers['x-api-key'], auth_requirement === '[]' ? undefined : 'local-key');
        assert.equal(requests.at(-1).headers.authorization, undefined);
      }
      console.log(JSON.stringify({ installedAnonymousExample: 'passed', requireAuth: optional.requireAuth,
        selections: optional.props.auth_requirement.options.options, anonymousRequests: 'no connection and populated connection: no credentials',
        protectedRequest: 'both named keys only', negativeCases: 'invalid choice and incomplete credentials before HTTP; 422 sends one request per explicit selection' }));

      const createWidget = widgets.getAction('createwidget');
      const writable = { name: 'Miso', secret: 'local-secret', nested: { label: 'new' }, items: [{ value: 0 }] };
      assert.deepEqual(await createWidget.run({ propsValue: { base_url, body: writable } }), { ok: true });
      assert.equal(requests.at(-1).url, '/v1/widgets');
      assert.deepEqual(JSON.parse(requests.at(-1).body), writable);
      const widgetCount = requests.length;
      for (const [body, path] of [
        [{ ...writable, id: 1 }, 'body.id'],
        [{ ...writable, nested: { label: 'new', createdAt: 'hidden' } }, 'body.nested.createdAt'],
        [{ ...writable, items: [{ id: 2, value: 0 }] }, 'body.items[0].id'],
      ]) await assert.rejects(createWidget.run({ propsValue: { base_url, body } }),
        { message: `Read-only request property: ${path}` });
      for (const body of [{ secret: 'valid' }, { name: 'Miso', secret: 'ab' }]) {
        await assert.rejects(createWidget.run({ propsValue: { base_url, body } }), /^Error: Invalid or missing input: body$/);
      }
      assert.equal(requests.length, widgetCount);
      console.log(JSON.stringify({ installedRequestProjectionExample: 'passed', body: writable,
        negativeCases: 'missing name, short writeOnly secret, id, nested.createdAt and items[0].id all fail before HTTP',
        typecheckAndRealMetadata: 'passed' }));

    } finally {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    }
}
