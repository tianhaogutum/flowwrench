"""Shared fixtures for the ``flowwrench.nodered_merge`` port of the nrflow-merge tests.

* ``../../src`` is put on ``sys.path`` so the library imports without installation.
* CLI tests run ``python -P -m flowwrench.nodered_merge`` (the module form of
  ``flowwrench nodered-merge``).
* Real Git merge tests need ``flowwrench`` on ``PATH`` because the installed
  merge driver is ``flowwrench nodered-merge git-driver "%O" "%A" "%B"``; a tiny
  POSIX shell shim standing in for the console script is generated for that.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = HERE / "fixtures"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
TIMEOUT = 120

RunCli = Callable[..., subprocess.CompletedProcess[str]]


def write_shim(directory: Path) -> Path:
    """Create ``directory/flowwrench`` dispatching ``nodered-merge`` to this checkout."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "flowwrench"
    script.write_text(
        "#!/bin/sh\n"
        "# Test stand-in for the installed flowwrench console script.\n"
        'if [ "$1" != "nodered-merge" ]; then\n'
        "  echo \"flowwrench test shim: only 'nodered-merge' is supported\" >&2\n"
        "  exit 2\n"
        "fi\n"
        "shift\n"
        f"PYTHONPATH={shlex.quote(str(SRC))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f'exec {shlex.quote(sys.executable)} -P -m flowwrench.nodered_merge "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return directory


def with_path(env: dict[str, str], directory: Path) -> dict[str, str]:
    """Copy *env* with *directory* first on ``PATH``."""
    return {**env, "PATH": f"{directory}{os.pathsep}{env.get('PATH', os.defpath)}"}


@pytest.fixture(scope="session")
def cli_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_ENV)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    shim = write_shim(tmp_path_factory.mktemp("flowwrench-bin"))
    return with_path(env, shim)


@pytest.fixture
def make_shim(cli_env: dict[str, str]) -> Callable[[Path], tuple[list[str], dict[str, str]]]:
    """``make_shim(dir)`` -> (``[dir/flowwrench, 'nodered-merge']``, env with *dir* first on PATH)."""

    def make(directory: Path) -> tuple[list[str], dict[str, str]]:
        tool = write_shim(directory)
        return [str(tool / "flowwrench"), "nodered-merge"], with_path(cli_env, tool)

    return make


@pytest.fixture
def run_cli(cli_env: dict[str, str]) -> RunCli:
    """``run(args, cwd=...)``: run the CLI, returning text stdout/stderr and ``returncode``."""

    def run(
        args: Sequence[str | os.PathLike[str]],
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        executable: Sequence[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = list(executable) if executable else [sys.executable, "-P", "-m", "flowwrench.nodered_merge"]
        return subprocess.run(
            [*command, *map(str, args)],
            cwd=str(cwd or HERE),
            env=env or cli_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            check=False,
        )

    return run


@pytest.fixture
def git(cli_env: dict[str, str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """``git(cwd, *args, input=None)``: run Git without asserting success."""

    def run(
        cwd: str | os.PathLike[str],
        *args: str,
        input: str | bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        data = input.encode("utf-8") if isinstance(input, str) else input
        extra: dict[str, Any] = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), env=env or cli_env, capture_output=True, timeout=TIMEOUT, check=False, **extra
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
        )

    return run


@pytest.fixture
def git_ok(git: Callable[..., subprocess.CompletedProcess[str]]) -> Callable[..., str]:
    """``git_ok(cwd, *args, input=None)``: run Git, assert exit 0, return trimmed stdout."""

    def run(cwd: str | os.PathLike[str], *args: str, input: str | bytes | None = None, env: dict[str, str] | None = None) -> str:
        result = git(cwd, *args, input=input, env=env)
        assert result.returncode == 0, f"{' '.join(args)}\n{result.stderr}"
        return result.stdout.strip()

    return run


@pytest.fixture
def init_repo(git_ok: Callable[..., str]) -> Callable[[Path], None]:
    def init(directory: Path) -> None:
        git_ok(directory, "init", "-b", "main")
        git_ok(directory, "config", "user.name", "Offline Test")
        git_ok(directory, "config", "user.email", "test@example.invalid")
        git_ok(directory, "config", "commit.gpgsign", "false")
        git_ok(directory, "config", "core.hooksPath", "/dev/null")

    return init


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A private temporary directory (symlinks resolved, as Git reports real paths)."""
    return tmp_path.resolve()
