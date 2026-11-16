# flowwrench openapi2piece

`flowwrench openapi2piece` generates a standalone, editable **Activepieces piece** from an OpenAPI 3.0/3.1 YAML or JSON file. Each supported operation becomes an action with typed input properties, connection authentication and a working HTTP request. Operations that cannot be mapped faithfully are skipped, with the reason recorded in a report, instead of being generated half-correct.

The command was previously the standalone Node.js tool `openapi2piece`. Generation runs locally and deterministically in Python: no API is contacted, nothing is installed, deployed or published, and the generated files are byte-identical to those of the original tool. Upstream relationships are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer for the generator. Building the generated piece needs Node.js and npm.

```sh
pip install flowwrench                  # or: pipx install flowwrench
flowwrench openapi2piece --help

# Or run without installing:
uvx flowwrench openapi2piece generate --spec api.yaml --out piece-api --name api --dry-run
```

`python -m flowwrench openapi2piece` is equivalent to `flowwrench openapi2piece`. Like the original tool, it has a single `generate` subcommand and no `--version` flag. The synthetic sample specs below live in `examples/openapi2piece/` in the flowwrench repository; run the commands from the repository root (or adjust the paths).

## Generate a piece

```sh
mkdir -p out
flowwrench openapi2piece generate \
  --spec examples/openapi2piece/petstore.yaml --out out/piece-petstore --name petstore
```

The Pet Store sample generates **3 actions** (`getpet`, `updatepet`, `getpet_2`), reports **2 skipped operations** (a cookie parameter and a multipart body) and warns about one duplicate operationId. The output directory must not exist yet and its parent must already exist.

Build the generated piece with npm:

```sh
cd out/piece-petstore
npm install --ignore-scripts
npm run typecheck
npm run build
```

Commit the generated project's `package-lock.json` after installing. The generated `package.json` pins its direct dependencies and includes pinned compatibility overrides for vulnerable upstream transitive dependencies (including a maintained `expr-eval` fork; generated actions never evaluate expressions). npm applies overrides only at the installation root, so carry them into your root manifest if you embed the piece in another package.

The output is a private CommonJS package with `dist/index.js` as its entry point. Review the generated actions, set a logo and authors in `src/index.ts`, then follow your Activepieces installation's procedure for private pieces.

## Selection and dry run

```sh
flowwrench openapi2piece generate \
  --spec examples/openapi2piece/petstore.yaml --out out/pets-only --name pets \
  --include-tags pets --include-ops getPet,updatePet --max-actions 10 --dry-run
```

| Flag | Meaning |
| --- | --- |
| `--spec` | Local YAML or JSON file, at most 8 MiB |
| `--out` | New output directory; its parent must exist; no symlinked ancestors |
| `--name` | Lowercase slug, 1–64 characters, starting with a letter |
| `--include-tags` | Comma-separated tags, repeatable; any matching tag qualifies |
| `--include-ops` | Comma-separated operationIds or exact `METHOD /path` strings, repeatable |
| `--max-actions` | Positive integer; caps supported actions in sorted path/method order |
| `--dry-run` | Validate everything and print the JSON report and file list, without writing anything |

When both filters are given, both must match. Duplicate operationIds can select more than one operation; use `--include-ops 'GET /status'` to disambiguate. Options also accept `--name=value`.

Exit codes: **0** generation succeeded, even with skipped operations (inspect `report.json` before use); **1** for everything else: no selected or supported operations, unknown options, invalid specs, unresolved or remote `$ref`s, and unsafe or existing output paths. Errors are printed with a `flowwrench openapi2piece:` prefix and never echo the spec's content.

Generated files:

```text
package.json, tsconfig.json, README.md, LICENSE, THIRD_PARTY.md, report.json
src/index.ts
src/lib/auth.ts
src/lib/runtime.ts
src/lib/actions/getpet.ts
src/lib/actions/getpet_2.ts
src/lib/actions/updatepet.ts
```

The stdout report and the generated `report.json` contain `generated`, `skipped` (with reasons) and `warnings`:

```json
{
  "generated": [{"method":"GET","path":"/pets/{petId}","operationId":"getPet","name":"getpet","file":"src/lib/actions/getpet.ts"}],
  "skipped": [{"method":"GET","path":"/cookies","operationId":"cookies","reason":"Cookie parameters are unsupported"}],
  "warnings": [{"method":"GET","path":"/status","operationId":"getPet","message":"operationId sanitized or renamed to getpet_2"}]
}
```

