// Node half of tests/openapi2piece/test_runtime.py.
//
// Everything below the "original test/runtime.test.js" marker is the original
// Node test file, unchanged, except that:
// * the generator imports (parseSpec, renderPiece, writePiece, normalizeSchema)
//   come from harness.mjs, which bridges them to the Python port;
// * node:test is replaced by harness.mjs's sequential runner, which writes one
//   result per test for pytest (one parametrized pytest case per test);
// * the temporary directory is created by pytest (OPENAPI2PIECE_TEMP, with a
//   node_modules link to the pinned packages) and `require`, `tsc` resolve from
//   there; tsc runs as `npx tsc`.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createServer as createSecureServer } from 'node:https';
import { createRequire } from 'node:module';
import { readFile, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { test, baseSpec, parseSpec, renderPiece, writePiece, normalizeSchema, runRegisteredTests } from './harness.mjs';

const temp = process.env.OPENAPI2PIECE_TEMP;
const require = createRequire(join(temp, 'package.json'));
await writeFile(join(temp, 'package.json'), JSON.stringify({ type: 'commonjs' }));
// ---- original test/runtime.test.js (setup) ----
const fixture = parseSpec(await readFile('fixtures/petstore.yaml', 'utf8'));
const nullableBooleans = parseSpec(await readFile('fixtures/oneof-nullable-booleans.json', 'utf8'));
const widgets = parseSpec(await readFile('fixtures/request-projection.json', 'utf8'));
const schemes = {
  key: { type: 'apiKey', in: 'query', name: 'api_key' },
  bearer: { type: 'http', scheme: 'bearer' },
  basic: { type: 'http', scheme: 'basic' },
  oauth: { type: 'oauth2', flows: { authorizationCode: { authorizationUrl: 'https://auth.example.test/authorize', tokenUrl: 'https://auth.example.test/token', scopes: { read: 'Read', admin: 'Unused permission' } } } },
};
const configs = {
  petstore: fixture,
  widgets,
  widgetarrays: { ...widgets, paths: { '/widget-batches': { post: { operationId: 'batch',
    requestBody: { required: true, content: { 'application/json': { schema: {
      type: 'array', minItems: 1, items: widgets.components.schemas.Widget,
    } } } }, responses: { 200: { description: 'OK' } },
  } } } },
  none: baseSpec(),
  booleanenums: parseSpec(await readFile('fixtures/oneof-boolean-enums.json', 'utf8')),
  stringenums: parseSpec(await readFile('fixtures/oneof-string-enums.json', 'utf8')),
  nullablearrays: parseSpec(await readFile('fixtures/oneof-nullable-arrays.json', 'utf8')),
  nullablearrays30: baseSpec({ openapi: '3.0.4', paths: { '/send30': JSON.parse(await readFile('fixtures/oneof-nullable-arrays.json', 'utf8')).paths['/send30'] } }),
  nullableobjects: parseSpec(await readFile('fixtures/oneof-nullable-objects.json', 'utf8')),
  nullableobjects30: baseSpec({ openapi: '3.0.4', paths: { '/send30': JSON.parse(await readFile('fixtures/oneof-nullable-objects.json', 'utf8')).paths['/send30'] } }),
  nullablenumbers: parseSpec(await readFile('fixtures/oneof-nullable-numbers.json', 'utf8')),
  nullablenumbers30: baseSpec({ openapi: '3.0.4', paths: Object.fromEntries(Object.entries(JSON.parse(await readFile('fixtures/oneof-nullable-numbers.json', 'utf8')).paths).filter(([path]) => ['/send30', '/integer30'].includes(path))) }),
  nullablestrings: parseSpec(await readFile('fixtures/oneof-nullable-strings.json', 'utf8')),
  nullablestrings30: baseSpec({ openapi: '3.0.4', paths: { '/send30': JSON.parse(await readFile('fixtures/oneof-nullable-strings.json', 'utf8')).paths['/send30'] } }),
  nullablebooleans: nullableBooleans,
  nullablebooleans30: baseSpec({ openapi: '3.0.4', paths: { '/send30': nullableBooleans.paths['/send30'] } }),
  strings: parseSpec(await readFile('fixtures/oneof-strings.json', 'utf8')),
  uniquearrays: parseSpec(await readFile('fixtures/oneof-unique-arrays.json', 'utf8')),
  arraywrappers: parseSpec(await readFile('fixtures/oneof-arrays.json', 'utf8')),
  numericenums: parseSpec(await readFile('fixtures/oneof-numeric-enums.json', 'utf8')),
  numeric: parseSpec(await readFile('fixtures/oneof-numbers.json', 'utf8')),
  numeric30: baseSpec({ openapi: '3.0.4', paths: { '/bounds': { post: { operationId: 'bounds',
    requestBody: { required: true, content: { 'application/json': { schema: {
      type: 'number', minimum: 0, maximum: 3, exclusiveMinimum: true, exclusiveMaximum: true,
      oneOf: [{ type: 'number' }],
    } } } }, responses: { 200: { description: 'OK' } },
  } } } }),
  numeric31: baseSpec({ paths: { '/bounds': { post: { operationId: 'bounds',
    requestBody: { required: true, content: { 'application/json': { schema: {
      type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 3, oneOf: [{ type: 'number' }],
    } } } }, responses: { 200: { description: 'OK' } },
  } } } }),
  constrained: parseSpec(await readFile('fixtures/oneof-constraints.json', 'utf8')),
  alternatives: parseSpec(await readFile('fixtures/request-alternatives.json', 'utf8')),
  authchoices: parseSpec(await readFile('fixtures/auth-alternatives.json', 'utf8')),
  optionalbearer: baseSpec({ components: { securitySchemes: { bearer: schemes.bearer } },
    security: [{}, { bearer: [] }] }),
  authflows: baseSpec({ components: { securitySchemes: { basic: schemes.basic, bearer: schemes.bearer, oauth: schemes.oauth } },
    security: [{ oauth: ['read'] }, { basic: [] }, { bearer: [] }] }),
  authgroups: baseSpec({ components: { securitySchemes: schemes },
    security: [{ key: [], basic: [] }, { key: [], oauth: ['read'] }, { bearer: [] }] }),
  unions: baseSpec({ paths: Object.fromEntries([
    ['overlap', { oneOf: [{ type: 'object', required: ['a'] }, { type: 'object', required: ['b'] }] }],
    ['nullOverlap', { oneOf: [{ type: ['object', 'null'] }, { type: ['object', 'null'] }] }],
    ['numbers', { oneOf: [{ type: 'integer', minimum: 0 }, { type: 'number', maximum: -1 }] }],
    ['arrays', { type: 'array', items: { oneOf: [{ type: 'boolean' }, { type: 'null' }] } }],
    ['arrayunion', { oneOf: [
      { type: 'array', items: { type: 'string' }, minItems: 1 },
      { type: 'array', items: { type: 'integer' }, minItems: 1 },
    ] }],
    ['dictionary', { type: 'object', additionalProperties: { oneOf: [{ type: 'boolean' }, { type: 'string' }] } }],
  ].map(([name, schema]) => [`/${name}`, { post: { operationId: name, requestBody: { required: true, content: { 'application/json': { schema } } }, responses: { 200: { description: 'OK' } } } }])) }),
  nullable30: baseSpec({ openapi: '3.0.3', paths: { '/nullable': { post: { operationId: 'nullable',
    requestBody: { required: true, content: { 'application/json': { schema: { oneOf: [
      { type: 'string', nullable: true, minLength: 1 }, { type: 'string', enum: ['reserved'] },
    ] } } } }, responses: { 200: { description: 'OK' } },
  } } } }),
  key: baseSpec({ components: { securitySchemes: { key: schemes.key } }, security: [{ key: [] }] }),
  bearer: baseSpec({ components: { securitySchemes: { bearer: schemes.bearer } }, security: [{ bearer: [] }] }),
  basic: baseSpec({ components: { securitySchemes: { basic: schemes.basic } }, security: [{ basic: [] }] }),
  oauth: baseSpec({ components: { securitySchemes: { oauth: schemes.oauth } }, security: [{ oauth: ['read'] }] }),
  custom: baseSpec({ components: { securitySchemes: { key: schemes.key, basic: schemes.basic } }, security: [{ key: [], basic: [] }] }),
  collisions: baseSpec({ components: { securitySchemes: { Basic_username: schemes.key, basic: schemes.basic } }, security: [{ Basic_username: [], basic: [] }] }),
  mixedoauth: baseSpec({ components: { securitySchemes: { key: schemes.key, oauth: schemes.oauth } }, security: [{ key: [], oauth: ['read'] }] }),
  scalars: baseSpec({ paths: Object.fromEntries([
    ['array', { type: 'array', items: { type: 'integer' }, minItems: 1 }],
    ['boolean', { type: 'boolean' }], ['string', { type: ['string', 'null'] }], ['number', { type: 'number' }],
  ].map(([name, schema]) => [`/${name}`, { post: { operationId: name, requestBody: { required: true, content: { 'application/json': { schema } } }, responses: { 200: { description: 'OK' } } } }])) }),
  quoted: (() => {
    const doc = baseSpec();
    doc.paths['/hello'].get.operationId = 'export default; `${process.exit(1)}`';
    doc.paths['/hello'].get.summary = '"\n`${process.exit(2)}`';
    doc.paths['/hello'].get.description = '\u2028\u2029\\"';
    return doc;
  })(),
};
for (const [name, doc] of Object.entries(configs)) {
  const spec = name === 'petstore' ? doc : parseSpec(JSON.stringify(doc));
  const { files } = await renderPiece(spec, { name });
  await writePiece(files, join(temp, name));
}
const compiler = { compilerOptions: { target: 'ES2022', module: 'CommonJS', moduleResolution: 'Node', strict: true,
  esModuleInterop: true, skipLibCheck: true, declaration: true, rootDir: '.', outDir: 'build', types: ['node'] }, include: ['*/src/**/*.ts'] };
await writeFile(join(temp, 'tsconfig.json'), JSON.stringify(compiler));
const compile = args => {
  const result = spawnSync(process.env.OPENAPI2PIECE_NPX, ['tsc', ...args], { cwd: temp, encoding: 'utf8', timeout: 120000 });
  console.log(JSON.stringify({ command: ['npx', 'tsc', ...args], exitCode: result.status,
    stdout: result.stdout, stderr: result.stderr }));
  return result;
};
const getPiece = name => require(join(temp, 'build', name, 'src/index.js'))[`piece_${name}`];
const action = (name, id = 'hello') => getPiece(name).getAction(id);
const run = (name, id, propsValue, auth) => action(name, id).run({ propsValue, auth });

let server, url, secureServer, secureUrl;
let requests = [];

test('generated TypeScript passes --noEmit against real framework and compiles all auth/property variants', () => {
  const noEmit = compile(['--noEmit', '-p', join(temp, 'nullablenumbers/tsconfig.json')]);
  assert.equal(noEmit.status, 0, noEmit.stdout + noEmit.stderr);
  const result = compile(['-p', join(temp, 'tsconfig.json')]);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  for (const name of Object.keys(configs)) assert.ok(getPiece(name).actions());
  assert.equal(getPiece('petstore').auth.type, 'SECRET_TEXT');
  assert.equal(getPiece('basic').auth.type, 'BASIC_AUTH');
  assert.equal(getPiece('oauth').auth.type, 'OAUTH2');
  assert.equal(getPiece('custom').auth.type, 'CUSTOM_AUTH');
  assert.equal(getPiece('none').auth, undefined);
});

test('real framework metadata validation accepts every generated piece; OAuth scopes stay minimal', () => {
  const { PieceMetadata } = require('@activepieces/pieces-framework');
  for (const name of Object.keys(configs)) {
    const result = PieceMetadata.safeParse({ ...getPiece(name).metadata(), name, version: '0.1.0' });
    assert.equal(result.success, true, `${name}: ${result.error?.message}`);
  }
  assert.deepEqual(getPiece('oauth').auth.scope, ['read']);
  assert.equal(getPiece('custom').auth.props.auth_key.type, 'SHORT_TEXT');
});

test.before(async () => {
  server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString('utf8');
    const entry = { url: req.url, method: req.method, headers: req.headers, body };
    requests.push(entry);
    res.setHeader('Content-Type', 'application/json');
    if (req.url.includes('/fail')) { res.writeHead(422); res.end(JSON.stringify({ echo: 'DO_NOT_LOG_SECRET' })); }
    else if (req.url.includes('/redirect')) { res.writeHead(302, { Location: '/must-not-follow' }); res.end('{}'); }
    else if (req.method === 'GET' && req.url === '/v1/widgets/7') {
      res.end(JSON.stringify({ id: 7, name: 'Stored', nested: { createdAt: '2026-01-01T00:00:00Z', label: 'existing' }, items: [{ id: 8, value: 0 }] }));
    } else { res.end(JSON.stringify({ ok: true, request: entry })); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  url = `http://127.0.0.1:${server.address().port}`;
  secureServer = createSecureServer({ key: await readFile('fixtures/tls/key.pem'), cert: await readFile('fixtures/tls/cert.pem') }, (_, res) => res.end('untrusted'));
  await new Promise(resolve => secureServer.listen(0, '127.0.0.1', resolve));
  secureUrl = `https://127.0.0.1:${secureServer.address().port}`;
});
test.after(async () => {
  server?.closeAllConnections();
  secureServer?.closeAllConnections();
  if (server) await new Promise(resolve => server.close(resolve));
  if (secureServer) await new Promise(resolve => secureServer.close(resolve));
  await rm(temp, { recursive: true, force: true });
});

test('fixture end to end: encoded path, query false/zero, header, API key and parsed response', async () => {
  const before = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  const promise = run('petstore', 'getpet', { base_url: `${url}/v1`, path_petid: 'a/b ?&', query_verbose: false, query_limit: 0, header_x_trace: 'trace-42' }, { type: 'SECRET_TEXT', secret_text: 'fixture-token' });
  assert.equal(process.env.NODE_TLS_REJECT_UNAUTHORIZED, before);
  const result = await promise;
  assert.equal(result.ok, true);
  const received = requests.at(-1);
  assert.equal(received.url, '/v1/pets/a%2Fb%20%3F%26?limit=0&verbose=false');
  assert.equal(received.method, 'GET');
  assert.equal(received.headers['x-api-key'], 'fixture-token');
  assert.equal(received.headers['x-trace'], 'trace-42');
  assert.equal(process.env.NODE_TLS_REJECT_UNAUTHORIZED, before);
});

test('JSON body, nested schema validation, optional omission and anonymous override', async () => {
  const body = { name: 'Miso', active: false, age: 0, kind: 'cat', labels: ['tiny'], metadata: { team: 'demo' } };
  await run('petstore', 'updatepet', { base_url: url, path_petid: '7', body }, { secret_text: 'key' });
  const received = requests.at(-1);
  assert.equal(received.method, 'PATCH');
  assert.equal(received.headers['content-type'], 'application/json');
  assert.deepEqual(JSON.parse(received.body), body);
  assert.equal(action('petstore', 'getpet_2').requireAuth, false);
  await run('petstore', 'getpet_2', { base_url: url }, { secret_text: 'MUST_NOT_SEND' });
  assert.equal(requests.at(-1).headers['x-api-key'], undefined);
  await run('petstore', 'getpet', { base_url: url, path_petid: '1' }, { secret_text: 'key' });
  assert.equal(requests.at(-1).url, '/pets/1');
});

test('request projection sends only writable widget fields and rejects readOnly paths before HTTP', async () => {
  assert.equal(action('widgets', 'createwidget').props.body.type, 'OBJECT');
  assert.equal(action('widgetarrays', 'batch').props.body.type, 'ARRAY');
  const body = { name: 'Miso', secret: 'local-secret', nested: { label: 'new' }, items: [{ value: 0 }] };
  const before = structuredClone(body);
  for (const value of [body, { name: 'Minimal' }]) {
    await run('widgets', 'createwidget', { base_url: `${url}/v1`, body: value });
    assert.equal(requests.at(-1).url, '/v1/widgets');
    assert.equal(requests.at(-1).method, 'POST');
    assert.deepEqual(JSON.parse(requests.at(-1).body), value);
  }
  await run('widgetarrays', 'batch', { base_url: url, body: [body] });
  assert.equal(requests.at(-1).url, '/widget-batches');
  assert.deepEqual(JSON.parse(requests.at(-1).body), [body]);
  assert.deepEqual(body, before);
  const count = requests.length;
  for (const [invalidBody, path] of [
    [{ ...body, id: 1 }, 'body.id'], [{ ...body, id: null }, 'body.id'], [{ ...body, id: undefined }, 'body.id'],
    [{ ...body, nested: { ...body.nested, createdAt: 'DO_NOT_ECHO' } }, 'body.nested.createdAt'],
    [{ ...body, items: [{ value: 0 }, { value: 1, id: 2 }] }, 'body.items[1].id'],
  ]) {
    const snapshot = structuredClone(invalidBody);
    await assert.rejects(run('widgets', 'createwidget', { base_url: url, body: invalidBody }),
      error => { assert.equal(error.message, `Read-only request property: ${path}`); return true; });
    assert.deepEqual(invalidBody, snapshot);
  }
  await assert.rejects(run('widgetarrays', 'batch', { base_url: url, body: [{ name: 'Miso', id: 1 }] }),
    { message: 'Read-only request property: body[0].id' });
  for (const body of [{}, { secret: 'valid' }, { name: '' }, { name: 'Miso', secret: 'ab' },
    { name: 'Miso', nested: {} }, { name: 'Miso', items: [{}] }, { name: 'Miso', items: [{ value: -1 }] }, null, undefined]) {
    await assert.rejects(run('widgets', 'createwidget', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
  const response = await run('widgets', 'getwidget', { base_url: `${url}/v1`, path_id: '7' });
  assert.deepEqual(response, { id: 7, name: 'Stored', nested: { createdAt: '2026-01-01T00:00:00Z', label: 'existing' }, items: [{ id: 8, value: 0 }] });
});

test('request projection handles typed dictionaries, unusual keys and required writeOnly constraints', () => {
  const { validate } = require(join(temp, 'build/widgets/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: 'object', additionalProperties: {
    type: 'array', items: { type: 'object', required: ['audit.id', 'secret'], properties: {
      'audit.id': { type: 'integer', readOnly: true },
      ...JSON.parse('{"__proto__":{"type":"string","readOnly":true}}'),
      secret: { type: 'string', writeOnly: true, enum: ['abc'], minLength: 3 },
      editable: { type: 'boolean', readOnly: false },
      serverObject: { type: 'object', readOnly: true, properties: { id: { type: 'integer' } } },
    } },
  } }, 'request body', 0, { projectRequest: true });
  validate({ 'map.key': [{ secret: 'abc', editable: false }] }, schema, 'body');
  for (const [value, path] of [
    [{ 'map.key': [{ secret: 'abc', 'audit.id': 1 }] }, 'body["map.key"][0]["audit.id"]'],
    [JSON.parse('{"map.key":[{"secret":"abc","__proto__":"hidden"}]}'), 'body["map.key"][0].__proto__'],
    [{ a: [{ secret: 'abc', serverObject: {} }] }, 'body.a[0].serverObject'],
  ]) assert.throws(() => validate(value, schema, 'body'), { message: `Read-only request property: ${path}` });
  for (const value of [{ a: [{}] }, { a: [{ secret: 'ab' }] }, { a: [{ secret: 'abcd' }] }]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  assert.equal({}.polluted, undefined);
});

test('real requests authenticate query key, bearer, basic, OAuth2, CustomAuth and mixed OAuth2', async () => {
  const cases = [
    ['none', undefined, undefined, undefined],
    ['key', { secret_text: 'key &+=' }, '?api_key=key+%26%2B%3D', undefined],
    ['bearer', { secret_text: 'bearer-value' }, undefined, 'Bearer bearer-value'],
    ['basic', { username: 'alice', password: 'p:ass' }, undefined, `Basic ${Buffer.from('alice:p:ass').toString('base64')}`],
    ['oauth', { access_token: 'oauth-value' }, undefined, 'Bearer oauth-value'],
    ['custom', { props: { auth_key: 'k', auth_basic_username: 'u', auth_basic_password: 'p' } }, '?api_key=k', `Basic ${Buffer.from('u:p').toString('base64')}`],
    ['collisions', { props: { auth_basic_username: 'k', auth_basic_username_2: 'u', auth_basic_password: 'p' } }, '?api_key=k', `Basic ${Buffer.from('u:p').toString('base64')}`],
    ['mixedoauth', { props: { auth_key: 'k', auth_oauth: 'manual-token' } }, '?api_key=k', 'Bearer manual-token'],
  ];
  for (const [name, auth, query, authorization] of cases) {
    await run(name, 'hello', { base_url: url }, auth);
    const received = requests.at(-1);
    assert.equal(received.url, '/hello' + (query ?? ''));
    assert.equal(received.headers.authorization, authorization);
  }
});

test('JSON scalar, array and nullable body types preserve false, zero, quotes and null', async () => {
  for (const [id, body] of [['array', [0, 2]], ['boolean', false], ['string', 'text "quotes"'], ['string', null], ['number', 0]]) {
    await run('scalars', id, { base_url: url, body });
    assert.deepEqual(JSON.parse(requests.at(-1).body), body);
  }
});

test('anonymous OR alternatives permit no connection and isolate credentials over real HTTP', async () => {
  const choice = action('authchoices', 'optional');
  assert.equal(choice.requireAuth, false);
  assert.equal(choice.props.auth_scheme, undefined);
  assert.equal(choice.props.auth_requirement.displayName, 'Authentication requirement');
  assert.equal(choice.props.auth_requirement.type, 'STATIC_DROPDOWN');
  assert.equal(choice.props.auth_requirement.required, true);
  assert.equal(choice.props.auth_requirement.defaultValue, undefined);
  assert.deepEqual(choice.props.auth_requirement.options.options, [
    { label: '"HeaderKey" + "QueryKey"', value: '["HeaderKey","QueryKey"]' },
    { label: 'Anonymous', value: '[]' },
  ]);
  const auth = { type: 'CUSTOM_AUTH', props: { auth_headerkey: 'local-key', auth_querykey: 'q &+=', auth_bearer: 'unused' } };
  for (const connection of [undefined, auth, { props: { auth_headerkey: 'hidden\r\ninjected', auth_querykey: 42 } }]) {
    await run('authchoices', 'optional', { base_url: `${url}/v1`, auth_requirement: '[]' }, connection);
    assert.equal(requests.at(-1).url, '/v1/anonymous-alternative');
    assert.equal(requests.at(-1).headers['x-api-key'], undefined);
    assert.equal(requests.at(-1).headers.authorization, undefined);
  }
  await run('authchoices', 'optional', { base_url: `${url}/v1`, auth_requirement: '["HeaderKey","QueryKey"]' }, auth);
  assert.equal(requests.at(-1).url, '/v1/anonymous-alternative?api_key=q+%26%2B%3D');
  assert.equal(requests.at(-1).headers['x-api-key'], 'local-key');
  assert.equal(requests.at(-1).headers.authorization, undefined);
  // A lone protected scheme still uses its native SecretText connection shape.
  assert.equal(getPiece('optionalbearer').auth.type, 'SECRET_TEXT');
  assert.equal(action('optionalbearer').requireAuth, false);
  await run('optionalbearer', 'hello', { base_url: url, auth_requirement: '[]' });
  assert.equal(requests.at(-1).headers.authorization, undefined);
  await run('optionalbearer', 'hello', { base_url: url, auth_requirement: '["bearer"]' }, { type: 'SECRET_TEXT', secret_text: 'token' });
  assert.equal(requests.at(-1).headers.authorization, 'Bearer token');
});

test('anonymous OR alternatives reject invalid choices and incomplete credentials locally with no HTTP fallback', async () => {
  const auth = { props: { auth_headerkey: 'hidden', auth_querykey: 'hidden', auth_bearer: 'unused' } };
  const count = requests.length;
  for (const auth_requirement of [undefined, null, '', 'Anonymous', '[ ]', [], {}, 'Bearer', '["HeaderKey"]',
    '["QueryKey","HeaderKey"]', '["HeaderKey", "QueryKey"]']) {
    await assert.rejects(run('authchoices', 'optional', { base_url: url, auth_requirement }, auth),
      /^Error: Invalid or missing input: auth_requirement$/);
  }
  for (const connection of [undefined, { props: {} }, { props: { auth_headerkey: 'hidden' } },
    { props: { auth_querykey: 'hidden' } }, { props: { auth_headerkey: '', auth_querykey: 'hidden' } },
    { props: { auth_headerkey: 'hidden', auth_querykey: 42 } }]) {
    await assert.rejects(run('authchoices', 'optional', { base_url: url, auth_requirement: '["HeaderKey","QueryKey"]' }, connection),
      /^Error: Missing credentials for this operation$/);
  }
  await assert.rejects(run('optionalbearer', 'hello', { base_url: url, auth_requirement: '["bearer"]' }),
    /^Error: Missing credentials for this operation$/);
  assert.equal(requests.length, count);
  for (const auth_requirement of ['["HeaderKey","QueryKey"]', '[]']) {
    const before = requests.length;
    await assert.rejects(run('authchoices', 'optional', { base_url: `${url}/fail`, auth_requirement }, auth),
      /^Error: HTTP request failed with status 422$/);
    assert.equal(requests.length, before + 1);
    assert.equal(requests.at(-1).headers['x-api-key'], auth_requirement === '[]' ? undefined : 'hidden');
    assert.equal(requests.at(-1).headers.authorization, undefined);
    assert.equal(requests.at(-1).url.includes('api_key='), auth_requirement !== '[]');
  }
});

test('authentication alternatives send only the explicit choice from a real CustomAuth connection', async () => {
  const choice = action('authchoices', 'items');
  assert.equal(getPiece('authchoices').auth.type, 'CUSTOM_AUTH');
  assert.equal(choice.requireAuth, true);
  assert.equal(choice.props.auth_scheme.type, 'STATIC_DROPDOWN');
  assert.equal(choice.props.auth_scheme.required, true);
  assert.equal(choice.props.auth_scheme.defaultValue, undefined);
  assert.deepEqual(choice.props.auth_scheme.options.options, [
    { label: 'Bearer', value: 'Bearer' }, { label: 'HeaderKey', value: 'HeaderKey' }, { label: 'QueryKey', value: 'QueryKey' },
  ]);
  const auth = { type: 'CUSTOM_AUTH', props: { auth_headerkey: 'header-value', auth_querykey: 'key &+=猫', auth_bearer: 'bearer-value' } };
  for (const [auth_scheme, query, header, bearer] of [
    ['HeaderKey', '', 'header-value', undefined],
    ['QueryKey', '?api_key=key+%26%2B%3D%E7%8C%AB', undefined, undefined],
    ['Bearer', '', undefined, 'Bearer bearer-value'],
  ]) {
    const result = await run('authchoices', 'items', { base_url: `${url}/v1`, auth_scheme }, auth);
    assert.equal(result.ok, true);
    assert.equal(requests.at(-1).url, '/v1/items' + query);
    assert.equal(requests.at(-1).headers['x-api-key'], header);
    assert.equal(requests.at(-1).headers.authorization, bearer);
  }
  // Only selected credentials need exist; malformed unselected values are inert.
  await run('authchoices', 'items', { base_url: url, auth_scheme: 'HeaderKey' },
    { props: { auth_headerkey: 'only-key', auth_bearer: 'hidden\r\ninjected' } });
  assert.equal(requests.at(-1).headers.authorization, undefined);
  await run('authchoices', 'keyonly', { base_url: url, auth_scheme: 'Bearer' }, auth);
  assert.equal(requests.at(-1).headers['x-api-key'], 'header-value');
  assert.equal(requests.at(-1).headers.authorization, undefined);
  await run('authchoices', 'public', { base_url: url, auth_scheme: 'Bearer' }, auth);
  assert.equal(requests.at(-1).url, '/public');
  assert.equal(requests.at(-1).headers['x-api-key'], undefined);
  assert.equal(requests.at(-1).headers.authorization, undefined);
});

test('authentication alternatives reject invalid selections or missing chosen credentials before HTTP without fallback', async () => {
  const auth = { props: { auth_headerkey: 'hidden', auth_querykey: 'hidden', auth_bearer: 'hidden' } };
  const count = requests.length;
  for (const auth_scheme of [undefined, null, '', 'bearer', 'hidden', ['Bearer'], { name: 'Bearer' }]) {
    await assert.rejects(run('authchoices', 'items', { base_url: url, auth_scheme }, auth), /^Error: Invalid or missing input: auth_scheme$/);
  }
  for (const connection of [undefined, { secret_text: 'hidden' }, { props: { auth_headerkey: 'hidden' } },
    { props: { auth_bearer: '' } }, { props: { auth_bearer: 42 } }]) {
    await assert.rejects(run('authchoices', 'items', { base_url: url, auth_scheme: 'Bearer' }, connection), /^Error: Missing credentials for this operation$/);
  }
  await assert.rejects(run('authchoices', 'items', { base_url: url, auth_scheme: 'HeaderKey' },
    { props: { auth_headerkey: 'hidden\r\ninjected', auth_bearer: 'valid' } }), /^Error: Invalid header value$/);
  assert.equal(requests.length, count);
  // A rejected HTTP response must never trigger a retry with another credential.
  await assert.rejects(run('authchoices', 'items', { base_url: `${url}/fail`, auth_scheme: 'Bearer' }, auth), /^Error: HTTP request failed with status 422$/);
  assert.equal(requests.length, count + 1);
  assert.equal(requests.at(-1).headers.authorization, 'Bearer hidden');
  assert.equal(requests.at(-1).headers['x-api-key'], undefined);
});

test('same-slot Basic, bearer and manual OAuth alternatives retain their existing wire adapters', async () => {
  const auth = { props: { auth_basic_username: 'alice', auth_basic_password: 'p:ass', auth_bearer: 'bearer', auth_oauth: 'manual' } };
  for (const [auth_scheme, expected] of [
    ['basic', `Basic ${Buffer.from('alice:p:ass').toString('base64')}`], ['bearer', 'Bearer bearer'], ['oauth', 'Bearer manual'],
  ]) {
    await run('authflows', 'hello', { base_url: url, auth_scheme }, auth);
    assert.equal(requests.at(-1).headers.authorization, expected);
  }
});

test('AND groups within OR send all and only selected credentials through real HTTP', async () => {
  const choice = action('authchoices', 'compound');
  assert.equal(choice.props.auth_scheme, undefined);
  assert.equal(choice.props.auth_requirement.type, 'STATIC_DROPDOWN');
  assert.equal(choice.props.auth_requirement.required, true);
  assert.equal(choice.props.auth_requirement.defaultValue, undefined);
  assert.deepEqual(choice.props.auth_requirement.options.options, [
    { label: '"Bearer"', value: '["Bearer"]' },
    { label: '"HeaderKey" + "QueryKey"', value: '["HeaderKey","QueryKey"]' },
  ]);
  const auth = { props: { auth_headerkey: 'header', auth_querykey: 'q &+=猫', auth_bearer: 'bearer' } };
  for (const [auth_requirement, query, key, authorization] of [
    ['["HeaderKey","QueryKey"]', '?api_key=q+%26%2B%3D%E7%8C%AB', 'header', undefined],
    ['["Bearer"]', '', undefined, 'Bearer bearer'],
  ]) {
    await run('authchoices', 'compound', { base_url: `${url}/v1`, auth_requirement }, auth);
    assert.equal(requests.at(-1).url, '/v1/compound-alternative' + query);
    assert.equal(requests.at(-1).headers['x-api-key'], key);
    assert.equal(requests.at(-1).headers.authorization, authorization);
  }
  await run('authchoices', 'compound', { base_url: url, auth_requirement: '["HeaderKey","QueryKey"]' },
    { props: { auth_headerkey: 'header', auth_querykey: 'query', auth_bearer: 'hidden\r\ninjected' } });
  assert.equal(requests.at(-1).headers.authorization, undefined);
  await run('authchoices', 'compound', { base_url: url, auth_requirement: '["Bearer"]' },
    { props: { auth_bearer: 'only-selected' } });
  assert.equal(requests.at(-1).headers['x-api-key'], undefined);

  const shared = { props: { auth_key: 'shared', auth_basic_username: 'alice', auth_basic_password: 'p:ass', auth_oauth: 'manual', auth_bearer: 'bearer' } };
  for (const [auth_requirement, query, authorization] of [
    ['["basic","key"]', '?api_key=shared', `Basic ${Buffer.from('alice:p:ass').toString('base64')}`],
    ['["key","oauth"]', '?api_key=shared', 'Bearer manual'],
    ['["bearer"]', '', 'Bearer bearer'],
  ]) {
    await run('authgroups', 'hello', { base_url: url, auth_requirement }, shared);
    assert.equal(requests.at(-1).url, '/hello' + query);
    assert.equal(requests.at(-1).headers.authorization, authorization);
  }
});

test('AND groups within OR reject partial selections and credentials locally with no fallback', async () => {
  const auth = { props: { auth_headerkey: 'header', auth_querykey: 'query', auth_bearer: 'bearer' } };
  const count = requests.length;
  for (const auth_requirement of [undefined, null, '', 'Bearer', 'HeaderKey', '["HeaderKey"]',
    '["QueryKey","HeaderKey"]', '["HeaderKey", "QueryKey"]', ['HeaderKey', 'QueryKey'], { value: '["Bearer"]' }]) {
    await assert.rejects(run('authchoices', 'compound', { base_url: url, auth_requirement, auth_scheme: 'Bearer' }, auth),
      /^Error: Invalid or missing input: auth_requirement$/);
  }
  for (const props of [{ auth_headerkey: 'header', auth_bearer: 'valid' }, { auth_querykey: 'query', auth_bearer: 'valid' },
    { auth_headerkey: '', auth_querykey: 'query', auth_bearer: 'valid' },
    { auth_headerkey: 'header', auth_querykey: 42, auth_bearer: 'valid' }]) {
    await assert.rejects(run('authchoices', 'compound', { base_url: url, auth_requirement: '["HeaderKey","QueryKey"]' }, { props }),
      /^Error: Missing credentials for this operation$/);
  }
  await assert.rejects(run('authchoices', 'compound', { base_url: url, auth_requirement: '["HeaderKey","QueryKey"]' },
    { props: { ...auth.props, auth_headerkey: 'hidden\r\ninjected' } }), /^Error: Invalid header value$/);
  await assert.rejects(run('authgroups', 'hello', { base_url: url, auth_requirement: '["basic","key"]' },
    { props: { auth_key: 'shared', auth_basic_username: 'alice', auth_oauth: 'valid' } }), /^Error: Missing credentials for this operation$/);
  assert.equal(requests.length, count);
  for (const [suffix, status] of [['fail', 422], ['redirect', 302]]) {
    await assert.rejects(run('authchoices', 'compound', { base_url: `${url}/${suffix}`, auth_requirement: '["HeaderKey","QueryKey"]' }, auth),
      new RegExp(`^Error: HTTP request failed with status ${status}$`));
    assert.equal(requests.at(-1).headers['x-api-key'], 'header');
    assert.equal(requests.at(-1).headers.authorization, undefined);
  }
  assert.equal(requests.length, count + 2);
});

test('oneOf validates exactly one branch, including nullable and nested alternatives, before HTTP', async () => {
  assert.equal(action('alternatives', 'notify').props.body.type, 'OBJECT');
  assert.equal(action('nullable30', 'nullable').props.body.type, 'SHORT_TEXT');
  for (const [name, id, body] of [
    ['alternatives', 'notify', { email: 'a@example.test', reference: 0 }],
    ['alternatives', 'notify', { email: 'a@example.test', reference: null }],
    ['alternatives', 'notify', { sms: '123' }], ['alternatives', 'notify', null],
    ['nullable30', 'nullable', null], ['nullable30', 'nullable', 'ordinary'],
    ['unions', 'numbers', 0], ['unions', 'numbers', -1.5],
    ['unions', 'arrays', [false, null, true]], ['unions', 'overlap', { a: 0 }],
    ['unions', 'arrayunion', ['a']], ['unions', 'arrayunion', [0]], ['unions', 'dictionary', { a: false, b: 'x' }],
  ]) {
    await run(name, id, { base_url: `${url}/v2`, body });
    assert.deepEqual(JSON.parse(requests.at(-1).body), body);
    assert.equal(requests.at(-1).url, `/v2/${id}`);
  }
  const count = requests.length;
  for (const [name, id, body] of [
    ['alternatives', 'notify', {}], ['alternatives', 'notify', { email: '', sms: '123' }],
    ['alternatives', 'notify', { email: 'a', reference: false }], ['alternatives', 'notify', 'hidden'],
    ['alternatives', 'notify', undefined], ['unions', 'overlap', { a: 0, b: 0 }],
    ['unions', 'nullOverlap', null], ['nullable30', 'nullable', 'reserved'],
    ['nullable30', 'nullable', ''], ['unions', 'numbers', -0.5], ['unions', 'arrays', [1]],
    ['unions', 'arrayunion', []], ['unions', 'arrayunion', ['a', 0]], ['unions', 'dictionary', { a: 0 }],
  ]) {
    await assert.rejects(run(name, id.toLowerCase(), { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
});

test('oneOf object wrapper validates alongside exactly one branch before HTTP', async () => {
  assert.equal(action('constrained', 'notify').props.body.type, 'OBJECT');
  for (const body of [{ requestId: 'r1', email: 'a' }, { requestId: 'r2', sms: '123' }]) {
    await run('constrained', 'notify', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/notify');
    assert.deepEqual(JSON.parse(requests.at(-1).body), body);
  }
  const count = requests.length;
  for (const body of [
    { email: 'a' }, { requestId: '', email: 'a' }, { requestId: 1, email: 'a' },
    { requestId: 'r1', email: '' }, { requestId: 'r1', email: 'a', extra: true },
    { requestId: 'r1' }, { requestId: 'r1', email: 'a', sms: '123' },
    null, 'hidden', [], undefined,
  ]) await assert.rejects(run('constrained', 'notify', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
});

test('oneOf string wrapper validates length and exactly one branch before HTTP', async () => {
  assert.equal(action('strings', 'sendstring').props.body.type, 'SHORT_TEXT');
  for (const body of ['abc', 'xyz']) {
    await run('strings', 'sendstring', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/string');
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  for (const body of ['ab', 'def', '', null, undefined, 123, {}, []]) {
    await assert.rejects(run('strings', 'sendstring', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('strings', 'overlap', { base_url: url, body: 'abc' }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
});

test('oneOf string enum wrapper generated-runtime regression', () => {
  const { validate } = require(join(temp, 'build/strings/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: 'string', enum: ['ab', 'abc', 'xyz', 'none'], minLength: 3,
    oneOf: [{ type: 'string', enum: ['ab', 'abc'] }, { type: 'string', enum: ['xyz', 'other'] }],
  });
  for (const value of ['abc', 'xyz']) validate(value, schema, 'body');
  for (const value of ['ab', 'other', 'none']) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf string enum wrapper sends strings unchanged and rejects independent failures before HTTP', async () => {
  const send = action('stringenums', 'sendstring');
  assert.equal(send.props.body.type, 'SHORT_TEXT');
  assert.equal(send.props.body.required, true);
  for (const body of ['abc', 'xyz']) {
    await run('stringenums', 'sendstring', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/string');
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.equal(typeof JSON.parse(requests.at(-1).body), 'string');
  }
  for (const body of ['😀😀', '😀😀😀', 'e\u0301', 'ok']) {
    await run('stringenums', 'unicode', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/unicode');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  for (const body of ['ab', 'other', 'none', 'abc ', '', 123, true, ['abc'], { value: 'abc' }, null, undefined]) {
    await assert.rejects(run('stringenums', 'sendstring', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('stringenums', 'overlap', { base_url: url, body: 'abc' }), /^Error: Invalid or missing input: body$/);
  for (const body of ['😀', '😀😀😀😀', 'no', 'é']) {
    await assert.rejects(run('stringenums', 'unicode', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ stringEnumNoRequestFixture: 'passed', requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf string enum wrapper recurses through properties, items, dictionaries and nested oneOf', async () => {
  const body = { value: 'abc', values: ['xyz', 'abc'], lookup: { a: 'xyz' }, composed: 'abc' };
  const before = JSON.stringify(body);
  for (const composed of ['xyz', false]) {
    const value = { ...body, composed };
    await run('stringenums', 'nested', { base_url: `${url}/v2`, body: value });
    assert.equal(requests.at(-1).body, JSON.stringify(value));
  }
  const count = requests.length;
  for (const bad of ['ab', 'other', 'none']) {
    for (const value of [
      { ...body, value: bad }, { ...body, values: [bad] },
      { ...body, lookup: { a: bad } }, { ...body, composed: bad },
    ]) await assert.rejects(run('stringenums', 'nested', { base_url: url, body: value }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('stringenums', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill('abc') } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(JSON.stringify(body), before);
  assert.equal(requests.length, count);
});

test('oneOf string enum wrapper retains complete branch bounds, null exclusion and shared work limits', () => {
  const { validate } = require(join(temp, 'build/stringenums/src/lib/runtime.js'));
  const wrapper = { type: 'string', enum: ['ab', 'abc', 'xyz', 'none'], minLength: 2, maxLength: 3,
    oneOf: [{ type: 'string', enum: ['ab', 'abc'], minLength: 3 }, { type: 'string', enum: ['xyz'], maxLength: 2 }, { type: 'null' }],
  };
  const before = JSON.stringify(wrapper);
  const schema = normalizeSchema(wrapper);
  validate('abc', schema, 'body');
  for (const value of ['ab', 'xyz', 'none', null]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  assert.deepEqual(schema.oneOf.slice(0, 2).map(b => b.enum), [['ab', 'abc'], ['xyz']]);
  assert.equal(JSON.stringify(wrapper), before);
  // Root, independent wrapper and all three complete branches share five steps.
  validate('abc', schema, 'body', 0, { remaining: 5 });
  assert.throws(() => validate('abc', schema, 'body', 0, { remaining: 4 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf uniqueItems generated-runtime duplicate regression', () => {
  const { validate } = require(join(temp, 'build/arraywrappers/src/lib/runtime.js'));
  // Isolate runtime enforcement from schema admission: startHead rejects the
  // input keyword too. This is the normalized form the new keyword must retain.
  const schema = normalizeSchema({ type: 'array', items: { type: 'integer' },
    oneOf: [{ type: 'array', items: { type: 'integer' } }],
  });
  schema.constraints.uniqueItems = true;
  validate([1, 2], schema, 'body');
  assert.throws(() => validate([1, 1], schema, 'body'), /^Error: Invalid or missing input: body$/);
});

test('oneOf uniqueItems rejects duplicates and preserves independent wrapper/branch validation before HTTP', async () => {
  const send = action('uniquearrays', 'sendunique');
  assert.equal(send.props.body.type, 'ARRAY');
  assert.deepEqual(send.props.body.properties, {});
  for (const body of [[0, 1], [2, 3]]) {
    const snapshot = structuredClone(body);
    await run('uniquearrays', 'sendunique', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/sendunique');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.deepEqual(body, snapshot);
  }
  for (const [id, body] of [['allowduplicates', [0, 0]], ['allowduplicates', [2, 2]], ['branchunique', [2, 2]], ['branchunique', [0, 1]]]) {
    await run('uniquearrays', id, { base_url: url, body });
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  // Duplicate values, wrapper bounds/items, branch bounds/items, zero matches,
  // and multiple matches must all retain their independent rejection behavior.
  for (const body of [[0, 0], [2, 2], [], [-1], [2], [0, 2], [0, 1, 2, 3, 4], ['0'], null, undefined]) {
    await assert.rejects(run('uniquearrays', 'sendunique', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('uniquearrays', 'branchunique', { base_url: url, body: [0, 0] }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('uniquearrays', 'overlap', { base_url: url, body: [0, 1] }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ uniqueItemsNoRequestFixture: 'passed', requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf uniqueItems uses structural JSON equality and sends unique JSON unchanged', async () => {
  for (const body of [
    [0, false, 1, true, '1', null], [0.5, 1.5],
    [{ a: 1, b: [2, 3] }, { b: [3, 2], a: 1 }, [1, 2], [2, 1]],
    [{ a: true }, { a: 1 }, { 'a:b': 'c' }, { a: 'b:c' }],
    ['a,b', 'a', 'b', '[1]', '\u0000', 'null'],
    [Object.assign(Object.create(null), { b: 2, a: 1 })], [],
  ]) {
    const before = JSON.stringify(body);
    await run('uniquearrays', 'jsonvalues', { base_url: url, body });
    assert.equal(requests.at(-1).body, before);
    assert.equal(JSON.stringify(body), before);
  }
  const count = requests.length;
  for (const body of [
    [1, 1], [0, -0], [0.5, 0.5], [true, true], [false, false], ['x', 'x'], [null, null],
    [{ a: 1, b: 2 }, { b: 2, a: 1 }],
    [{ a: { c: [1, 2], d: false }, b: 0 }, { b: 0, a: { d: false, c: [1, 2] } }],
    [[1, 2], [1, 2]], [[], []], [{}, {}],
    [NaN], [Infinity], [-Infinity], [undefined], [1n], [new Date()], [() => 1],
    [{ a: undefined }], [{ a: Infinity }], [Symbol('invalid')], null,
  ]) await assert.rejects(run('uniquearrays', 'jsonvalues', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
});

test('oneOf uniqueItems recurses through properties, items, dictionaries and branches before HTTP', async () => {
  const good = { value: [0, 1], values: [[2, 3]], lookup: { a: [0, 1] }, composed: [2, 3] };
  await run('uniquearrays', 'nested', { base_url: url, body: good });
  assert.equal(requests.at(-1).body, JSON.stringify(good));
  const count = requests.length;
  for (const body of [
    { ...good, value: [0, 0] }, { ...good, values: [[2, 2]] },
    { ...good, lookup: { a: [0, 0] } }, { ...good, composed: [2, 2] },
  ]) await assert.rejects(run('uniquearrays', 'nested', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
});

test('oneOf uniqueItems shares bounded equality work with wrapper, branches and open JSON traversal', async () => {
  const { validate } = require(join(temp, 'build/uniquearrays/src/lib/runtime.js'));
  const items = { type: 'integer' };
  const schema = normalizeSchema({ type: 'array', items, uniqueItems: true, oneOf: [{ type: 'array', items }] });
  // root + wrapper + 2 items + 2 equality visits + branch + 2 items = 9.
  validate([1, 2], schema, 'body', 0, { remaining: 9 });
  assert.throws(() => validate([1, 2], schema, 'body', 0, { remaining: 8 }), /^Error: Input validation limit exceeded$/);
  const branch = normalizeSchema({ oneOf: [{ type: 'array', items, uniqueItems: true }, { type: 'null' }] });
  assert.throws(() => validate([1, 2], branch, 'body', 0, { remaining: 5 }), /^Error: Input validation limit exceeded$/);
  const count = requests.length;
  for (const body of [
    ['x'.repeat(100001)], [{ payload: Array(100001).fill(0) }],
    [{ ['a'.repeat(40000)]: 1, ['b'.repeat(40000)]: 2 }],
  ]) await assert.rejects(run('uniquearrays', 'jsonvalues', { base_url: url, body }), /^Error: Input validation limit exceeded$/);
  let deep = 0;
  for (let i = 0; i < 42; i++) deep = { child: deep };
  const cyclic = {}; cyclic.child = cyclic;
  for (const body of [[deep], [cyclic]]) {
    await assert.rejects(run('uniquearrays', 'jsonvalues', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
});

test('oneOf array wrapper validates items, bounds and exactly one branch before HTTP', async () => {
  const send = action('arraywrappers', 'sendarray');
  assert.equal(send.props.body.type, 'ARRAY');
  assert.deepEqual(send.props.body.properties, {});
  for (const [id, bodies] of [
    ['sendarray', [[0, 1], [2, 2]]], ['independent', [[0, 0], [1]]],
  ]) for (const body of bodies) {
    const snapshot = structuredClone(body);
    await run('arraywrappers', id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${id === 'sendarray' ? 'array' : id}`);
    assert.equal(requests.at(-1).method, 'POST');
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.deepEqual(body, snapshot);
  }
  const count = requests.length;
  for (const body of [[1], [], [0, 2], [-1, -1], [1.5, 1.5], ['1', '1'], null, undefined, '[]', 1, {}]) {
    await assert.rejects(run('arraywrappers', 'sendarray', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  for (const body of [[], [-1], [0], [0, 0, 0], [1, 1], [0, 1], [0, 0, 0, 0]]) {
    await assert.rejects(run('arraywrappers', 'independent', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('arraywrappers', 'overlap', { base_url: url, body: [0, 1] }), /^Error: Invalid or missing input: body$/);
  assert.equal(requests.length, count);
});

test('oneOf array wrapper recurses through properties, items, dictionaries and nested branches before HTTP', async () => {
  const good = { value: [0, 1], values: [[2, 2]], lookup: { a: [0, 0] }, composed: [2, 2] };
  const snapshot = structuredClone(good);
  for (const body of [good, { ...good, composed: [4, 4] }]) {
    await run('arraywrappers', 'nested', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  for (const body of [
    { ...good, value: [1] }, { ...good, values: [[2, 2], [2]] }, { ...good, lookup: { a: [0, 2] } },
    { ...good, composed: [4] }, { ...good, composed: [4, 4, 4, 4] }, { ...good, composed: [0, 4] },
  ]) await assert.rejects(run('arraywrappers', 'nested', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.deepEqual(good, snapshot);
  assert.equal(requests.length, count);
});

test('oneOf array wrapper keeps wrapper item constraints, zero/max bounds, null exclusion and shared budget', () => {
  const { validate } = require(join(temp, 'build/arraywrappers/src/lib/runtime.js'));
  const branch = { type: 'array', items: { type: 'integer' } };
  const schema = normalizeSchema({ type: 'array', minItems: 1, maxItems: 2,
    items: { type: 'integer', minimum: 0 }, oneOf: [branch, { type: 'null' }],
  });
  for (const body of [[0], [1, 2]]) validate(body, schema, 'body');
  // [-1] matches the unrestricted branch but fails the independent wrapper item.
  for (const body of [[-1], [], [0, 1, 2], null]) {
    assert.throws(() => validate(body, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  const emptyOnly = normalizeSchema({ type: 'array', minItems: 0, maxItems: 0,
    items: { type: 'integer' }, oneOf: [branch],
  });
  validate([], emptyOnly, 'body');
  assert.throws(() => validate([0], emptyOnly, 'body'), /Invalid or missing input: body/);
  // Root + wrapper + wrapper item + branch + branch item all consume the same budget.
  assert.throws(() => validate([0], schema, 'body', 0, { remaining: 4 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf numeric enum wrapper validates the wrapper and exactly one branch before HTTP', async () => {
  for (const id of ['sendnumber', 'sendinteger', 'fractional']) {
    assert.equal(action('numericenums', id).props.body.type, 'NUMBER');
    assert.equal(action('numericenums', id).props.body.required, true);
  }
  for (const [id, values, path] of [
    ['sendnumber', [1, 2], 'number'], ['sendinteger', [1, 2], 'integer'], ['fractional', [0.5, 1.5], 'fractional'],
  ]) for (const body of values) {
    await run('numericenums', id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${path}`);
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.equal(typeof JSON.parse(requests.at(-1).body), 'number');
  }
  const count = requests.length;
  // 0/3 match branches, 4 passes the wrapper only; each must fail locally.
  for (const body of [0, 3, 4, -1, 1.5, '1', false, null, undefined, NaN, Infinity, -Infinity, {}, []]) {
    await assert.rejects(run('numericenums', 'sendnumber', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('numericenums', 'overlap', { base_url: url, body: 1 }), /^Error: Invalid or missing input: body$/);
  for (const body of [0, 1.5, 4]) {
    await assert.rejects(run('numericenums', 'sendinteger', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  for (const body of [0, 1, 2, 2.5, '0.5']) {
    await assert.rejects(run('numericenums', 'fractional', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
});

test('oneOf numeric enum wrapper recurses without changing input or merging branch enums', async () => {
  const good = { value: 1, values: [1, 2], lookup: { a: 2 }, composed: 2 };
  for (const body of [good, { ...good, composed: 6 }]) {
    await run('numericenums', 'nested', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.deepEqual(JSON.parse(requests.at(-1).body), body);
  }
  const count = requests.length;
  for (const body of [
    { ...good, value: 0 }, { ...good, value: 4 }, { ...good, values: [1, 3] },
    { ...good, lookup: { a: 4 } }, { ...good, composed: 1 }, { ...good, composed: 4 },
  ]) await assert.rejects(run('numericenums', 'nested', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.deepEqual(good, { value: 1, values: [1, 2], lookup: { a: 2 }, composed: 2 });
  assert.equal(requests.length, count);

  const { validate } = require(join(temp, 'build/numericenums/src/lib/runtime.js'));
  const wrapper = { type: 'number', enum: [1, 2, 4], oneOf: [
    { type: 'number', enum: [0, 1] }, { type: 'number', enum: [2, 3] }, { type: 'null' },
  ] };
  const before = JSON.stringify(wrapper);
  const normalized = normalizeSchema(wrapper);
  for (const value of [1, 2]) validate(value, normalized, 'body');
  for (const value of [0, 3, 4, null]) assert.throws(() => validate(value, normalized, 'body'), /Invalid or missing input: body/);
  // A wrapper enum must not overwrite a branch enum, changing zero/one/two matches.
  assert.deepEqual(normalized.oneOf.slice(0, 2).map(b => b.enum), [[0, 1], [2, 3]]);
  assert.equal(JSON.stringify(wrapper), before);
  assert.throws(() => validate(1, normalized, 'body', 0, { remaining: 2 }), /^Error: Input validation limit exceeded$/);
  const independent = normalizeSchema({ type: 'number', enum: [0.5, 1, 2], minimum: 0, maximum: 2,
    oneOf: [{ type: 'number', enum: [0.5, 1], maximum: 0.5 }, { type: 'number', enum: [1, 2], minimum: 1 }],
  });
  for (const value of [0.5, 1, 2]) validate(value, independent, 'body');
});

test('oneOf numeric wrapper validates bounds and exactly one branch before HTTP', async () => {
  assert.equal(action('numeric', 'sendnumber').props.body.type, 'NUMBER');
  assert.equal(action('numeric', 'sendinteger').props.body.type, 'NUMBER');
  for (const [name, id, bodies] of [
    ['numeric', 'sendnumber', [1, 2]], ['numeric', 'sendinteger', [1, 2]],
    ['numeric30', 'bounds', [0.5, 2.5]], ['numeric31', 'bounds', [0.5, 2.5]],
  ]) for (const body of bodies) {
    await run(name, id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${id === 'sendnumber' ? 'number' : id === 'sendinteger' ? 'integer' : id}`);
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.equal(typeof JSON.parse(requests.at(-1).body), 'number');
  }
  const count = requests.length;
  for (const body of [0, 3, -1, 1.5, '1', false, null, undefined, NaN, Infinity, -Infinity, {}, []]) {
    await assert.rejects(run('numeric', 'sendnumber', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  await assert.rejects(run('numeric', 'overlap', { base_url: url, body: 1 }), /^Error: Invalid or missing input: body$/);
  for (const body of [0, 3, 1.5]) {
    await assert.rejects(run('numeric', 'sendinteger', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  for (const name of ['numeric30', 'numeric31']) for (const body of [0, 3, -0.5, 3.5]) {
    await assert.rejects(run(name, 'bounds', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
});

test('oneOf numeric wrapper recurses through properties, items, dictionaries and branches before HTTP', async () => {
  const good = { value: 1, values: [1, 2], lookup: { a: 2 }, composed: 2 };
  for (const body of [good, { ...good, composed: 4 }]) {
    await run('numeric', 'nested', { base_url: `${url}/v2`, body });
    assert.deepEqual(JSON.parse(requests.at(-1).body), body);
    assert.equal(requests.at(-1).url, '/v2/nested');
  }
  const count = requests.length;
  for (const body of [
    { ...good, value: 0 }, { ...good, values: [1, 0] }, { ...good, lookup: { a: 0 } },
    { ...good, composed: 1 }, { ...good, composed: 3 },
  ]) await assert.rejects(run('numeric', 'nested', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  assert.deepEqual(good, { value: 1, values: [1, 2], lookup: { a: 2 }, composed: 2 });
  assert.equal(requests.length, count);
});

test('oneOf numeric wrapper keeps inclusive bounds, branch constraints, null exclusion and shared budget', () => {
  const { validate } = require(join(temp, 'build/numeric/src/lib/runtime.js'));
  const normalized = normalizeSchema({ type: 'number', minimum: 0, maximum: 2, oneOf: [
    { type: 'number', minimum: 1 }, { type: 'number', enum: [0] }, { type: 'null' },
  ] });
  // 0 must match only the enum branch; copying wrapper minimum over the first
  // branch would create a second match. Bounds are inclusive and independent.
  for (const value of [0, 1, 1.5, 2]) validate(value, normalized, 'body');
  for (const value of [-1, 3, null]) assert.throws(() => validate(value, normalized, 'body'), /Invalid or missing input: body/);
  assert.throws(() => validate(1, normalized, 'body', 0, { remaining: 2 }), /^Error: Input validation limit exceeded$/);
  const nonexclusive = normalizeSchema({ type: 'number', minimum: 0, maximum: 2,
    exclusiveMinimum: false, exclusiveMaximum: false, oneOf: [{ type: 'number' }] });
  for (const value of [0, 2]) validate(value, nonexclusive, 'body');
});

test('oneOf string wrapper bounds recurse, count Unicode and preserve match independence', () => {
  const { validate } = require(join(temp, 'build/strings/src/lib/runtime.js'));
  const wrapper = { type: 'string', minLength: 2, maxLength: 3, oneOf: [
    { type: 'string', enum: ['a', 'ab', 'abcd', '😀😀'] }, { type: 'string', enum: ['xyz'] },
    { type: 'null' },
  ] };
  for (const [schema, good, bad] of [
    [wrapper, '😀😀', 'abcd'],
    [{ type: 'object', properties: { value: wrapper }, required: ['value'] }, { value: 'ab' }, { value: 'a' }],
    [{ type: 'array', items: wrapper }, ['xyz'], ['abcd']],
    [{ type: 'object', additionalProperties: wrapper }, { a: '😀😀' }, { a: null }],
  ]) {
    const normalized = normalizeSchema(schema);
    validate(good, normalized, 'body');
    assert.throws(() => validate(bad, normalized, 'body'), /^Error: Invalid or missing input: body$/);
  }
  for (const value of ['a', 'abcd', null]) assert.throws(() => validate(value, normalizeSchema(wrapper), 'body'), /Invalid or missing input: body/);
  // minLength belongs to the wrapper; it must not be merged over branch bounds.
  const branchBound = normalizeSchema({ type: 'string', minLength: 3, oneOf: [
    { type: 'string', minLength: 4 }, { type: 'string', enum: ['abc'] },
  ] });
  validate('abc', branchBound, 'body');
  assert.throws(() => validate('abc', branchBound, 'body', 0, { remaining: 1 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf wrapper intersections recurse without merging branch object constraints', () => {
  const { validate } = require(join(temp, 'build/constrained/src/lib/runtime.js'));
  const wrapper = configs.constrained.paths['/notify'].post.requestBody.content['application/json'].schema;
  const valid = { requestId: 'r1', email: 'a' }, invalid = { email: 'a' };
  for (const [schema, good, bad] of [
    [{ type: 'object', properties: { contact: wrapper } }, { contact: valid }, { contact: invalid }],
    [{ type: 'array', items: wrapper }, [valid], [invalid]],
    [{ type: 'object', additionalProperties: wrapper }, { a: valid }, { a: invalid }],
  ]) {
    const normalized = normalizeSchema(schema);
    validate(good, normalized, 'body');
    assert.throws(() => validate(bad, normalized, 'body'), /^Error: Invalid or missing input: body$/);
  }
  // additionalProperties belongs to its own schema, not a merged set of fields.
  const closedBranch = normalizeSchema({ ...wrapper, oneOf: [
    { type: 'object', properties: { email: { type: 'string' } }, additionalProperties: false },
  ] });
  assert.throws(() => validate(valid, closedBranch, 'body'), /Invalid or missing input: body/);
  const closedWrapper = normalizeSchema({ type: 'object', additionalProperties: false,
    oneOf: [{ type: 'object', properties: { email: { type: 'string' } } }] });
  assert.throws(() => validate({ email: 'a' }, closedWrapper, 'body'), /Invalid or missing input: body/);
  // A wrapper's property constraint must not select a branch or change match counts.
  const overlap = normalizeSchema({ type: 'object', properties: { a: { type: 'string' } },
    oneOf: [{ type: 'object' }, { type: 'object', properties: { a: { type: 'number' } } }] });
  assert.throws(() => validate({}, overlap, 'body'), /Invalid or missing input: body/);
  const typedExtras = normalizeSchema({ type: 'object', additionalProperties: { type: 'integer' },
    oneOf: [{ type: 'object', required: ['n'] }] });
  validate({ n: 0 }, typedExtras, 'body');
  assert.throws(() => validate({ n: '0' }, typedExtras, 'body'), /Invalid or missing input: body/);
  // The same work budget covers both the wrapper and the alternatives.
  assert.throws(() => validate(valid, normalizeSchema(wrapper), 'body', 0, { remaining: 2 }), /^Error: Input validation limit exceeded$/);
});

test('alternative validation bounds work and cannot mistake a work-limit failure for a branch mismatch', () => {
  const { validate } = require(join(temp, 'build/unions/src/lib/runtime.js'));
  const schema = { type: 'oneOf', nullable: false, oneOf: [
    { type: 'array', nullable: false, items: { type: 'string', nullable: false } },
    { type: 'array', nullable: false, items: { type: 'string', nullable: false } },
  ] };
  assert.throws(() => validate(['a', 'b'], schema, 'body', 0, { remaining: 2 }), /^Error: Input validation limit exceeded$/);
});

test('input/credential failures occur before networking, with no secret values in errors', async () => {
  const count = requests.length;
  const invalid = [
    ['petstore', 'getpet', { base_url: url }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '..' }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '1', query_limit: 0.5 }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '1', query_limit: 101 }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '1', query_verbose: 'false' }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '1', header_x_trace: 'hidden\r\nX-Evil: injected' }, { secret_text: 'hidden' }],
    ['petstore', 'getpet', { base_url: url, path_petid: '1' }, undefined],
    ['petstore', 'getpet', { base_url: url, path_petid: '1' }, { secret_text: 'hidden\nvalue' }],
    ['basic', 'hello', { base_url: url }, { username: 'a:b', password: 'hidden' }],
    ['custom', 'hello', { base_url: url }, { props: { auth_key: 'hidden' } }],
  ];
  for (const body of [{ name: 'hidden' }, { name: 'hidden', active: true, kind: 'fish' },
    { name: 'hidden', active: true, metadata: { x: 3 } }, { name: 'hidden', active: true, unexpected: true },
    { name: 'hidden', active: true, labels: [1] }, new Date()]) {
    invalid.push(['petstore', 'updatepet', { base_url: url, path_petid: '1', body }, { secret_text: 'hidden' }]);
  }
  for (const [name, id, props, auth] of invalid) {
    await assert.rejects(run(name, id, props, auth), e => { assert.doesNotMatch(e.message, /hidden|injected/); return true; });
  }
  assert.equal(requests.length, count);
});

test('unsafe URLs, no redirects, status-only HTTP errors, TLS verification and environment restoration', async () => {
  const count = requests.length;
  for (const base_url of ['file:///etc/passwd', 'http://user:hidden@localhost', `${url}?token=hidden`, `${url}#hidden`, '/relative']) {
    await assert.rejects(run('none', 'hello', { base_url }), /URL|base/);
  }
  assert.equal(requests.length, count);
  await assert.rejects(run('none', 'hello', { base_url: `${url}/redirect` }), /^Error: HTTP request failed with status 302$/);
  assert.equal(requests.length, count + 1);
  const logs = [];
  const previousError = console.error;
  const before = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  console.error = (...args) => logs.push(args);
  try {
    await assert.rejects(run('none', 'hello', { base_url: `${url}/fail` }), /^Error: HTTP request failed with status 422$/);
    await assert.rejects(run('none', 'hello', { base_url: secureUrl }), /^Error: HTTP transport failed$/);
  } finally { console.error = previousError; }
  assert.deepEqual(logs, []);
  assert.equal(process.env.NODE_TLS_REJECT_UNAUTHORIZED, before);
});

test('upstream environment proxy settings cannot reroute generated requests', async () => {
  const saved = { HTTP_PROXY: process.env.HTTP_PROXY, HTTPS_PROXY: process.env.HTTPS_PROXY, NO_PROXY: process.env.NO_PROXY };
  try {
    process.env.HTTP_PROXY = 'http://127.0.0.1:1';
    process.env.HTTPS_PROXY = 'http://127.0.0.1:1';
    process.env.NO_PROXY = '';
    await run('none', 'hello', { base_url: url });
    assert.equal(requests.at(-1).url, '/hello');
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('oneOf boolean enum wrapper generated-runtime regression', () => {
  // Existing compiled runtime isolates the new schema admission regression.
  const { validate } = require(join(temp, 'build/strings/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: 'boolean', enum: [true],
    oneOf: [{ type: 'boolean', enum: [true] }, { type: 'boolean', enum: [false] }],
  });
  validate(true, schema, 'body');
  for (const value of [false, 'true', 'false', 0, 1, null, undefined]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf boolean enum wrapper sends required true and false unchanged before rejecting invalid inputs without HTTP', async () => {
  for (const [id, body] of [['sendtrue', true], ['sendfalse', false], ['zero', true], ['typeonly', true], ['typeonly', false]]) {
    const send = action('booleanenums', id);
    assert.equal(send.props.body.type, 'CHECKBOX');
    assert.equal(send.props.body.required, true);
    await run('booleanenums', id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${{ sendtrue: 'sendTrue', sendfalse: 'sendFalse', typeonly: 'typeOnly' }[id] ?? id}`);
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
    assert.equal(typeof JSON.parse(requests.at(-1).body), 'boolean');
  }
  const count = requests.length;
  for (const id of ['sendtrue', 'sendfalse', 'typeonly']) {
    for (const body of ['true', 'false', '', 0, 1, null, undefined, {}, []]) {
      await assert.rejects(run('booleanenums', id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
    }
    await assert.rejects(run('booleanenums', id, { base_url: url }), /^Error: Invalid or missing input: body$/);
  }
  for (const [id, body] of [['sendtrue', false], ['sendfalse', true], ['zero', false], ['overlap', true], ['overlap', false]]) {
    await assert.rejects(run('booleanenums', id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
  }
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ booleanEnumNoRequestFixture: 'passed', requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf boolean enum wrapper recurses through properties, items, dictionaries and nested oneOf before HTTP', async () => {
  const body = { value: true, values: [true, false], lookup: { a: false }, composed: true };
  const before = JSON.stringify(body);
  for (const composed of [true, false]) {
    const value = { ...body, composed };
    await run('booleanenums', 'nested', { base_url: `${url}/v2`, body: value });
    assert.equal(requests.at(-1).body, JSON.stringify(value));
    assert.equal(requests.at(-1).url, '/v2/nested');
  }
  const count = requests.length;
  for (const value of [
    { ...body, value: false }, { ...body, values: ['false'] }, { ...body, lookup: { a: true } },
    { ...body, composed: 'true' }, { ...body, composed: null }, { ...body, value: undefined },
  ]) await assert.rejects(run('booleanenums', 'nested', { base_url: url, body: value }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('booleanenums', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill(false) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(JSON.stringify(body), before);
  assert.equal(requests.length, count);
});

test('oneOf boolean enum wrapper validates wrapper first, complete branches, null exclusion and shared work', () => {
  const { validate } = require(join(temp, 'build/booleanenums/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: 'boolean', enum: [true],
    oneOf: [{ type: 'boolean', enum: [true] }, { type: 'boolean', enum: [false] }],
  });
  validate(true, schema, 'body', 0, { remaining: 4 });
  assert.throws(() => validate(true, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
  // Two steps cover root + wrapper only: a rejected wrapper must not visit branches.
  assert.throws(() => validate(false, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
  assert.deepEqual(schema.constraints.enum, [true]);
  assert.deepEqual(schema.oneOf.map(b => b.enum), [[true], [false]]);
  const disjoint = normalizeSchema({ type: 'boolean', enum: [false], oneOf: [{ type: 'boolean', enum: [true] }] });
  for (const value of [true, false]) assert.throws(() => validate(value, disjoint, 'body'), /^Error: Invalid or missing input: body$/);
  const nested = normalizeSchema({ type: 'boolean', enum: [true, false], oneOf: [
    { type: 'boolean', enum: [true], oneOf: [{ type: 'boolean', enum: [false] }] },
    { type: 'boolean', enum: [false] },
  ] });
  validate(false, nested, 'body');
  assert.throws(() => validate(true, nested, 'body'), /^Error: Invalid or missing input: body$/);
  for (const branch of [{ type: 'null' }, { type: 'boolean', nullable: true }, { type: ['boolean', 'null'] }]) {
    const constrained = normalizeSchema({ type: 'boolean', oneOf: [branch] });
    assert.throws(() => validate(null, constrained, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf nullable boolean wrapper generated-runtime regression', () => {
  // Compile an existing supported piece first to isolate schema admission on prior source.
  const { validate } = require(join(temp, 'build/strings/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: ['boolean', 'null'], enum: [true, null],
    oneOf: [{ type: 'boolean', enum: [true] }, { type: 'null' }],
  });
  for (const value of [true, null]) validate(value, schema, 'body', 0, { remaining: 4 });
  for (const value of [false, 'true', 'null', 0, 1, undefined, {}, []]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  // Root + wrapper consume two steps. A failed enum must stop before branches.
  assert.throws(() => validate(false, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
  assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf nullable boolean wrapper sends true null and permitted required false unchanged; negatives add no HTTP', async () => {
  for (const [id, body] of [
    ['send31', true], ['send31', null], ['send30', true], ['send30', null],
    ['falseallowed', false], ['falseallowed', null], ['falseallowed', true], ['zero', true],
    ['enumexcludes', true], ['composed', true], ['composed', null], ['overlap', true],
  ]) {
    const send = action('nullablebooleans', id);
    assert.equal(send.props.body.type, 'CHECKBOX');
    assert.equal(send.props.body.required, true);
    await run('nullablebooleans', id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${{ falseallowed: 'falseAllowed', enumexcludes: 'enumExcludes' }[id] ?? id}`);
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  for (const body of [true, null]) {
    await run('nullablebooleans30', 'send30', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  let negatives = 0;
  for (const body of [false, 'true', 'null', 1, undefined]) {
    await assert.rejects(run('nullablebooleans30', 'send30', { base_url: url, body }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  for (const id of ['send31', 'send30', 'falseallowed', 'zero', 'enumexcludes', 'composed']) {
    for (const body of ['true', 'false', 'null', 0, 1, undefined, {}, []]) {
      await assert.rejects(run('nullablebooleans', id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
      negatives++;
    }
    await assert.rejects(run('nullablebooleans', id, { base_url: url }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  for (const [id, body] of [
    ['send31', false], ['send30', false], ['zero', null], ['zero', false],
    ['overlap', null], ['overlap', false], ['enumexcludes', null], ['composed', false],
  ]) {
    await assert.rejects(run('nullablebooleans', id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ nullableBooleanNoRequestFixture: 'passed', negatives, requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf nullable boolean wrapper validates nested fields items dictionaries and complete inner branches before HTTP', async () => {
  const body = { value: null, values: [true, false, null], lookup: { a: null, b: true }, composed: null };
  const before = JSON.stringify(body);
  for (const composed of [true, null]) {
    const value = { ...body, composed };
    await run('nullablebooleans', 'nested', { base_url: `${url}/v2`, body: value });
    assert.equal(requests.at(-1).url, '/v2/nested');
    assert.equal(requests.at(-1).body, JSON.stringify(value));
  }
  const count = requests.length;
  for (const value of [
    { ...body, value: false }, { ...body, values: ['null'] }, { ...body, lookup: { a: false } },
    { ...body, composed: false }, { ...body, composed: 'true' }, { ...body, value: undefined },
  ]) await assert.rejects(run('nullablebooleans', 'nested', { base_url: url, body: value }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('nullablebooleans', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill(null) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  assert.equal(JSON.stringify(body), before);
});

test('oneOf nullable boolean wrapper preserves wrapper-first enums complete null matches and shared work', () => {
  const { validate } = require(join(temp, 'build/nullablebooleans/src/lib/runtime.js'));
  for (const wrapper of [{ type: 'boolean', nullable: true }, { type: ['boolean', 'null'] }, { type: ['null', 'boolean'] }]) {
    const branches = [{ type: 'boolean', enum: [true] }, { type: 'null' }];
    const raw = { ...wrapper, enum: [true, null], oneOf: branches };
    const before = JSON.stringify(raw);
    const schema = normalizeSchema(raw);
    for (const value of [true, null]) validate(value, schema, 'body', 0, { remaining: 4 });
    assert.throws(() => validate(false, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
    assert.throws(() => validate(null, schema, 'body', 41), /^Error: Invalid or missing input: body$/);
    assert.deepEqual(schema.constraints.enum, [true, null]);
    assert.deepEqual(schema.oneOf.map(b => b.enum), [[true], undefined]);
    assert.equal(JSON.stringify(raw), before);
    for (const oneOf of [
      [{ type: 'boolean' }], // Nullable wrapper cannot grant null to a branch.
      [{ type: 'boolean', nullable: true, enum: [true] }], // Branch enum independently excludes null.
      [{ type: 'boolean', nullable: true }, { type: 'null' }],
      [{ type: 'null' }, { type: 'null' }],
      [{ ...wrapper, oneOf: [{ type: 'boolean' }] }], // Complete nested branch, zero null matches.
      [{ ...wrapper, oneOf: [{ type: 'null' }, { type: 'null' }] }], // Complete nested branch, duplicate null matches.
    ]) assert.throws(() => validate(null, normalizeSchema({ ...wrapper, oneOf }), 'body'), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, normalizeSchema({ ...wrapper, enum: [true], oneOf: branches }), 'body', 0, { remaining: 2 }),
      /^Error: Invalid or missing input: body$/);
  }
  // nullable: false keeps ordinary explicit boolean semantics.
  const nonnull = normalizeSchema({ type: 'boolean', nullable: false, oneOf: [{ type: 'boolean' }, { type: 'null' }] });
  validate(false, nonnull, 'body');
  assert.throws(() => validate(null, nonnull, 'body'), /^Error: Invalid or missing input: body$/);
});

test('oneOf nullable string wrapper generated-runtime regression', () => {
  // Use an existing compiled piece so this fails at admission on the prior source.
  const { validate } = require(join(temp, 'build/strings/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: ['string', 'null'], enum: ['ab', 'abc', null], minLength: 3,
    oneOf: [{ type: 'string', enum: ['ab', 'abc'] }, { type: 'null' }],
  });
  for (const value of ['abc', null]) validate(value, schema, 'body', 0, { remaining: 4 });
  for (const value of ['ab', 'other', '', 0, true, false, undefined, {}, []]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  for (const value of ['ab', 'other']) {
    assert.throws(() => validate(value, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
  }
  assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf nullable string wrapper sends unchanged abc null Unicode and required empty string; negatives add no HTTP', async () => {
  const cases = [
    ['send31', 'abc'], ['send31', null], ['send30', 'abc'], ['send30', null],
    ['empty', ''], ['empty', null], ['zero', 'abc'], ['overlap', 'abc'], ['enumexcludes', 'abc'],
    ['independent', 'bb'], ['independent', 'ccc'], ['independent', null],
    ['unicode', '😀😀'], ['unicode', '😀😀😀'], ['unicode', 'e\u0301'], ['unicode', null],
    ['composed', 'abc'], ['composed', null],
  ];
  for (const [id, body] of cases) {
    const send = action('nullablestrings', id);
    assert.equal(send.props.body.type, 'SHORT_TEXT');
    assert.equal(send.props.body.required, true);
    await run('nullablestrings', id, { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).url, `/v2/${id === 'enumexcludes' ? 'enumExcludes' : id}`);
    assert.equal(requests.at(-1).headers['content-type'], 'application/json');
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  for (const body of ['abc', null]) {
    await run('nullablestrings30', 'send30', { base_url: `${url}/v2`, body });
    assert.equal(requests.at(-1).body, JSON.stringify(body));
  }
  const count = requests.length;
  let negatives = 0;
  const reject = async (piece, id, body) => {
    await assert.rejects(run(piece, id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
    negatives++;
  };
  for (const id of ['send31', 'send30', 'empty', 'zero', 'enumexcludes', 'composed', 'unicode', 'independent']) {
    for (const body of [0, 1, true, false, undefined, {}, []]) await reject('nullablestrings', id, body);
    await assert.rejects(run('nullablestrings', id, { base_url: url }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  for (const [id, body] of [
    ['send31', 'ab'], ['send31', 'other'], ['send31', 'null'], ['send31', ''],
    ['send30', 'ab'], ['send30', 'other'], ['empty', 'a'], ['zero', null],
    ['overlap', null], ['enumexcludes', null], ['composed', 'ab'],
    ['independent', 'a'], ['independent', 'dddd'], ['independent', 'other'],
    ['unicode', '😀'], ['unicode', '😀😀😀😀'], ['unicode', ''],
  ]) await reject('nullablestrings', id, body);
  for (const body of ['ab', 'other', 0, undefined]) await reject('nullablestrings30', 'send30', body);
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ nullableStringNoRequestFixture: 'passed', negatives, requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf nullable string wrapper validates nested properties items dictionaries and complete inner branches before HTTP', async () => {
  const body = { value: null, values: ['abc', null], lookup: { a: null, b: 'abc' }, composed: null };
  const before = JSON.stringify(body);
  for (const composed of ['abc', null]) {
    const value = { ...body, composed };
    await run('nullablestrings', 'nested', { base_url: `${url}/v2`, body: value });
    assert.equal(requests.at(-1).body, JSON.stringify(value));
    assert.equal(requests.at(-1).url, '/v2/nested');
  }
  const count = requests.length;
  for (const value of [
    { ...body, value: 'ab' }, { ...body, values: ['other'] }, { ...body, lookup: { a: 'ab' } },
    { ...body, composed: 'ab' }, { ...body, composed: 0 }, { ...body, value: undefined },
  ]) await assert.rejects(run('nullablestrings', 'nested', { base_url: url, body: value }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('nullablestrings', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill(null) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  assert.equal(JSON.stringify(body), before);
});

test('oneOf nullable string wrapper keeps enum length and every complete branch independent within work and depth limits', () => {
  const { validate } = require(join(temp, 'build/nullablestrings/src/lib/runtime.js'));
  for (const wrapper of [{ type: 'string', nullable: true }, { type: ['string', 'null'] }, { type: ['null', 'string'] }]) {
    const branches = [{ type: 'string', enum: ['ab', 'abc'] }, { type: 'null' }];
    const raw = { ...wrapper, minLength: 3, maxLength: 3, enum: ['ab', 'abc', null], oneOf: branches };
    const before = JSON.stringify(raw);
    const schema = normalizeSchema(raw);
    for (const value of ['abc', null]) validate(value, schema, 'body', 0, { remaining: 4 });
    for (const value of ['ab', 'other']) assert.throws(() => validate(value, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
    validate(null, schema, 'body', 39);
    assert.throws(() => validate(null, schema, 'body', 40), /^Error: Invalid or missing input: body$/);
    assert.equal(JSON.stringify(raw), before);
    for (const oneOf of [
      [{ type: 'string' }],
      [{ type: 'string', nullable: true, enum: ['abc'] }],
      [{ type: 'string', nullable: true }, { type: 'null' }],
      [{ type: 'null' }, { type: 'null' }],
      [{ ...wrapper, oneOf: [{ type: 'string' }] }],
      [{ ...wrapper, oneOf: [{ type: 'null' }, { type: 'null' }] }],
    ]) assert.throws(() => validate(null, normalizeSchema({ ...wrapper, oneOf }), 'body'), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, normalizeSchema({ ...wrapper, enum: ['abc'], oneOf: branches }), 'body', 0, { remaining: 2 }),
      /^Error: Invalid or missing input: body$/);
    const independent = normalizeSchema({ ...wrapper, minLength: 1, maxLength: 4, oneOf: [
      { type: 'string', enum: ['a', 'bb'], minLength: 2 }, { type: 'string', enum: ['ccc', 'dddd'], maxLength: 3 }, { type: 'null' },
    ] });
    for (const value of ['bb', 'ccc', null]) validate(value, independent, 'body');
    for (const value of ['a', 'dddd']) assert.throws(() => validate(value, independent, 'body'), /^Error: Invalid or missing input: body$/);
    const emptyOverlap = normalizeSchema({ ...wrapper, minLength: 0, maxLength: 0, enum: ['', null], oneOf: [
      { type: 'string', enum: [''] }, { type: 'string', enum: [''] }, { type: 'null' },
    ] });
    assert.throws(() => validate('', emptyOverlap, 'body'), /^Error: Invalid or missing input: body$/);
    validate(null, emptyOverlap, 'body');
  }
  const nonnull = normalizeSchema({ type: 'string', nullable: false, oneOf: [{ type: 'string' }, { type: 'null' }] });
  validate('', nonnull, 'body');
  assert.throws(() => validate(null, nonnull, 'body'), /^Error: Invalid or missing input: body$/);
});

test('oneOf nullable numeric wrapper generated-runtime regression', () => {
  // Existing compiled numeric piece keeps the prior-source failure at schema admission.
  const { validate } = require(join(temp, 'build/numeric/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: ['number', 'null'], enum: [0, 1, null], minimum: 1,
    oneOf: [{ type: 'number', enum: [0, 1] }, { type: 'null' }],
  });
  for (const value of [1, null]) validate(value, schema, 'body', 0, { remaining: 4 });
  for (const value of [0, 2, '1', 'null', true, false, NaN, Infinity, -Infinity, undefined, {}, []]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  for (const value of [0, 2]) {
    assert.throws(() => validate(value, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
  }
  assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf nullable numeric wrapper sends unchanged numbers null and required zero; negatives add no HTTP', async () => {
  const cases = [
    ['nullablenumbers', 'send31', [1, null]], ['nullablenumbers', 'send30', [1, null]],
    ['nullablenumbers30', 'send30', [1, null]], ['nullablenumbers30', 'integer30', [1, 2, null]],
    ['nullablenumbers', 'integer30', [1, 2, null]], ['nullablenumbers', 'integer31', [1, 2, null]],
    ['nullablenumbers', 'zeroallowed', [0, null]], ['nullablenumbers', 'fractional', [0.5, 1.5, null]],
    ['nullablenumbers', 'independent', [1, 2, null]], ['nullablenumbers', 'composed', [1, null]],
    ['nullablenumbers', 'overlap', [1]],
  ];
  for (const [name, id, values] of cases) {
    assert.equal(action(name, id).props.body.type, 'NUMBER');
    assert.equal(action(name, id).props.body.required, true);
    for (const body of values) {
      assert.equal((await run(name, id, { base_url: `${url}/v2`, body })).ok, true);
      assert.equal(requests.at(-1).url, `/v2/${id}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, JSON.stringify(body));
    }
  }
  const count = requests.length;
  let negatives = 0;
  for (const [name, id] of cases) {
    for (const body of ['1', 'null', true, false, NaN, Infinity, -Infinity, undefined, {}, []]) {
      await assert.rejects(run(name, id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
      negatives++;
    }
    await assert.rejects(run(name, id, { base_url: url }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  for (const [id, values] of [
    ['send31', [0, 2]], ['send30', [0, 2]], ['integer30', [0, 3, 1.5]], ['integer31', [0, 3, 1.5]],
    ['zeroallowed', [1, 0.5]], ['fractional', [0, 2]], ['independent', [0, 3]],
    ['zero', [null]], ['overlap', [null]], ['enumexcludes', [null]], ['composed', [0, 2]],
  ]) for (const body of values) {
    await assert.rejects(run('nullablenumbers', id, { base_url: url, body }), /^Error: Invalid or missing input: body$/);
    negatives++;
  }
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ nullableNumericNoRequestFixture: 'passed', negatives, requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf nullable numeric wrapper validates nested fields items dictionaries and complete inner branches before HTTP', async () => {
  const body = { value: null, values: [1, null], lookup: { a: null, b: 1 }, composed: null };
  const before = JSON.stringify(body);
  await run('nullablenumbers', 'nested', { base_url: `${url}/v2`, body });
  assert.equal(requests.at(-1).body, before);
  const count = requests.length;
  for (const bad of [
    { ...body, value: 0 }, { ...body, values: [2] }, { ...body, lookup: { a: 0 } },
    { ...body, composed: 0 }, { ...body, value: undefined }, { ...body, values: [true] },
  ]) await assert.rejects(run('nullablenumbers', 'nested', { base_url: url, body: bad }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('nullablenumbers', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill(null) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  assert.equal(JSON.stringify(body), before);
});

test('oneOf nullable numeric wrapper keeps enum bounds and complete branches independent within work and depth limits', () => {
  const { validate } = require(join(temp, 'build/nullablenumbers/src/lib/runtime.js'));
  for (const scalar of ['number', 'integer']) for (const wrapper of [{ type: scalar, nullable: true }, { type: [scalar, 'null'] }, { type: ['null', scalar] }]) {
    const branches = [{ type: scalar, enum: [0, 1] }, { type: 'null' }];
    const schema = normalizeSchema({ ...wrapper, enum: [0, 1, null], minimum: 1, maximum: 1, oneOf: branches });
    for (const value of [1, null]) validate(value, schema, 'body', 0, { remaining: 4 });
    for (const value of [0, 2]) assert.throws(() => validate(value, schema, 'body', 0, { remaining: 2 }), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
    validate(null, schema, 'body', 39);
    assert.throws(() => validate(null, schema, 'body', 40), /^Error: Invalid or missing input: body$/);
    for (const oneOf of [
      [{ type: scalar }], [{ type: scalar, nullable: true, enum: [1] }],
      [{ type: scalar, nullable: true }, { type: 'null' }], [{ type: 'null' }, { type: 'null' }],
      [{ ...wrapper, oneOf: [{ type: scalar }] }],
      [{ ...wrapper, oneOf: [{ type: 'null' }, { type: 'null' }] }],
    ]) assert.throws(() => validate(null, normalizeSchema({ ...wrapper, oneOf }), 'body'), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, normalizeSchema({ ...wrapper, enum: [1], oneOf: branches }), 'body', 0, { remaining: 2 }),
      /^Error: Invalid or missing input: body$/);
    // A complete broad branch still matches even when another bound rejects it.
    const independent = normalizeSchema({ ...wrapper, minimum: 0, maximum: 2, oneOf: [
      { type: scalar, maximum: 0 }, { type: scalar, minimum: 1, maximum: 1 }, { type: 'null' },
    ] });
    for (const value of [0, 1, null]) validate(value, independent, 'body');
    assert.throws(() => validate(2, independent, 'body'), /^Error: Invalid or missing input: body$/);
    const completeOverlap = normalizeSchema({ ...wrapper, minimum: 1, oneOf: [
      { type: scalar, minimum: 0 }, { type: scalar, minimum: 1 }, { type: 'null' },
    ] });
    assert.throws(() => validate(1, completeOverlap, 'body'), /^Error: Invalid or missing input: body$/);
    const zero = normalizeSchema({ ...wrapper, enum: [0, null], minimum: 0, maximum: 0, oneOf: [{ type: scalar }, { type: 'null' }] });
    validate(0, zero, 'body'); validate(null, zero, 'body');
    for (const bounds of [
      { minimum: 0, maximum: 2, exclusiveMinimum: true, exclusiveMaximum: true },
      { exclusiveMinimum: 0, exclusiveMaximum: 2 },
    ]) {
      const bounded = normalizeSchema({ ...wrapper, ...bounds, oneOf: [{ type: scalar }, { type: 'null' }] });
      validate(1, bounded, 'body'); validate(null, bounded, 'body');
      for (const value of [0, 2]) assert.throws(() => validate(value, bounded, 'body'), /^Error: Invalid or missing input: body$/);
    }
  }
  for (const nullable of [false, undefined]) {
    const schema = normalizeSchema({ type: 'number', ...(nullable === false ? { nullable } : {}), oneOf: [{ type: 'number' }, { type: 'null' }] });
    validate(0, schema, 'body');
    assert.throws(() => validate(null, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf nullable array wrapper generated-runtime regression', () => {
  // Compile an existing supported piece to exercise the real generated validator.
  const { validate } = require(join(temp, 'build/arraywrappers/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: ['array', 'null'], minItems: 2, uniqueItems: true,
    items: { type: 'integer', minimum: 0 },
    oneOf: [{ type: 'array', items: { type: 'integer', enum: [0, 1] } }, { type: 'null' }],
  });
  for (const value of [[0, 1], null]) validate(value, schema, 'body');
  for (const value of [[1], [0, 0], [-1, 0], [0, 2], [], ['0', '1'], 'null', true, 0, undefined, {}]) {
    assert.throws(() => validate(value, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
  validate(null, schema, 'body', 0, { remaining: 4 });
  assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
});

test('oneOf nullable array wrapper sends unchanged arrays null and required empty; negatives add no HTTP', async () => {
  const cases = [
    ['nullablearrays', 'send31', [[0, 1], null]], ['nullablearrays', 'send30', [[0, 1], null]],
    ['nullablearrays30', 'send30', [[0, 1], null]], ['nullablearrays', 'empty', [[], null]],
    ['nullablearrays', 'independent', [[0, 0], [1], null]],
    ['nullablearrays', 'branchunique', [[0, 1], [2, 2], null]],
    ['nullablearrays', 'composed', [[0, 1], null]], ['nullablearrays', 'overlap', [[0, 1]]],
    ['nullablearrays', 'jsonvalues', [null, [false, 0, true, 1, '1', null],
      [{ a: [1, 2], b: false }, { b: false, a: [2, 1] }, [1, 2], [2, 1]]]],
  ];
  for (const [name, id, values] of cases) {
    assert.equal(action(name, id).props.body.type, 'ARRAY');
    assert.deepEqual(action(name, id).props.body.properties, {});
    assert.equal(action(name, id).props.body.required, true);
    for (const body of values) {
      const before = JSON.stringify(body);
      assert.equal((await run(name, id, { base_url: `${url}/v2`, body })).ok, true);
      assert.equal(requests.at(-1).url, `/v2/${id}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, before);
      assert.equal(JSON.stringify(body), before);
    }
  }
  const count = requests.length;
  let negatives = 0;
  const rejects = async (name, id, body, reason = /^Error: Invalid or missing input: body$/) => {
    await assert.rejects(run(name, id, { base_url: url, body }), reason); negatives++;
  };
  for (const [name, id] of cases) {
    for (const body of ['[0,1]', 'null', 0, true, false, undefined, {}]) await rejects(name, id, body);
    await assert.rejects(run(name, id, { base_url: url }), /^Error: Invalid or missing input: body$/); negatives++;
  }
  for (const [id, values] of [
    ['send31', [[1], [0, 0], [-1, 0], [0, 2], [0, 1, 2, 3], [], ['0', '1']]],
    ['send30', [[1], [0, 0], [-1, 0], [0, 2]]], ['empty', [[0]]], ['zero', [null]],
    ['overlap', [null]], ['arrayoverlap', [[0, 1]]],
    ['independent', [[-1], [0], [1, 1], [0, 0, 0], [0, 0, 0, 0]]],
    ['branchunique', [[0, 0], [0, 2]]], ['composed', [[1], [0, 2]]],
    ['jsonvalues', [[true, true], ['x', 'x'], [null, null], [0, -0],
      [{ a: 1, b: [2] }, { b: [2], a: 1 }], [[1, 2], [1, 2]], [NaN], [Infinity], [undefined], [new Date()]]],
  ]) for (const body of values) await rejects('nullablearrays', id, body);
  await rejects('nullablearrays30', 'send30', [0, 0]);
  let deep = { leaf: true };
  for (let i = 0; i < 41; i++) deep = { child: deep };
  await rejects('nullablearrays', 'jsonvalues', [deep]);
  await rejects('nullablearrays', 'jsonvalues', ['x'.repeat(100001)], /^Error: Input validation limit exceeded$/);
  await rejects('nullablearrays', 'jsonvalues', [{ ['k'.repeat(100001)]: true }], /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ nullableArrayNoRequestFixture: 'passed', negatives, requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf nullable array wrapper validates nested fields items dictionaries and complete inner branches before HTTP', async () => {
  const body = { value: null, values: [[0, 1], null], lookup: { a: null, b: [0, 1] }, composed: null };
  const before = JSON.stringify(body);
  await run('nullablearrays', 'nested', { base_url: `${url}/v2`, body });
  assert.equal(requests.at(-1).body, before);
  const count = requests.length;
  for (const bad of [
    { ...body, value: [1] }, { ...body, values: [[0, 2]] }, { ...body, lookup: { a: [0, 0] } },
    { ...body, composed: [-1, 0] }, { ...body, value: undefined }, { ...body, values: [true] },
  ]) await assert.rejects(run('nullablearrays', 'nested', { base_url: url, body: bad }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('nullablearrays', 'nested', { base_url: url, body: { ...body, values: Array(26000).fill(null) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  assert.equal(JSON.stringify(body), before);
});

test('oneOf nullable array wrapper keeps complete null matches items bounds uniqueness independent within work and depth limits', () => {
  const { validate } = require(join(temp, 'build/nullablearrays/src/lib/runtime.js'));
  const items = { type: 'integer', minimum: 0 };
  const arrayBranch = { type: 'array', items: { type: 'integer', enum: [0, 1] } };
  for (const wrapper of [{ type: 'array', nullable: true }, { type: ['array', 'null'] }, { type: ['null', 'array'] }]) {
    const base = { ...wrapper, items, minItems: 2, maxItems: 3, uniqueItems: true };
    const schema = normalizeSchema({ ...base, oneOf: [arrayBranch, { type: 'null' }] });
    validate([0, 1], schema, 'body');
    validate(null, schema, 'body', 0, { remaining: 4 });
    for (const value of [[1], [-1, 0]]) assert.throws(() => validate(value, schema, 'body', 0, { remaining: 3 }), /^Error: Invalid or missing input: body$/);
    assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
    validate(null, schema, 'body', 39);
    assert.throws(() => validate(null, schema, 'body', 40), /^Error: Invalid or missing input: body$/);
    for (const oneOf of [
      [arrayBranch], [{ ...arrayBranch, nullable: true }, { type: 'null' }],
      [{ type: 'null' }, { type: 'null' }],
      [{ ...base, oneOf: [arrayBranch] }],
      [{ ...base, oneOf: [{ type: 'null' }, { type: 'null' }] }],
    ]) assert.throws(() => validate(null, normalizeSchema({ ...base, oneOf }), 'body'), /^Error: Invalid or missing input: body$/);
    const empty = normalizeSchema({ ...base, minItems: 0, maxItems: 0, oneOf: [arrayBranch, { type: 'null' }] });
    validate([], empty, 'body'); validate(null, empty, 'body');
    for (const oneOf of [[{ ...arrayBranch, minItems: 1 }, { type: 'null' }], [arrayBranch, arrayBranch, { type: 'null' }]]) {
      assert.throws(() => validate([], normalizeSchema({ ...base, minItems: 0, oneOf }), 'body'), /^Error: Invalid or missing input: body$/);
    }
    const overlap = normalizeSchema({ ...base, oneOf: [
      { type: 'array', items: { type: 'integer' } }, arrayBranch, { type: 'null' },
    ] });
    assert.throws(() => validate([0, 1], overlap, 'body'), /^Error: Invalid or missing input: body$/);
    // Wrapper uniqueness must not narrow a complete branch into a null match.
    const nullOverlap = normalizeSchema({ ...base, oneOf: [
      { ...arrayBranch, type: ['array', 'null'], uniqueItems: true, minItems: 9 }, { type: 'null' },
    ] });
    assert.throws(() => validate(null, nullOverlap, 'body'), /^Error: Invalid or missing input: body$/);
  }
  for (const nullable of [false, undefined]) {
    const schema = normalizeSchema({ type: 'array', items, ...(nullable === false ? { nullable } : {}), oneOf: [arrayBranch, { type: 'null' }] });
    validate([], schema, 'body');
    assert.throws(() => validate(null, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf nullable object wrapper generated-runtime regression', () => {
  const { validate } = require(join(temp, 'build/nullableobjects/src/lib/runtime.js'));
  const schema = normalizeSchema({ type: ['object', 'null'], required: ['requestId'],
    properties: { requestId: { type: 'string', minLength: 1 } },
    oneOf: [{ type: 'object', required: ['email'], properties: { email: { type: 'string', minLength: 1 } } }, { type: 'null' }],
  });
  for (const body of [{ requestId: 'r1', email: 'a' }, null]) validate(body, schema, 'body');
  for (const body of [{ email: 'a' }, { requestId: 'r1' }, { requestId: '', email: 'a' },
    { requestId: 'r1', email: '' }, {}, [], 'null', true, 0, undefined]) {
    assert.throws(() => validate(body, schema, 'body'), /^Error: Invalid or missing input: body$/);
  }
});

test('oneOf nullable object wrapper sends unchanged objects and null; negatives add no HTTP', async () => {
  const valid = { requestId: 'r1', email: 'a', extra: 1 };
  const cases = [
    ['nullableobjects', 'send31', [valid, null]], ['nullableobjects', 'send30', [valid, null]],
    ['nullableobjects30', 'send30', [valid, null]], ['nullableobjects', 'closed', [null]],
    ['nullableobjects', 'composed', [valid, null]], ['nullableobjects', 'overlap', [valid]],
    ['nullableobjects', 'zero', [valid]],
  ];
  for (const [name, id, values] of cases) {
    assert.equal(action(name, id).props.body.type, 'OBJECT');
    assert.equal(action(name, id).props.body.required, true);
    for (const body of values) {
      const before = JSON.stringify(body);
      assert.equal((await run(name, id, { base_url: `${url}/v2`, body })).ok, true);
      assert.equal(requests.at(-1).url, `/v2/${id}`);
      assert.equal(requests.at(-1).headers['content-type'], 'application/json');
      assert.equal(requests.at(-1).body, before);
      assert.equal(JSON.stringify(body), before);
    }
  }
  const count = requests.length;
  let negatives = 0;
  const rejects = async (name, id, body) => {
    await assert.rejects(run(name, id, { base_url: url, body }), /^Error: Invalid or missing input: body$/); negatives++;
  };
  const wrong = ['[0,1]', 'null', 'x', true, 0, [], undefined];
  for (const id of ['send31', 'send30']) {
    for (const body of [{ email: 'a' }, { requestId: 'r1' }, { requestId: '', email: 'a' }, { requestId: 'r1', email: '' }, {}, ...wrong]) {
      await rejects('nullableobjects', id, body);
    }
  }
  for (const body of [{ requestId: 'r1' }, {}, 'null', 'x', true, 0, [], undefined]) await rejects('nullableobjects30', 'send30', body);
  // The closed branch rejects the wrapper requestId, so no object matches; only null remains valid.
  for (const body of [{ requestId: 'r1', email: 'a' }, { email: 'a' }, { requestId: 'r1' }, {}]) await rejects('nullableobjects', 'closed', body);
  for (const body of [{ email: 'a' }, { requestId: 'r1' }, { requestId: 'r1', email: '' }]) await rejects('nullableobjects', 'composed', body);
  for (const body of [null, { requestId: 'r1' }, { email: 'a' }]) await rejects('nullableobjects', 'zero', body);
  for (const body of [null, { email: 'a' }, { requestId: 'r1' }]) await rejects('nullableobjects', 'overlap', body);
  let deep = { leaf: true };
  for (let i = 0; i < 41; i++) deep = { child: deep };
  await rejects('nullableobjects', 'send31', { requestId: 'r1', email: 'a', extra: deep });
  assert.equal(requests.length, count);
  console.log(JSON.stringify({ nullableObjectNoRequestFixture: 'passed', negatives, requestsBeforeNegatives: count, requestsAfterNegatives: requests.length }));
});

test('oneOf nullable object wrapper validates nested fields, lists and dictionaries before HTTP', async () => {
  const item = { requestId: 'r1', email: 'a' };
  const body = { wrapper: null, list: [null, item], lookup: { a: null, b: item } };
  const before = JSON.stringify(body);
  await run('nullableobjects', 'nested', { base_url: `${url}/v2`, body });
  assert.equal(requests.at(-1).url, '/v2/nested');
  assert.equal(requests.at(-1).body, before);
  const count = requests.length;
  for (const bad of [
    { ...body, wrapper: { email: 'a' } }, { ...body, list: [{ requestId: 'r1' }] },
    { ...body, lookup: { a: { requestId: '', email: 'a' } } }, { ...body, extra: true },
    { ...body, wrapper: undefined }, { ...body, list: [true] },
  ]) await assert.rejects(run('nullableobjects', 'nested', { base_url: url, body: bad }), /^Error: Invalid or missing input: body$/);
  await assert.rejects(run('nullableobjects', 'nested', { base_url: url, body: { ...body, list: Array(26000).fill(null) } }),
    /^Error: Input validation limit exceeded$/);
  assert.equal(requests.length, count);
  assert.equal(JSON.stringify(body), before);
});

test('oneOf nullable object wrapper keeps wrapper and complete branch constraints independent within work and depth limits', () => {
  const { validate } = require(join(temp, 'build/nullableobjects/src/lib/runtime.js'));
  const requestId = { type: 'string', minLength: 1 }, email = { type: 'string', minLength: 1 };
  const branch = { type: 'object', required: ['email'], properties: { email } };
  const nullBranch = { type: 'null' };
  const value = { requestId: 'r1', email: 'a' };
  const invalid = /^Error: Invalid or missing input: body$/;
  for (const wrapper of [{ type: 'object', nullable: true }, { type: ['object', 'null'] }, { type: ['null', 'object'] }]) {
    const base = { ...wrapper, required: ['requestId'], properties: { requestId } };
    const schema = normalizeSchema({ ...base, oneOf: [branch, nullBranch] });
    validate(value, schema, 'body');
    validate(null, schema, 'body', 0, { remaining: 4 });
    assert.throws(() => validate(null, schema, 'body', 0, { remaining: 3 }), /^Error: Input validation limit exceeded$/);
    validate(null, schema, 'body', 39);
    assert.throws(() => validate(null, schema, 'body', 40), invalid);
    for (const bad of [{ email: 'a' }, { requestId: 'r1' }, { requestId: 'r1', email: '' }, { requestId: '', email: 'a' }]) {
      assert.throws(() => validate(bad, schema, 'body'), invalid);
    }
    // Wrapper additionalProperties and complete branch additionalProperties are checked separately, never merged.
    const wrapperClosed = normalizeSchema({ ...base, additionalProperties: false, oneOf: [branch, nullBranch] });
    assert.throws(() => validate(value, wrapperClosed, 'body'), invalid);
    validate(null, wrapperClosed, 'body');
    const branchClosed = normalizeSchema({ ...base, oneOf: [{ ...branch, additionalProperties: false }, nullBranch] });
    assert.throws(() => validate(value, branchClosed, 'body'), invalid);
    validate(null, branchClosed, 'body');
    // Exactly one complete match is required for null; duplicates and zero matches fail.
    for (const oneOf of [[branch], [branch, nullBranch, nullBranch], [nullBranch, nullBranch]]) {
      assert.throws(() => validate(null, normalizeSchema({ ...base, oneOf }), 'body'), invalid);
    }
    // Branch constraints do not narrow the wrapper, and the wrapper does not narrow a complete null match.
    const nested = normalizeSchema({ ...base, oneOf: [{ ...branch, properties: { email, requestId } }, nullBranch] });
    validate({ requestId: 'r1', email: 'a' }, nested, 'body');
  }
  const nullOnly = normalizeSchema({ type: ['object', 'null'], required: ['requestId'], properties: { requestId }, oneOf: [nullBranch] });
  validate(null, nullOnly, 'body');
  assert.throws(() => validate({ requestId: 'r1' }, nullOnly, 'body'), invalid);
  for (const nullable of [false, undefined]) {
    const schema = normalizeSchema({ type: 'object', ...(nullable === false ? { nullable } : {}), required: ['requestId'], properties: { requestId }, oneOf: [branch, nullBranch] });
    validate(value, schema, 'body');
    assert.throws(() => validate(null, normalizeSchema({ type: 'object', ...(nullable === false ? { nullable } : {}), oneOf: [branch] }), 'body'), invalid);
  }
});

await runRegisteredTests();
