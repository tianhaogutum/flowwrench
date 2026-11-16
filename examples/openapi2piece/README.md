# flowwrench openapi2piece examples

Synthetic, redistributable OpenAPI fixtures (MIT) copied from the original tool:

| File | Shows |
| --- | --- |
| `petstore.yaml` | YAML input, path/query/header parameters, a JSON body, an API key, a duplicate operationId warning and two skipped operations (cookie, multipart) |
| `request-projection.json` | OpenAPI 3.0 `readOnly` request projection |
| `auth-alternatives.json` | OR/AND security alternatives, anonymous access and skipped unsupported schemes |

Generate a piece (the output directory must not exist; its parent must):

```sh
mkdir -p out
flowwrench openapi2piece generate --spec examples/openapi2piece/petstore.yaml --out out/piece-petstore --name petstore
```

Without the `flowwrench` launcher, `python -m flowwrench.openapi2piece generate …` is equivalent.
Validate first without writing anything:

```sh
flowwrench openapi2piece generate --spec examples/openapi2piece/petstore.yaml --out out/pets-only --name pets \
  --include-tags pets --include-ops getPet,updatePet --max-actions 10 --dry-run
```

The petstore run prints a JSON report with 3 generated actions (`getpet`, `updatepet`, `getpet_2`),
2 skipped operations and 1 warning. The generated files are byte-identical to the Node.js
`openapi2piece` 0.1.0 output; `tests/openapi2piece/snapshots/petstore.json` holds them.
Build the generated piece with `npm install --ignore-scripts && npm run typecheck && npm run build`.