Action names are ASCII identifiers with a stable suffix for collisions; operation names are never used as raw file paths or executable source. Generated files contain no timestamps or absolute paths. The generated `LICENSE` and `THIRD_PARTY.md` are templates for the generated scaffold; the API description you generate from may have its own license and terms.

## Mapping and request behavior

Local JSON Pointer `$ref`s resolve throughout the document, including escaped names. Path-item parameters are inherited, with operation-level overrides by location and name (header names are case-insensitive). Operation, path and root servers follow OpenAPI precedence; the first server is used and server variables need defaults. When the server is relative or missing, the user supplies the absolute API base URL in the required **Base URL** property. Authority references (`//host`), query/fragment references, backslashes and `..` traversal are rejected.

| OpenAPI input | Activepieces property |
| --- | --- |
| `string` | `Property.ShortText` |
| `number`, `integer` | `Property.Number` (integers checked at runtime) |
| `boolean` | `Property.Checkbox` |
| Scalar `enum` | `Property.StaticDropdown` |
| JSON `object` body | `Property.Object` |
| JSON `array` body | `Property.Array` |
| Body `oneOf` alternatives | One shared control for compatible top-level types; explicit exactly-one validation |

Path, query and header parameters must be non-null scalars. Property keys carry a location prefix (`path_petid`, `query_limit`, `header_x_trace`); the whole JSON request body is the `body` property. Required fields, types, enums, numeric bounds, length and item bounds, `uniqueItems` and `additionalProperties` are validated recursively **before** any request is sent. OpenAPI 3.0 `nullable` and 3.1 `[type, "null"]` are supported for bodies. String `format` is descriptive only; binary/byte formats are rejected. Schema defaults and examples are never copied into generated code.

**OpenAPI 3.0 request projections.** In OpenAPI 3.0 JSON bodies, object properties marked `readOnly: true` are excluded recursively from the request schema and its `required` lists, including nested objects, array items and typed `additionalProperties`. Supplying a read-only property fails before HTTP with its path (for example `Read-only request property: body.nested.createdAt`); it is never silently stripped. Response schemas and shared components are not modified. Try `examples/openapi2piece/request-projection.json`: it generates `createwidget` and `getwidget` with no skips.

**`oneOf` alternatives** validate **exactly one** alternative, recursively, so zero matches and overlapping matches both fail before HTTP with `Invalid or missing input: body`. All non-null top-level alternatives must fit the same control (object, array, string, boolean or numeric); incompatible alternatives are skipped visibly. Typed wrappers can add their own constraints next to `oneOf` (object `required`/`properties`/`additionalProperties`; string `enum` and code-point `minLength`/`maxLength`; numeric `enum` and bounds; array `items`, `minItems`, `maxItems`, `uniqueItems`; boolean `enum`; and their nullable forms). The value must satisfy the wrapper **and** exactly one complete branch: constraints are never merged, values never coerced. Other validation siblings are skipped with explicit reasons. Each `oneOf` allows at most 16 alternatives, and input validation has a shared budget of 100,000 steps per property.

### Authentication

| Security scheme | PieceAuth and wire mapping |
| --- | --- |
| None / operation `security: []` | `PieceAuth.None`, or an anonymous action on an authenticated piece; sends no credentials |
| Header/query `apiKey` | `PieceAuth.SecretText`; the exact named header or query field |
| HTTP bearer | `PieceAuth.SecretText`; `Authorization: Bearer …` |
| HTTP basic | `PieceAuth.BasicAuth`; base64 UTF-8 `username:password` |
| OAuth2 authorizationCode | `PieceAuth.OAuth2`; bearer access token; union of the scopes needed by the selected actions |
| Several schemes across operations, or AND requirements | `PieceAuth.CustomAuth`; visible text fields per scheme, checked per action |
| OR alternatives | A required **Authentication** (single schemes) or **Authentication requirement** (AND groups or anonymous alternatives) dropdown; only the selected requirement is sent |

Connection values target pieces-framework **0.32.0**. **CustomAuth fields are visible text**, because that framework's metadata validator rejects SecretText child fields; the report warns whenever this fallback is used, and you get masked fields by selecting operations that use a single scheme. Mixed OAuth2 connections take a manually supplied access token without automatic refresh. No credential values are embedded in generated code.

