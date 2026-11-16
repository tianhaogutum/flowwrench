# flowwrench ap-promote

`flowwrench ap-promote` (formerly `ap-flow-promote`) promotes an **Activepieces** flow export from one environment to another, entirely offline. It inventories every connection reference, rewrites connection external IDs with minimal-diff byte edits, and checks the result against the destination before you import it. It can also save a hashed promotion plan for review and verify a destination re-export afterwards. The runtime uses only the Python standard library.

You promote one reviewed JSON file. Neither instance is contacted, no secrets are copied, no steps run, and no other flows change. Embedded agent tools have their own piece ownership, including inside routers, loops and error branches.

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench              # or: pipx install flowwrench
flowwrench ap-promote --version     # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench ap-promote inventory ./flow.json
```

`python -m flowwrench ap-promote` is equivalent to `flowwrench ap-promote`. The command makes no network calls.

Subcommands: `inventory`, `remap`, `check`, `pieces`, `scrub`, `plan`, `apply` and `verify-roundtrip`. Run `flowwrench ap-promote <subcommand> --help` for the flags of each one.

## A complete promotion

The example inputs below live in `tests/ap_promote/fixtures/` in the flowwrench repository. Run the commands from the repository root, or adjust the paths. The redistributable fixture uses Gmail and Slack in a trigger, a router, a loop, an ordinary action and two embedded agent tools. It contains **5 references** to 2 connection external IDs. All sample values are synthetic.

```sh
flowwrench ap-promote inventory tests/ap_promote/fixtures/support-flow.json --json
mkdir -p out
flowwrench ap-promote remap tests/ap_promote/fixtures/support-flow.json \
  --map slack-dev=slack-prod --map gmail-dev=gmail-prod \
  -o out/support-flow.prod.json --strict
flowwrench ap-promote check out/support-flow.prod.json \
  --target-connections tests/ap_promote/fixtures/target-connections.json
flowwrench ap-promote pieces out/support-flow.prod.json \
  --target-pieces tests/ap_promote/fixtures/target-pieces.json
flowwrench ap-promote scrub out/support-flow.prod.json \
  -o out/support-flow.clean.json
```

The remap reports `5 references rewritten; 0 unmapped`. `check` exits 0. `pieces` reports six piece usages, including both tool versions. `scrub` removes four step sample-data metadata objects. Review the output, then import it yourself in Activepieces: this command never performs an import.

To validate the destinations **before anything is written**, pass the target catalog to `remap`:

```sh
flowwrench ap-promote remap tests/ap_promote/fixtures/support-flow.json \
  --map-file tests/ap_promote/fixtures/map.json --strict \
  --target-connections tests/ap_promote/fixtures/target-connections.json \
  -o out/support-flow.prod.json --dry-run --json
```

Remove `--dry-run` to write the reviewed result. A dry run does the same parsing, mapping and target checks, but it writes nothing, creates no directories and never calls an external workflow step. It does not test output permissions.

## Review a saved promotion plan

`plan` adds a review step before the output is written. The versioned synthetic catalog declares each connection's `pieceVersion` as well as its external ID, piece and status:

```sh
mkdir -p out
flowwrench ap-promote plan tests/ap_promote/fixtures/support-flow.json \
  --map-file tests/ap_promote/fixtures/map.json \
  --target-connections tests/ap_promote/fixtures/target-connections-versioned.json \
  --target-pieces tests/ap_promote/fixtures/target-pieces.json \
  -o out/support-flow.plan.json --json
flowwrench ap-promote apply out/support-flow.plan.json \
  --source tests/ap_promote/fixtures/support-flow.json \
  --map-file tests/ap_promote/fixtures/map.json \
  --target-connections tests/ap_promote/fixtures/target-connections-versioned.json \
  --target-pieces tests/ap_promote/fixtures/target-pieces.json \
  -o out/support-flow.applied.json --json
