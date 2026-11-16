"""Bounded input parsing and confined, never-overwriting outputs (``lib/io.js``)."""

from __future__ import annotations

import errno
import os
import posixpath
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import yaml

from .errors import FormatError, error_message
from .jsvalues import UNDEFINED, is_finite_number, js_items, js_keys, parse_json, replace_lone_surrogates
from .yamlload import describe_error, load_yaml

MAX_BYTES = 1024 * 1024
_FORBIDDEN_KEYS = ("__proto__", "constructor", "prototype")
_EXCLUSIVE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

# libuv's messages, so diagnostics read like the Node.js tool's.
_UV_MESSAGES = {
    errno.EACCES: "permission denied",
    errno.EEXIST: "file already exists",
    errno.EINVAL: "invalid argument",
    errno.EISDIR: "illegal operation on a directory",
    errno.ELOOP: "too many symbolic links encountered",
    errno.EMLINK: "too many links",
    errno.ENAMETOOLONG: "name too long",
    errno.ENOENT: "no such file or directory",
    errno.ENOSPC: "no space left on device",
    errno.ENOTDIR: "not a directory",
    errno.EPERM: "operation not permitted",
    errno.EROFS: "read-only file system",
    errno.EXDEV: "cross-device link not permitted",
}


class _PlainError(Exception):
    """An already-formatted message (``new Error(message)`` in JavaScript)."""


def uv_error(error: OSError, syscall: str, path: str, dest: str | None = None) -> Exception:
    """Node.js-style ``CODE: message, syscall 'path'`` error for an OSError."""
    code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    text = _UV_MESSAGES.get(error.errno or 0) or (error.strerror or str(error)).lower()
    message = f"{code}: {text}, {syscall} '{path}'"
    if dest is not None:
        message += f" -> '{dest}'"
    return _PlainError(message)


_uv_error = uv_error


def resolve_path(*parts: str) -> str:
    """``path.resolve(...parts)`` on POSIX."""
    resolved = os.getcwd()
    for part in parts:
        if part:
            resolved = posixpath.join(resolved, part)
    resolved = posixpath.normpath(resolved)
    if resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    return resolved


def relative_path(start: str, target: str) -> str:
    """``path.relative(start, target)`` (empty string for the same path)."""
    start, target = resolve_path(start), resolve_path(target)
    return "" if start == target else posixpath.relpath(target, start)


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        # Node's JavaScript realpath reports the failing lstat.
        raise _uv_error(error, "lstat", path) from None


def json_value(
    value: Any,
    label: str = "value",
    depth: int = 0,
    seen: set[int] | None = None,
    max_depth: int = 64,
) -> None:
    """Reject non-JSON values, cycles, forbidden keys and excessive nesting."""
    if seen is None:
        seen = set()
    if depth > max_depth:
        raise FormatError(f"{label}: nesting exceeds {max_depth}")
    if value is None or isinstance(value, (str, bool)) or is_finite_number(value):
        return
    if not isinstance(value, (dict, list)):
        raise FormatError(f"{label}: only finite JSON values are supported")
    if id(value) in seen:
        raise FormatError(f"{label}: cyclic/shared YAML aliases are unsupported")
    seen.add(id(value))
    entries = js_items(value) if isinstance(value, dict) else enumerate(value)
    for key, item in entries:
        if key in _FORBIDDEN_KEYS:
            raise FormatError(f"{label}: forbidden key {key}")
        json_value(item, label, depth + 1, seen, max_depth)
    seen.discard(id(value))


class _DuplicateJsonKey(ValueError):
    pass


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateJsonKey(key)
        mapping[key] = value
    return mapping


