"""``flowwrench windmill-dryrun`` command line (port of ``bin/openflow-dryrun.js``)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import __version__
from .bundle import encode_bundle, replay_bundle
from .cases import Entry, Suite, iterate_cases, junit, load_cases, run_case_entries
from .errors import FormatError, NodeUnavailableError, error_message
from .expression import find_node, node_runtime
from .fileio import checkpoint_outputs, read_data, utf8, write_output
from .jsvalues import stringify, to_number
from .runner import execute_flow

PROG = "flowwrench windmill-dryrun"

HELP = f"""{PROG} {__version__} — offline Windmill OpenFlow simulation

  {PROG} run FLOW --input in.json [--mocks mocks.yaml] [--trace out.json]
  {PROG} test CASES... [--junit results.xml] [--report suite.json] [--bundle saved.json]
  {PROG} replay BUNDLE --case N [--junit results.xml] [--report suite.json]

Options (run and test):
  --unmocked error|null|echo-args   Default: error
  --max-iterations N             Per-loop ceiling, default 100 (1..10000)
  --timeout-ms N                 Per-expression timeout, default 100 (1..2000)
  --max-steps N                  Trace/attempt budget, default 1000 (1..10000)
  --require-all-mocks-used true|false  Fail on unused sequence responses, default false
  --output-dir DIR               Output confinement root, default current directory

Outputs must be new files in existing directories beneath --output-dir.
--bundle checkpoints each completed case before loading the next; also writes
BUNDLE.junit.xml unless --junit selects its path. Replay reruns one saved case
offline from its beginning, with saved execution options (no overrides).
No workflow code, API, resource lookup, sleep or external step is executed.
Exit: 0 success, 1 run/assertion failure, 2 invalid input/unsupported format/IO.
"""

_COMMANDS = ("run", "test", "replay")
_EXECUTION_FLAGS = ("unmocked", "max-iterations", "timeout-ms", "max-steps", "require-all-mocks-used", "output-dir")
_NUMERIC_FLAGS = (("max-iterations", "maxIterations"), ("timeout-ms", "timeoutMs"), ("max-steps", "maxSteps"))
_DIGITS = re.compile(r"[0-9]+")
_POSITIVE = re.compile(r"[1-9][0-9]*")


@dataclass
class _Args:
    help: bool = False
    version: bool = False
    command: str = ""
    files: list[str] = field(default_factory=list)
    opts: dict[str, str] = field(default_factory=dict)
    runner_options: dict[str, Any] = field(default_factory=dict)


def _parse(argv: list[str]) -> _Args:
    if not argv or argv[0] in ("--help", "-h", ""):
        return _Args(help=True)
    command, rest = argv[0], argv[1:]
    if command == "--version":
        return _Args(version=True)
    if command not in _COMMANDS:
        raise FormatError(f"Unknown command '{command}'")
    if command == "replay":
        allowed: tuple[str, ...] = ("case", "junit", "report", "output-dir")
    else:
        allowed = _EXECUTION_FLAGS + (("input", "mocks", "trace") if command == "run" else ("junit", "report", "bundle"))
    opts: dict[str, str] = {}
    files: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in ("--help", "-h"):
            return _Args(help=True)
        if not arg.startswith("-"):
            files.append(arg)
            index += 1
            continue
        flag, *inline = (arg[2:] if arg.startswith("--") else arg).split("=")
        if flag not in allowed or not arg.startswith("--"):
            raise FormatError(f"Unknown option {arg}")
        if flag in opts:
            raise FormatError(f"Duplicate option --{flag}")
        if inline:
            value: str | None = "=".join(inline)
        else:
            index += 1
            value = rest[index] if index < len(rest) else None
        if not value or value.startswith("--"):
            raise FormatError(f"--{flag} requires a value")
        opts[flag] = value
        index += 1
    if command == "run" and (len(files) != 1 or not opts.get("input")):
        raise FormatError("run requires one FLOW and --input JSON")
    if command == "test" and not files:
        raise FormatError("test requires one or more CASE files (shell globs supported)")
    if command == "replay" and (len(files) != 1 or not _POSITIVE.fullmatch(opts.get("case", ""))):
        raise FormatError("replay requires one BUNDLE and --case positive integer")
    runner_options: dict[str, Any] = {}
    if "require-all-mocks-used" in opts:
        if opts["require-all-mocks-used"] not in ("true", "false"):
            raise FormatError("--require-all-mocks-used requires true or false")
        runner_options["requireAllMocksUsed"] = opts["require-all-mocks-used"] == "true"
    if "unmocked" in opts:
        runner_options["unmocked"] = opts["unmocked"]
    for flag, key in _NUMERIC_FLAGS:
        if flag in opts:
            runner_options[key] = _number(opts[flag])
    return _Args(command=command, files=files, opts=opts, runner_options=runner_options)


def _number(text: str) -> int | float:
    """``/^\\d+$/.test(text) ? Number(text) : NaN``."""
    return to_number(text) if _DIGITS.fullmatch(text) else float("nan")


def _write(stream: TextIO, text: str) -> None:
    """Write UTF-8 like Node, whatever the stream's configured encoding."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        return
    stream.flush()
    buffer.write(utf8(text))
    buffer.flush()