For OR alternatives there is no default choice, no inference from populated fields and no fallback to another scheme or to anonymous access after an HTTP error. Missing or invalid selections fail with `Invalid or missing input: auth_scheme` / `auth_requirement`, and missing chosen credentials fail with `Missing credentials for this operation`, both before HTTP. Credential collisions are checked within each requirement; an invalid group skips the whole operation. Try `examples/openapi2piece/auth-alternatives.json`: it generates the `optional`, `compound`, `items`, `keyonly` and `public` actions, skips 3 operations with reasons and emits the visible-CustomAuth-fields warning.

### Generated requests

Generated actions call the real `@activepieces/pieces-common` HTTP client. Path values are URI-encoded, the server's base path is kept, optional undefined values are omitted, and `false`, `0`, null bodies and quoted JSON strings are preserved. Actions return the parsed response body. The timeout is 30 seconds, retries and redirects are disabled, and bodies and responses are capped at 8 MiB. HTTP errors include only the status; network errors contain no request, response, URL or credential values. The generated transport adapter verifies TLS certificates with its own HTTPS agent, restores the `NODE_TLS_REJECT_UNAUTHORIZED` setting that pieces-common 0.12.5 changes temporarily, bypasses environment proxies (they are intentionally unsupported) and sanitizes errors before the upstream logger sees them.

## Boundaries and limitations

This is a scaffold generator for a documented OpenAPI subset, not a complete OpenAPI validator or an Activepieces publishing system.

- HTTP methods: GET, POST, PUT, PATCH, DELETE and HEAD. OPTIONS and TRACE are skipped because the target framework has no matching method.
- One JSON request-body media type (`application/json` or `application/*+json`). Multipart and form uploads, GET/HEAD bodies, cookie parameters, complex parameter arrays/objects, nonstandard serialization, callbacks, reserved headers and credential/parameter conflicts are skipped with reasons.
- No Swagger 2.0, triggers or webhooks, pagination helpers, response validation, remote or file refs, circular refs, YAML alias cycles or structural `$ref` siblings. A ref failure anywhere, even in an unselected operation, fails the whole generation. The expanded document is limited to 16 MiB, 100,000 visited nodes, reference depth 80 and input-schema depth 30.
- No `allOf`/`anyOf`, discriminators, patterns, boolean schemas or arbitrary JSON Schema keywords; read-only projection is limited to the OpenAPI 3.0 subset above. Unsupported validation keywords skip the operation; `x-` extensions and descriptive annotations are inert. A JSON body is edited as one object, not one property per nested field.
- Only OAuth2 authorizationCode with absolute HTTPS auth and token URLs. No implicit, password, clientCredentials or OpenID Connect flows.
- URL credentials, base queries and fragments, traversal paths, `.`/`..` path values, unsafe headers and symlinked output ancestors are rejected. The Base URL stays configurable: configure access only to trusted API hosts. This is not a network sandbox.
- Output is staged in a sibling directory and never overwrites existing output. The output parent must be a trusted local directory.
- No live Activepieces instance is used. Compatibility is checked against the pinned framework types, runtime metadata validation and action execution; installation into a particular Activepieces server and its UI is not tested. Recheck compatibility before changing framework versions.

## Tests

```sh
uv run --extra test pytest tests/openapi2piece
```

Reference resolution, mapping tables, authentication and scope selection, identifier collisions, full generated-file snapshots, CLI errors and output boundaries run in pure Python. The TypeScript, metadata-validation and HTTP execution tests of generated pieces additionally need Node.js with `npx` and the pinned packages (TypeScript 5.9.3, `@activepieces/pieces-framework` 0.32.0, `@activepieces/pieces-common` 0.12.5, axios 1.20.0, `@types/node` 22.19.0) in a `node_modules` directory named by `OPENAPI2PIECE_NODE_MODULES`; without them those tests are skipped. They exercise generated actions against ephemeral `127.0.0.1` HTTP/HTTPS servers. The TLS fixture's key is public synthetic test data.

## License

Apache-2.0; see [LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md). This is an independent tool, not an official Activepieces project; no Activepieces enterprise code is used. Motivation: [Activepieces #748](https://github.com/activepieces/activepieces/issues/748) and [PR #13014](https://github.com/activepieces/activepieces/pull/13014) (no code was taken from the PR).