```

The plan records **5** rewrites and **5** exact connection-version checks, including both embedded tools. It also records **6** available piece usages; the AI step has no connection. `apply` reports `planVerified: true`, `pieceCatalogVerified: true` and `pieceUsagesVerified: 6`, and it produces the same minimal-diff bytes as the remap example. `plan` writes no flow output. Before applying the saved JSON, review its mapping, its per-reference compatibility records and its hashes.

### Plan format

The plan is a version-1 artifact with the format identifier `ap-flow-promote.plan/v1`. That identifier was kept from the standalone tool, so plans saved earlier still apply. A plan contains:

- `sourceSha256`: a hash of the exact UTF-8 source bytes, including BOM and whitespace.
- `configSha256`: a hash of the canonical effective mappings, the complete allowlisted target metadata, and the strict/version policy.
- `outputSha256`: a hash of the expected output.

Object-key order, catalog order and config-file formatting do not change the config hash. Changing any mapping, piece assertion, target ID, status or version does. Catalog values, tokens and other unknown fields are dropped before hashing and are never copied into the plan. The plan does not include the flow's business data. Plans contain no file paths: `apply` reads only the paths given on its command line.

### Connection-version gate

Both commands require mappings and a complete target catalog, and they always remap in strict mode. `--map` and `--map-file` can be repeated and combined, as for `remap`. For **every** resulting connection reference, including IDs that did not change, two versions must be exact SemVer pins and must be equal: the version of the owning step or tool, and the `pieceVersion` of the target connection. A missing version, a range on a source usage, or two different versions refuses the promotion. A malformed target pin is an input error. This is a conservative equality check on metadata you supply. It does not claim that a different version is necessarily incompatible. A destination used by two owning steps with different versions cannot pass this check. Connection-version checks on their own cover only the usages that own a connection.

### Whole-flow piece availability

Pass the optional `--target-pieces PIECES` to **both** `plan` and `apply` to also record whole-flow piece availability in the same artifact. It accepts the same complete array or `data` page, with the same name/version aliases, as `pieces` below. Every piece step and every embedded piece tool must have an exact installed pin. That includes usages without connections and usages inside nested routers, loops, batches and error branches. A missing or ranged source pin (`unpinned`) or a pair absent from the target (`missing-version`) exits **1**. The usage's JSON pointer goes to stderr, and no plan or output is written. An empty catalog is still checked, even for flows without any connections. Malformed pins or records and incomplete pages exit **2**. Non-piece steps and FLOW, MCP and KNOWLEDGE_BASE tools are not covered by this piece-version catalog.

With this option, `pieceAvailability` records every usage with its `available` status, and `targetPiecesSha256` hashes the canonical installed catalog. The normalized catalog is a sorted, deduplicated list of `{ "pieceName": "...", "pieceVersion": "..." }` pairs, serialized as compact UTF-8 JSON with sorted object keys. It is also part of `configSha256`. Formatting, row order, aliases, identical duplicate pairs and unknown fields (including credential fields) do not change either hash. Adding or removing any pair, even an unused version, changes the config hash. Catalog paths and unknown fields are never copied into the plan.

`apply` requires the same effective catalog. Omitting it, changing it, or adding one to a plan created without it is refused as stale configuration with exit **1**, also during a dry run. The output may not overwrite the catalog. Without the option, connection-only v1 plans behave exactly as before, and the availability of pieces without connections is not checked. `pieces` is also available as a separate check.

### Apply inputs

`apply` needs the current source, the mappings and the catalogs again, and exactly one nonempty `--source PATH`. Repeating `--source`, even with the same path, or passing an empty path is a usage error (exit **2**). It is reported before the command runs, reads input, prints a report or writes output, also with `--dry-run`. The separate form and the `--source=PATH` form follow the same rule. A nonempty path is taken literally, including leading or trailing spaces. If `--source` is omitted, argparse reports the missing required option. A later `--source` cannot hide a missing, malformed or empty first one.

The positional path of the reviewed plan must also be nonempty. `apply ''` is a usage error (exit **2**), reported before the command runs, reads input, prints a report or writes anything. That holds with `--dry-run` and when the current inputs are missing or malformed. The message names `file` and does not echo file contents. A nonempty plan path is taken literally, including leading or trailing spaces. If the argument is omitted, argparse reports the missing required argument.

`apply` verifies the input hashes and reruns the mapping, target and version checks. It then rebuilds the complete plan and compares every field with the saved one, including the expected output hash. A changed source (even one extra newline), a changed effective config or an inconsistent plan exits **1**, and any existing output is left in place. Mapping conflicts and malformed inputs keep the usual exit **1**/**2** rules. After any such change, create and review a new plan. Add `--dry-run` to `apply` to run every check without writing files or creating directories. An unknown plan format exits **2**. A generated plan must fit the same JSON resource limits as the inputs; otherwise it is refused before anything is written.

`plan` and `apply` write output the same way as `remap`: atomically, with mode 0600, and with the same rules for protected inputs and symlinks. `apply` also refuses to overwrite the plan itself. Only the connection-ID spans are edited, so opaque business fields stay byte-for-byte intact. Nothing writes or refreshes credentials, and no live authentication check runs. A plan is an unsigned local review artifact. Its hashes detect stale or inconsistent inputs. They do not detect someone replacing an entire plan together with its inputs.

## Verify a destination re-export

After you import the flow into the destination instance and export it again by hand, compare that re-export with the original source, using the same forward mapping. In this offline synthetic example, the promoted file from the first example plays the role of the destination export:

```sh
flowwrench ap-promote verify-roundtrip tests/ap_promote/fixtures/support-flow.json \
  out/support-flow.prod.json --map-file tests/ap_promote/fixtures/map.json --json
