"""Semantic diff and headless three-way Git merge driver for Node-RED flows.

Python port of the Node.js ``nrflow-merge`` tool, exposed on the command line
as ``flowwrench nodered-merge``.  Nodes are matched by ID; merge output is
always valid JSON and conflicts never become text markers.

The library API mirrors ``lib/flows.js`` (``mergeFlows`` -> :func:`merge_flows`,
etc.); options are keyword arguments instead of an options object.
"""

from __future__ import annotations

from .flow_document import adapt_flow_document, merge_snapshots, read_merge_snapshot, serialize_merge_result
from .flows import (
    LIMITS,
    InputError,
    check_integrity,
    diff_flows,
    merge_flows,
    normalize,
    read_flow,
    serialize,
    summarize_merge,
    validate_flow,
)
from .resolution import create_manifest, resolve_manifest

__version__ = "0.1.0"

__all__ = [
    "LIMITS",
    "InputError",
    "__version__",
    "adapt_flow_document",
    "check_integrity",
    "create_manifest",
    "diff_flows",
    "merge_flows",
    "merge_snapshots",
    "normalize",
    "read_flow",
    "read_merge_snapshot",
    "resolve_manifest",
    "serialize",
    "serialize_merge_result",
    "summarize_merge",
    "validate_flow",
]
