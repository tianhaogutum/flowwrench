"""Explicit, lossless field handling when relocating two schema-v1 exports."""
from __future__ import annotations

from copy import deepcopy

from .core import (EDGE_FIELDS, changed_paths, from_export, guid_value, mapping, nonempty, pointer_path,
                   reference_links, substitute, to_export)
from .formats import KitError, LocatedValidationError, ReferenceValidationError, json_value

GRAPH_FIELDS = {"agents", *EDGE_FIELDS}


def validate_added_edges(manifest, source):
    """V2 endpoints are concrete identities in an original input, never templates."""
    for kind, fields in EDGE_FIELDS.items():
        for index, edge in enumerate(reference_links(manifest, kind, fields, source)):
            for field in fields:
                endpoint = edge[field]
                pointer = f"/{kind}/{index}/{field}"
                if type(endpoint) is not dict or set(endpoint) != {"input", "guid"}:
                    raise ReferenceValidationError(pointer, "endpoint must contain exactly input, guid",
                                                   source=source)
                if type(endpoint["input"]) is not str or endpoint["input"] not in ("left", "right"):
                    raise ReferenceValidationError(pointer + "/input", "input must be concrete left or right",
                                                   source=source)
                try:
                    guid_value(endpoint["guid"], "endpoint guid")
                except KitError:
                    raise ReferenceValidationError(pointer + "/guid", "invalid concrete original guid",
                                                   source=source) from None


def combine(left, right, manifest, variables=None, *, left_source=None, right_source=None,
            manifest_source=None, reject_unused_vars=False):
    """Return a new export with declared relocations, metadata and v2 added edges.

    Mappings are simultaneous and scoped to an input. Only structural graph
    references are relocated: option strings and unknown fields remain opaque.
    Conflicting metadata requires explicit top-level replacement in the manifest.
    Opt-in binding checks count only original manifest HSK references.
    """
    json_value(manifest)
    mapping(manifest, "relocation manifest")
    version = manifest.get("relocation_version")
    if type(version) is not int or version not in (1, 2):
        raise KitError("unsupported relocation_version (only integers 1 and 2 are supported)")
    required = {"relocation_version", "guids", "scenario"}
    if version == 2:
        required.update(EDGE_FIELDS)
    if set(manifest) != required:
        raise KitError(f"relocation v{version} manifest must contain exactly " + ", ".join(sorted(required)))
    mapping(manifest["guids"], "manifest.guids")
    if set(manifest["guids"]) != {"left", "right"}:
        raise KitError("manifest.guids must contain exactly left and right")
    for side in ("left", "right"):
        mapping(manifest["guids"][side], f"manifest.guids.{side}")
    mapping(manifest["scenario"], "manifest.scenario")
    if set(manifest["scenario"]) & (GRAPH_FIELDS | {"schema_version"}):
        raise KitError("manifest.scenario contains reserved graph/schema fields")
    if version == 2:
        validate_added_edges(manifest, manifest_source)

    def manifest_error(path, reason):
        # Paths come from the original manifest. Introduced data is checked at
        # its substitution string, never at keys/indices supplied by variables.
        raise LocatedValidationError(pointer_path(path), reason, source=manifest_source)

    manifest = substitute(manifest, {} if variables is None else variables,
                          reject_unused_vars=reject_unused_vars, template_error=manifest_error)

    # Validate originals first: relocation must not hide invalid source exports.
    inputs = {"left": from_export(left, source=left_source),
              "right": from_export(right, source=right_source)}
    overrides = manifest["scenario"]
    metadata = {key: deepcopy(value) for key, value in left.items() if key not in GRAPH_FIELDS}
    conflicts = []
    for key, value in right.items():
        if key in GRAPH_FIELDS:
            continue
        if key in metadata and key not in overrides and changed_paths(metadata[key], value):
            conflicts.append(key)
        elif key not in metadata:
            metadata[key] = deepcopy(value)
    if conflicts:
        raise KitError("scenario metadata conflicts: " + ", ".join(sorted(conflicts))
                       + "; provide explicit manifest.scenario overrides")
    metadata.update(deepcopy(overrides))
    result = {**metadata, "agents": [], "links": [], "control_links": []}
    final_guids = {}
    for side, doc in inputs.items():
        relocations = manifest["guids"][side]
        known = {agent["guid"] for agent in doc["agents"]}
        for source, target in relocations.items():
            guid_value(source, f"manifest.guids.{side} source")
            try:
                guid_value(target, f"manifest.guids.{side} target")
            except KitError as exc:
                # Keys are never substituted: even a collection replacement
                # belongs to this original manifest value, not inserted data.
                raise LocatedValidationError(pointer_path(("guids", side, source)), str(exc),
                                             source=manifest_source) from None
            if source not in known:
                raise LocatedValidationError(pointer_path(("guids", side, source)),
                                             f"unknown source guid in manifest.guids.{side}: {source}",
                                             source=manifest_source)
        # Retain original identities before mutating copied agent dictionaries.
        final_guids[side] = {guid: relocations.get(guid, guid) for guid in known}
        for agent in doc["agents"]:
            agent["guid"] = relocations.get(agent["guid"], agent["guid"])
            result["agents"].append(agent)
        for kind, fields in EDGE_FIELDS.items():
            result[kind].extend({field: relocations.get(edge[field], edge[field]) for field in fields}
                                for edge in doc[kind])

    if version == 2:
        for kind, fields in EDGE_FIELDS.items():
            unique = {tuple(edge[field] for field in fields) for edge in result[kind]}
            for index, edge in enumerate(manifest[kind]):
                relocated = {}
                for field in fields:
                    endpoint = edge[field]
                    scoped_guids = final_guids[endpoint["input"]]
                    if endpoint["guid"] not in scoped_guids:
                        raise ReferenceValidationError(f"/{kind}/{index}/{field}/guid",
                                                       "unknown original guid in selected input",
                                                       source=manifest_source)
                    relocated[field] = scoped_guids[endpoint["guid"]]
                pair = tuple(relocated[field] for field in fields)
                if pair in unique:
                    raise ReferenceValidationError(f"/{kind}/{index}", "duplicate edge after relocation",
                                                   source=manifest_source)
                unique.add(pair)
                result[kind].append(relocated)

    # Revalidate the whole graph: rejects duplicate final identities (never dedup),
    # dangling references and aggregate limits, then rebuild indices by final GUID.
    # Keep final JSON bounds ahead of metadata validation, as in to_export.
    # Originals already validated schema_version; overrides cannot replace it.
    json_value(result)
    for field, validator in (("name", nonempty), ("guid", guid_value)):
        if field in overrides:
            try:
                validator(result[field], f"scenario.{field}")
            except KitError as exc:
                raise LocatedValidationError(pointer_path(("scenario", field)), str(exc),
                                             source=manifest_source) from None
    export = to_export(result)
    # Unlike split/join's deliberate normalization, combining must retain every
    # untouched metadata field, including exported_at (or its explicit override).
    if "exported_at" in metadata:
        export["exported_at"] = metadata["exported_at"]
    return export
