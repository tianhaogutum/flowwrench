# flowwrench nodered-merge

`flowwrench nodered-merge` is a semantic diff and three-way **Git merge driver for Node-RED `flows.json`**. It merges independent edits to flat flow arrays or strict API-v2 `/flows` backups by node ID and property, combines wire edits per output port, and keeps conflict markers out of the JSON file. Conflicts and broken references are reported in a separate JSON report, and can be resolved with an editable, hash-checked decision manifest.

The command was previously the standalone Node.js tool `nrflow-merge`. It runs offline and uses only the Python standard library; Git is needed only for the Git integration. No Node-RED code is loaded or executed. Upstream relationships are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench                 # or: pipx install flowwrench
flowwrench nodered-merge --version     # prints the bare version, e.g. 0.1.0
flowwrench nodered-merge --help        # lists every subcommand and flag
```

`python -m flowwrench nodered-merge` is equivalent to `flowwrench nodered-merge`. The synthetic sample flows below live in `examples/nodered_merge/` in the flowwrench repository; run the commands from that directory (or adjust the paths).

Subcommands: `diff`, `merge`, `resolve`, `git-manifest`, `normalize`, `install`, `textconv` and `git-driver`. Use `--` before file names that begin with `-`.

## Diff

```sh
cd examples/nodered_merge
flowwrench nodered-merge diff base.json theirs.json --ignore-layout
flowwrench nodered-merge diff base.json ours.json --format json
```

The first command reports a new debug node, a change to function B, and a wire from function A to **Inspect prepared value [debug-a]** on port 0. Text output names wire endpoints; JSON has `added`, `removed`, `changed` and `layoutOnly` arrays. Changed nodes carry property paths (JSON Pointers), before/after values and per-port wire additions and removals. `diff` exits 0 for a successful comparison even when the files differ.

Node entries also include:

- `wireChanges`: outgoing edge additions/removals. Each endpoint has `id`, `name`, `valid` and `reason` (`missing-target`, `non-runtime-target` or `wrong-flow`, in that priority).
- `configReferences`: `removed`/`added` config-node references with the property pointer, target `id`, target `name`, `valid` and `reason` (`missing-target` or `non-config-target`).
- `subflowWireChanges` on subflow definitions: `in`, `out` and `status` wires with exact endpoint ports and `passThrough` for direct input-to-output wires. Reasons are `self-input-reference`, `input-port-out-of-range`, `missing-target`, `wrong-flow`, `non-runtime-target` and `output-port-out-of-range`.

Text output appends, for example, `(invalid: missing-target)` to invalid lines. `valid` is a local reference check, not a runtime deployability guarantee: no node package or runtime schema is loaded.

Only top-level `x`, `y`, `w` and `h` are layout properties. Layout-only nodes are listed separately, and `--ignore-layout` drops those properties from every changed node. Membership (`z`, `g`), subflow ports and other arrays are semantic changes.

Config references are discovered from the built-in MQTT In/Out `broker` rules and from string values that match config-node IDs seen in any input version (names, labels, function code and structural fields are excluded). A config node has no `x`, `y` or `wires` and is not a tab, subflow definition or group. For third-party references, including targets that never existed in the inputs, add repeatable `--config-ref TYPE.PROPERTY` rules (split at the final dot):

```sh
flowwrench nodered-merge diff base.json ours.json --config-ref 'mqtt in.broker' --config-ref 'mqtt out.broker'
```

## Three-way merge

```sh
flowwrench nodered-merge merge base.json ours.json theirs.json \
  -o /tmp/merged.json --conflicts-file /tmp/conflicts.json
```

The sample merges cleanly (exit 0): ours moves nodes and edits function A; theirs edits function B and adds a wired debug node. The merged file contains both edits, the new wire and ours' layout, and the report has `clean: true`.

Merge rules:

- Nodes are matched by their case-sensitive string `id`; file order is irrelevant.
- Identical changes and changes made by only one side are accepted. Different object properties merge recursively; a missing property differs from JSON `null`.
- Function source is an ordinary string and arrays are atomic properties, except top-level `wires`. No JavaScript, templates or node modules run.
- For each wire port, newly added targets are united and a base edge is removed if either side removed it. Targets are sorted and deduplicated; port indexes stay fixed, and concurrent changes to the number of output ports conflict.
- Overlapping property changes, different additions sharing an ID (`add/add`) and modify/delete are conflicts. The fallback is ours; `--prefer theirs` selects theirs. Both **still exit 1**.
- The merged graph is then checked. Dangling, non-runtime-target and cross-flow wires (including invalid subflow `in`/`out`/`status` wires) are pruned; valid direct subflow pass-through wires are kept. Nodes whose tab/subflow (`z`), group (`g`) or subflow definition is missing are kept for review, never deleted. Any integrity finding exits 1, even when its wire was pruned.

A JSON report is written on every merge, even a clean one: `OUT.conflicts.json` by default, or `--conflicts-file FILE`. It must differ from every input and the output (hard-link aliases included), and an existing report is replaced. It contains `clean`, absolute `inputs`/`output`, `conflicts`, `integrity`, `summary` and `revisionProvenance`. An abridged conflict:

```json
{
  "kind": "property",
  "nodeId": "function-a",
  "property": "/func",
  "base": {"present": true, "value": "return msg;"},
  "ours": {"present": true, "value": "return null;"},
  "theirs": {"present": true, "value": "msg.payload = 1; return msg;"},
  "selected": "ours"
}
```

Integrity findings have `kind` (`dangling-wire`, `dangling-subflow-wire`, `missing-config`, `missing-tab`, `missing-group` or `missing-subflow`), `nodeId`, `property`, `target`, `action` (`pruned` or `review`) and a `reason`. Reports include property values: store them with the same care as your flow files.

`summary.ours`, `summary.theirs` and `summary.merged` use the diff structure above, each compared with **base**. `merged` describes the graph actually written, including conflict fallbacks, reviewed decisions and pruning. `--ignore-layout` on `merge` and `resolve` only filters these summaries; it never changes merge decisions, conflicts, integrity checks or exit codes.

```sh
# Named config and wire changes in the summaries (exit 0).
flowwrench nodered-merge merge summary/base.json summary/ours.json summary/theirs.json \
  -o /tmp/merged.json --ignore-layout

