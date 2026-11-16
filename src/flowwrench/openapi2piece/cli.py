"""``flowwrench openapi2piece`` command line (port of ``bin/openapi2piece.js``).

Argument parsing reproduces Node's ``util.parseArgs`` with ``strict: true`` and
``allowPositionals: true``, including its error messages, so exit codes and
stderr match the original tool.  Only the program name differs.
"""

from __future__ import annotations

import errno
import re
import sys
from typing import Any, TextIO

from ._js import js_stringify, js_trim, to_utf8
from .generate import render_piece, write_piece
from .spec import load_spec

__all__ = ["main", "PROG", "HELP"]

PROG = "flowwrench openapi2piece"

HELP = f"""{PROG} 0.1.0 — deterministic OpenAPI 3.x → Activepieces scaffold

Usage:
  {PROG} generate --spec api.yaml --out new-directory --name api [options]

Options:
  --include-tags tag,tag   Select operations with any listed tag (repeatable)
  --include-ops id,id      Select operationIds or "GET /path" (repeatable)
  --max-actions N         Generate at most N supported actions in stable path/method order
  --dry-run               Validate and print report/file list without writing or networking
  --help                  Show this help

--out must not exist; its parent must already exist. No overwrite or remote $ref.
"""

_OPTIONS: dict[str, dict[str, Any]] = {
    "spec": {"type": "string"},
    "out": {"type": "string"},
    "name": {"type": "string"},
    "include-tags": {"type": "string", "multiple": True},
    "include-ops": {"type": "string", "multiple": True},
    "max-actions": {"type": "string"},
    "dry-run": {"type": "boolean", "default": False},
    "help": {"type": "boolean"},
}
_FS_CODES = ("ENOENT", "EACCES", "ENOTDIR", "EISDIR", "EEXIST")
_MAX_ACTIONS_RE = re.compile(r"[1-9][0-9]*")


class ParseArgsError(Exception):
    """``ERR_PARSE_ARGS_*`` (a TypeError in Node)."""


class CliError(Exception):
    pass


def _option_type(name: str) -> str | None:
    config = _OPTIONS.get(name)
    return config["type"] if config else None


def _tokenize(args: list[str]) -> list[dict[str, Any]]:
    """``argsToTokens`` from Node's parse_args (no option defines a ``short`` alias)."""
    tokens: list[dict[str, Any]] = []
    remaining = list(args)
    while remaining:
        arg = remaining.pop(0)
        next_arg = remaining[0] if remaining else None
        if arg == "--":
            tokens.append({"kind": "option-terminator"})
            tokens.extend({"kind": "positional", "value": value} for value in remaining)
            break
        if len(arg) == 2 and arg[0] == "-" and arg[1] != "-":
            # Lone short option; unknown shorts map to themselves.
            name = arg[1]
            value = None
            inline = None
            if _option_type(name) == "string" and next_arg is not None:
                value = remaining.pop(0)
                inline = False
            tokens.append({"kind": "option", "name": name, "raw": arg, "value": value, "inline": inline})
            continue
        if len(arg) > 2 and arg[0] == "-" and arg[1] != "-":
            if _option_type(arg[1]) != "string":
                # Short option group: expand -abc to -a -b -c.
                expanded = []
                for index in range(1, len(arg)):
                    short = arg[index]
                    if _option_type(short) != "string" or index == len(arg) - 1:
                        expanded.append(f"-{short}")
                    else:
                        expanded.append(f"-{arg[index:]}")
                        break
                remaining[:0] = expanded
                continue
            tokens.append({"kind": "option", "name": arg[1], "raw": f"-{arg[1]}", "value": arg[2:], "inline": True})
            continue
        if len(arg) > 2 and arg.startswith("--") and "=" not in arg[3:]:
            name = arg[2:]
            value = None
            inline = None
            if _option_type(name) == "string" and next_arg is not None:
                value = remaining.pop(0)
                inline = False
            tokens.append({"kind": "option", "name": name, "raw": arg, "value": value, "inline": inline})
            continue
        if len(arg) > 2 and arg.startswith("--"):
            equals = arg.index("=")
            name = arg[2:equals]
            tokens.append({"kind": "option", "name": name, "raw": f"--{name}", "value": arg[equals + 1 :], "inline": True})
            continue
        tokens.append({"kind": "positional", "value": arg})
    return tokens