```

The report shows `sourceReferences: 5`, `destinationReferences: 5`, `verified: 5`, `errors: []` and `ok: true`, and the command exits **0**. Each `matches` record contains the exact `sourcePointer` and `destinationPointer`, a zero-based `occurrence`, the actual destination ID and `restoredExternalId` (`slack-dev` or `gmail-dev` here). Identities are restored per source location. Two source IDs mapped to one destination can therefore each recover their own identity, without an ambiguous global inverse map. An ID that already equals a mapped destination keeps its original source identity. The command works out identities in memory and never writes either export.

`check` only validates the references that are still present. This comparison also detects connection references that were deleted. For example, deleting the loop's `firstLoopAction` reports `problem: "lost-reference"` with `sourcePointer: "/flows/0/trigger/nextAction/children/0/firstLoopAction/settings/input/auth"`. Deleting a tool's auth field reports its exact `.../agentTools/0/pieceMetadata/predefinedInput/auth` pointer. A lost reference has `destinationPointer: null` because no destination occurrence exists. Wrong IDs, changed owning pieces or versions and unexpected destination references are errors too, each with the exact input pointers. Any comparison error exits **1** and prints the full JSON report (`ok: false`). Text mode prints a table of errors and a count of verified references. Malformed or unsupported inputs exit **2** before any report is printed, with the same safe diagnostics as the other commands. Invalid inputs never produce a partial report.

### `verify-roundtrip SOURCE DESTINATION --map OLD=NEW | --map-file MAP [--flow-pair SOURCE_INDEX=DESTINATION_INDEX] [--json]`

Both positional export paths must be nonempty. An explicitly empty `SOURCE` or `DESTINATION` exits **2** with an argparse usage error naming `source` or `destination`. The error comes before the command runs, reads flows or maps, or prints a report. If both are empty, `source` is reported first. This holds when the mapping or the other export is absent, missing or malformed, with explicit pairing, and in both JSON and text mode. A nonempty path is taken literally, including filenames made of whitespace, and is never trimmed. If a positional argument is omitted, argparse reports the missing required argument. A missing or malformed file at a nonempty path gets the usual diagnostics when the command runs. The command writes neither export, and no other file next to them.

The mapping flags repeat and combine as for `remap`, and they get the same strict ownership, piece-merge, conflict and chain checks. An unmapped source reference is refused with exit 1. Use identity mappings for IDs that stay the same and are not mapped destinations. An explicit map file containing `{}` is allowed for exports with no references.

By default, references are matched by **flow array order, unique step name, step-relative JSON field path and occurrence**. When a multi-flow re-export has its flows in a different order, pair each source flow with its destination index explicitly:

```sh
flowwrench ap-promote verify-roundtrip \
  tests/ap_promote/fixtures/roundtrip-multi-source.json \
  tests/ap_promote/fixtures/roundtrip-multi-reexport.json \
  --map-file tests/ap_promote/fixtures/roundtrip-multi-map.json \
  --flow-pair 0=1 --flow-pair 1=0 --json
