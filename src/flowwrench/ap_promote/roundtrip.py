"""Read-only connection-reference verification of a destination re-export."""
from __future__ import annotations

import re

from .core import Flow, Mapping, remap
from .jsondoc import InvalidInput, pointer


def _flow_roots(flow: Flow):
    roots = {}
    for path, _ in flow.steps:
        # Traversal starts at each trigger, including reference-free flows.
        roots.setdefault(flow.contexts[path].flow, pointer(path[:-1]))
    return roots


def _pair_flows(source_roots: dict, destination_roots: dict, entries: list[str]):
    """Explicit pairs are a total bijection; never fill gaps heuristically."""
    pairs = {}
    destinations = set()
    for entry in entries:
        if not re.fullmatch(r'(0|[1-9][0-9]*)=(0|[1-9][0-9]*)', entry):
            raise InvalidInput('--flow-pair requires SOURCE_INDEX=DESTINATION_INDEX (zero-based integers)')
        indexes = []
        for value, count in zip(entry.split('='), (len(source_roots), len(destination_roots))):
            # Bound before int conversion, including for resource-limit-sized arguments.
            if len(value) > len(str(count - 1)) or int(value) >= count:
                raise InvalidInput('--flow-pair index out of range')
            indexes.append(int(value))
        source, destination = indexes
        if source in pairs or destination in destinations:
            raise InvalidInput('--flow-pair must use each source and destination index exactly once')
        pairs[source] = destination
        destinations.add(destination)
    if len(pairs) != len(source_roots) or len(destinations) != len(destination_roots):
        raise InvalidInput('--flow-pair must cover every source and destination flow exactly once')
    return pairs


def _index(flow: Flow, flow_indexes: dict[str, int]):
    # Names are unique within a flow (enforced by Flow). Root envelopes and
    # structural positions may change on export; field/tool positions within
    # a named step remain significant. Never collapse repeated occurrences.
    steps = {}
    for path, _ in flow.steps:
        ctx = flow.contexts[path]
        steps[ctx.flow, ctx.step] = path
    result = {}
    for ref in flow.references:
        ctx = ref.context
        if ctx is None:
            # Source remap refuses unknown ownership. Destination-only orphan
            # references must still be reported rather than silently ignored.
            key = ('unowned', ref.path, ref.occurrence)
        else:
            base = steps[ctx.flow, ctx.step]
            key = (flow_indexes[ctx.flow], ctx.step, ref.path[len(base):], ref.occurrence)
        result[key] = ref
    return result


def verify_roundtrip(source: Flow, destination: Flow, mapping: dict[str, Mapping],
                     flow_pairs: list[str] | None = None):
    """Restore IDs per source location, avoiding ambiguous global inverse maps.

    A successful result verifies reference presence, destination ID and owning
    piece/version. It does not assert structural/business-data equivalence or
    contact an instance. Optional zero-based flow pairs must cover both exports
    exactly once. Without them, flows match by order. Neither input is changed.
    """
    source_roots, destination_roots = _flow_roots(source), _flow_roots(destination)
    source_indexes = {root: i for i, root in enumerate(source_roots)}
    destination_indexes = {root: i for i, root in enumerate(destination_roots)}
    pairs = _pair_flows(source_roots, destination_roots, flow_pairs) if flow_pairs is not None else None
    if pairs is None:
        comparison_indexes = destination_indexes
    else:
        inverse = {dst: src for src, dst in pairs.items()}
        comparison_indexes = {root: inverse[i] for root, i in destination_indexes.items()}
    # Apply existing strict mapping/ownership/merge guards. This only computes
    # text in memory; there is no write or reverse-map mutation of the export.
    remap(source, mapping, strict=True)
    expected = _index(source, source_indexes)
    actual = _index(destination, comparison_indexes)
    matches, errors = [], []
    for key, ref in expected.items():
        found = actual.pop(key, None)
        target = mapping[ref.external_id].external_id if ref.external_id in mapping else ref.external_id
        record = dict(flowIndex=key[0], step=ref.context.step, occurrence=ref.occurrence,
                      sourcePointer=pointer(ref.path), destinationPointer=pointer(found.path) if found else None,
                      sourceExternalId=ref.external_id, expectedDestinationExternalId=target,
                      destinationExternalId=found.external_id if found else None)
        if pairs is not None:
            record.update(sourceFlowIndex=key[0], destinationFlowIndex=pairs[key[0]])
        if found is None:
            errors.append(dict(record, problem='lost-reference'))
            continue
        problems = []
        if found.external_id != target:
            problems.append(dict(record, problem='wrong-connection'))
        if found.context.piece != ref.context.piece:
            problems.append(dict(record, problem='wrong-piece',
                                 expectedPieceName=ref.context.piece, destinationPieceName=found.context.piece))
        if found.context.version != ref.context.version:
            problems.append(dict(record, problem='wrong-version',
                                 expectedPieceVersion=ref.context.version, destinationPieceVersion=found.context.version))
        if problems:
            errors.extend(problems)
        else:
            matches.append(dict(record, restoredExternalId=ref.external_id))
    for key, ref in actual.items():
        destination_index = destination_indexes[ref.context.flow] if ref.context else None
        record = dict(problem='unexpected-reference', sourcePointer=None,
                      destinationPointer=pointer(ref.path), flowIndex=destination_index,
                      step=ref.context.step if ref.context else None,
                      occurrence=ref.occurrence, destinationExternalId=ref.external_id)
        if pairs is not None:
            record.update(sourceFlowIndex=key[0] if ref.context else None,
                          destinationFlowIndex=destination_index)
        errors.append(record)
    report = dict(sourceReferences=len(source.references), destinationReferences=len(destination.references),
                  verified=len(matches), matches=matches, errors=errors, ok=not errors)
    if pairs is not None:
        source_pointers, destination_pointers = list(source_roots.values()), list(destination_roots.values())
        report['flowPairs'] = [dict(sourceFlowIndex=src, destinationFlowIndex=dst,
                                    sourceFlowPointer=source_pointers[src], destinationFlowPointer=destination_pointers[dst])
                               for src, dst in sorted(pairs.items())]
    return report
