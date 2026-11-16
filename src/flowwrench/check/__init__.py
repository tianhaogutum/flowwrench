"""``flowwrench check``: one read-only pass over a folder of exported workflows.

``check`` finds exported n8n, Node-RED, Activepieces, Windmill, Huginn and
crontab files by their content, then runs the existing read-only analyses of
the other flowwrench tools on them (n8n-refs, nodered-merge integrity,
ap-promote inventory, huginn lint, windmill-dryrun validation, n8n2ap
readiness and the cross-platform schedules analysis). It never writes a file,
executes a workflow step or opens a network connection.

Library use::

    from flowwrench.check import CheckOptions, discover, run_checks
    discovery = discover(["exports/"])
    findings = run_checks(discovery, options=CheckOptions(default_timezone="Europe/Berlin"))
"""
from __future__ import annotations

from flowwrench import __version__
from .discover import Discovery, DiscoveryError, Source, discover
from .model import SEVERITIES, Finding
from .registry import CHECKS, CheckOptions, run_checks, select

__all__ = [
    "CHECKS",
    "SEVERITIES",
    "CheckOptions",
    "Discovery",
    "DiscoveryError",
    "Finding",
    "Source",
    "__version__",
    "discover",
    "run_checks",
    "select",
]
