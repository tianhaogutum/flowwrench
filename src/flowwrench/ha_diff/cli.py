"""Local-only command line interface. No HA credentials or network required."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from .compare import compare_traces
from .loader import TraceError, load_trace
from .render import render_html, render_text


def _write_report(path: Path, output: str, inputs: tuple[Path, Path]) -> None:
    """Atomic replacement keeps a failed write from damaging an existing report."""
    if path.resolve() in {item.resolve() for item in inputs} or (
        path.exists() and any(path.samefile(item) for item in inputs)
    ):
        raise TraceError("output path must not overwrite an input trace")
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise TraceError("output path must be a regular file, not a symlink or special file")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".ha-trace-diff-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowwrench ha-diff",
        description="Compare recorded semantics in two downloaded Home Assistant traces.",
    )
    parser.add_argument("left", help="baseline/successful trace JSON")
    parser.add_argument("right", help="comparison/failing trace JSON")
    parser.add_argument("--left-pointer", default="", help="RFC 6901 pointer selecting one trace")
    parser.add_argument("--right-pointer", default="", help="RFC 6901 pointer selecting one trace")
    parser.add_argument("--format", choices=("text", "json", "html"), default="text")
    parser.add_argument("--output", type=Path, help="write report to a file instead of stdout")
    parser.add_argument(
        "--exact", action="store_true", help="also compare generated context/child run IDs"
    )
    parser.add_argument(
        "--ordinal-repeats",
        action="store_true",
        help="disable directly recorded repeat-index anchors; use uncertain occurrence ordinals",
    )
    parser.add_argument(
        "--ignore-field",
        action="append",
        default=[],
        metavar="POINTER",
        help="ignore an exact field pointer relative to every step (repeatable)",
    )
    parser.add_argument("--show-equal", action="store_true", help="include equal rows in text")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 on differences, 3 on partial evidence"
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        left = load_trace(args.left, args.left_pointer)
        right = load_trace(args.right, args.right_pointer)
        report = compare_traces(
            left,
            right,
            exact=args.exact,
            ignored_fields=tuple(args.ignore_field),
            align_repeats=not args.ordinal_repeats,
        )
        if args.format == "json":
            output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        elif args.format == "html":
            output = render_html(report, show_equal=args.show_equal)
        else:
            output = render_text(report, show_equal=args.show_equal)
        if args.output:
            _write_report(args.output, output, (Path(args.left), Path(args.right)))
        else:
            sys.stdout.write(output)
    except BrokenPipeError:
        return 0
    except (TraceError, OSError, UnicodeError, RecursionError) as exc:
        print(f"flowwrench ha-diff: {exc}", file=sys.stderr)
        return 2
    if args.check:
        if report["coverage"] == "partial":
            return 3
        if report["status"] == "different":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
