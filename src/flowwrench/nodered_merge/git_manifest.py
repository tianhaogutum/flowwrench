"""Capture unmerged Git index stages 1/2/3 into a private review bundle.

Port of ``lib/git-manifest.js``.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any

from .fileio import write_outputs
from .flow_document import ROLES, adapt_flow_document, merge_snapshots, serialize_merge_result
from .flows import LIMITS, InputError, parse_json_snapshot
from .jsjson import stringify
from .resolution import create_manifest

__all__ = ["capture_git_manifest"]

_ENTRY = re.compile(r"([0-7]{6}) ([a-f0-9]{40,64}) ([123])")
_GIT_TIMEOUT = 15
_DEFAULT_MAX_BUFFER = 1024 * 1024


def _os_code(error: OSError) -> str:
    """Node-style error code (``ENOENT``, ``ENOTDIR``...) for a spawn failure."""
    return errno.errorcode.get(error.errno, "UNKNOWN") if error.errno else "UNKNOWN"


def _git_bytes(args: Sequence[str], cwd: str, max_buffer: int = _DEFAULT_MAX_BUFFER) -> bytes:
    """Binary output is essential: trimming or re-encoding a blob would invalidate
    exact-byte manifest hashes. Read only immutable OIDs captured by one ls-files."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise InputError(f"git {args[0]} failed (ETIMEDOUT)") from None
    except OSError as error:
        raise InputError(f"git {args[0]} failed ({_os_code(error)})") from None
    if len(completed.stdout) > max_buffer:
        raise InputError(f"git {args[0]} failed (ENOBUFS)")
    if completed.returncode != 0:
        status = completed.returncode if completed.returncode > 0 else "null"
        raise InputError(f"git {args[0]} failed ({status})")
    return completed.stdout


def _git_line(args: Sequence[str], cwd: str) -> str:
    text = os.fsdecode(_git_bytes(args, cwd))
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text


def _relative(start: str, target: str) -> str:
    """``path.relative``: '' (not '.') when both resolve to the same path."""
    result = os.path.relpath(target, start)
    return "" if result == "." else result


def _parse_size(text: str) -> int | None:
    stripped = text.strip()
    if stripped == "":
        return 0
    try:
        return int(stripped, 10)
    except ValueError:
        return None


def _json_text(value: Any) -> str:
    return f"{stringify(value, 2)}\n"


