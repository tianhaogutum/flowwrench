"""Git merge-driver / textconv installation and the shared ``git`` helper.

Port of ``lib/install.js``.  The configured command is always
``flowwrench nodered-merge`` (resolved through ``PATH`` when Git runs it),
instead of the absolute ``node`` + script paths used by the Node tool.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .fileio import output_path, write_outputs
from .flows import InputError
from .jsjson import js_trim

__all__ = ["COMMAND", "DRIVER", "git", "installation"]

COMMAND = "flowwrench nodered-merge"
#: Git driver/attribute name. Kept identical to the Node tool so existing
#: ``.gitattributes`` blocks and local Git config remain compatible.
DRIVER = "nrflow-merge"
START = f"# BEGIN {DRIVER}"
END = f"# END {DRIVER}"
_MAX_BUFFER = 1024 * 1024
_PATTERN = re.compile(r"[A-Za-z0-9_./*?\[\]-]+")


def git(args: Sequence[str], cwd: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    except OSError as error:
        code = errno.errorcode.get(error.errno, "UNKNOWN") if error.errno else "UNKNOWN"
        raise InputError(f"git {args[0]} failed: {code}") from None
    if len(completed.stdout) > _MAX_BUFFER or len(completed.stderr) > _MAX_BUFFER:
        raise InputError(f"git {args[0]} failed: ENOBUFS")
    if completed.returncode != 0:
        detail = js_trim(completed.stderr.decode("utf-8", "replace"))
        raise InputError(f"git {args[0]} failed: {detail}")
    return js_trim(completed.stdout.decode("utf-8", "replace"))


def installation(*, repo: str = ".", pattern: str = "flows.json", apply: bool = False) -> dict[str, Any]:
    # Keep attribute syntax unambiguous; no credentials-file wildcard by default.
    if (
        not _PATTERN.fullmatch(pattern)
        or pattern.startswith("/")
        or pattern.startswith("-")
        or ".." in pattern.split("/")
    ):
        raise InputError("pattern must be a relative Git glob without whitespace, quotes, backslashes, or .. segments")
    root = git(["rev-parse", "--show-toplevel"], os.path.abspath(repo))
    if git(["rev-parse", "--is-bare-repository"], root) != "false":
        raise InputError("install requires a non-bare Git working tree")
    config = {
        f"merge.{DRIVER}.name": "Node-RED semantic three-way merge",
        f"merge.{DRIVER}.driver": f'{COMMAND} git-driver "%O" "%A" "%B"',
        f"merge.{DRIVER}.recursive": "binary",
        f"diff.{DRIVER}.textconv": f"{COMMAND} textconv",
    }
    attribute = f"{pattern} merge={DRIVER} diff={DRIVER}"
    plan: dict[str, Any] = {"repo": root, "attributes": attribute, "config": config, "applied": False}
    if not apply:
        return plan
    attrs = output_path(os.path.join(root, ".gitattributes"))
    try:
        text = Path(attrs).read_bytes().decode("utf-8", "replace")
    except FileNotFoundError:
        text = ""
    lines = re.split(r"\r?\n", text)
    starts = [i for i, line in enumerate(lines) if line == START]
    ends = [i for i, line in enumerate(lines) if line == END]
    if len(starts) != len(ends) or len(starts) > 1 or (starts and ends[0] < starts[0]):
        raise InputError(f"malformed {DRIVER} block in .gitattributes")
    block = [START, attribute, END]
    if starts:
        lines[starts[0] : ends[0] + 1] = block
    else:
        if lines and lines[-1] == "":
            lines.pop()
        lines.extend(block)
    # Explicit --apply changes only local Git configuration and root attributes.
    # Local config also belongs to a shared repository when run in a linked worktree.
    for key, value in config.items():
        git(["config", "--local", "--replace-all", key, value], root)
    write_outputs([(attrs, re.sub(r"\n+\Z", "", "\n".join(lines)) + "\n")])
    return {**plan, "applied": True}