```

The report shows **8** verified references, `errors: []` and `ok: true`, and the command exits **0**. Both flows use the step names `trigger` and `agent`, with different connection IDs. The re-export reverses the flow order and moves each agent into a router. Without the pairing flags, the same fixture exits **1** with eight `wrong-connection` errors. Pairing uses only the indexes you supply. It never infers identity from names, titles, flow IDs or connection values.

`--flow-pair` can be repeated, and every occurrence must be nonempty. `--flow-pair ''` and `--flow-pair=` exit **2** with argparse usage errors naming `--flow-pair`, before the command runs, reads flows or maps, or prints a report. This holds before or after valid pairs, with missing or malformed inputs, and in both JSON and text mode. Nonempty values are taken literally, are never trimmed and can be repeated. Their index and one-to-one checks happen when the command runs. Indexes are zero-based decimal integers in canonical form (`0`, `1`, ...). A raw, template or API-version envelope has index `0`.

If any pair is supplied, **every flow in both exports must be paired exactly once**, including flows with no references. The order of the pairs does not matter. Partial pairing, a source or destination index used twice (even in identical repeated pairs), out-of-range or malformed indexes, and different flow counts all exit **2** without a partial report. Unpaired flows never fall back to order-based matching. A pairing that is valid but wrong runs the comparison and exits **1** when the references differ. If two flows have indistinguishable references, this check cannot establish which is which: the pairing is your assertion. Without the flags, the order-based behavior and the report fields are as described above.

Explicit pairing adds `flowPairs`, sorted by source index, with `sourceFlowIndex`, `destinationFlowIndex`, `sourceFlowPointer` and `destinationFlowPointer`. Each flow pointer identifies an envelope's flow object; the raw root uses the RFC 6901 empty pointer `""`. Match and error records gain `sourceFlowIndex` and `destinationFlowIndex`. A lost reference keeps the paired destination index even though its `destinationPointer` is null. An unexpected reference names its paired source flow, or has null indexes if it lies outside any flow. The existing `flowIndex` remains the source index for records that start from the source, and the destination index for unexpected references. Text mode also lists the explicit pairs. Neither input is written.

Any two supported export envelopes can be compared with each other. Named steps may move to a different position in the structure, and the report keeps each document's actual pointers. Keep step names, the order of tool arrays and the positions of input arrays stable. Keep the order of flows stable too, unless you pair them explicitly. Renames or changed field paths are reported conservatively as missing, extra or mismatched references. A literal auth value and the equivalent static template compare as equal. Stored owning versions must be equal, including when both are missing. Use `pieces`, or `plan` and `apply`, to require exact pins.

This verifies static **connection references**, including those in nested loops, routers, batches, error branches and embedded agent tools. It does not compare the full flow structure or business data, resolve arbitrary step-output expressions, check whether connections are healthy, perform an import or export, or prove how the workflow behaves. To validate destination metadata, run `check` with a target catalog separately. The example and the tests use local synthetic exports, not a real round trip through an instance.

## Commands and data contracts

The source `FILE` of `remap`, `plan`, `check`, `pieces` and `scrub` must be nonempty. An empty string (`''`) is a usage error (exit **2**) that names `file` and says `expected nonempty path`. It is reported before the command runs, reads flows, maps or catalogs, prints a report or writes anything. That holds whether the mapping and catalog options are valid, missing, malformed or omitted, in JSON and text mode, and in the dry-run, output and in-place modes. Existing artifacts and directory entries are left untouched. Every nonempty filename is taken literally, including names made only of whitespace, and paths are never trimmed. If the required `FILE` is omitted, argparse reports the missing positional argument. A missing or malformed file at a nonempty path gets the usual safe input diagnostics.

`-o` / `--output` takes at most one nonempty path, used literally, for `remap`, `plan`, `apply` and `scrub`. Repeating it in either spelling (even with the same path, or mixing the short and long forms), `-o ''` and `--output=` are usage errors (exit **2**). They are reported before any input is processed, any report is printed or anything is written, including in dry runs. The separate and `=` forms behave alike, and paths are not trimmed. On these usage errors, earlier plan and output files and all inputs stay untouched. An output path is still required for `remap`, `plan` and `apply`. `scrub` can omit it to write to stdout, or use `--in-place` instead; `-o` and `--in-place` cannot be combined. A valid single output path behaves as usual: the same bytes, the same canonical hashes, atomic replacement and the same protected-input and symlink checks. Diagnostics do not echo file contents.

### `inventory FILE... [--json]`

Every FILE entry must be nonempty. An empty path (`''`) is a usage error (exit **2**) naming `files`, whether it comes after other exports or appears several times. It is reported before the command runs, reads any file or prints a report. That holds in JSON and text mode, even when other exports are missing or malformed, and all local files are left untouched. Nonempty filenames are taken literally and never trimmed, including names made of whitespace. A path may be repeated, and records keep the order of the file arguments. At least one FILE is required. A missing or malformed export at a nonempty path gets the usual input error, without a partial report.

Lists every reference occurrence with its file, step name, flow pointer, piece name and version, RFC 6901 JSON pointer, `externalId`, and whether it is a template or a literal. For example, the Slack tool's inventory includes:

```json
{
  "step": "triage_agent",
  "pieceName": "@activepieces/piece-slack",
  "pieceVersion": "0.10.0",
  "pointer": "/flows/0/trigger/nextAction/nextAction/settings/input/agentTools/0/pieceMetadata/predefinedInput/auth",
  "externalId": "slack-dev"
}
```

Repeated references in one string get separate zero-based `occurrence` values. JSON output is one array. The default output is tab-separated, and every cell is JSON-escaped. All files are validated before any of the inventory is printed. An unknown piece owner appears as `null`, never as the piece of the enclosing parent. Such a reference fails `check` and cannot be remapped.

### `remap FILE --map OLD=NEW | --map-file MAP -o OUT [--strict] [--dry-run] [--target-connections TARGET] [--json]`

`--map` and `--map-file` can be repeated and combined. Every inline `--map` occurrence must be nonempty on `remap`, `plan`, `apply` and `verify-roundtrip`. `--map ''` and `--map=` are usage errors (exit **2**), reported before the command runs, reads any input, prints a report or writes anything. That includes dry runs, and holds whether the empty occurrence comes before or after valid inline or file mappings. Nonempty inline values are taken literally, never trimmed, and can be repeated. They must still use `OLD=NEW` syntax and valid IDs. An empty string is not an empty mapping object; for that, use a real map file containing `{}`.

Every `--map-file` occurrence needs a nonempty path on `remap`, `plan`, `apply` and `verify-roundtrip`. `--map-file ''` and `--map-file=` are usage errors (exit **2**), reported before the command runs, reads any input, prints a report or writes anything. That includes dry runs, and holds whether the empty path comes before or after valid map files. Nonempty paths are taken literally and never trimmed. Several files are read in the order given. File mappings are merged before inline mappings. Identical entries are accepted, and conflicting ones are errors. If no mapping flag is given at all, the command reports a missing mapping; for an empty map, use a real file containing `{}`.

Map files are JSON objects:

```json
{
  "slack-dev": {
    "externalId": "slack-prod",
    "pieceName": "@activepieces/piece-slack"
  },
  "gmail-dev": "gmail-prod"
}
```

A `pieceName` is a guard you assert, not a lookup. Each target must agree with the owning piece of the source reference. It must also agree with every other reference and mapping that uses the same destination, and with the target catalog if one is given. Merging connections from different pieces is refused. **Without a catalog, the command cannot check that a new destination exists or which piece it belongs to**, so the report says `catalogVerified: false`. Use `--target-connections` as a gate before import. It checks every resulting reference, including the unchanged ones, and refuses the whole write if any check fails.

`--strict` refuses references that are neither mapped sources nor already-mapped destinations. Because destinations are recognized, running the same strict promotion again produces identical bytes. Chains and cycles of non-identity mappings (`a=b,b=c` or `a=b,b=a`) are rejected: map straight to the final destination. Identical duplicate entries are accepted, and conflicting duplicates are errors. Unused mappings are listed in the JSON report. An empty map `{}` produces an exact byte copy in non-strict mode. Strict mode refuses it when the flow has references.

Only the source spans of the external IDs are replaced. Indentation, CRLF/LF line endings, the BOM, key order, unchanged Unicode/JSON escapes and every other byte stay intact. The parent directory of the output must already exist. `remap` refuses to overwrite its input, its maps or its catalog. It replaces the given output atomically through a temporary file with mode 0600, and refuses an output that is a symlink or has a symlinked parent. On success it replaces an existing output. On a validation failure it leaves that output intact. Paths inside JSON or mapping values are never used as output paths.

### `check FILE --target-connections TARGET [--json]`

`--target-connections` takes at most one nonempty path on `check`, `remap`, `plan` and `apply`. Repeating it (even with the same path), `--target-connections ''` and `--target-connections=` are usage errors (exit **2**). They are reported before any input is processed, any report is printed or anything is written, including in `remap` and `apply` dry runs. Merge all connection records into one complete catalog first. Diagnostics do not echo catalog values. A single nonempty path is taken literally and never trimmed, and the separate and `=` forms behave alike. The option is optional only for `remap`, which then reports `catalogVerified: false`; `check`, `plan` and `apply` require it. A real `[]` catalog is checked like any other and refuses every connection, which is reported as missing (exit **1**). With a valid single catalog, the checks, canonical hashes and saved-plan bytes are the same as before.

TARGET is a complete local list of the connections in **one** destination project or environment. Either a bare array or `{ "data": [...], "next": null }` is accepted. An example record:

```json
{"externalId":"slack-prod","pieceName":"@activepieces/piece-slack","status":"ACTIVE"}
```

Use external IDs, not internal database `id` values. `status` can be `ACTIVE`, `MISSING` or `ERROR`. If it is omitted, the record counts as `ACTIVE`; this is meant for handwritten inventories, and you are asserting that the connection is active. A reference whose connection is missing, belongs to the wrong piece, is inactive, or whose owner is unknown gives exit 1. Duplicate external IDs and a nonempty `next` or `nextCursor` pagination token are input errors: merge the pages into one array and filter it to the environment you intend first. All other fields are dropped, and connection values or tokens never appear in reports. This checks the metadata you supply. It does not check live authentication, project access or the health of remote services.

### `pieces FILE [--target-pieces PIECES] [--json]`

Reports the piece and version of every step and tool. PIECES accepts an array, or a complete `data` page, of `{ "name": "@activepieces/piece-slack", "version": "0.10.0" }` records. The field names `pieceName` and `pieceVersion` are also accepted, separately for each field. `pieces`, `plan` and `apply` share this catalog contract:

- Give `--target-pieces` at most once per invocation. Repeating it, even with the same path, is a usage error (exit **2**), reported before any file is read or any report or artifact is written, including in an `apply` dry run. Merge all installed pairs into one complete catalog file first. Giving the option once or not at all works and hashes exactly as before.
- An explicitly empty path (`--target-pieces ''` or `--target-pieces=`) is also a usage error (exit **2**), reported before any input is processed or anything is written. Leave the option out to skip this optional check. A real catalog containing `[]` is still checked, and every version is reported as missing. Nonempty paths are taken literally and never trimmed.
- Every alias that is present must be a nonempty JSON string. If a record has both `name` and `pieceName`, or both `version` and `pieceVersion`, the decoded strings must be exactly equal; neither alias wins. Names and versions are not case-folded, trimmed or SemVer-normalized.
- Contradictory aliases or an invalid alias value make the input malformed (exit **2**), even in unused rows or in otherwise identical duplicate records. No partial report is printed, and existing plan and output files stay intact, including in an `apply` dry run. Diagnostics name the fields without echoing their values or any unknown or credential fields.
- Single aliases, equal paired aliases, row or key order, identical name/version duplicates and ignored unknown fields all give the same canonical pairs, and the same `targetPiecesSha256` and `configSha256`. For example, `{"name":"p","pieceName":"p","version":"1.2.3-rc.1+build.007","pieceVersion":"1.2.3-rc.1+build.007"}` is valid, but changing only one of its version aliases is an input error. Prerelease and build suffixes are kept and compared exactly, build metadata included.

Versions must be exact SemVer pins. A missing source pin or a range (`~0.10.0`, `^1.0.0`, `*`) is reported as `unpinned` and exits 1, even without a target list. With a list, an exact pin missing from the target is reported as `missing-version` and exits 1. Nothing is fetched from a registry, and no version is upgraded or resolved from a range.

### `scrub FILE [-o OUT | --in-place] [--dry-run]`

Without an output flag, `scrub` writes the scrubbed JSON to stdout, and a JSON report of what was removed goes to stderr. `--in-place` explicitly turns on atomic replacement of FILE. `--dry-run` only reports the paths, and never prints or changes the document.

`scrub` removes **only** the `settings.sampleData` object of each structural step. That object holds the file IDs of test inputs and outputs, and `lastTestDate`. An unknown field inside the object is rejected, so that future data is never deleted silently. Business inputs named `sampleData`, dynamic properties, code, notes and credentials are kept. This cleans sample metadata. It is **not a general sanitizer for secrets or personal data**, and it does not delete files from Activepieces.

## Supported formats and limits

- Exports: the current template `{ "flows": [{ "trigger": ... }] }` (all flows), the legacy `{ "template": { "trigger": ... } }`, raw flow versions with `trigger`, and API flow records with `version`. Ambiguous or unknown shapes are errors.
- Structure: traversal follows `nextAction`, `firstLoopAction`, the `children` of routers and AI routers (including null branches), and `continueOnFailureBranches`. Piece, code, loop, batch, router and empty-trigger steps are recognized. Unknown step types and duplicate step names within a flow are errors.
- Connection references: every JSON string value is scanned for static connection template blocks, including nested inputs and embedded `agentTools[].pieceMetadata`. Single and double quotes are supported, as is whitespace around the index. Plain IDs are recognized only in a piece step's `settings.input.auth` and a tool's `pieceMetadata.predefinedInput.auth`. References in JSON object keys, dynamic indexing, compound connection expressions and inline or non-string auth are rejected. An auth template must contain exactly one connection expression.
- Connection IDs: 1 to 512 characters, with no whitespace, control characters, quotes, braces, brackets or backslashes. Code and templates are never evaluated. The scan is deliberately conservative: even a connection-like expression inside a code string must follow the static grammar.
- Input limits: UTF-8 JSON up to 16 MiB, nesting depth 128, 200,000 JSON value nodes and 50,000 references. Duplicate JSON keys, non-finite numbers and unpaired Unicode surrogates are errors. Archives, directories and other special files are not supported. Diagnostics do not echo values from the JSON source.
- Not covered: a nonempty saved `agentIds`, or an `agentId` input on an AI `run_agent` step, is explicitly rejected, because standalone saved agents are not embedded tool exports. Internal cached `connectionIds` are not external IDs and are kept as they are. There is no secret handling, API access, enterprise git sync, semantic editing or workflow execution. This is neither a full validator of the upstream schema nor a guarantee that an Activepieces import will succeed.

Exit codes: **0** success; **1** a refused promotion, a failed check or a missing piece; **2** malformed or unsupported input, a bad argument or an I/O error. A strict or refused remap never writes partial output. Full documents and the requested catalogs are loaded locally, and no background processes are started.

## Tests

```sh
uv run --extra test pytest tests/ap_promote
```

The suite runs promotions through the CLI in subprocesses. It covers strict and idempotent output, ownership of nested tools, targets that are missing, inactive or belong to the wrong piece, source spans with JSON escapes, error branches, what scrub preserves, resource limits, symlink rejection and the guarantee that code is never executed. Plan/apply regressions add stale sources and configs, altered plan contents, missing or mismatched tool connection versions, dry-run refusals and the exclusion of credentials. Whole-flow plan tests cover AI steps and tools without connections, error branches in nested batches, catalog hashing and omission, unpinned usages, empty and malformed catalogs, and protected output. Alias tests cover contradictory names and versions, invalid aliases in unused rows, unchanged outputs, and catalog hashes that stay stable with exact prerelease and build versions.

Round-trip regressions remove loop, router and tool references that `check` alone cannot detect, including tools inside a batch error branch. They also cover changed export envelopes, exactly escaped pointers, repeated occurrences, restoring IDs that several sources mapped to one destination, wrong IDs, ownership and versions, and extra references. The explicit-pairing tests use two-flow fixtures in different orders, with repeated step names, distinct IDs, relocated tools and repeated occurrences in escaped fields. They require a complete one-to-one pairing and check the exact pointers of lost, mismatched and extra references.

## Upstream relationship

`flowwrench ap-promote` is an independent interoperability tool and is not affiliated with Activepieces. The formats were checked against Activepieces commit `bf145a07489f5bc32a83e35e68564b861389aaed` (flow schema 28). [THIRD_PARTY.md](../THIRD_PARTY.md) lists the pinned source links and reproduces the upstream Activepieces MIT license notice. The fixtures are synthetic exports written to match these structures; no running instance was used to validate imports.

[Issue #14866](https://github.com/activepieces/activepieces/issues/14866) documents upstream support for scoped connection replacement and agent tools. This command adds offline preparation and review; it does not claim those features are missing upstream. Related motivation: [#16113](https://github.com/activepieces/activepieces/issues/16113) and [#16107](https://github.com/activepieces/activepieces/issues/16107).

## License

Apache-2.0; see [LICENSE](../LICENSE).
