"""Find exported workflow files by their content, not by their names.

Only JSON, YAML and crontab candidates are opened. A file that is not a
workflow export (``package.json``, CI configuration, ...) is skipped silently;
a file whose name says it is a workflow but cannot be parsed is reported.
Nothing is written and no symlinked directory is followed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any

import yaml

from .model import ERROR, INFO, Finding

# Directories that never hold exports worth checking (dot-directories are skipped too).
IGNORE_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages"})
MAX_CANDIDATES = 20_000
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_YAML_BYTES = 4 * 1024 * 1024
CRONTAB_NAMES = frozenset({"crontab", ".crontab"})
CRONTAB_SUFFIXES = frozenset({".cron", ".crontab"})
SCHEDULE_YAML_SUFFIXES = (".schedule.yaml", ".schedule.yml")
# Names that announce a workflow export: unparseable files with such a name are reported.
WORKFLOW_NAME = re.compile(r"flow|workflow|n8n|node-?red|scenario|template|huginn|windmill", re.IGNORECASE)

KINDS = {
    "n8n": ("n8n", "n8n workflow"),
    "n8n-bundle": ("n8n", "n8n workflow bundle"),
    "node-red": ("Node-RED", "Node-RED flows"),
    "activepieces": ("Activepieces", "Activepieces flow"),
    "windmill-flow": ("Windmill", "Windmill flow"),
    "windmill-schedule": ("Windmill", "Windmill schedule"),
    "huginn": ("Huginn", "Huginn scenario"),
    "huginn-kit": ("Huginn", "Huginn YAML kit"),
    "crontab": ("cron", "crontab"),
}
PLATFORMS = ("n8n", "Node-RED", "Activepieces", "Windmill", "Huginn", "cron")
AP_TRIGGER_TYPES = ("PIECE_TRIGGER", "EMPTY")


class DiscoveryError(ValueError):
    """A path given on the command line cannot be used (exit 2)."""


@dataclass
class Source:
    """One detected export. ``data`` is the parsed document when one was parsed."""

    path: Path
    display: str
    kind: str
    data: Any = None

    @property
    def platform(self) -> str:
        return KINDS[self.kind][0]

    @property
    def label(self) -> str:
        return KINDS[self.kind][1]


@dataclass
class Discovery:
    sources: list[Source] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    scanned: int = 0


class _TolerantLoader(yaml.SafeLoader):
    """Safe YAML that maps unknown local tags (Windmill ``!inline``) to None."""


_TolerantLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


# --------------------------------------------------------------------------- classification

def _n8n_workflow(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("nodes"), list)
            and isinstance(value.get("connections"), dict))


def _ap_version(value: Any) -> bool:
    trigger = value.get("trigger") if isinstance(value, dict) else None
    return isinstance(trigger, dict) and trigger.get("type") in AP_TRIGGER_TYPES


def _activepieces(data: dict) -> bool:
    if _ap_version(data) or _ap_version(data.get("version")) or _ap_version(data.get("template")):
        return True
    flows = data.get("flows")
    return isinstance(flows, list) and bool(flows) and all(_ap_version(flow) for flow in flows)


def _windmill_flow(data: Any) -> bool:
    value = data.get("value") if isinstance(data, dict) else None
    return isinstance(value, dict) and isinstance(value.get("modules"), list) and "summary" in data


def _node_red(data: list) -> bool:
    if not all(isinstance(node, dict) and isinstance(node.get("id"), str)
               and isinstance(node.get("type"), str) for node in data):
        return False
    return any("wires" in node or "z" in node or node["type"] in ("tab", "subflow") for node in data)


def classify(data: Any) -> str | None:
    """Return the export kind of one parsed JSON/YAML document, or None."""
    if isinstance(data, dict):
        if _n8n_workflow(data):
            return "n8n"
        if data.get("schema_version") == 1 and isinstance(data.get("agents"), list):
            return "huginn"
        if _activepieces(data):
            return "activepieces"
        if _windmill_flow(data):
            return "windmill-flow"
        if "schedule" in data and "script_path" in data:
            return "windmill-schedule"
        return None
    if isinstance(data, list) and data:
        if all(_n8n_workflow(item) for item in data):
            return "n8n-bundle"
        if _node_red(data):
            return "node-red"
    return None


def candidate_type(path: Path) -> str | None:
    """``crontab``, ``schedule-yaml``, ``json``, ``yaml`` or None (not opened)."""
    name = path.name
    lower = name.lower()
    if name in CRONTAB_NAMES or path.suffix in CRONTAB_SUFFIXES:
        return "crontab"
    if lower.endswith(SCHEDULE_YAML_SUFFIXES):
        return "schedule-yaml"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith((".yaml", ".yml")):
        return "yaml"
    return None


# --------------------------------------------------------------------------- reading

def _read(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"larger than {limit // (1024 * 1024)} MiB; not inspected")
    return raw.decode("utf-8-sig")


def _parse_failure(display: str, rule: str, reason: str) -> Finding:
    return Finding("parse", rule, ERROR, f"cannot parse this workflow export: {reason}", display,
                   hint="re-export the workflow from its platform; the file was not checked")


def _inspect(path: Path, display: str, kind: str, explicit: bool, result: Discovery) -> None:
    result.scanned += 1
    if kind == "crontab":
        result.sources.append(Source(path, display, "crontab"))
        return
    if kind == "schedule-yaml":
        result.sources.append(Source(path, display, "windmill-schedule"))
        return
    # Report parse failures only for files that announce themselves as exports:
    # plenty of ordinary JSON (tsconfig with comments) and YAML (multi-document
    # manifests) is not meant for any of these tools.
    in_flow_dir = path.parent.name.endswith(".flow")
    announced = explicit or in_flow_dir or (kind == "json" and bool(WORKFLOW_NAME.search(path.name)))
    try:
        if kind == "json":
            text = _read(path, MAX_JSON_BYTES)
            data = json.loads(text)
        else:
            text = _read(path, MAX_YAML_BYTES)
            data = yaml.load(text, Loader=_TolerantLoader)  # noqa: S506 - SafeLoader subclass
    except OSError as exc:
        result.findings.append(_parse_failure(display, "unreadable", exc.strerror or type(exc).__name__))
        return
    except UnicodeError:
        if announced:
            result.findings.append(_parse_failure(display, "invalid-encoding", "not UTF-8 text"))
        return
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        if announced:
            # json/yaml messages carry positions only, never file content.
            reason = (f"invalid JSON ({exc.msg} at line {exc.lineno})" if isinstance(exc, json.JSONDecodeError)
                      else "invalid YAML" if isinstance(exc, yaml.YAMLError)
                      else str(exc) if isinstance(exc, ValueError) else "nesting too deep")
            result.findings.append(_parse_failure(display, "invalid-json" if kind == "json" else "invalid-yaml",
                                                  reason))
        return
    detected = classify(data)
    if kind != "json" and detected != "windmill-flow":
        # Only Windmill flows are exported as YAML; the other tools read JSON
        # (and Windmill schedule YAML is recognised by its *.schedule.yaml name).
        detected = None
    if detected is not None:
        result.sources.append(Source(path, display, detected, data))
    elif explicit:
        result.findings.append(Finding(
            "parse", "unrecognised", INFO, "not a recognised n8n, Node-RED, Activepieces, Windmill or "
            "Huginn export; skipped", display))


def _huginn_kit(directory: Path) -> bool:
    try:
        manifest = yaml.safe_load(_read(directory / "scenario.yaml", MAX_YAML_BYTES))
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        return False
    return isinstance(manifest, dict) and "kit_version" in manifest


def _walk(root: Path, raw: str, result: Discovery, seen: set) -> None:
    def fail(error: OSError):
        raise DiscoveryError(f"cannot read directory {error.filename}: {error.strerror}")

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        here = Path(parent)
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")
                         and not (here / d).is_symlink())
        relative = here.relative_to(root)
        if "scenario.yaml" in files and "agents" in dirs and _huginn_kit(here):
            display = os.path.normpath(os.path.join(raw, relative))
            result.sources.append(Source(here, display, "huginn-kit"))
            result.scanned += 1
            dirs.remove("agents")
            files = [f for f in files if f != "scenario.yaml"]
        for name in sorted(files):
            path = here / name
            kind = candidate_type(path)
            if kind is None or path.is_symlink() or not path.is_file():
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_CANDIDATES:
                raise DiscoveryError(f"more than {MAX_CANDIDATES} candidate files; check a smaller folder")
            _inspect(path, os.path.normpath(os.path.join(raw, relative, name)), kind, False, result)


def discover(paths) -> Discovery:
    """Walk files and directories; raise DiscoveryError for unusable arguments."""
    result = Discovery()
    seen: set = set()
    for raw in paths:
        raw = str(raw)
        if not raw:
            raise DiscoveryError("empty path; use . for the current directory")
        path = Path(raw)
        if not path.exists():
            raise DiscoveryError(f"no such file or directory: {raw}")
        if path.is_dir():
            _walk(path, raw, result, seen)
            continue
        if not path.is_file():
            raise DiscoveryError(f"not a regular file or directory: {raw}")
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        kind = candidate_type(path)
        if kind is None:
            # An explicitly named file with an unusual name: sniff JSON vs YAML.
            try:
                with path.open("rb") as stream:
                    head = stream.read(64).lstrip(b"\xef\xbb\xbf \t\r\n")
            except OSError as exc:
                raise DiscoveryError(f"cannot read {raw}: {exc.strerror}") from None
            kind = "json" if head[:1] in (b"{", b"[") else "yaml"
        _inspect(path, os.path.normpath(raw), kind, True, result)
    result.sources.sort(key=lambda source: source.display)
    return result
