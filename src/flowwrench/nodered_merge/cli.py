"""``flowwrench nodered-merge`` command line (port of ``bin/nrflow-merge.js``)."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from .fileio import same_file, write_outputs
from .flow_document import merge_snapshots, read_merge_snapshot, serialize_merge_result
from .flows import InputError, diff_flows, read_flow, serialize
from .git_manifest import capture_git_manifest
from .install import git, installation
from .jsjson import js_to_string, stringify, to_utf8
from .resolution import create_manifest, resolve_manifest

__all__ = ["HELP", "PROG", "VERSION", "main"]

PROG = "flowwrench nodered-merge"
VERSION = "0.1.0"
HELP = f"""{PROG} {VERSION} — Node-RED semantic diff / three-way merge

  diff A B [--ignore-layout] [--format text|json] [--config-ref TYPE.PROPERTY ...]
  merge BASE OURS THEIRS -o OUT [--prefer ours|theirs]
        [--conflicts-file REPORT] [--config-ref TYPE.PROPERTY ...] [--manifest FILE]
        [--ignore-layout]
  resolve BASE OURS THEIRS --manifest FILE -o OUT [--conflicts-file REPORT] [--ignore-layout]
  git-manifest FILE --snapshot-dir DIR [--repo DIR] [--prefer ours|theirs]
        [--config-ref TYPE.PROPERTY ...] [--ignore-layout] [--allow-missing-base]
  normalize FILE [-o OUT]                (stdout without -o)
  install [--repo DIR] [--pattern GLOB] [--apply]
  textconv FILE                         (Git diff integration)
  git-driver BASE OURS THEIRS            (Git merge integration)

Exit: 0 success; 1 merge conflicts/integrity issues; 2 invalid input or I/O.
Merge accepts arrays or strict {{rev?, flows:[...]}} /flows documents.
Output keeps ours' shape and exact revision/absence; server revision is not verified.
Merge always writes a JSON report (default OUT.conflicts.json).
Reports summarize base-to-ours/theirs/result; --ignore-layout filters summaries only.
--prefer selects conflict fallback; conflicts still exit 1.
--manifest writes editable decisions; resolve refuses stale/unresolved input before writing.
git-manifest retains index stages 1/2/3 and a manifest in a new private directory.
--allow-missing-base explicitly uses [] for an absent stage 1; stages 2/3 are required.
install prints a plan unless --apply is given. No Node-RED code is executed.
"""

Options = dict[str, Any]


def _write(stream: TextIO, text: str) -> None:
    """Write UTF-8 bytes (Node semantics) regardless of the locale encoding."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        stream.flush()
        return
    stream.flush()
    buffer.write(to_utf8(text))
    buffer.flush()


def _json_text(value: Any) -> str:
    return f"{stringify(value, 2)}\n"


def _parse(args: Sequence[str], value_flags: Sequence[str], bool_flags: Sequence[str] = ()) -> tuple[list[str], Options]:
    positional: list[str] = []
    options: Options = {}
    literal = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" and not literal:
            literal = True
        elif not literal and arg in value_flags:
            if i + 1 == len(args) or args[i + 1].startswith("--"):
                raise InputError(f"{arg} requires a value")
            i += 1
            value = args[i]
            if arg == "--config-ref":
                options.setdefault(arg, []).append(value)
            else:
                if arg in options:
                    raise InputError(f"duplicate option {arg}")
                options[arg] = value
        elif not literal and arg in bool_flags:
            options[arg] = True
        elif not literal and arg.startswith("-"):
            raise InputError(f"unknown option {arg} (use -- before filenames beginning with -)")
        else:
            positional.append(arg)
        i += 1
    return positional, options


def _count(files: Sequence[str], n: int, command: str) -> None:
    if len(files) != n:
        raise InputError(f"{command} requires {n} input file(s); see --help")


def _invalid(entry: dict[str, Any]) -> str:
    return "" if entry["valid"] else f" (invalid: {js_to_string(entry['reason'])})"


