"""Command-line interface; stdout is data, stderr is diagnostics."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__
from .core import (EDGE_FIELDS, extract, lint, load_scenario, pointer_path, render,
                   required_variables, semantic_diff, split_files, to_export)
from .formats import (KitError, LocatedValidationError, MAX_FILE_BYTES, json_text,
                      load_data, write_directory, write_file)
from .graph import dot
from .relocation import combine


def parser():
    root = argparse.ArgumentParser(
        prog="flowwrench huginn", description="Offline git-friendly Huginn scenario tools")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    split = commands.add_parser("split", help="split schema-v1 JSON into a new YAML kit")
    split.add_argument("input", type=Path)
    split.add_argument("-o", "--output", type=Path, required=True)
    split.add_argument("--dry-run", action="store_true", help="validate without writing")
    join = commands.add_parser("join", help="assemble a YAML kit into importable JSON")
    join.add_argument("input", type=Path)
    join.add_argument("-o", "--output", type=Path, required=True)
    join.add_argument("--dry-run", action="store_true", help="validate without writing")
    render_cmd = commands.add_parser("render", help="substitute ${HSK:NAME} and emit importable JSON")
    render_cmd.add_argument("input", type=Path)
    render_cmd.add_argument("--vars", type=Path, required=True)
    render_cmd.add_argument("--reject-unused-vars", action="store_true",
                            help="reject bindings not referenced by original HSK placeholders")
    render_cmd.add_argument("-o", "--output", type=Path, help="new output file; default stdout")
    render_cmd.add_argument("--dry-run", action="store_true", help="validate without writing or exposing values")
    extract_cmd = commands.add_parser("extract-vars", help="list required variables and static credential names")
    extract_cmd.add_argument("input", type=Path)
    diff = commands.add_parser("diff", help="semantic diff of JSON exports or YAML kit directories")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--exit-code", action="store_true", help="exit 1 on differences")
    lint_cmd = commands.add_parser("lint", help="report orphan agents, cycles and unknown schedules")
    lint_cmd.add_argument("input", type=Path)
    combine_cmd = commands.add_parser("combine", help="combine JSON exports or concrete YAML kits using a relocation manifest")
    combine_cmd.add_argument("left", type=Path)
    combine_cmd.add_argument("right", type=Path)
    combine_cmd.add_argument("--manifest", type=Path, required=True)
    combine_cmd.add_argument("--vars", type=Path, help="manifest parameters only; no source-field rendering")
    combine_cmd.add_argument("--reject-unused-vars", action="store_true",
                             help="reject bindings not referenced by original manifest HSK placeholders")
    combine_cmd.add_argument("-o", "--output", type=Path, help="new output file; default stdout")
    combine_cmd.add_argument("--dry-run", action="store_true", help="validate without writing or exposing values")
    graph_cmd = commands.add_parser("graph", help="export a concrete GUID graph as DOT; schedules unevaluated")
    graph_cmd.add_argument("input", type=Path)
    graph_cmd.add_argument("-o", "--output", type=Path, help="new DOT file; default stdout")
    return root


def require_kit(path):
    if not path.is_dir():
        raise KitError("join requires a YAML kit directory")


def require_resolved(doc):
    variables = required_variables(doc)
    if variables:
        raise KitError("unresolved variables (use render): " + ", ".join(variables))


def combine_input(path):
    """Adapt concrete kits to join-equivalent exports; keep JSON behavior intact."""
    if not path.is_dir():
        return load_data(path, json_only=True)
    origins = {}
    doc = load_scenario(path, check_schedules=False, allow_templates=True, agent_origins=origins)

    def require_concrete(value, source, pointer):
        if type(value) is str and "${HSK:" in value:
            raise LocatedValidationError(pointer, "unresolved kit placeholder (use render before combine)",
                                         source=source)
        if type(value) is dict:
            for key, item in value.items():
                if "${HSK:" in key:
                    raise LocatedValidationError(pointer, "kit placeholder keys are not supported",
                                                 source=source)
                require_concrete(item, source, pointer + pointer_path([key]))
        elif type(value) is list:
            for index, item in enumerate(value):
                require_concrete(item, source, pointer + f"/{index}")

    metadata = {key: value for key, value in doc.items() if key not in {"agents", *EDGE_FIELDS}}
    require_concrete(metadata, path / "scenario.yaml", "/scenario")
    for agent in doc["agents"]:
        source, pointer = origins[agent["guid"]]
        require_concrete(agent, source, pointer)
    # Reuse strict join validation and canonical index rebuilding. No source is
    # rendered, and kit normalization cannot strip timestamps from JSON inputs.
    return to_export(doc, agent_origins=origins)


def run(args):
    if args.command == "graph":
        output = dot(load_scenario(args.input, check_schedules=False))
        if args.output:
            write_file(args.output, output)
        else:
            sys.stdout.write(output)
        return 0
    if args.command == "combine":
        doc = combine(combine_input(args.left), combine_input(args.right),
                      load_data(args.manifest), load_data(args.vars) if args.vars else {},
                      left_source=args.left, right_source=args.right, manifest_source=args.manifest,
                      reject_unused_vars=args.reject_unused_vars)
        output = json_text(doc)
        if len(output.encode("utf-8")) > MAX_FILE_BYTES:
            raise KitError("generated JSON exceeds 5 MiB file limit")
        if args.dry_run:
            sys.stdout.write(json_text({"valid": True, "agents": len(doc["agents"]),
                                        "links": len(doc["links"]), "control_links": len(doc["control_links"])}))
        elif args.output:
            write_file(args.output, output)
        else:
            sys.stdout.write(output)
        return 0
    if args.command == "diff":
        before = load_scenario(args.before, check_schedules=False, allow_templates=True)
        after = load_scenario(args.after, check_schedules=False, allow_templates=True)
        result = semantic_diff(before, after)
        sys.stdout.write(json_text(result))
        return 1 if args.exit_code and result["different"] else 0
    if args.command == "lint":
        result = lint(load_scenario(args.input, check_schedules=False))
        sys.stdout.write(json_text(result))
        return 0 if result["ok"] else 1
    if args.command == "extract-vars":
        agent_origins = {}
        doc = load_scenario(args.input, check_schedules=False, allow_templates=True,
                            agent_origins=agent_origins)
        sys.stdout.write(json_text(extract(doc, agent_origins=agent_origins)))
        return 0
    if args.command == "join":
        require_kit(args.input)
    if args.command == "split" and args.input.is_dir():
        raise KitError("split requires a scenario JSON file")
    agent_origins = {}
    doc = load_scenario(args.input, check_schedules=args.command == "split",
                        allow_templates=args.command in ("join", "render"), agent_origins=agent_origins)
    if args.command == "split":
        files = split_files(doc)
        if not args.dry_run:
            write_directory(args.output, files)
    else:
        if args.command == "render":
            doc = render(doc, load_data(args.vars), agent_origins=agent_origins,
                         reject_unused_vars=args.reject_unused_vars)
        require_resolved(doc)
        output = json_text(to_export(doc, agent_origins=agent_origins))
        if len(output.encode("utf-8")) > MAX_FILE_BYTES:
            raise KitError("generated JSON exceeds 5 MiB file limit")
        if not args.dry_run:
            if args.output:
                write_file(args.output, output)
            else:
                sys.stdout.write(output)
    if args.dry_run:
        sys.stdout.write(json_text({"valid": True, "agents": len(doc["agents"]),
                                    "links": len(doc["links"]), "control_links": len(doc["control_links"])}))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except BrokenPipeError:
        return 2
    except (KitError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
