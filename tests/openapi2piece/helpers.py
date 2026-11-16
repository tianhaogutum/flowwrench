"""Shared helpers for the openapi2piece tests (port of ``test/helpers.js`` plus Node tooling)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SNAPSHOTS = HERE / "snapshots"
NODE_DIR = HERE / "node"
PROJECT_ROOT = HERE.parents[1]
SRC = PROJECT_ROOT / "src"


def base_spec(overrides: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """``baseSpec(overrides)``: overridden keys keep their position, like an object spread."""
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1"},
        "servers": [{"url": "https://api.example.test/v1"}],
        "paths": {"/hello": {"get": {"operationId": "hello", "responses": {"200": {"description": "OK"}}}}},
    }
    spec.update(overrides or {})
    spec.update(kwargs)
    return spec


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def _python_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env


def run_cli(args: list[str], cwd: Path = HERE) -> subprocess.CompletedProcess[str]:
    """Run ``flowwrench openapi2piece`` in a real subprocess (like ``spawnSync`` of the bin script)."""
    return subprocess.run(
        [sys.executable, "-m", "flowwrench.openapi2piece", *args],
        cwd=cwd,
        env=_python_env(),
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


def log_command(command: list[str], result: subprocess.CompletedProcess[str]) -> None:
    print(json.dumps({"command": command, "exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}))


# --- Node toolchain for the TypeScript/runtime halves --------------------------------------


@dataclass(frozen=True)
class NodeToolchain:
    node: str
    npx: str
    node_modules: Path


def find_node_toolchain() -> NodeToolchain | None:
    """``npx tsc`` plus a node_modules with the generated pieces' pinned packages.

    The packages (typescript 5.9.3, @activepieces/pieces-framework 0.32.0,
    @activepieces/pieces-common 0.12.5, axios 1.20.0, @types/node 22.19.0) are
    taken from ``$OPENAPI2PIECE_NODE_MODULES`` or ``<project>/node_modules``.
    """
    npx = shutil.which("npx")
    node = shutil.which("node")
    if not npx or not node:
        return None
    configured = os.environ.get("OPENAPI2PIECE_NODE_MODULES")
    candidate = Path(configured) if configured else PROJECT_ROOT / "node_modules"
    required = [candidate / "typescript" / "bin" / "tsc", candidate / "@activepieces" / "pieces-framework" / "package.json"]
    if not all(path.is_file() for path in required):
        return None
    return NodeToolchain(node=node, npx=npx, node_modules=candidate.resolve())


def require_node_toolchain() -> NodeToolchain:
    toolchain = find_node_toolchain()
    if toolchain is None:
        pytest.skip(
            "TypeScript/runtime checks need `npx tsc` and the pinned Activepieces packages "
            "(set OPENAPI2PIECE_NODE_MODULES to their node_modules directory)"
        )
    return toolchain


def link_node_modules(directory: Path, toolchain: NodeToolchain) -> None:
    """Make ``directory`` resolve the pinned packages, as the repository root did for the original."""
    os.symlink(toolchain.node_modules, directory / "node_modules", target_is_directory=True)


def _node_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"npm_config_offline": "true", "npm_config_update_notifier": "false", "npm_config_fund": "false"})
    env.update(extra or {})
    return env


def run_tsc(toolchain: NodeToolchain, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """``npx tsc <args>`` from a directory whose node_modules holds the pinned packages."""
    command = [toolchain.npx, "tsc", *args]
    result = subprocess.run(command, cwd=cwd, env=_node_env(), capture_output=True, encoding="utf-8", timeout=600)
    log_command(command, result)
    return result


def run_node(
    toolchain: NodeToolchain, script: Path, *, cwd: Path, env: dict[str, str], timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    bridge_env = {
        "OPENAPI2PIECE_PYTHON": sys.executable,
        "OPENAPI2PIECE_BRIDGE": str(NODE_DIR / "bridge.py"),
        "OPENAPI2PIECE_SRC": str(SRC),
        "OPENAPI2PIECE_NPX": toolchain.npx,
    }
    command = [toolchain.node, str(script)]
    result = subprocess.run(
        command, cwd=cwd, env=_node_env({**bridge_env, **env}), capture_output=True, encoding="utf-8", timeout=timeout
    )
    log_command(command, result)
    return result


# --- shared steps of the *-package tests -----------------------------------------------------


def generate_example(spec: str | Path, out: Path, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI on a fixture (the original ran the npm-installed bin; here a real subprocess)."""
    args = ["generate", "--spec", str(FIXTURES / spec), "--out", str(out), "--name", name, *extra]
    result = run_cli(args)
    log_command(["flowwrench", "openapi2piece", *args], result)
    return result


def assert_missing(path: Path) -> None:
    """``await assert.rejects(stat(path), { code: 'ENOENT' })``."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return
    raise AssertionError(f"{path} must not exist")


def compile_and_run_driver(temp: Path, out: Path, driver: str) -> None:
    """``tsc --noEmit`` then ``tsc`` on the generated piece, then the original Node assertions."""
    toolchain = require_node_toolchain()
    link_node_modules(temp, toolchain)
    for extra in (["--noEmit"], []):
        compiled = run_tsc(toolchain, [*extra, "-p", str(out / "tsconfig.json")], cwd=temp)
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = run_node(toolchain, NODE_DIR / driver, cwd=HERE, env={"OPENAPI2PIECE_OUT": str(out)})
    assert executed.returncode == 0, executed.stdout + executed.stderr
