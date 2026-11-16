"""Output path checks and staged, atomic-per-file writes.

Port of ``lib/io.js`` (renamed so the module never shadows the stdlib ``io``).
"""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Sequence

from .flows import InputError
from .jsjson import to_utf8

__all__ = ["output_path", "same_file", "write_outputs"]


def output_path(file: str) -> str:
    """Resolve an output path; replacing a symlink would be surprising for an
    in-place formatter or Git driver."""
    target = os.path.abspath(file)
    parent = os.path.realpath(os.path.dirname(target), strict=True)
    result = os.path.join(parent, os.path.basename(target))
    try:
        info = os.lstat(result)
    except FileNotFoundError:
        return result
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise InputError(f"{file}: output must be a regular file, not a symlink")
    return result


def same_file(a: str, b: str) -> bool:
    if os.path.abspath(a) == os.path.abspath(b):
        return True
    try:
        if os.path.realpath(a, strict=True) == os.path.realpath(b, strict=True):
            return True
        x, y = os.stat(a), os.stat(b)
        return x.st_dev == y.st_dev and x.st_ino == y.st_ino
    except (OSError, ValueError):
        return False


def write_outputs(outputs: Sequence[tuple[str, str | bytes]]) -> None:
    """Stage every output before replacing any. No partial/truncated JSON files.

    Renames across multiple files are not a transaction if the OS fails mid-rename.
    """
    staged: list[tuple[str, str]] = []
    try:
        paths = [output_path(file) for file, _ in outputs]
        for i in range(len(paths)):
            for j in range(i):
                if same_file(paths[i], paths[j]):
                    raise InputError("output and conflict report must be different files")
        for (_, content), target in zip(outputs, paths):
            temp = os.path.join(os.path.dirname(target), f".nrflow-{uuid.uuid4()}.tmp")
            mode = 0o600
            try:
                mode = os.stat(target).st_mode & 0o777
            except OSError:
                pass
            staged.append((target, temp))
            data = content if isinstance(content, bytes) else to_utf8(content)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(temp, mode)
        for target, temp in staged:
            os.replace(temp, target)
    finally:
        for _, temp in staged:
            try:
                os.unlink(temp)
            except OSError:
                pass