def _parse_yaml(source: str) -> Any:
    # YAML 1.2 is a JSON superset: valid duplicate-free JSON parses identically,
    # so take the fast C JSON path first and fall back to the YAML loader.
    try:
        return parse_json(source, object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        pass
    try:
        return load_yaml(source)
    except yaml.MarkedYAMLError as error:
        raise _PlainError(describe_error(error)) from None
    except yaml.YAMLError as error:
        raise _PlainError(" ".join(str(error).split())) from None


def read_data(path: str, *, json: bool = False) -> Any:
    """Read a regular file of at most 1 MiB as JSON or strict YAML."""
    try:
        try:
            info = os.stat(path)
        except OSError as error:
            raise _uv_error(error, "stat", path) from None
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise _PlainError("expected a regular file of at most 1 MiB")
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as error:
            raise _uv_error(error, "open", path) from None
        source = raw.decode("utf-8", errors="replace")
        data = parse_json(source) if json else _parse_yaml(source)
        json_value(data, path)
        return data
    except Exception as error:  # noqa: BLE001 - every failure is invalid input
        raise FormatError(f"{path}: {error_message(error)}") from None


def _output_target(path: str, root: str) -> str:
    """Confine a requested output path beneath ``root`` (resolving parent links)."""
    base = _realpath(root)
    absolute = resolve_path(path)
    parent = _realpath(posixpath.dirname(absolute))
    rel = relative_path(base, parent)
    if rel == ".." or rel.startswith("../") or posixpath.isabs(rel):
        raise _PlainError("output escapes --output-dir")
    return resolve_path(parent, posixpath.basename(absolute))


def _write_fd(fd: int, content: str) -> None:
    """Write all of ``content`` as UTF-8 (tests may replace this hook)."""
    if not isinstance(content, str):
        raise TypeError(f'The "data" argument must be of type string. Received {type(content).__name__}')
    data = memoryview(utf8(content))
    while data:
        written = os.write(fd, data)
        data = data[written:]


def utf8(text: str) -> bytes:
    """UTF-8 bytes as Node writes them (a lone surrogate becomes U+FFFD)."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return replace_lone_surrogates(text).encode("utf-8")


def _open_exclusive(target: str) -> int:
    try:
        return os.open(target, _EXCLUSIVE_FLAGS, 0o600)
    except OSError as error:
        raise _uv_error(error, "open", target) from None


def write_output(path: str, content: str, root: str | None = None) -> None:
    """Create a new file (never overwrite, never follow a symlink leaf)."""
    try:
        target = _output_target(path, os.getcwd() if root is None else root)
        fd = _open_exclusive(target)
        try:
            _write_fd(fd, content)
        finally:
            os.close(fd)
    except Exception as error:  # noqa: BLE001
        raise FormatError(f"cannot write {path}: {error_message(error)}") from None


@dataclass
class _Checkpoint:
    target: str
    installed: os.stat_result | None = None


CheckpointWriter = Callable[[str, str], None]


def checkpoint_outputs(paths: Iterable[str | None], root: str | None = None) -> CheckpointWriter:
    """Preflight distinct new outputs; return an atomic writer for them.

    Only files installed by the returned writer may later be replaced; every
    update is a mode-0600 temporary file in the same directory followed by an
    exclusive link (first write) or an atomic rename (later writes).
    """
    base = os.getcwd() if root is None else root
    outputs: dict[str, _Checkpoint] = {}
    targets: set[str] = set()
    for path in paths:
        if not path:
            continue
        try:
            target = _output_target(path, base)
            if target in targets:
                raise _PlainError("output paths must be distinct")
            try:
                os.lstat(target)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise _uv_error(error, "lstat", target) from None
            else:
                raise _PlainError("output already exists")
            targets.add(target)
            outputs[path] = _Checkpoint(target)
        except Exception as error:  # noqa: BLE001
            raise FormatError(f"cannot write {path}: {error_message(error)}") from None

    def write(path: str, content: str) -> None:
        temporary: str | None = None
        try:
            try:
                output = outputs.get(path)
                if output is None:
                    raise _PlainError("output was not preflighted")
                candidate = posixpath.join(
                    posixpath.dirname(output.target), f".openflow-dryrun-{uuid.uuid4()}.tmp"
                )
                fd = _open_exclusive(candidate)
                temporary = candidate
                try:
                    _write_fd(fd, content)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                installed = os.lstat(temporary)
                if output.installed is not None:
                    try:
                        current = os.lstat(output.target)
                    except OSError as error:
                        raise _uv_error(error, "lstat", output.target) from None
                    if (current.st_dev, current.st_ino) != (output.installed.st_dev, output.installed.st_ino):
                        raise _PlainError("checkpoint output was replaced")
                    # A same-directory rename installs a whole file: an
                    # interruption cannot truncate the last checkpoint.
                    try:
                        os.rename(temporary, output.target)
                    except OSError as error:
                        raise _uv_error(error, "rename", temporary, output.target) from None
                    temporary = None
                else:
                    # Exclusive first installation, even if a file appeared
                    # after the preflight.
                    try:
                        os.link(temporary, output.target)
                    except OSError as error:
                        raise _uv_error(error, "link", temporary, output.target) from None
                output.installed = installed
            except Exception as error:  # noqa: BLE001
                raise FormatError(f"cannot write {path}: {error_message(error)}") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    return write


def is_object(value: Any) -> bool:
    return isinstance(value, dict)


def require_object(value: Any, label: str) -> None:
    """``object(x, label)``: a plain JSON object."""
    if not isinstance(value, dict):
        raise FormatError(f"{label}: expected object")


def require_keys(value: Any, allowed: Iterable[str], label: str) -> None:
    """``keys(x, allowed, label)``: an object with only the allowed fields."""
    require_object(value, label)
    permitted = set(allowed)
    for key in js_keys(value):
        if key not in permitted:
            raise FormatError(f"{label}: unsupported field '{key}'")


def get(mapping: Any, key: str) -> Any:
    """Property access returning UNDEFINED for absent keys or non-objects."""
    if isinstance(mapping, dict):
        return mapping.get(key, UNDEFINED)
    return UNDEFINED