def _out(text: str) -> None:
    _write(sys.stdout, text)


def _err(text: str) -> None:
    _write(sys.stderr, text)


def _ensure_node() -> None:
    if find_node() is None:
        raise NodeUnavailableError()
    node_runtime()


def _run(args: _Args) -> int:
    report = execute_flow(
        read_data(args.files[0]),
        read_data(args.opts["input"], json=True),
        read_data(args.opts["mocks"]) if args.opts.get("mocks") else {},
        args.runner_options,
    )
    encoded = (stringify(report, 2) or "") + "\n"
    if args.opts.get("trace"):
        write_output(args.opts["trace"], encoded, args.opts.get("output-dir"))
    _out(encoded)
    return 0 if report["status"] == "ok" else 1


def _checkpointed_suite(args: _Args, junit_path: str) -> Suite:
    bundle_path = args.opts["bundle"]
    report_path = args.opts.get("report")
    write_checkpoint = checkpoint_outputs([bundle_path, junit_path, report_path], args.opts.get("output-dir"))
    completed: list[Entry] = []
    last_prefix: Suite | None = None

    def on_case(entry: Entry, prefix: Suite) -> None:
        nonlocal last_prefix
        completed.append(entry)
        # Encode before touching the last checkpoint: an oversized next prefix
        # must leave the earlier, replayable one intact. JSON embeds its XML.
        encoded = encode_bundle(completed, prefix, bundle_path, junit_path, len(args.files))
        write_checkpoint(bundle_path, encoded)
        write_checkpoint(junit_path, junit(prefix))
        last_prefix = prefix

    def on_invalid_case(error: FormatError) -> None:
        if last_prefix is None:
            return  # An invalid first case still creates no outputs.
        terminal = {"case": len(completed) + 1, "path": args.files[len(completed)], "message": error.message}
        try:
            write_checkpoint(
                bundle_path,
                encode_bundle(completed, last_prefix, bundle_path, junit_path, len(args.files), terminal),
            )
        except Exception as write_error:  # noqa: BLE001
            # Preserve both the previous atomic checkpoint and the original
            # invalid-input diagnostic/exit code.
            _err(f"Cannot save terminal diagnostics: {error_message(write_error)}\n")

    suite = run_case_entries(iterate_cases(args.files, args.runner_options), on_case, on_invalid_case)
    if report_path:
        write_checkpoint(report_path, (stringify(suite, 2) or "") + "\n")
    return suite


def _suite(args: _Args) -> int:
    junit_path = args.opts.get("junit") or (args.opts["bundle"] + ".junit.xml" if args.opts.get("bundle") else None)
    if args.opts.get("bundle"):
        assert junit_path is not None
        suite = _checkpointed_suite(args, junit_path)
    else:
        if args.command == "test":
            suite = run_case_entries(load_cases(args.files, args.runner_options))
        else:
            suite = replay_bundle(args.files[0], to_number(args.opts["case"]))
        output_dir = args.opts.get("output-dir")
        if junit_path:
            write_output(junit_path, junit(suite), output_dir)
        if args.opts.get("report"):
            write_output(args.opts["report"], (stringify(suite, 2) or "") + "\n", output_dir)
    lines: list[str] = []
    for case in suite["cases"]:
        lines.append(f"{'PASS' if case['passed'] else 'FAIL'} {case['name']}")
        lines.extend(f"  {failure}" for failure in case["failures"])
    modules = sum(len(item["modules"]["executed"]) for item in suite["coverage"])
    module_total = sum(item["modules"]["total"] for item in suite["coverage"])
    branches = sum(len(item["branches"]["executed"]) for item in suite["coverage"])
    branch_total = sum(item["branches"]["total"] for item in suite["coverage"])
    lines.append(
        f"{len(suite['cases'])} cases, {suite['passed']} passed, {suite['failed']} failed; "
        f"modules {modules}/{module_total} executed, branches {branches}/{branch_total}"
    )
    if "replay" in suite:
        replay = suite["replay"]
        lines.append(f"REPLAY case {replay['case']}: {'reproduced' if replay['reproduced'] else 'differs'}")
    _out("".join(f"{line}\n" for line in lines))
    return 1 if suite["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    """Run ``flowwrench windmill-dryrun``; return the process exit code."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        args = _parse(arguments)
        if args.help:
            _out(HELP)
            return 0
        if args.version:
            _out(f"{__version__}\n")
            return 0
        _ensure_node()
        if args.command == "run":
            return _run(args)
        return _suite(args)
    except NodeUnavailableError as error:
        _err(f"{error.message}\n")
        return 2
    except FormatError as error:
        _err(f"Invalid input: {error.message}\n")
        return 2
    except Exception as error:  # noqa: BLE001 - no host stack traces, like the Node tool
        _err(f"Error: {error_message(error)}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
