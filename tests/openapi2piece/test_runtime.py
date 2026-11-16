"""Port of test/runtime.test.js.

Every original case compiles the generated TypeScript with tsc and executes it
under Node against the real Activepieces framework, so the cases run in Node:
``node/runtime.test.mjs`` is the original file with its generator imports bridged
to this Python port (all generated pieces and normalized schemas come from
Python).  The Node script runs once per module; each original test is one
parametrized pytest case.  Skipped unless ``npx tsc`` and the pinned packages are
available (see helpers.find_node_toolchain).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from .helpers import HERE, NODE_DIR, link_node_modules, require_node_toolchain, run_node

DRIVER = NODE_DIR / "runtime.test.mjs"
TEST_NAMES = re.findall(r"^test\('((?:[^'\\]|\\.)*)',", DRIVER.read_text(encoding="utf-8"), re.MULTILINE)


@pytest.fixture(scope="module")
def runtime_results(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], str]:
    toolchain = require_node_toolchain()
    root = tmp_path_factory.mktemp("runtime").resolve()
    temp = root / "pieces"
    temp.mkdir()
    link_node_modules(temp, toolchain)
    results_path: Path = root / "results.json"
    completed = run_node(
        toolchain,
        DRIVER,
        cwd=HERE,
        env={"OPENAPI2PIECE_TEMP": str(temp), "OPENAPI2PIECE_RESULTS": str(results_path)},
        timeout=3600,
    )
    output = f"exit {completed.returncode}\nstdout:\n{completed.stdout[-8000:]}\nstderr:\n{completed.stderr[-8000:]}"
    if not results_path.exists():
        pytest.fail(f"runtime driver did not finish:\n{output}")
    return json.loads(results_path.read_text(encoding="utf-8")), output


def test_original_case_count() -> None:
    assert len(TEST_NAMES) == 65


@pytest.mark.parametrize("name", TEST_NAMES)
def test_runtime(name: str, runtime_results: tuple[dict[str, Any], str]) -> None:
    results, output = runtime_results
    result = results.get(name)
    assert result is not None, f"{name!r} did not run\n{output}"
    assert result["ok"], result.get("error")
