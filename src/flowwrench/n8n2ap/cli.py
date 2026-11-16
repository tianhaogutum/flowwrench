"""``flowwrench n8n2ap`` command line (port of ``bin/n8n2ap.js``).

Exit codes: 0 conversion succeeded; 1 input/usage/I/O error; 2 strict review needed.
Results go to stdout, the single error line to stderr, both UTF-8 like Node.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from typing import Any, TextIO

from .connections import UNSET
from .converter import ConversionResult, convert, report_markdown
from .input import LIMITS
from .jscompat import (
    JSONSyntaxError,
    OBJECT_PROTOTYPE_KEYS,
    js_string,
    parse_json,
    stringify,
    utf8,
    well_formed,
)

PROG = "flowwrench n8n2ap"
VERSION = "0.1.0"

HELP = f"""{PROG} {VERSION} — offline n8n → Activepieces converter
Usage: {PROG} convert INPUT -o OUTPUT.json [--report REPORT.json|REPORT.md]
       [--trigger NAME_OR_ID] [--strict] [--dry-run] [--force]
       [--output-root DIRECTORY] [--connection-map FILE.json]

--strict       Write artifacts, then exit 2 if migration needs review/configuration.
--dry-run      Parse, convert and validate; print JSON report; write nothing.
--force        Replace existing regular output files (never input or symlinks).
--connection-map  Bind exact source credentials to unresolved named placeholders.
--output-root  Optional boundary: all output paths must be inside this directory.
Default report: OUTPUT.json.report.json. No workflow steps or network calls run.
Exit codes: 0 conversion succeeded; 1 input/usage/I/O error; 2 strict review needed.
"""

_VALUES = {"-o": "output", "--output": "output", "--report": "report", "--trigger": "trigger",
           "--output-root": "root", "--connection-map": "connection_map"}
_FLAGS = {"--strict": "strict", "--dry-run": "dry_run", "--force": "force"}
_REPORT_SUFFIX = re.compile(r"\.(json|md)\Z", re.IGNORECASE | re.ASCII)
_MARKDOWN_SUFFIX = re.compile(r"\.md\Z", re.IGNORECASE | re.ASCII)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# libuv's uv_strerror() texts, which Node puts into fs error messages.
_UV_MESSAGES = {
    errno.ENOENT: "no such file or directory", errno.EEXIST: "file already exists",
    errno.EACCES: "permission denied", errno.EPERM: "operation not permitted",
    errno.EISDIR: "illegal operation on a directory", errno.ENOTDIR: "not a directory",
    errno.ELOOP: "too many symbolic links encountered", errno.ENAMETOOLONG: "name too long",
    errno.ENOSPC: "no space left on device", errno.EROFS: "read-only file system",
    errno.EXDEV: "cross-device link not permitted", errno.EBUSY: "resource busy or locked",
    errno.EINVAL: "invalid argument", errno.EMFILE: "too many open files", errno.ENFILE: "file table overflow",
    errno.EIO: "i/o error", errno.ENOTEMPTY: "directory not empty", errno.EBADF: "bad file descriptor",
    errno.EFBIG: "file too large", errno.EAGAIN: "resource temporarily unavailable",
    errno.ETXTBSY: "text file is busy",
}


class CliError(Exception):
    """A user-facing failure; its message is printed after the program name."""


class FsError(CliError):
    """An OS error formatted the way Node formats ``fs`` errors."""

    def __init__(self, error: OSError, syscall: str, path: str | None = None, dest: str | None = None) -> None:
        self.errno = error.errno
        code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
        description = _UV_MESSAGES.get(error.errno or 0) or (error.strerror or "unknown error").lower()
        message = f"{code}: {description}, {syscall}"
        if path is not None:  # FileHandle read/write errors carry no path in Node.
            message += f" '{path}'" + (f" -> '{dest}'" if dest is not None else "")
        super().__init__(message)


@dataclass
class Options:
    input: str = ""
    output: str | None = None
    report: str | None = None
    trigger: str | None = None
    root: str | None = None
    connection_map: str | None = None
    strict: bool = False
    dry_run: bool = False
    force: bool = False
    help: bool = False
    version: bool = False


def parse_args(argv: list[str]) -> Options:
    if len(argv) == 1 and argv[0] in ("--help", "-h", "--version"):
        return Options(help=argv[0] != "--version", version=argv[0] == "--version")
    if not argv or argv[0] != "convert" or len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
        raise CliError("Expected: convert INPUT -o OUTPUT.json (use --help)")
    out = Options(input=argv[1])
    seen: set[str] = set()
    i = 2
    while i < len(argv):
        arg = argv[i]
        takes_value = arg in _VALUES or arg in OBJECT_PROTOTYPE_KEYS
        # Node looks options up on object literals, so inherited Object.prototype
        # member names ("constructor", "toString", ...) behave as ignored value options.
        key = _VALUES.get(arg) or _FLAGS.get(arg) or (f"<prototype {arg}>" if arg in OBJECT_PROTOTYPE_KEYS else None)
        if not key or key in seen:
            raise CliError("Unknown or duplicate argument (use --help)")
        seen.add(key)
        if takes_value:
            if i + 1 >= len(argv) or not argv[i + 1] or argv[i + 1].startswith("-"):
                raise CliError("Missing option value")
            i += 1
            if arg in _VALUES:
                setattr(out, key, argv[i])
        else:
            setattr(out, key, True)
        i += 1
    if not out.output and not out.dry_run:
        raise CliError("-o/--output is required unless --dry-run")
    if out.output and not out.report:
        out.report = out.output + ".report.json"
    if out.report and not _REPORT_SUFFIX.search(out.report):
        raise CliError("--report must end in .json or .md")
    return out


# --------------------------------------------------------------------------- file system


def _resolve(*paths: str) -> str:
    """Node ``path.resolve`` (POSIX): purely lexical, no trailing slash."""
    result = os.path.normpath(os.path.join(os.getcwd(), *paths))
    if result.startswith("//"):
        result = "/" + result.lstrip("/")
    return result


def _lstat(path: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise FsError(error, "lstat", path) from None


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        raise FsError(error, "realpath", path) from None


def _open(path: str, flags: int, mode: int = 0o666) -> int:
    try:
        return os.open(path, flags | _CLOEXEC, mode)
    except OSError as error:
        raise FsError(error, "open", path) from None


def _contained(root: str, target: str) -> bool:
    rel = "" if root == target else os.path.relpath(target, root)
    return rel != "" and rel != ".." and not rel.startswith("../") and not os.path.isabs(rel)


def _inspect_path(path: str) -> None:
    cursor = os.sep if os.path.isabs(path) else ""
    for part in [p for p in path[len(cursor):].split("/") if p]:
        cursor = _resolve(cursor, part)
        try:
            info = _lstat(cursor)
        except FsError as error:
            if error.errno == errno.ENOENT:
                continue
            raise
        if stat.S_ISLNK(info.st_mode):
            raise CliError("Symlink in output path is refused")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def write_artifacts(opts: Options, result: ConversionResult) -> None:
    assert opts.output is not None and opts.report is not None
    inputs = [(_resolve(path), _realpath(path)) for path in (opts.input, opts.connection_map) if path]
    targets = [_resolve(opts.output), _resolve(opts.report)]
    if targets[0] == targets[1]:
        raise CliError("Flow and report destinations must differ")
    root = _resolve(opts.root) if opts.root else None
    for target in targets:
        if any(target == real or target == path for path, real in inputs):
            raise CliError("Refusing to overwrite input workflow or connection map")
        if root is not None and not _contained(root, target):
            raise CliError("Output path escapes --output-root")
        _inspect_path(target)
        try:
            info = _lstat(target)
            if not stat.S_ISREG(info.st_mode):
                raise CliError("Output destination is not a regular file")
            if not opts.force:
                raise CliError("Output exists; use --force to replace")
            for _, real in inputs:
                source = _lstat(real)
                if source.st_ino == info.st_ino and source.st_dev == info.st_dev:
                    raise CliError("Refusing to overwrite input via hard link")
        except FsError as error:
            if error.errno != errno.ENOENT:
                raise
    report_text = (report_markdown(result["report"]) if _MARKDOWN_SUFFIX.search(opts.report)
                   else stringify(result["report"], 2) + "\n")
    payloads = [stringify(result["template"], 2) + "\n", report_text]
    # Exclusive creates protect final paths. Force uses same-directory temporary files + atomic rename.
    written: list[str] = []
    temps: list[str] = []
    try:
        for target, payload in zip(targets, payloads):
            directory = os.path.dirname(target)
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as error:
                raise FsError(error, "mkdir", directory) from None
            _inspect_path(target)
            if opts.force:
                dest = _resolve(directory, f".{os.path.basename(target)}.{uuid.uuid4()}.tmp")
            else:
                dest = target
            fd = _open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
            (temps if opts.force else written).append(dest)
            try:
                _write_all(fd, utf8(payload))
            except OSError as error:
                raise FsError(error, "write") from None
            finally:
                os.close(fd)
        if opts.force:
            for temp, target in zip(temps, targets):
                try:
                    os.replace(temp, target)
                except OSError as error:
                    raise FsError(error, "rename", temp, target) from None
    except BaseException:
        for file in [*temps, *written]:
            try:
                os.unlink(file)
            except OSError:
                pass
        raise


def read_json_input(path: str, label: str = "Input") -> Any:
    fd = _open(path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CliError(f"{label} must be a regular JSON file")
        if info.st_size > LIMITS.bytes:
            raise CliError(f"{label} exceeds the 5 MiB limit")
        # Bounded read also handles a file growing after fstat().
        buffer = bytearray()
        limit = LIMITS.bytes + 1
        while len(buffer) < limit:
            try:
                chunk = os.read(fd, limit - len(buffer))
            except OSError as error:
                raise FsError(error, "read") from None
            if not chunk:
                break
            buffer += chunk
        if len(buffer) > LIMITS.bytes:
            raise CliError(f"{label} exceeds the 5 MiB limit")
        raw = bytes(buffer).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    try:
        return parse_json(raw)
    except JSONSyntaxError:
        raise CliError(f"Invalid JSON {label.lower()} (contents withheld)") from None


# --------------------------------------------------------------------------- output


def _emit(stream: TextIO, text: str) -> None:
    """``console.log``/``console.error``: one line, always UTF-8."""
    data = text + "\n"
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(utf8(data))
        buffer.flush()
    else:
        stream.write(well_formed(data))
        stream.flush()


def _message(error: BaseException) -> str:
    if isinstance(error, OSError) and not isinstance(error, CliError):
        code = errno.errorcode.get(error.errno or 0)
        description = _UV_MESSAGES.get(error.errno or 0) or (error.strerror or str(error)).lower()
        if code:
            suffix = f", '{error.filename}'" if error.filename is not None else ""
            return f"{code}: {description}{suffix}"
    return str(error)


def _run(argv: list[str]) -> int:
    opts = parse_args(argv)
    if opts.help or opts.version:
        _emit(sys.stdout, HELP if opts.help else VERSION)
        return 0
    workflow = read_json_input(opts.input)
    connection_map = read_json_input(opts.connection_map, "Connection map") if opts.connection_map else UNSET
    result = convert(workflow, trigger=opts.trigger, connection_map=connection_map)
    report = result["report"]
    if opts.dry_run:
        _emit(sys.stdout, stringify(report, 2))
    else:
        write_artifacts(opts, result)
        counts = report["counts"]
        _emit(sys.stdout, f"Converted {js_string(counts['converted'])}/{js_string(report['total'])} nodes "
                          f"({js_string(report['coveragePercent'])}%); partial {js_string(counts['partial'])}, "
                          f"unsupported {js_string(counts['unsupported'])}.")
        placeholders = report.get("connectionPlaceholders")
        if placeholders:
            unique = len({binding["placeholder"] for binding in placeholders})
            _emit(sys.stdout, f"Connection placeholders: {unique} unresolved; template remains invalid.")
        assert opts.output is not None and opts.report is not None
        _emit(sys.stdout, f"Flow: {_resolve(opts.output)}\nReport: {_resolve(opts.report)}")
    return 2 if opts.strict and not report["complete"] else 0


def main(argv: list[str] | None = None) -> int:
    """Run ``flowwrench n8n2ap``; ``argv`` excludes the program and subcommand names."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except Exception as error:  # Node prints every rejection the same way.
        # OS errors include user-selected paths, never echo input values or JSON parse excerpts.
        _emit(sys.stderr, f"{PROG}: {_message(error)}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
