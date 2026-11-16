"""JavaScript expression evaluation in an isolated Node.js process.

Python cannot evaluate Windmill JavaScript faithfully, so every expression is
evaluated by the bundled ``js/evaluate.js`` program in a fresh, permission-
restricted, heap-bounded ``node`` process (one process per expression), the
same isolation the Node.js tool uses. Protocol:

* stdin: ``JSON.stringify({expr, scope, ids, timeout})`` as UTF-8;
* stdout: ``{"ok": true, "value": <JSON>}`` or ``{"ok": false, "message": str}``.
"""

from __future__ import annotations

import errno
import functools
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .errors import FormatError, NodeUnavailableError, StepError, error_message
from .fileio import json_value
from .jsvalues import UNDEFINED, number_to_string, parse_json, quote, stringify

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_CHILD_ENV = {"NODE_NO_WARNINGS": "1", "TZ": "UTC"}
_VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)\S*")


@dataclass(frozen=True)
class NodeRuntime:
    path: str
    version: str
    major: int
    minor: int

    @property
    def permission_flag(self) -> str:
        # --permission is stable from Node 22.13; earlier releases are experimental.
        if self.major > 22 or (self.major == 22 and self.minor >= 13):
            return "--permission"
        return "--experimental-permission"


def find_node() -> str | None:
    """The ``node`` executable on PATH, if any."""
    return shutil.which("node")


@functools.lru_cache(maxsize=1)
def node_runtime() -> NodeRuntime:
    """Locate Node.js 20+ (cached after the first success).

    ``node`` on PATH may be a version-manager shim that needs the caller's
    environment, so it is asked once (with that environment, minus
    NODE_OPTIONS) for its real executable; every expression process then runs
    that executable directly with the minimal sandbox environment.
    """
    path = find_node()
    if path is None:
        raise NodeUnavailableError()
    env = {key: value for key, value in os.environ.items() if key != "NODE_OPTIONS"}
    env["NODE_NO_WARNINGS"] = "1"
    try:
        completed = subprocess.run(
            [path, "-p", 'process.version + " " + process.execPath'],
            capture_output=True, timeout=30, env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NodeUnavailableError(f"cannot run {path}: {error}") from None
    version, _, executable = completed.stdout.decode("utf-8", errors="replace").strip().partition(" ")
    match = _VERSION.fullmatch(version)
    if completed.returncode != 0 or match is None or int(match.group(1)) < 20:
        raise NodeUnavailableError(f"found {version or 'an unknown version'} at {path}")
    return NodeRuntime(executable or path, version, int(match.group(1)), int(match.group(2)))


@functools.lru_cache(maxsize=1)
def _child_source() -> str:
    return resources.files(__package__).joinpath("js").joinpath("evaluate.js").read_text(encoding="utf-8")


def _budget_error(code: str, step_id: str | None) -> StepError:
    return StepError(f"Expression process failed or exceeded budget ({code})", step_id, "ExpressionError")


def evaluate_encoded(expr: str, scope_json: str, ids: list[str], step_id: str | None, timeout: int | float = 100) -> Any:
    """Evaluate ``expr`` against an already ``JSON.stringify``-ed scope."""
    runtime = node_runtime()
    request = (
        '{"expr":' + quote(expr) + ',"scope":' + scope_json + ',"ids":' + (stringify(ids) or "[]")
        + ',"timeout":' + number_to_string(timeout) + "}"
    )
    try:
        child = subprocess.run(
            [
                runtime.path, runtime.permission_flag, "--disable-proto=throw",
                "--max-old-space-size=64", "-e", _child_source(),
            ],
            input=request.encode("utf-8"),
            capture_output=True,
            timeout=(timeout + 1500) / 1000,
            env=dict(_CHILD_ENV),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _budget_error("ETIMEDOUT", step_id) from None
    except OSError as error:
        raise _budget_error(errno.errorcode.get(error.errno or 0, "ERROR"), step_id) from None
    if len(child.stdout) > MAX_OUTPUT_BYTES or len(child.stderr) > MAX_OUTPUT_BYTES:
        raise _budget_error("ENOBUFS", step_id)
    if child.returncode < 0:
        try:
            name = signal.Signals(-child.returncode).name
        except ValueError:
            name = str(-child.returncode)
        raise _budget_error(name, step_id)
    try:
        response = parse_json(child.stdout.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        raise StepError("Invalid expression response", step_id, "ExpressionError") from None
    if not isinstance(response, dict):
        raise StepError("Invalid expression response", step_id, "ExpressionError")
    if not response.get("ok"):
        message = response.get("message", "")
        raise StepError(message if isinstance(message, str) else str(message), step_id, "ExpressionError")
    if child.returncode != 0:
        raise StepError("Expression process failed", step_id, "ExpressionError")
    value = response.get("value", UNDEFINED)
    try:
        json_value(value, "Expression result")
    except FormatError as error:
        raise StepError(error_message(error), step_id, "ExpressionError") from None
    return value


def evaluate(expr: str, scope: dict[str, Any], ids: list[str], step_id: str | None, timeout: int | float = 100) -> Any:
    """Evaluate one Windmill JavaScript expression; raise ``StepError`` on failure."""
    return evaluate_encoded(expr, stringify(scope) or "null", ids, step_id, timeout)
