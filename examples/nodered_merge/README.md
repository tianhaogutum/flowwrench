# flowwrench nodered-merge examples

Synthetic Node-RED flows (copied unchanged from the original `nrflow-merge`
fixtures) for trying the semantic diff and three-way merge offline. Run the
commands from this directory. `python -m flowwrench.nodered_merge` is equivalent
to `flowwrench nodered-merge`.

## Diff

```sh
flowwrench nodered-merge diff base.json theirs.json --ignore-layout
flowwrench nodered-merge diff base.json ours.json --format json
```

Exit 0 for a successful comparison even when the files differ.

## Three-way merge

```sh
flowwrench nodered-merge merge base.json ours.json theirs.json \
  -o /tmp/merged.json --conflicts-file /tmp/conflicts.json
```

This example merges cleanly (exit 0). Nodes are matched by `id`; the output is
always valid, normalized JSON and a JSON report is always written (default
`OUT.conflicts.json`). Conflicts exit 1 and never become text markers;
`--prefer ours|theirs` only selects the fallback value. Exit 2 means invalid
input, CLI misuse or an I/O error, and no output is replaced.

Other examples:

```sh
# API-v2 {rev, flows} backups: output keeps ours' shape and revision (r1).
flowwrench nodered-merge merge api-v2/base.json api-v2/ours.json api-v2/theirs.json -o /tmp/api-v2-merged.json

# Named config/wire summaries; --ignore-layout filters only the report summaries.
flowwrench nodered-merge merge summary/base.json summary/ours.json summary/theirs.json -o /tmp/merged.json --ignore-layout

# Subflow in/out/status edge annotations.
flowwrench nodered-merge diff subflow-summary/base.json subflow-summary/ours.json --ignore-layout
```

## Reviewed resolution with a decision manifest

```sh
flowwrench nodered-merge merge resolution/base.json resolution/ours.json resolution/theirs.json \
  -o /tmp/merged.json --manifest /tmp/decisions.json      # exit 1: /func conflicts
# Edit /tmp/decisions.json: set each "decision" to {"choice": "ours"}, {"choice": "theirs"},
# {"choice": "custom", "present": true, "value": ...} or {"choice": "custom", "present": false}.
flowwrench nodered-merge resolve resolution/base.json resolution/ours.json resolution/theirs.json \
  --manifest /tmp/decisions.json -o /tmp/merged.json
```

The manifest records SHA-256 hashes of the exact input bytes; any input change
makes it stale (exit 2, nothing written).

## Normalize / Git textconv

```sh
flowwrench nodered-merge normalize base.json > /tmp/canonical.json
flowwrench nodered-merge textconv base.json
```

## Git merge driver

Run inside the target repository (or pass `--repo DIR`). `flowwrench` must be on
`PATH` for Git and for every collaborator, because Git runs the configured
command through the shell.

```sh
flowwrench nodered-merge install            # prints the JSON plan, changes nothing
flowwrench nodered-merge install --apply    # writes .gitattributes block + local git config
```

`--apply` adds this managed block to the root `.gitattributes`
(`--pattern GLOB` changes `flows.json`; the default does not match `flows_cred.json`):

```gitattributes
# BEGIN nrflow-merge
flows.json merge=nrflow-merge diff=nrflow-merge
# END nrflow-merge
```

and sets local Git configuration equivalent to:

```sh
git config --local merge.nrflow-merge.name 'Node-RED semantic three-way merge'
git config --local merge.nrflow-merge.driver 'flowwrench nodered-merge git-driver "%O" "%A" "%B"'
git config --local merge.nrflow-merge.recursive binary
git config --local diff.nrflow-merge.textconv 'flowwrench nodered-merge textconv'
```

Git passes `%O` (ancestor), `%A` (ours, rewritten in place with valid JSON) and
`%B` (theirs). The driver exits 1 on conflict and keeps a report under
`$(git rev-parse --git-path nrflow-merge-conflicts)/`, named after Git's temporary
ours file. Reports contain property values; remove them when done. The driver
and attribute name stay `nrflow-merge` so repositories configured with the
original Node tool keep working.

To capture a reviewable bundle from an unmerged index before staging:

```sh
flowwrench nodered-merge git-manifest flows.json --snapshot-dir ./flow-review
# edit flow-review/decisions.json, then:
flowwrench nodered-merge resolve flow-review/base.json flow-review/ours.json \
  flow-review/theirs.json --manifest flow-review/decisions.json -o flows.json
git add flows.json
```

Add `--allow-missing-base` for add/add file conflicts (no stage 1). To remove the
integration, delete the managed `.gitattributes` block and run:

```sh
git config --local --remove-section merge.nrflow-merge
git config --local --remove-section diff.nrflow-merge
```
