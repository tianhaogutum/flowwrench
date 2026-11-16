"""Port of test/array-package.test.js.

The npm pack/offline-install steps have no Python equivalent; the CLI runs as a
real subprocess instead. The TypeScript and runtime half (`tsc --noEmit`, `tsc`,
real framework metadata and local HTTP) runs the original JavaScript assertions
in node/array_package.mjs and is skipped unless `npx tsc` is available.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .helpers import assert_missing, compile_and_run_driver, generate_example


def test_offline_installed_cli_uniqueitems_array_wrapper_example_compiles_validates_metadata_and_executes_local_http(tmp_path: Path) -> None:
    temp = tmp_path.resolve()
    consumer = temp / "consumer"
    consumer.mkdir()
    out = consumer / "arrays"
    generated = generate_example("oneof-unique-arrays.json", out, "arrays")
    assert generated.returncode == 0, generated.stderr
    report = json.loads(generated.stdout)
    assert [a["name"] for a in report["generated"]] == ["allowduplicates", "branchunique", "jsonvalues", "nested", "overlap", "sendunique"]
    assert len(report["skipped"]) == 1
    assert re.search(r"oneOf.*sibling minLength", report["skipped"][0]["reason"])
    assert json.loads((out / "report.json").read_text(encoding="utf-8"))["skipped"] == report["skipped"]
    rejected_out = consumer / "unsupported"
    rejected = generate_example("oneof-unique-arrays.json", rejected_out, "unsupported", "--include-ops", "unsupported")
    assert rejected.returncode == 1
    assert re.search(r"oneOf.*sibling minLength", rejected.stderr)
    assert_missing(rejected_out)

    # Actual CLI output resolves the pinned packages (skipped without `npx tsc`).
    compile_and_run_driver(temp, out, "array_package.mjs")
