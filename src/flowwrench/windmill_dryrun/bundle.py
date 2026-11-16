"""Self-contained diagnostic bundles and offline replay (port of ``lib/bundle.js``).

The bundle format is byte-compatible with openflow-dryrun 0.1.0 bundle v1, so
bundles can be replayed by either implementation.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from typing import Any, NoReturn

from .cases import Entry, Suite, junit, run_case_entries
from .errors import FormatError, NodeUnavailableError, error_message
from .expression import node_runtime
from .fileio import MAX_BYTES, json_value, relative_path, require_keys, resolve_path, uv_error
from .jsvalues import UNDEFINED, deep_equal, is_number, is_safe_integer, parse_json, stringify

TOOL_NAME = "openflow-dryrun"
TOOL_VERSION = "0.1.0"
OPENFLOW_VERSION = "v1.868.0"
MAX_BUNDLE_BYTES = 64 * MAX_BYTES
SNAPSHOT_FIELDS = ("flow", "input", "mocks", "expect", "options")


def digest(value: Any) -> str:
    """SHA-256 of UTF-8 ``JSON.stringify(value)`` (property order preserved)."""
    return hashlib.sha256((stringify(value) or "").encode("utf-8")).hexdigest()


def _node_version() -> str:
    """The Node.js version recorded in the bundle, looked up only when encoding."""
    try:
        return node_runtime().version
    except NodeUnavailableError:
        return "unknown"


def _invalid(message: str) -> NoReturn:
    raise FormatError(f"bundle: {message}")


def encode_bundle(
    entries: list[Entry],
    suite: Suite,
    bundle_path: str,
    junit_path: str,
    total: int | None = None,
    terminal_error: dict[str, Any] | None = None,
) -> str:
    """Encode the completed cases (a checkpoint) as pretty JSON text."""
    total = len(entries) if total is None else total
    href = relative_path(posixpath.dirname(resolve_path(bundle_path)), resolve_path(junit_path)).replace("\\", "/")
    bundle: dict[str, Any] = {
        "format": "openflow-dryrun-bundle",
        "version": 1,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION, "node": _node_version(), "openflow": OPENFLOW_VERSION},
        "hash_encoding": "sha256 of UTF-8 JSON.stringify(parsed value), preserving property order",
        "checkpoint": {"total": total, "completed": len(entries), "complete": len(entries) == total},
    }
    if terminal_error is not None:
        bundle["terminal_error"] = terminal_error
    bundle["junit_xml"] = junit(suite)
    cases = []
    for index, entry in enumerate(entries):
        outcome = suite["cases"][index]
        report = outcome["report"]
        hashes = {name: digest(entry["snapshot"][name]) for name in SNAPSHOT_FIELDS}
        hashes["outcome"] = digest(outcome)
        cases.append({
            "id": index + 1,
            "name": entry["name"],
            "path": entry["path"],
            "flowPath": entry["flowPath"],
            "snapshot": entry["snapshot"],
            "hashes": hashes,
            "outcome": outcome,
            "selected_branches": report["coverage"]["branches"]["executed"],
            "mock_calls": [
                {"trace_index": trace_index, "module_id": event["module_id"], "path": event["path"],
                 "attempt": event["attempt"], **event["mock"]}
                for trace_index, event in enumerate(report["trace"])
                if event.get("mock")
            ],
            "junit": {"href": href, "testcase_index": index, "name": outcome["name"]},
        })
    bundle["cases"] = cases
    _validate_terminal_error(bundle)
    encoded = (stringify(bundle, 2) or "") + "\n"
    if len(encoded.encode("utf-8")) > MAX_BUNDLE_BYTES:
        _invalid("diagnostics exceed 64 MiB; save fewer cases")
    return encoded


def _validate_terminal_error(bundle: dict[str, Any]) -> None:
    """Optional metadata: old and interrupted checkpoints have no terminal record."""
    if "terminal_error" not in bundle:
        return
    error = bundle["terminal_error"]
    checkpoint = bundle.get("checkpoint")
    require_keys(error, ("case", "path", "message"), "bundle.terminal_error")
    case = error.get("case", UNDEFINED)
    path = error.get("path", UNDEFINED)
    message = error.get("message", UNDEFINED)
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("complete") is not False
        or not is_safe_integer(case)
        or not (is_number(checkpoint.get("completed")) and case == checkpoint["completed"] + 1)
        or not (is_number(checkpoint.get("total")) and case <= checkpoint["total"])
        or not isinstance(path, str)
        or not path
        or not isinstance(message, str)
        or not message
    ):
        _invalid("invalid terminal_error: expected next requested case/path/message in an incomplete checkpoint")


def _read_bundle(path: str) -> dict[str, Any]:
    try:
        try:
            info = os.stat(path)
        except OSError as error:
            raise uv_error(error, "stat", path) from None
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE_BYTES:
            _invalid("expected a regular JSON file of at most 64 MiB")
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as error:
            raise uv_error(error, "open", path) from None
        bundle = parse_json(raw.decode("utf-8", errors="replace"))
    except Exception as error:  # noqa: BLE001
        raise FormatError(f"{path}: {error_message(error)}") from None
    require_keys(bundle, ("format", "version", "tool", "hash_encoding", "checkpoint", "terminal_error", "junit_xml", "cases"), "bundle")
    if bundle.get("format") != "openflow-dryrun-bundle" or not (
        is_number(bundle.get("version")) and bundle["version"] == 1
    ):
        _invalid("unsupported format/version")
    tool = bundle.get("tool", UNDEFINED)
    require_keys(tool, ("name", "version", "node", "openflow"), "bundle.tool")
    if tool.get("name") != TOOL_NAME or tool.get("version") != TOOL_VERSION or tool.get("openflow") != OPENFLOW_VERSION:
        _invalid("unsupported tool version or OpenFlow version")
    cases = bundle.get("cases")
    if not isinstance(cases, list) or not cases:
        _invalid("cases must be a nonempty array")
    # Optional for compatibility with bundles saved before incremental capture.
    if "checkpoint" in bundle:
        checkpoint = bundle["checkpoint"]
        require_keys(checkpoint, ("total", "completed", "complete"), "bundle.checkpoint")
        total = checkpoint.get("total", UNDEFINED)
        completed = checkpoint.get("completed", UNDEFINED)
        complete = checkpoint.get("complete", UNDEFINED)
        if (
            not is_safe_integer(total)
            or total < len(cases)
            or not (is_number(completed) and completed == len(cases))
            or not (isinstance(complete, bool) and complete == (completed == total))
        ):
            _invalid("invalid checkpoint counts/completion")
    _validate_terminal_error(bundle)
    return bundle


def replay_bundle(path: str, case_id: int | float) -> Suite:
    """Rerun one saved case from its captured inputs; compare with its outcome."""
    bundle = _read_bundle(path)
    cases = bundle["cases"]
    if not is_safe_integer(case_id) or case_id < 1 or case_id > len(cases):
        _invalid(f"case must be 1..{len(cases)}")
    saved = cases[int(case_id) - 1]
    require_keys(saved, ("id", "name", "path", "flowPath", "snapshot", "hashes", "outcome", "selected_branches", "mock_calls", "junit"), "bundle.case")
    if not (is_number(saved.get("id")) and saved["id"] == case_id) or any(
        not isinstance(saved.get(key), str) for key in ("name", "path", "flowPath")
    ):
        _invalid("invalid case identity")
    snapshot = saved.get("snapshot", UNDEFINED)
    require_keys(snapshot, SNAPSHOT_FIELDS, "bundle.case.snapshot")
    hashes = saved.get("hashes", UNDEFINED)
    require_keys(hashes, (*SNAPSHOT_FIELDS, "outcome"), "bundle.case.hashes")
    for name in (*SNAPSHOT_FIELDS, "outcome"):
        value = saved.get("outcome", UNDEFINED) if name == "outcome" else snapshot.get(name, UNDEFINED)
        if value is UNDEFINED:
            _invalid(f"missing {name}")
        # Each captured input retains the ordinary JSON safety/depth boundary.
        # Reports have extra wrapper/trace levels, so allow those for outcomes.
        json_value(value, f"bundle.case.{name}", 0, set(), 128 if name == "outcome" else 64)
        if hashes.get(name) != digest(value):
            _invalid(f"hash mismatch for {name}")
    # Original paths and JUnit hrefs are metadata only: never read them, and
    # never accept new execution options.
    suite = run_case_entries([saved])
    actual = suite["cases"][0]
    reproduced = deep_equal(actual, saved["outcome"])
    original = saved["outcome"]
    suite["replay"] = {
        "case": case_id,
        "reproduced": reproduced,
        "original_passed": original.get("passed", UNDEFINED) if isinstance(original, dict) else UNDEFINED,
    }
    if not reproduced:
        actual["failures"].append("ReplayMismatch: current report or assertion diagnostics differ from the saved outcome")
        actual["passed"] = False
        suite["passed"] = 0
        suite["failed"] = 1
    return suite