# Subflow in/out/status edge annotations.
flowwrench nodered-merge diff subflow-summary/base.json subflow-summary/ours.json --ignore-layout
```

### API-v2 backups and revisions

`merge`, `resolve`, `git-driver` and `git-manifest` also accept complete `/flows` backups of the form `{"rev": "r1", "flows": [...]}` or `{"flows": [...]}`. Each input may independently be an array or a wrapper. Only `flows` and an optional nonempty string `rev` are accepted; top-level `credentials`, unknown fields and rich single-flow exports are rejected with exit 2 before anything is written.

```sh
flowwrench nodered-merge merge api-v2/base.json api-v2/ours.json api-v2/theirs.json -o /tmp/api-v2-merged.json
```

The output always keeps **ours'** shape and exact revision (here `r1`), or its absence, even with `--prefer theirs`. Revisions are opaque: they are never compared, generated or merged. The report's `revisionProvenance` records each input's format and revision and `"serverCurrentRevisionVerified": false`: **the server's current revision is never checked**, and there is no network connection or deploy command. `diff`, `normalize` and `textconv` stay array-only.

## Resolve with a decision manifest

Keep the three original files unchanged and generate an editable manifest alongside the fallback merge:

```sh
flowwrench nodered-merge merge resolution/base.json resolution/ours.json resolution/theirs.json \
  -o /tmp/merged.json --manifest /tmp/decisions.json
# Exit 1: /func conflicts; x=150 and y=200 merge independently.
```

Each manifest conflict has a stable `id`, its JSON Pointer `property`, base/ours/theirs presence and values, and `"decision": null`. Change only `decision`:

```json
{"choice": "ours"}
{"choice": "theirs"}
{"choice": "custom", "present": true, "value": "msg.reviewed = true; return msg;"}
{"choice": "custom", "present": false}
```

The last form deletes the conflicting property or whole node (use `"present": true, "value": null` to set JSON null). For a whole-node conflict, a custom replacement must be a valid node with the same ID. Then apply the reviewed decisions:

```sh
flowwrench nodered-merge resolve resolution/base.json resolution/ours.json resolution/theirs.json \
  --manifest /tmp/decisions.json -o /tmp/merged.json