def parse_args(args: list[str]) -> tuple[dict[str, Any], list[str]]:
    """``util.parseArgs({ options, allowPositionals: true, strict: true })``."""
    values: dict[str, Any] = {}
    positionals: list[str] = []
    for token in _tokenize(args):
        if token["kind"] == "positional":
            positionals.append(token["value"])
            continue
        if token["kind"] != "option":
            continue
        name, raw, value = token["name"], token["raw"], token["value"]
        if name not in _OPTIONS:
            raise ParseArgsError(
                f"Unknown option '{raw}'. To specify a positional argument starting with a '-', "
                f"place it at the end of the command after '--', as in '-- {js_stringify(raw)}"
            )
        kind = _OPTIONS[name]["type"]
        if kind == "string" and not isinstance(value, str):
            raise ParseArgsError(f"Option '--{name} <value>' argument missing")
        if kind == "boolean" and value is not None:
            raise ParseArgsError(f"Option '--{name}' does not take an argument")
        if not token["inline"] and isinstance(value, str) and len(value) > 1 and value[0] == "-":
            example = f"'{raw}=-XYZ'" if raw.startswith("--") else f"'--{name}=-XYZ' or '{raw}-XYZ'"
            raise ParseArgsError(
                f"Option '{raw}' argument is ambiguous.\n"
                f"Did you forget to specify the option argument for '{raw}'?\n"
                f"To specify an option argument starting with a dash use {example}."
            )
        new_value: Any = True if value is None else value
        if _OPTIONS[name].get("multiple"):
            values.setdefault(name, []).append(new_value)
        else:
            values[name] = new_value
    for name, config in _OPTIONS.items():
        if "default" in config and name not in values:
            values[name] = config["default"]
    return values, positionals


def _write(stream: TextIO, text: str) -> None:
    """Write UTF-8 like Node's process streams, whatever the locale encoding is."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(to_utf8(text).decode("utf-8"))
        return
    stream.flush()
    buffer.write(to_utf8(text))
    buffer.flush()


def _filesystem_code(error: BaseException) -> str | None:
    if isinstance(error, OSError) and error.errno is not None:
        code = errno.errorcode.get(error.errno)
        if code in _FS_CODES:
            return code
    return None


def _message(error: BaseException) -> str:
    if isinstance(error, OSError) and error.errno is not None and error.strerror:
        # Node formats other filesystem errors as "<CODE>: <description>, <syscall> '<path>'".
        code = errno.errorcode.get(error.errno, "E?")
        suffix = f", '{error.filename}'" if error.filename is not None else ""
        return f"{code}: {error.strerror}{suffix}"
    return str(error)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        values, positionals = parse_args(args)
        if values.get("help"):
            _write(sys.stdout, HELP)
            return 0
        if len(positionals) != 1 or positionals[0] != "generate":
            raise CliError("Expected generate command; use --help")
        for key in ("spec", "out", "name"):
            if not values.get(key):
                raise CliError(f"Missing --{key}")
        max_actions = values.get("max-actions")
        if max_actions is not None and not _MAX_ACTIONS_RE.fullmatch(max_actions):
            raise CliError("--max-actions must be a positive integer")

        def listed(key: str) -> list[str]:
            return [js_trim(part) for value in values.get(key, []) for part in value.split(",")]

        spec = load_spec(values["spec"])
        files, model = render_piece(
            spec,
            name=values["name"],
            include_tags=listed("include-tags"),
            include_ops=listed("include-ops"),
            max_actions=None if max_actions is None else int(max_actions),
        )
        write_piece(files, values["out"], dry_run=values["dry-run"])
        report = {"dryRun": values["dry-run"], "files": list(files), **model["report"]}
        _write(sys.stdout, js_stringify(report, 2) + "\n")
        return 0
    except Exception as error:  # noqa: BLE001 - every failure becomes exit code 1 with a message.
        # Never echo raw input documents or parser context: specs can contain credentials.
        code = _filesystem_code(error)
        if code is not None:
            message = f"Filesystem error ({code}); check spec/output paths and permissions"
        else:
            message = _message(error)
        _write(sys.stderr, f"{PROG}: {message}\n")
        report = getattr(error, "report", None)
        if report:
            _write(sys.stderr, js_stringify(report, 2) + "\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
