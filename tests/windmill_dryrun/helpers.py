"""Shared helpers for the windmill-dryrun tests (ports of the Node test utilities)."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowwrench.windmill_dryrun.cli import main

TEST_ROOT = Path(__file__).resolve().parent
FIXTURES = TEST_ROOT / "fixtures"
SRC = TEST_ROOT.parent.parent / "src"


def fixture(relative: str) -> str:
    """Absolute path of a file below tests/windmill_dryrun/fixtures (keeps a trailing slash)."""
    path = str(FIXTURES / relative)
    return path + "/" if relative.endswith("/") else path


def js(value: Any) -> str:
    """``JSON.stringify(value)`` for test values (no unsafe integers or -0)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def sha(value: Any) -> str:
    """``createHash('sha256').update(JSON.stringify(value)).digest('hex')``."""
    return hashlib.sha256(js(value).encode("utf-8")).hexdigest()


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str | Path, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return str(path)


def write_json(path: str | Path, value: Any) -> str:
    return write_text(path, js(value))


def clone(value: Any) -> Any:
    """``structuredClone``."""
    return copy.deepcopy(value)


@dataclass
class CliResult:
    status: int
    stdout: str
    stderr: str


def cli(*args: Any, cwd: str | Path) -> CliResult:
    """Run ``flowwrench windmill-dryrun`` in-process with ``cwd`` as working directory."""
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            status = main([str(arg) for arg in args])
    finally:
        os.chdir(previous)
    return CliResult(status, out.getvalue(), err.getvalue())


def subprocess_command(*args: Any) -> list[str]:
    return [sys.executable, "-m", "flowwrench.windmill_dryrun", *(str(arg) for arg in args)]


def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("NODE_OPTIONS", None)
    return env


class Files:
    """Numbered JSON files in a directory (the Node tests' ``write`` helpers)."""

    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)
        self.sequence = 0

    def write(self, value: Any, base: str | Path | None = None, prefix: str = "input") -> str:
        self.sequence += 1
        return write_json(Path(base or self.base) / f"{prefix}-{self.sequence}.json", value)

    def case_file(self, value: dict[str, Any], base: str | Path | None = None) -> str:
        """``write({...value, flow: write(value.flow, base)}, base)``."""
        return self.write({**value, "flow": self.write(value["flow"], base)}, base)