def capture_git_manifest(
    file: str,
    *,
    repo: str = ".",
    snapshot_dir: str | None = None,
    allow_missing_base: bool = False,
    prefer: str = "ours",
    config_refs: Sequence[str] = (),
    ignore_layout: bool = False,
) -> dict[str, Any]:
    if not snapshot_dir:
        raise InputError("git-manifest requires --snapshot-dir DIR")
    cwd = os.path.realpath(os.path.abspath(repo), strict=True)
    root = _git_line(["rev-parse", "--show-toplevel"], cwd)
    path = _relative(root, os.path.abspath(os.path.join(cwd, file)))
    if path == ".." or path.startswith(f"..{os.sep}") or os.path.isabs(path):
        raise InputError("flow path must be inside the Git worktree")
    # Resolve the output parent before mkdir; require a new directory even if an
    # existing one is empty or a symlink. mkdir below also guards a concurrent creator.
    resolved_snapshot = os.path.abspath(snapshot_dir)
    target = os.path.join(
        os.path.realpath(os.path.dirname(resolved_snapshot), strict=True), os.path.basename(resolved_snapshot)
    )
    if target == os.path.abspath(os.path.join(root, path)):
        raise InputError("snapshot directory must differ from the working flow path")
    try:
        os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        raise InputError("snapshot directory must not exist; use a fresh directory")

    # NUL records and literal pathspecs handle spaces, tabs, newlines, Unicode,
    # wildcard characters and leading colons without shell or revision parsing.
    entries = _git_bytes(
        ["--literal-pathspecs", "ls-files", "--full-name", "--unmerged", "--stage", "-z", "--", path or "."], root
    )
    stages: dict[str, dict[str, Any] | None] = {}
    for record in os.fsdecode(entries).split("\0"):
        tab = record.find("\t")
        if tab < 0 or record[tab + 1 :] != path:
            continue
        match = _ENTRY.fullmatch(record[:tab])
        if not match:
            raise InputError("unsupported Git index entry")
        mode, blob, stage = match.groups()
        stages[ROLES[int(stage) - 1]] = {"stage": int(stage), "mode": mode, "blob": blob}
    if "ours" not in stages or "theirs" not in stages or ("base" not in stages and not allow_missing_base):
        raise InputError(
            "git-manifest requires unmerged stages 1 (base), 2 (ours), 3 (theirs) for the exact file; "
            "--allow-missing-base permits only an absent stage 1 with stages 2 and 3 present"
        )
    # Keep an absent Git ancestor distinct from a real stage-1 [] blob. Only
    # the explicit add/add opt-in synthesizes bytes; ours/theirs are mandatory.
    synthetic_base = "base" not in stages
    if synthetic_base:
        stages["base"] = None
    snapshots: list[dict[str, Any]] = []
    for role in ROLES:
        if role == "base" and synthetic_base:
            data = b"[]\n"
            parsed = parse_json_snapshot(data, "Synthetic empty base (missing Git stage 1)")
            snapshots.append({"bytes": data, "hash": parsed["hash"], **adapt_flow_document(parsed["value"])})
            continue
        entry = stages[role]
        assert entry is not None
        mode, blob, number = entry["mode"], entry["blob"], entry["stage"]
        if mode not in ("100644", "100755"):
            raise InputError(f"stage {number}: expected a regular blob (mode 100644 or 100755)")
        source = f"Git index stage {number}"
        size = _parse_size(_git_line(["cat-file", "-s", blob], root))
        if size is None or size < 0 or size > LIMITS.bytes:
            raise InputError(f"{source}: exceeds {LIMITS.bytes} byte limit")
        data = _git_bytes(["cat-file", "blob", blob], root, LIMITS.bytes)
        parsed = parse_json_snapshot(data, source)
        snapshots.append({"bytes": data, "hash": parsed["hash"], **adapt_flow_document(parsed["value"], source)})
    result = merge_snapshots(snapshots, prefer=prefer, config_refs=config_refs, ignore_layout=ignore_layout)
    inputs = [os.path.join(target, f"{role}.json") for role in ROLES]
    output = os.path.join(target, "merged.json")
    report = f"{output}.conflicts.json"
    manifest = os.path.join(target, "decisions.json")
    git_index: dict[str, Any] = {"repo": root, "path": path, "stages": stages}
    if synthetic_base:
        git_index["syntheticBase"] = {"reason": "missing-stage-1", "value": []}
    receipt = {
        "version": 1,
        "clean": result["clean"],
        "inputs": inputs,
        "output": output,
        "report": report,
        "manifest": manifest,
        "gitIndex": git_index,
        "revisionProvenance": result["revisionProvenance"],
    }
    document = {
        "version": 1,
        "clean": result["clean"],
        "inputs": inputs,
        "output": output,
        "gitIndex": git_index,
        "conflicts": result["conflicts"],
        "integrity": result["integrity"],
        "summary": result["summary"],
        "revisionProvenance": result["revisionProvenance"],
    }
    outputs: list[tuple[str, str | bytes]] = [(file_path, snapshots[i]["bytes"]) for i, file_path in enumerate(inputs)]
    outputs += [
        (manifest, _json_text({**create_manifest(snapshots, prefer=prefer, config_refs=config_refs, result=result), "gitIndex": git_index})),
        (output, serialize_merge_result(result)),
        (report, _json_text(document)),
    ]
    # All validation/serialization precedes creation. Remove our new directory
    # on a write failure; never clean up a destination created by somebody else.
    os.mkdir(target, 0o700)
    try:
        write_outputs(outputs)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return receipt
