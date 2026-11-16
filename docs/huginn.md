# flowwrench huginn

`flowwrench huginn` (formerly `huginn-scenario-kit`) manages Huginn scenarios in git
without a running Huginn instance. Split one export into deterministic per-agent YAML
files, keep links by GUID, render reusable configuration, and review semantic changes
before importing the assembled JSON. Combine two exports or concrete YAML kits with a
parameterized GUID relocation manifest while retaining their agent fields and explicitly
resolving scenario metadata conflicts. Manifest v2 can also add explicit event and
control edges between the inputs.

No login, network access, agent execution, environment-variable lookup or paid API is
used by any command.

## Install

Requires Python 3.11 or newer.

```sh
pip install flowwrench          # or: pipx install flowwrench
flowwrench huginn --version     # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx flowwrench huginn lint ./my-kit
```

`python -m flowwrench huginn` is equivalent to `flowwrench huginn`. The only runtime
dependency of this command is [PyYAML](https://github.com/yaml/pyyaml), which is a
regular dependency of `flowwrench` and is installed automatically.

The example inputs below live in `tests/huginn/fixtures/` in the flowwrench repository;
run the commands from the repository root (or adjust the paths).

## Split and join an export

Use fresh output paths; existing files/directories are never overwritten. Output
parents must already exist.

```sh
mkdir -p out
flowwrench huginn split tests/huginn/fixtures/weather.json -o out/weather
flowwrench huginn join out/weather -o out/weather.json
flowwrench huginn diff tests/huginn/fixtures/weather.json out/weather.json --exit-code
flowwrench huginn lint out/weather
```

The fixture has three agents and two event links. Split produces:

```text
out/weather/
  scenario.yaml
  agents/
    weather-feed--aaaaaaaa.yaml
    format-report--bbbbbbbb.yaml
    morning-digest--cccccccc.yaml
```

`scenario.yaml` contains `kit_version: 1`, a `scenario` mapping with export metadata,
and `links` / `control_links` lists. Link fields retain their Huginn names, but use
GUIDs instead of indices:

```yaml
links:
- source: aaaaaaaa000000000000000000000001
  receiver: bbbbbbbb000000000000000000000002
control_links: []
```

Each agent file contains the original agent object, including `guid`, `type`,
`name`, `disabled`, `options`, and any scheduling/retention fields. Filenames are
labels, not identities: you may rename them. All regular `agents/*.yaml` files
are read; unexpected entries in `agents/` are errors. Other files in the kit root
are ignored, allowing local notes or a vars file.

Agents and links are sorted by GUID during assembly. Index-based
`source/receiver` and `controller/control_target` are rebuilt from that order.
`exported_at` is intentionally removed; absent link lists become empty lists.
All other scenario/agent fields are retained, including unknown extension fields.
Round-trip equivalence means the same GUID-keyed agents, metadata and directed
links, rather than identical JSON bytes or original array order.

Invalid or duplicate **original agent GUIDs** also report their original identity
field before sorting. JSON exports identify `/agents/<original index>/guid`;
kits identify the original agent YAML file at `/guid`, even after files are
renamed. A duplicate identifies the first repeated occurrence in JSON array
order or sorted kit filename order, rather than its earlier occurrence:

```text
error: /path/to/input.json#/agents/2/guid: duplicate agent guid
error: /path/to/kit/agents/renamed.yaml#/guid: agent guid must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric
```

Missing GUIDs and invalid JSON types use the same invalid-GUID reason; supplied
values are omitted. This loader check applies to `graph`, `split` (JSON), `join`
(kit), `render`, `lint`, `diff` (either input), `extract-vars`, and either input
of `combine`. Failures exit 2 with empty stdout and leave sources and outputs
unchanged, including applicable dry-run. Library `load_scenario`/`from_export`
raise `AgentValidationError` with `source`, `pointer`, and `reason`; in-memory
`from_export` uses the original index without inventing a filename. Identity
locations are separate from the GUID-keyed field-origin sidecar below. Existing
validation order is retained. Final collisions introduced by combining or
relocating valid inputs keep their generic collision diagnostics; they do not
claim an original input field. Original scenario metadata and schema diagnostics
are outside this extension.

Event/control link errors identify the **absolute source filename and JSON Pointer**
before any sorting. For example, changing a receiver to an absent GUID in a split
kit makes both `join` and `render` fail with exit 2:

```text
error: /path/to/kit/scenario.yaml#/links/0/receiver: dangling guid
```

An invalid JSON export index instead identifies the original export, for example
`/path/to/input.json#/control_links/1/control_target: dangling or non-integer link index`.
All four endpoint fields are checked. A deleted agent file leaves a dangling
reference at its remaining link endpoint; diagnostics point to that reference.
Malformed arrays/edges and duplicate edges identify the array or edge pointer.
The same checks apply during dry-run, before creating output, and through
`lint`, `diff`, `extract-vars`, and either source of `combine`. Endpoint values
are omitted. Library callers can catch `ReferenceValidationError` (a `KitError`)
from `flowwrench.huginn.formats` and inspect `source`, `pointer`, and `reason`;
in-memory validation has no source filename unless one is supplied.

Huginn's pinned schema-v1 format represents agents' propagation/receiver
relationships in the top-level `links` and `control_links` arrays. It does not
define per-agent `receivers` or `propagation` arrays. Such arrays in options or
unknown extension fields remain opaque data; they are not inferred as graph links.

Names are converted to bounded ASCII slugs, with `agent` as a fallback. GUID
prefixes start at eight characters (or the whole GUID if shorter) and extend on
filename collisions. A name change can rename a file; a GUID change is a new
agent identity.

## Export a read-only dependency graph

```sh
flowwrench huginn graph tests/huginn/fixtures/control.json
flowwrench huginn graph out/weather -o out/weather.dot
```

`graph SCENARIO_OR_KIT [-o NEW.dot]` accepts a concrete schema-v1 JSON export
or YAML kit directory. It writes deterministic DOT to stdout by default; `-o`
creates a new private file (0600), with the same exclusive-create rules and
existing-parent requirement as other commands. Invalid input produces exit 2,
empty stdout and no output creation/replacement. Source files are never changed.
Graphviz is not required or invoked.

Each agent has one node whose identity is its **exact GUID**, independent of its
name or kit filename. Labels display the agent name and type; disabled agents
are marked `(disabled)` with a dashed node outline. Isolated agents remain
visible. Event edges are solid and labeled `event`; control edges are dashed
and labeled `control`. Cycles, self-links and both kinds between the same pair
are retained in a non-strict `digraph`. Nodes are sorted by GUID; event edges
then control edges are each sorted by their source/target GUIDs. Reordering an
export (with indices remapped) or renaming kit files produces identical bytes.
Changing an agent name affects its display label, never its identity or edges.

Display text uses DOT HTML-like labels, with numeric character references for
quotes, backslashes, markup characters and non-ASCII Unicode scalars. Control
characters and Unicode line/paragraph separators display as `[U+XXXX]` markers;
input text cannot introduce label markup, literal line breaks or DOT attributes.
Only GUID/name/type/disabled and top-level event/control links enter the output.
Options, credential references, scenario metadata and opaque `receivers` or
`propagation` fields are omitted; they never imply edges.

The full structural loader, GUID/link checks and resource limits still apply,
including a 5 MiB generated-DOT limit checked before output. Duplicate identities
or per-kind edges and dangling/invalid references are errors. Inputs must be
concrete: unresolved/malformed HSK values or reserved HSK keys are rejected,
including in omitted fields. Render templates separately first. This command
has no vars or dry-run option and does not expand HSK/Liquid, look up environment
variables, inventory/resolve credentials, evaluate schedules or execute agents.
Unknown string schedules are accepted **unevaluated**, consistent with structural
loading; non-string/non-null schedules remain invalid. A successful graph export
does not validate Huginn runtime behavior, agent options or plugin availability.

## Render variables while preserving Liquid

The reserved placeholder syntax is **`${HSK:NAME}`**, distinct from Liquid's
`{{ expression }}` and `{% tag %}`. Start from the included editable template:

```sh
flowwrench huginn extract-vars tests/huginn/fixtures/template
flowwrench huginn render tests/huginn/fixtures/template \
  --vars tests/huginn/fixtures/vars.example.yaml -o out/rendered.json
flowwrench huginn lint out/rendered.json
```

Inventory output:

```json
{
  "credentials": ["weather_key"],
  "variables": ["FEED_URL", "LOCATION", "RETRIES", "SCHEDULE"]
}
```

For example, an option `url: '${HSK:FEED_URL}'` takes the provided URL, while
`message: '{{ location }}: {{ temperature }} C'` and
`'{% credential weather_key %}'` remain literal Liquid text for Huginn.
`render` emits assembled Huginn JSON, to stdout by default or to a new `-o` file.
It does not modify the template directory.

Variable names match `[A-Za-z_][A-Za-z0-9_.-]*`. Vars YAML is a flat mapping;
`a.b` is one key, not a lookup expression. Missing names are reported together in
sorted order; unused variables are permitted by default. No environment fallback exists.

Add `--reject-unused-vars` to catch extra bindings such as misspelled names:

```sh
flowwrench huginn render tests/huginn/fixtures/template \
  --vars tests/huginn/fixtures/vars.example.yaml --reject-unused-vars -o out/rendered.json
# Intentional extra LOCATON binding: exits 2 with "error: unused variables: LOCATON".
flowwrench huginn render tests/huginn/fixtures/template \
  --vars tests/huginn/fixtures/vars.unused.example.yaml --reject-unused-vars
```

This opt-in check counts supported HSK placeholders in original template values,
including nested mappings/lists, repeated references and whole-field placeholders.
Liquid and credential names do not count as HSK references, and supplied values
are never scanned for usage. Unused names are listed in sorted order without their
values; invalid binding names receive a generic error without echoing the key.
Original malformed placeholders/reserved template keys and aggregate missing-variable
errors retain their existing diagnostics and take precedence over unused bindings.
Rejection exits 2 with empty stdout before substitution or any output write, also
with `--dry-run`; existing destinations stay untouched. Outputs are never overwritten
and there is no `--force` option. Library callers can opt in with
`render(doc, variables, reject_unused_vars=True)`.

- A whole-string placeholder preserves the supplied JSON type: string, boolean,
  number, null, list or mapping. This also supports typed agent fields such as
  `disabled`, `keep_events_for`, `propagate_immediately` and an entire `options`
  mapping; the rendered result must pass validation.
- An embedded placeholder accepts scalar values only. Booleans/null use the text
  `true`, `false`, `null`. Lists and mappings embedded in text are errors.
- Replacement runs once. Values that introduce another HSK placeholder, malformed
  placeholders, and placeholders in mapping keys are errors. The syntax is
  reserved; literal `${HSK:...}` text has no escape in v1.
- Scenario/agent GUIDs, link endpoints and schema versions must be concrete.
  `join` rejects unresolved placeholders; use `render` for a template.
- `extract-vars` reports sorted, deduplicated static credential names composed of
  letters/digits/underscore/dot/hyphen, optionally quoted. Complex/dynamic names
  in credential tags cause an explicit error. It does not interpret Liquid or
  infer credentials constructed through other Liquid expressions.

Invalid agent `name`, `type`, `options`, `disabled`, `propagate_immediately`,
`keep_events_for`, and `schedule` fields identify their original source file and
JSON Pointer. For example, `disabled: '${HSK:DISABLED}'` with vars `DISABLED: 1`
fails with exit 2 before writing output, including with `--dry-run`:

```text
error: /path/to/kit/agents/original-label.yaml#/disabled: must be a boolean
```

Renamed YAML files retain their own locations even when GUID sorting changes
agent order. JSON templates identify the original export array position, e.g.
`/path/to/template.json#/agents/2/disabled`. Nested embedded-collection errors
also identify the template location: an option `a/b` containing a `~key` list
can report `#/options/a~1b/~0key/0`. Pointers escape `~` as `~0` and `/` as `~1`;
diagnostics omit field and variable values. Options remain opaque data: the tool
checks their mapping type and substitution rules, not per-agent class semantics.

`render` and `extract-vars` also locate malformed placeholders in original agent
values. For example, `a/b: {~key: ['${HSK:bad space}']}` reports the original agent
file with `#/options/a~1b/~0key/0: malformed placeholder; use ${HSK:NAME}`. JSON
templates retain the original `/agents/<index>` prefix through sorting.

Reserved HSK keys already present in agent templates remain forbidden. During
`render` or `extract-vars`, a valid or malformed `${HSK:` key reports `placeholders in mapping keys
are not supported` at the **containing mapping**, without including the key text.
For example, `options -> a/b -> ~key -> list index 0 -> mapping` reports
`#/options/a~1b/~0key/0`. A key on the agent itself identifies the YAML document
root (`#`) or the original JSON `/agents/<index>` object. Keys are never rendered.
`extract-vars` exits 2 with empty stdout on either syntax error, without changing
input files or printing a partial inventory, malformed string or forbidden key.
Valid inventories retain sorted, deduplicated variable and static credential names.

Values that introduce valid HSK placeholders report `variable values may not
introduce placeholders (rendering is single-pass)` at the original substitution
site. Introduced malformed placeholders retain the `malformed placeholder`
reason. For example, `options: '${HSK:OPTIONS}'` with a supplied nested mapping
containing either kind points to **`#/options`**, never to inserted keys or list
indices. Introduced placeholder keys remain forbidden and also point to the
substitution site. Embedded replacements that assemble a placeholder across
multiple variables are rejected there too. These errors omit supplied values and
inserted names, exit 2 with empty stdout, and create no output, including during
dry-run. No recursive expansion occurs, even when the introduced name is supplied.

Library callers can catch `AgentValidationError` (a `KitError`) from
`flowwrench.huginn.formats` and inspect `source`, `pointer`, and `reason`.
To retain file locations across loading, sorting and copying, pass a fresh dict
as `agent_origins` to `load_scenario` (or `from_export`), then pass that same dict
to `extract`, `render`, `validate`, or `to_export`. This GUID-keyed sidecar is
scoped to one scenario and never enters exported data. Without it, in-memory errors
identify the current agent index with no invented filename. This placeholder
diagnostic extension covers `render` agent values and original agent values/keys
during `extract-vars`. Apart from original agent GUIDs described above, identities,
scenario metadata (including its keys), other commands' placeholder errors, parser
errors, aggregate missing-variable errors and expansion-limit errors retain their
existing styles.

Do not commit real vars or rendered secrets; keep them in ignored paths such as
`vars.local.yaml` and `out/` (add them to your own `.gitignore`). File outputs are
created with mode 0600; split directories use 0700. Rendering to stdout deliberately
prints the resulting configuration. Diff output contains paths and GUIDs, with no
old/new option values; inventories contain names only. Parser diagnostics omit
source snippets.

## Combine exports or kits with a relocation manifest

```sh
mkdir -p out
flowwrench huginn combine tests/huginn/fixtures/relocation/left.json tests/huginn/fixtures/relocation/right.json \
  --manifest tests/huginn/fixtures/relocation/manifest.yaml --vars tests/huginn/fixtures/relocation/vars.yaml \
  -o out/combined.json
flowwrench huginn diff tests/huginn/fixtures/relocation/expected.json out/combined.json --exit-code
flowwrench huginn lint out/combined.json
```

The two synthetic inputs each have agents named by GUID `feed` and `sink`; the
right input also has a `control` agent. The manifest is a separate data-only
YAML/JSON file, with exactly these three sections:

```yaml
relocation_version: 1
guids:
  left: {}
  right:
    feed: '${HSK:PREFIX}_feed'
    sink: '${HSK:PREFIX}_sink'
scenario:
  guid: 'combined_${HSK:ENV}'
  name: 'Combined ${HSK:ENV}'
```

With `PREFIX: copy` and `ENV: demo`, the output agents are sorted as `control`,
`copy_feed`, `copy_sink`, `feed`, `sink`. Event links become indices `1 → 2` and
`3 → 4`; control links become `0 → 1` and `0 → 2`. The full expected output is
[tests/huginn/fixtures/relocation/expected.json](../tests/huginn/fixtures/relocation/expected.json).

- Each input independently accepts a valid schema-v1 **JSON export or concrete YAML
  kit directory**. Each `guids` map uses original agent GUIDs as concrete keys,
  scoped to `left` or `right`. Unlisted GUIDs remain unchanged. Mappings apply
  simultaneously: swaps are allowed, targets are not looked up again, and
  event/control references follow the new GUIDs before indices are rebuilt.
  Duplicate final GUIDs, within either input or across them, are errors; agents
  are never silently deduplicated.
- `scenario` contains explicit top-level metadata replacements. Nonconflicting
  fields from either input survive. If a field occurs in both with different
  values or JSON types (including nested differences), it must have a replacement
  here. For example, different input names/GUIDs require explicit output values.
  Nested metadata objects are compared as complete values and are not merged.
  `agents`, `links`, `control_links`, and `schema_version` are reserved.
- JSON inputs preserve `exported_at`; different JSON input timestamps require an
  explicit `scenario.exported_at` replacement. Kits use split/join normalization:
  their format omits `exported_at` and forbids adding it to `scenario.yaml`. A
  mixed JSON+kit result inherits the JSON timestamp, while kit+kit has no timestamp
  unless the manifest explicitly sets one. For exact JSON/kit equivalence compare
  against exports with the same normalized metadata. Every other agent/metadata
  field remains unchanged unless explicitly mapped or replaced. Agent/link array
  order is canonicalized and absent link lists become empty lists. Unknown fields,
  option values, Liquid and credential tags remain literal data; GUID-like strings
  inside them are not graph references.
- `${HSK:NAME}` parameters apply **only to manifest values**, using the same
  bounded single-pass rules as render. Source inputs are not rendered. `--vars`
  is optional for a concrete manifest. No environment lookup or key substitution
  occurs; `relocation_version` must be concrete integer 1 (or 2 for added edges,
  described below).
- Removing the two right-side mappings from this example fails with duplicate
  GUIDs. Removing the metadata replacements fails with sorted conflicting field
  names. Unknown source GUIDs, invalid targets and missing parameters also fail
  with exit 2 before writing output. Diagnostics omit conflicting values.

Output defaults to stdout; `-o` creates a new private file. `--dry-run` performs
the same validation and generated-file size check, printing counts only, without
checking destination writability. The combined graph must fit the same aggregate
agent/link/node limits as any scenario. Manifest v1 retains the two input graphs
and does not accept new edge sections.

Add `--reject-unused-vars` to reject extra manifest bindings, for either manifest
version and any supported JSON/kit input pair:

```sh
flowwrench huginn combine tests/huginn/fixtures/relocation/left.json tests/huginn/fixtures/relocation/right.json \
  --manifest tests/huginn/fixtures/relocation/manifest.yaml --vars tests/huginn/fixtures/relocation/vars.yaml \
  --reject-unused-vars --dry-run
# Intentional extra PREFX binding: exits 2 with "error: unused variables: PREFX".
flowwrench huginn combine tests/huginn/fixtures/relocation/left.json tests/huginn/fixtures/relocation/right.json \
  --manifest tests/huginn/fixtures/relocation/manifest.yaml \
  --vars tests/huginn/fixtures/relocation/vars.unused.example.yaml \
  --reject-unused-vars
```

Only supported HSK placeholders in **original manifest values** count as usage,
including nested, repeated and whole-field references. Source fields, Liquid,
credential names and supplied variable values never count or become rendered.
When all bindings are used, output is identical to default combine. Extra bindings
remain permitted by default; a concrete manifest may omit `--vars`.
Strict failures list sorted unused valid names without values; invalid binding
names receive a generic error without echoing keys. Original malformed/key and
aggregate missing-variable errors retain their diagnostics and precedence.
Rejection exits 2 with empty stdout before output operations, including `--dry-run`
and existing destinations. Successful outputs remain private and exclusive;
there is no `--force`. Library callers can pass `reject_unused_vars=True` to
`flowwrench.huginn.relocation.combine` with the same manifest-only rules.

Manifest substitution errors identify the **absolute original manifest filename
and escaped JSON Pointer**, for both v1/v2 and YAML/JSON. For example, a malformed
value under `scenario -> extra -> a/b -> ~key -> list index 0` exits 2 with:

```text
error: /path/to/manifest.yaml#/scenario/extra/a~1b/~0key/0: malformed placeholder; use ${HSK:NAME}
```

Original valid or malformed `${HSK:` mapping keys identify their containing
mapping, without printing the forbidden key. Values that introduce valid or
malformed HSK syntax, including nested values/keys or tokens assembled across
embedded replacements, identify the **original substitution string**. Inserted
names, paths and supplied values never appear in these diagnostics. Replacement
still runs once; even a supplied introduced name is not expanded. Embedded
collection substitutions also identify their original string, omitting binding
names. These failures produce empty stdout and no output creation/replacement,
including with `--dry-run` and existing destinations. Input files are unchanged.
Library callers can catch `LocatedValidationError` from
`flowwrench.huginn.formats` and inspect `source`, `pointer`, and `reason`;
pass `manifest_source` to `combine` to retain the filename. In-memory errors
have only pointers. Parser/manifest-shape, aggregate missing/unused-variable and
expansion-limit errors retain their existing styles and precedence. V2 endpoints
remain concrete and are validated before substitution. Default and strict valid
combined exports remain identical.

Invalid relocated GUID targets also identify their **original manifest mapping
value**, for both sides and manifest versions, whether literal or supplied by
a whole-field parameter. For example:

```text
error: /path/to/manifest.yaml#/guids/right/feed: manifest.guids.right target must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric
```

Invalid strings and JSON types (including null, booleans, lists and mappings)
retain the same target reason, without supplied values or inserted collection
paths. These errors use the same `LocatedValidationError` API and output/input
protections described above, including dry-run. Pointers use JSON Pointer
escaping; original source GUID keys still undergo their existing concrete GUID
validation first.

Unknown relocation source GUIDs also identify the original mapping value:

```text
error: /path/to/manifest.yaml#/guids/left/absent: unknown source guid in manifest.guids.left: absent
```

Sources are scoped to the selected input: a GUID found only in the opposite
input is still unknown. This applies to both sides, v1/v2 JSON/YAML manifests,
and JSON/kit input pairs, with concrete or parameterized valid targets. The
same location API and output/input protections apply, including dry-run; targets
are omitted. Source-key grammar and invalid targets retain precedence over
unknown-source checks. Valid GUID keys exclude `/` and `~`; the shared pointer
encoder still applies JSON Pointer escaping. Known-source mappings, swaps and
duplicate identity rejection remain unchanged. This bounded location scope does
not add diagnostics for other manifest shapes or change graph export/render
substitution policies.

Invalid `manifest.scenario.name` and `manifest.scenario.guid` replacements also
identify their **original manifest field**, for v1/v2 JSON/YAML manifests:

```text
error: /path/to/manifest.yaml#/scenario/name: scenario.name must be a nonempty string
```

Invalid literal or whole-field supplied strings/types retain the existing
scenario validation reason at `/scenario/name` or `/scenario/guid`. Embedded
replacements that produce invalid metadata identify the same original string;
embedded collection and placeholder errors retain their substitution reasons.
Supplied values and inserted collection paths are omitted. The same
`LocatedValidationError` API, empty-stdout and input/output protections apply,
including dry-run and JSON/kit input pairs. Earlier input, metadata-conflict,
relocation and v2 edge errors retain precedence; final JSON bounds precede name,
then GUID, then graph validation. Valid overrides produce identical output.
This location extension applies only to these manifest replacements; other
commands and original scenario/schema diagnostics keep their existing behavior.

### Use concrete kit inputs

Either side may be a kit, including when the original GUIDs overlap. Relocations
and v2 endpoints still refer to **original GUIDs scoped to that input**; filenames
are only labels. Swaps and mappings apply once, simultaneously, as with JSON.
For example, after creating `out/`:

```sh
flowwrench huginn split tests/huginn/fixtures/relocation/left.json -o out/left-kit
flowwrench huginn combine out/left-kit tests/huginn/fixtures/relocation/right.json \
  --manifest tests/huginn/fixtures/relocation/manifest-v2.yaml --vars tests/huginn/fixtures/relocation/vars.yaml \
  -o out/mixed.json
flowwrench huginn diff tests/huginn/fixtures/relocation/expected-v2.json out/mixed.json --exit-code
```

This mixed example equals `expected-v2.json` exactly, including the timestamp
from the right JSON input. Splitting both inputs and combining the two kits yields
the same result **without `exported_at`**. All other metadata, agent fields, opaque
options, Liquid and credential tags survive. Kits are assembled in memory and
never modified; no temporary export is written into the source directories.

Kits must be concrete: manifest `--vars` never resolves source fields. Render a
template to a separate JSON file first, then combine that rendered export.
Unresolved or malformed HSK placeholders in kit agent values identify the original
YAML filename and escaped JSON Pointer (e.g. `agents/renamed.yaml#/options/url`);
metadata values identify `scenario.yaml#/scenario/...`. Placeholder keys are
rejected at their containing mapping. Concrete identities/schema fields retain
the existing validation rules. Dangling kit edges identify `scenario.yaml` and
the original endpoint pointer. These errors, metadata conflicts, duplicate final
identities and duplicate edges fail before output, including `--dry-run`.

For backward compatibility, JSON input strings remain literal, including HSK-like
strings in opaque options; kit inputs follow the concrete `join` boundary.
Arbitrary standalone YAML exports and automatic source rendering are unsupported.
The Python `flowwrench.huginn.relocation.combine` API continues to accept two export
dictionaries; kit-path loading is a CLI adapter, not a new dictionary format.

### Add event and control edges with manifest v2

Use **`relocation_version: 2`** to add edges. It requires exactly five sections:
`relocation_version`, `guids`, `scenario`, `links`, and `control_links`. The GUID
maps and metadata rules are the same as v1; both edge lists are required, with
`[]` meaning no additions. Version 1 keeps its original three-section contract.

[tests/huginn/fixtures/relocation/manifest-v2.yaml](../tests/huginn/fixtures/relocation/manifest-v2.yaml)
extends the example above with these declarations:

```yaml
relocation_version: 2
# guids and scenario: same as the v1 example above
links:
- source: {input: left, guid: sink}
  receiver: {input: right, guid: feed}
control_links:
- controller: {input: right, guid: control}
  control_target: {input: left, guid: feed}
```

```sh
flowwrench huginn combine tests/huginn/fixtures/relocation/left.json tests/huginn/fixtures/relocation/right.json \
  --manifest tests/huginn/fixtures/relocation/manifest-v2.yaml --vars tests/huginn/fixtures/relocation/vars.yaml \
  -o out/combined-v2.json
flowwrench huginn diff tests/huginn/fixtures/relocation/expected-v2.json out/combined-v2.json --exit-code
flowwrench huginn lint out/combined-v2.json
```

Each endpoint must contain exactly `input` (`left` or `right`) and `guid`, a
**concrete original agent GUID in that input**. Endpoint placeholders, indices,
unscoped GUID strings, and extra fields are rejected. The original GUID is
resolved once through that input's simultaneous relocation map, even during
swaps. A final GUID is not an endpoint alias: `{input: right, guid: copy_feed}`
fails here because the right input originally contains `feed`, not `copy_feed`.
GUID-map targets and scenario metadata values may still use HSK parameters;
endpoint GUIDs and input selectors may not.

The output retains all existing agents, metadata, and edges, plus event
`sink → copy_feed` and control `control → feed`. In the final sorted five-agent
array these added edges are `4 → 1` and `0 → 3`; the output has three edges of
each kind and matches
[tests/huginn/fixtures/relocation/expected-v2.json](../tests/huginn/fixtures/relocation/expected-v2.json).

New edges may also connect agents within one input. Duplicate pairs are rejected
**after relocation**, both among additions and against inherited edges, separately
for event and control links. The same pair may occur once in each kind. Self-loops
are retained and lint can warn about them. Unknown/wrong-input original GUIDs and
duplicates exit 2 with the absolute manifest filename and JSON Pointer, e.g.
`/path/to/manifest.yaml#/links/0/receiver/guid` or `#/control_links/1`.
Endpoint values are omitted from these diagnostics. Library callers can pass
`manifest_source` to `combine` for file locations; otherwise errors have only
pointers. Validation finishes before output, including `--dry-run`; the final
graph must satisfy the existing identity, reference and aggregate bounds. Inputs
are never modified, and no missing agent or edge is silently deduplicated.

## Diff and lint

```sh
flowwrench huginn diff old.json new-kit/ --exit-code
flowwrench huginn lint new-kit/
```

Diff accepts either JSON exports or kit directories on either side, including
unrendered templates. It reports added/removed agent GUIDs, changed agent paths,
scenario metadata paths, and added/removed links of both kinds. Paths use escaped
JSON Pointer syntax, e.g. `/options/headers/X-API-Key` or `/options/rules/0/value`.
Nested arrays are compared by index. Object key order, agent order, link order and
`exported_at` do not create changes. Types are significant (`true` differs from `1`).

Lint reports unknown schedules, isolated agents with **no incoming or outgoing
links of either kind**, and strongly connected cycle components (including
self-loops). Event-link and control-link cycles are checked separately. An
isolated scheduled source may be intentional; warnings require human judgment.
Lint validates concrete agent fields; render typed templates before linting.
Diff and lint can inspect unknown schedules; graph leaves schedules unevaluated.
Split, join and rendered outputs reject unknown schedules.

| Exit | Meaning |
| --- | --- |
| 0 | Successful operation; diff has no changes or `--exit-code` was omitted; lint has no findings |
| 1 | Diff found changes with `--exit-code`, or lint found warnings/errors |
| 2 | CLI usage, parse, validation, unsupported-input or filesystem error |

`split`, `join`, `render`, and `combine` accept `--dry-run`. They perform input,
transformation and format validation, then print counts only. They create no
files/directories, perform no output-destination writability check, and never
invoke workflow steps.

## Supported format and boundaries

- Huginn **integer `schema_version: 1`** only. Version 0 day-based retention and
  future schema versions are rejected. Required scenario name/GUID and required
  agent GUID/name/type/options/disabled are checked; booleans and nonnegative
  retention seconds retain their JSON types.
- GUIDs: 1–128 ASCII letters/digits/underscore/hyphen, starting alphanumeric;
  agent GUIDs must be unique. This covers Huginn-generated hexadecimal GUIDs.
- Both link directions are validated. Negative, boolean, fractional, string or
  out-of-range indices, dangling GUIDs, duplicate edges and unknown link fields
  are errors. Links may only refer to agents contained in the scenario.
- Known schedules: `every_1m`, `every_2m`, `every_5m`, `every_10m`, `every_30m`,
  `every_1h`, `every_2h`, `every_5h`, `every_12h`, `every_1d`, `every_2d`, `every_7d`,
  `midnight`, `noon`, `1am`–`11am`, `1pm`–`11pm`, `never`.
  Omitted/null schedules are allowed for unscheduled agents. Custom schedules or
  new upstream names are rejected by split/join/render; graph leaves them
  unevaluated and lint reports them.
- UTF-8 JSON / data-only YAML with string keys and finite JSON-compatible values.
  YAML uses JSON-style booleans/numbers: `true`/`false` are booleans; `yes`, `on`,
  dates and `012` remain strings. Quote strings when in doubt. Duplicate keys,
  YAML aliases/merge keys, executable tags and non-JSON scalar types are rejected.
- Bounds: 5 MiB per input or generated file, 20 MiB per kit, 1,000 agents,
  10,000 links of each kind, nesting depth 64, 100,000 nodes per loaded document
  and combined scenario. Expansion bytes, nodes and depth are checked before
  variable substitution. YAML counts both keys and values toward its node cap.
- Symlink paths, including ancestor components, are rejected. No archive inputs,
  template evaluation or paths drawn from scenario data are used. Outputs are
  exclusive, with staging for directories and cleanup on failure. Use directories
  under your control; hostile concurrent filesystem mutation is not supported.
- This validates export structure and graph consistency, not each Huginn agent
  class's option rules, retention menu, permissions, OAuth services or plugin
  availability. Extension fields are preserved as data; upstream may ignore them.
  Import into a real Huginn instance was not exercised. Review configurations
  before importing them; Huginn may later execute their configured agents.
- No live Huginn API, credentials resolution, database backup, agent execution,
  cross-platform conversion, or migration of agents outside the export is provided.

## Tests

```sh
uv run --extra test pytest tests/huginn
```

The offline suite covers four synthetic JSON fixture round-trips (including
control links, Unicode and an empty scenario), deterministic bytes, an independent
index-to-GUID oracle, CLI end-to-end flows, render/diff/lint behavior, exit codes,
and hostile-input/filesystem boundaries. Relocation cases compare against an
explicit combined export, exercise both link kinds and simultaneous GUID maps,
and check field preservation, conflicts, duplicate identities and aggregate limits.
It uses one process at a time and starts no servers.

- Reference-diagnostic regressions cover every event/control endpoint, original
  array positions, deleted agent files, join/render and dry-run failure without
  output or input mutation, both combine sources, and unchanged valid output.
- Relocation-v2 regressions compare an explicit expected export, check original
  input scopes through GUID swaps, strict endpoint shapes, duplicate additions and
  inherited edges, per-kind identity, aggregate link limits, and CLI/dry-run failures.
- Agent-diagnostic regressions cover original YAML filenames and JSON indices through
  GUID sorting, typed fields and schedules, escaped nested substitution paths,
  structured library errors, and unchanged valid output. Failure cases also check
  empty stdout, absent output and byte-identical inputs, including dry-run.
- Inventory regressions cover malformed original values, reserved keys at containing
  mappings, original sources through sorting, no partial output and unchanged sorted
  names, including repeated variables and quoted credential tags.
- Graph regressions cover exact GUID identity, both edge kinds, cycles/self-links,
  isolated/disabled nodes, filename/export reordering, safe Unicode/control display,
  opaque data and unevaluated schedules, concrete-input refusals, input/output
  bounds, immutable inputs and private exclusive outputs.
- Manifest-diagnostic regressions cover v1/v2 YAML/JSON, original malformed values
  and forbidden keys under scenario/GUID maps, introduced scalar/nested/key syntax
  and assembled tokens at original substitution sites, default/strict refusals,
  dry-run/existing destinations, library origins and immutable inputs.

## Provenance and license

Huginn's MIT-licensed export/import interface was checked against upstream source;
no upstream implementation or default scenario was copied. See
[THIRD_PARTY.md](../THIRD_PARTY.md) for pinned source references and dependency
attribution, including PyYAML's MIT license.

`flowwrench huginn` is part of flowwrench, licensed under the Apache License 2.0
(see [LICENSE](../LICENSE) and [NOTICE](../NOTICE)).
