"""Test setup for flowwrench.windmill_dryrun.

* Puts ``../../src`` on ``sys.path`` so the package imports without install.
* Tests that evaluate JavaScript expressions (or run the CLI, which requires
  Node.js for run/test/replay) are marked ``needs_node`` and skipped when no
  ``node`` executable is on PATH.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "needs_node: evaluates JavaScript or runs run/test/replay; needs Node.js 20+ on PATH"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if shutil.which("node") is not None:
        return
    skip = pytest.mark.skip(reason="node not found on PATH: windmill-dryrun needs Node.js 20+ to evaluate JavaScript expressions")
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if item.get_closest_marker("needs_node") is not None and _HERE in path.parents:
            item.add_marker(skip)
