"""JSON stdin/stdout bridge exposing the Python generator to the Node test drivers.

Usage: ``python bridge.py <parse|normalize|render|write>`` with a JSON payload on
stdin.  The response is ``{"value": ...}`` or ``{"error": message}``.  JavaScript
``undefined`` and nonfinite numbers are encoded as ``{"$openapi2piece:js": kind}``
in both directions (see harness.mjs).
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

from flowwrench.openapi2piece import UNDEFINED, normalize_schema, parse_spec, render_piece, write_piece

MARK = "$openapi2piece:js"


def _decode_marker(obj: dict[str, Any]) -> Any:
    if len(obj) == 1 and MARK in obj:
        kind = obj[MARK]
        if kind == "undefined":
            return UNDEFINED
        return float(kind.replace("Infinity", "inf"))
    return obj


def _encode(value: Any) -> Any:
    if value is UNDEFINED:
        return {MARK: "undefined"}
    if isinstance(value, float) and not math.isfinite(value):
        return {MARK: "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _option(options: Any, key: str, default: Any) -> Any:
    if not isinstance(options, dict):
        return default
    value = options.get(key, UNDEFINED)
    return default if value is UNDEFINED else value


def _dispatch(op: str, payload: dict[str, Any]) -> Any:
    if op == "parse":
        return parse_spec(payload["text"])
    if op == "normalize":
        label = payload.get("label", UNDEFINED)
        depth = payload.get("depth", UNDEFINED)
        options = payload.get("options", UNDEFINED)
        return normalize_schema(
            payload.get("input", UNDEFINED),
            "schema" if label is UNDEFINED else label,
            0 if depth is UNDEFINED else depth,
            project_request=bool(_option(options, "projectRequest", False)),
            property=bool(_option(options, "property", False)),
        )
    if op == "render":
        options = payload.get("options", UNDEFINED)
        max_actions = _option(options, "maxActions", None)
        rendered = render_piece(
            payload["spec"],
            name=_option(options, "name", UNDEFINED),
            include_tags=_option(options, "includeTags", None),
            include_ops=_option(options, "includeOps", None),
            max_actions=max_actions,
        )
        return {"files": [[path, contents] for path, contents in rendered.files.items()], "model": rendered.model}
    if op == "write":
        options = payload.get("options", UNDEFINED)
        write_piece(dict(payload["files"]), payload["out"], dry_run=bool(_option(options, "dryRun", False)))
        return None
    raise ValueError(f"unknown bridge operation {op!r}")


def main() -> int:
    op = sys.argv[1]
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"), object_hook=_decode_marker)
    try:
        response: dict[str, Any] = {"value": _dispatch(op, payload)}
    except Exception as error:  # noqa: BLE001 - every generator error is reported to the JS caller.
        response = {"error": str(error)}
        report = getattr(error, "report", None)
        if report:
            response["report"] = report
    sys.stdout.buffer.write(json.dumps(_encode(response), ensure_ascii=True, allow_nan=False).encode("ascii"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