```

Choosing `theirs` here exits 0 and writes node `a` with `func` equal to `msg.payload = 1; return msg;`, `x=150` and `y=200`.

The manifest records SHA-256 hashes of the **exact input bytes**, the fallback preference and the explicit config-reference rules; even a whitespace-only change to an input makes it stale. `resolve` recomputes the conflicts, requires every decision, applies them, validates the schema and checks graph integrity again. Stale hashes, unknown or missing conflicts, invalid decisions, new schema conflicts or remaining integrity issues exit **2 without replacing the output or report**. The manifest, output and report must be separate files, no original input may be overwritten, and all outputs are staged before they are renamed into place.

## Normalize and Git textconv

```sh
flowwrench nodered-merge normalize base.json > /tmp/canonical.json
flowwrench nodered-merge normalize /tmp/canonical.json -o /tmp/canonical.json
flowwrench nodered-merge textconv base.json
```

Normalization sorts nodes by ID and object keys, sorts and deduplicates each wire port, and writes two-space JSON with a final newline. Other array orders are kept. It is idempotent and never prunes or repairs references. `textconv` produces the same output for Git's diff interface.

## Git merge driver

The driver is installed per repository. Git runs the configured command through the shell, so **`flowwrench` must be on `PATH`** whenever Git merges or diffs `flows.json`, for you and for every collaborator (with `pipx install flowwrench` it is). Run from the target repository, or pass `--repo DIR`:

```sh
flowwrench nodered-merge install            # prints a JSON plan, changes nothing
flowwrench nodered-merge install --apply    # writes the .gitattributes block and local Git config
```

`--apply` adds an idempotent managed block to the root `.gitattributes`:

```gitattributes
# BEGIN nrflow-merge
flows.json merge=nrflow-merge diff=nrflow-merge
# END nrflow-merge
```

and sets **local** Git configuration equivalent to:

```sh
git config --local merge.nrflow-merge.name 'Node-RED semantic three-way merge'
git config --local merge.nrflow-merge.driver 'flowwrench nodered-merge git-driver "%O" "%A" "%B"'
git config --local merge.nrflow-merge.recursive binary
git config --local diff.nrflow-merge.textconv 'flowwrench nodered-merge textconv'
```

The driver and attribute name stay `nrflow-merge`, so repositories already configured with the original tool keep working once their config points at `flowwrench`. No hooks are installed. Use `--pattern 'config/flows.json'` (a relative Git glob) for another location; the default matches `flows.json`, **not** `flows_cred.json`, and you should keep credentials files out of custom patterns. Whitespace, quotes, backslashes, absolute patterns and `..` segments are rejected. Git does not distribute local config: each collaborator runs `install --apply` once. Linked worktrees share the repository's local config.

Git passes `%O` (ancestor), `%A` (ours, rewritten in place with valid JSON) and `%B` (theirs). The driver exits 1 on conflict and keeps its report under `$(git rev-parse --git-path nrflow-merge-conflicts)/`, named after Git's temporary ours file; the diagnostic prints the path. These reports persist until you remove them and can contain property values. The recursive driver is Git's `binary` driver, so a preferred fallback is never used as a synthetic common ancestor.

After a conflict, review the report, edit `flows.json`, `git add` it and continue the merge. To remove the integration, delete the managed `.gitattributes` block and run:

```sh
git config --local --remove-section merge.nrflow-merge
git config --local --remove-section diff.nrflow-merge
```

### Capture a manifest from the unmerged index

Git deletes the driver's temporary inputs once it returns. Before staging a conflicted flow, retain the index versions and generate a manifest:

```sh
flowwrench nodered-merge git-manifest flows.json --snapshot-dir ./flow-review
# Exit 1 when the semantic merge has conflicts or integrity issues.
# Edit flow-review/decisions.json, then:
flowwrench nodered-merge resolve flow-review/base.json flow-review/ours.json \
  flow-review/theirs.json --manifest flow-review/decisions.json -o flows.json
git add flows.json
```

`git-manifest` reads only that path's stages **1 (base), 2 (ours) and 3 (theirs)** from the current index; it never reads the working file, updates the index or continues the merge. `--snapshot-dir` must not exist and its parent must. It creates a private (0700) directory with `base.json`, `ours.json`, `theirs.json` (exact bytes), an editable `decisions.json`, a preview `merged.json` and `merged.json.conflicts.json`, and prints a JSON receipt with their paths and the `gitIndex` provenance (repository, path, stage modes and blob IDs). The snapshots stay usable after the index changes. `--prefer`, `--config-ref` and `--ignore-layout` work as on `merge`.

For an **add/add file conflict** there is no stage 1, and capture exits 2 by default. Opt into an empty ancestor explicitly with `--allow-missing-base`: `base.json` is then exactly `[]` and the provenance records `gitIndex.syntheticBase: {"reason":"missing-stage-1","value":[]}`. Missing ours/theirs stages, stage-0-only paths, symlink or gitlink entries, malformed or oversized blobs and existing destinations exit 2. The `gitIndex` record is an informational capture claim, not authenticated Git history.

## Exit codes and limits

| Code | Meaning |
| --- | --- |
| 0 | Successful operation or clean merge |
| 1 | Merge conflict or graph integrity issue; the JSON output and report were written |
| 2 | Invalid input, unsupported format, CLI misuse, I/O or configuration error, or rejected resolution; nothing is replaced |

Invalid JSON or UTF-8, unsupported shapes, duplicate IDs, invalid wire shapes and oversized input fail before any merge output is written. Outputs are staged next to their destinations and renamed atomically; symlinked or non-regular outputs are rejected and parent directories must exist. Existing file permissions are kept; new merge files and reports are owner-only. Several renames and Git configuration changes are not a filesystem transaction.

Limits: 32 MiB per input, 100,000 nodes and 64 JSON nesting levels. Non-finite numbers are rejected; other numbers use JavaScript JSON precision. Nested single-flow/subflow exports, YAML and top-level credentials objects are rejected. There is no editor, credential decryption, semantic merge of function code, third-party node schema loading or port renumbering inference, and no guarantee that the result deploys. Other array edits (link and group lists, subflow port arrays) conflict atomically when both sides change them.

## Tests

```sh
uv run --extra test pytest tests/nodered_merge
```

The suite runs offline with synthetic fixtures. Git integration tests need `git`: they create temporary repositories and run real `git merge` and `git diff --textconv` through the configured driver, using a temporary `flowwrench` shim on `PATH` that points at the checkout.

## License

Apache-2.0; see [LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md). This tool uses the documented [Node-RED flow format](https://nodered.org/docs/api/admin/types) and [Git's attribute interfaces](https://git-scm.com/docs/gitattributes); it does not embed either project and is not affiliated with or endorsed by them.
