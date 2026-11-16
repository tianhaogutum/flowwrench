"""Deterministic review artifacts; never store credentials or follow artifact paths."""
from __future__ import annotations

import hashlib
import json

from .core import Flow, Mapping, PIN, connection_catalog, entries, piece_catalog, piece_records, remap
from .jsondoc import Document, InvalidInput, Refused

FORMAT = 'ap-flow-promote.plan/v1'
POLICY = 'exact-piece-version'


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def versioned_catalog(document: Document) -> dict:
    # Reuse completeness, duplicate-ID, status and ownership validation. Only
    # this explicit version-aware path requires version compatibility metadata.
    catalog = connection_catalog(document)
    for node in entries(document, 'connection'):
        version = node.text('pieceVersion', required=False)
        if version is not None and not PIN.fullmatch(version):
            raise InvalidInput('connection pieceVersion must be an exact x.y.z pin')
        catalog[node.text('externalId')]['pieceVersion'] = version
    return catalog


def configuration(mapping: dict[str, Mapping], catalog: dict, installed=None) -> dict:
    # All fields here are allowlisted metadata; raw catalog values/tokens are
    # deliberately excluded even from the config hash.
    config = dict(policy=POLICY, strict=True,
                  mappings={old: dict(externalId=new.external_id, pieceName=new.piece)
                            for old, new in mapping.items()},
                  connections=catalog)
    # Omission retains the original v1 configuration/artifact. An explicitly
    # empty catalog is a distinct, enforced input, never a disabled check.
    if installed is not None:
        config['targetPieces'] = [dict(pieceName=name, pieceVersion=version)
                                  for name, version in sorted(installed)]
    return config


def _build(flow: Flow, mapping: dict[str, Mapping], catalog: dict, installed=None):
    output, report = remap(flow, mapping, strict=True, target=catalog)
    compatibility = []
    for ref in flow.references:
        destination = mapping[ref.external_id].external_id if ref.external_id in mapping else ref.external_id
        # remap has already verified every resulting destination and owner.
        source_version = ref.context.version
        target_version = catalog[destination]['pieceVersion']
        if source_version is None or not PIN.fullmatch(source_version):
            raise Refused('connection version check: owning piece must have an exact version pin')
        if target_version is None or source_version != target_version:
            raise Refused('connection version check: target pieceVersion missing or differs from owning piece pin')
        compatibility.append(dict(ref.record(), targetExternalId=destination,
                                  connectionVersion=target_version, status='exact'))
    availability = None
    if installed is not None:
        availability = piece_records(flow, installed)
        for record in availability:
            if record['status'] != 'available':
                raise Refused(f"piece availability check: {record['status']} at {record['pointer']}")
        report.update(pieceCatalogVerified=True, pieceUsagesVerified=len(availability))
    config = configuration(mapping, catalog, installed)
    plan = dict(format=FORMAT, policy=POLICY,
                sourceSha256=sha256(flow.document.text),
                configSha256=sha256(canonical(config)), outputSha256=sha256(output),
                mappings=config['mappings'], compatibility=compatibility, report=report)
    if availability is not None:
        plan.update(targetPiecesSha256=sha256(canonical(config['targetPieces'])),
                    pieceAvailability=availability)
    return output, plan


def create_plan(flow: Flow, mapping: dict[str, Mapping], target: Document,
                target_pieces: Document | None = None):
    """Return lossless output plus a review plan containing only safe metadata."""
    installed = piece_catalog(target_pieces) if target_pieces is not None else None
    return _build(flow, mapping, versioned_catalog(target), installed)


def apply_plan(document: Document, flow: Flow, mapping: dict[str, Mapping], target: Document,
               target_pieces: Document | None = None):
    """Rebuild the complete artifact from current inputs before authorizing output."""
    saved = document.root.plain()
    if not isinstance(saved, dict) or saved.get('format') != FORMAT:
        raise InvalidInput('unsupported promotion plan format')
    if saved.get('sourceSha256') != sha256(flow.document.text):
        raise Refused('stale source: bytes differ from the reviewed plan; create a new plan')
    catalog = versioned_catalog(target)
    installed = piece_catalog(target_pieces) if target_pieces is not None else None
    if saved.get('configSha256') != sha256(canonical(configuration(mapping, catalog, installed))):
        raise Refused('stale configuration: mapping or target metadata changed; create a new plan')
    output, fresh = _build(flow, mapping, catalog, installed)
    # Hashes alone are insufficient: recheck the human-reviewed mapping,
    # compatibility results, output hash and summary too. Reject extra fields.
    if canonical(saved) != canonical(fresh):
        raise Refused('plan contents differ from the revalidated promotion; create a new plan')
    return output, dict(fresh['report'], planVerified=True)