def text_diff(diff: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []

    def show(entry: dict[str, Any]) -> str:
        return stringify(entry["value"]) if entry["present"] else "<absent>"

    signs = (("removed", "-"), ("added", "+"))
    for key in ("added", "removed", "changed", "layoutOnly"):
        for node in diff[key]:
            lines.append(f"{key}: {node['name']}")
            for prop in node.get("properties") or []:
                lines.append(f"  {prop['property']}: {show(prop['before'])} -> {show(prop['after'])}")
            for port in node.get("wireChanges") or []:
                for side, sign in signs:
                    for target in port[side]:
                        lines.append(f"    port {port['port']} {sign} {target['name']}{_invalid(target)}")
            for port in node.get("subflowWireChanges") or []:
                for side, sign in signs:
                    for target in port[side]:
                        if target["passThrough"]:
                            endpoint = f" input port {js_to_string(target['port'])} (pass-through)"
                        elif target["port"] is None:
                            endpoint = " (port unspecified)"
                        else:
                            endpoint = f" port {js_to_string(target['port'])}"
                        lines.append(
                            f"    subflow {port['kind']} port {port['port']} {sign} {target['name']}{endpoint}{_invalid(target)}"
                        )
            references = node.get("configReferences") or {}
            for side, sign in signs:
                for ref in references.get(side) or []:
                    lines.append(f"    config {ref['property']} {sign} {ref['name']}{_invalid(ref)}")
    body = "\n".join(lines) or "No changes."
    return f"{body}\n"


def _run(argv: Sequence[str]) -> int:
    command = argv[0] if argv else None
    args = list(argv[1:])
    if command in ("--help", "-h") or not command:
        _write(sys.stdout, HELP)
        return 0
    if command == "--version":
        _write(sys.stdout, f"{VERSION}\n")
        return 0
    if command == "diff":
        files, options = _parse(args, ("--format", "--config-ref"), ("--ignore-layout",))
        _count(files, 2, command)
        fmt = options.get("--format", "text")
        if fmt not in ("text", "json"):
            raise InputError("--format must be text or json")
        diff = diff_flows(
            read_flow(files[0]),
            read_flow(files[1]),
            ignore_layout=bool(options.get("--ignore-layout", False)),
            config_refs=options.get("--config-ref", []),
        )
        _write(sys.stdout, _json_text(diff) if fmt == "json" else text_diff(diff))
        return 0
    if command in ("normalize", "textconv"):
        files, options = _parse(args, ("-o",) if command == "normalize" else ())
        _count(files, 1, command)
        output = serialize(read_flow(files[0]))
        if options.get("-o"):
            write_outputs([(options["-o"], output)])
        else:
            _write(sys.stdout, output)
        return 0
    if command == "git-manifest":
        files, options = _parse(
            args,
            ("--repo", "--snapshot-dir", "--prefer", "--config-ref"),
            ("--ignore-layout", "--allow-missing-base"),
        )
        _count(files, 1, command)
        receipt = capture_git_manifest(
            files[0],
            repo=options.get("--repo", "."),
            snapshot_dir=options.get("--snapshot-dir"),
            allow_missing_base=bool(options.get("--allow-missing-base", False)),
            prefer=options.get("--prefer", "ours"),
            config_refs=options.get("--config-ref", []),
            ignore_layout=bool(options.get("--ignore-layout", False)),
        )
        _write(sys.stdout, _json_text(receipt))
        if not receipt["clean"]:
            _write(sys.stderr, f"{PROG}: review retained manifest: {receipt['manifest']}; report: {receipt['report']}\n")
        return 0 if receipt["clean"] else 1
    if command in ("merge", "git-driver", "resolve"):
        return _merge(command, args)
    if command == "install":
        files, options = _parse(args, ("--repo", "--pattern"), ("--apply",))
        _count(files, 0, command)
        plan = installation(
            repo=options.get("--repo", "."),
            pattern=options.get("--pattern", "flows.json"),
            apply=bool(options.get("--apply", False)),
        )
        _write(sys.stdout, _json_text(plan))
        return 0
    raise InputError(f"unknown command {command}; see --help")


def _merge(command: str, args: Sequence[str]) -> int:
    driver = command == "git-driver"
    resolving = command == "resolve"
    if driver:
        value_flags: tuple[str, ...] = ()
        bool_flags: tuple[str, ...] = ()
    elif resolving:
        value_flags, bool_flags = ("-o", "--manifest", "--conflicts-file"), ("--ignore-layout",)
    else:
        value_flags = ("-o", "--prefer", "--conflicts-file", "--config-ref", "--manifest")
        bool_flags = ("--ignore-layout",)
    files, options = _parse(args, value_flags, bool_flags)
    _count(files, 3, command)
    if not driver and not options.get("-o"):
        raise InputError(f"{command} requires -o OUT")
    if resolving and not options.get("--manifest"):
        raise InputError("resolve requires --manifest FILE")
    snapshots = [read_merge_snapshot(file) for file in files]
    prefer = options.get("--prefer", "ours")
    config_refs: list[str] = options.get("--config-ref", [])
    ignore_layout = bool(options.get("--ignore-layout", False))
    if resolving:
        result = resolve_manifest(snapshots, options["--manifest"], ignore_layout=ignore_layout)
    else:
        result = merge_snapshots(snapshots, prefer=prefer, config_refs=config_refs, ignore_layout=ignore_layout)
    out: str = files[1] if driver else options["-o"]
    report: str = options.get("--conflicts-file", f"{out}.conflicts.json")
    if driver:
        directory = os.path.abspath(git(["rev-parse", "--git-path", "nrflow-merge-conflicts"]))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        report = os.path.join(directory, f"{os.path.basename(files[1])}.json")
    if any(same_file(file, report) for file in files):
        raise InputError("conflict report must not overwrite an input file")
    manifest = options.get("--manifest")
    if manifest:
        if any(same_file(file, manifest) or same_file(file, out) for file in files):
            raise InputError("manifest workflow must preserve all three original input files")
        if same_file(manifest, out) or same_file(manifest, report):
            raise InputError("manifest, output and report must be different files")
    document: dict[str, Any] = {
        "version": 1,
        "clean": result["clean"],
        "inputs": [os.path.abspath(file) for file in files],
        "output": os.path.abspath(out),
        "conflicts": result["conflicts"],
        "integrity": result["integrity"],
        "summary": result["summary"],
        "revisionProvenance": result["revisionProvenance"],
    }
    if result.get("gitIndex"):
        document["gitIndex"] = result["gitIndex"]
    outputs: list[tuple[str, str | bytes]] = [(out, serialize_merge_result(result)), (report, _json_text(document))]
    if manifest and not resolving:
        outputs.append(
            (manifest, _json_text(create_manifest(snapshots, prefer=prefer, config_refs=config_refs, result=result)))
        )
    write_outputs(outputs)
    if not result["clean"]:
        _write(
            sys.stderr,
            f"{PROG}: {len(result['conflicts'])} conflict(s), {len(result['integrity'])} integrity issue(s); report: {report}\n",
        )
    return 0 if result["clean"] else 1


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the process exit code (0, 1 or 2)."""
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except Exception as error:  # noqa: BLE001 - every failure maps to exit 2, as in Node
        _write(sys.stderr, f"{PROG}: {error}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
