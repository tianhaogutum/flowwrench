"""Compare static hazards without using mutable node-array indexes or offsets."""
from collections import defaultdict
import json

from .core import WorkflowError, analyze, validate


IDENTITY_REASONS = {
    "duplicate-node-name": "comparison requires unique node names",
    "invalid-node-id": "comparison requires nonempty string node IDs when present",
    "duplicate-node-id": "comparison requires unique node IDs",
}


def _node_index(data):
    """Collect identity blockers once for both comparison and check metadata."""
    validate(data)
    by_name, by_id = {}, {}
    reasons = []
    for node in data["nodes"]:
        if node["name"] in by_name:
            reasons.append("duplicate-node-name")
        by_name[node["name"]] = node
        if "id" in node:
            node_id = node["id"]
            if not isinstance(node_id, str) or not node_id.strip():
                reasons.append("invalid-node-id")
            else:
                if node_id in by_id:
                    reasons.append("duplicate-node-id")
                by_id[node_id] = node
    return by_name, by_id, reasons


def _nodes(data, side):
    by_name, by_id, reasons = _node_index(data)
    if reasons:
        # Preserve compare's first-error behavior, including message and side.
        raise WorkflowError(f"{side}: {IDENTITY_REASONS[reasons[0]]}")
    return by_name, by_id


def _node_identity(name, by_name):
    if name is None:
        return None
    node = by_name.get(name)
    if node is None:
        return {"kind": "missing", "value": name}
    if "id" in node:
        return {"kind": "id", "value": node["id"]}
    return {"kind": "name", "value": name}


def _index(findings, by_name):
    indexed = {}
    occurrences = defaultdict(int)
    for finding in findings:
        # Keep RFC 6901 escaping in the relative path. The consumer/source
        # identity replaces only the node index or connection source key.
        parts = finding.pointer.split("/")
        scope = parts[1]
        path = "/" + "/".join(parts[3:]) if len(parts) > 3 else ""
        identity = {
            "rule": finding.rule,
            "scope": scope,
            "node": _node_identity(finding.node, by_name),
            "path": path,
            "reference": _node_identity(finding.reference, by_name),
        }
        key = json.dumps(identity, sort_keys=True, ensure_ascii=True)
        occurrences[key] += 1
        identity["occurrence"] = occurrences[key]
        indexed[(key, occurrences[key])] = (identity, finding.to_dict())
    return indexed, all(f.rule != "R007" for f in findings)


def check_identity_context(data, findings):
    """Return identities and all omission reasons without repeating analysis.

    Check accepts ambiguous IDs/names as before. Only fingerprint eligibility
    uses comparison's stricter node contract; analysis is not repeated.
    """
    by_name, _, issues = _node_index(data)
    reasons = [code for code in IDENTITY_REASONS if code in issues]
    if any(f.rule == "R007" for f in findings):
        reasons.append("incomplete-analysis")
    if reasons:
        return None, reasons
    indexed, _ = _index(findings, by_name)
    return [identity for identity, _ in indexed.values()], []


def check_identities(data, findings):
    """Return comparison identities in finding order, or None for the workflow."""
    try:
        return check_identity_context(data, findings)[0]
    except WorkflowError:
        return None


def compare(baseline, candidate, *, _findings=None):
    """Return new/resolved/unchanged hazard occurrences and ID-backed renames.

    Identity is (rule, scope, node ID/name, relative JSON Pointer, referenced
    node ID/name, occurrence). Offsets, wording and names of ID-backed nodes
    are not identity. This compares hazard categories, not runtime behavior.
    R007 on either side marks the entire comparison incomplete.
    CLI SARIF may supply already analyzed findings for these exact inputs;
    node validation and the comparison result contract remain identical.
    """
    old_names, old_ids = _nodes(baseline, "baseline")
    new_names, new_ids = _nodes(candidate, "candidate")
    old_findings = analyze(baseline) if _findings is None else _findings["baseline"]
    new_findings = analyze(candidate) if _findings is None else _findings["candidate"]
    old, old_complete = _index(old_findings, old_names)
    new, new_complete = _index(new_findings, new_names)
    result = {"complete": old_complete and new_complete, "new": [], "resolved": [], "unchanged": [],
              "renames": [{"id": node_id, "from": old_ids[node_id]["name"], "to": new_ids[node_id]["name"]}
                          for node_id in sorted(old_ids.keys() & new_ids.keys())
                          if old_ids[node_id]["name"] != new_ids[node_id]["name"]]}
    for key, (identity, finding) in new.items():
        result["unchanged" if key in old else "new"].append({
            "identity": identity, "baseline": old[key][1] if key in old else None, "candidate": finding,
        })
    for key, (identity, finding) in old.items():
        if key not in new:
            result["resolved"].append({"identity": identity, "baseline": finding, "candidate": None})
    return result
