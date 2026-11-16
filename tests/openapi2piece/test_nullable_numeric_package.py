"""Port of test/nullable-numeric-package.test.js.

The npm pack/offline-install steps have no Python equivalent; the CLI runs as a
real subprocess instead. The TypeScript and runtime half (`tsc --noEmit`, `tsc`,
real framework metadata and local HTTP) runs the original JavaScript assertions
in node/nullable_numeric_package.mjs and is skipped unless `npx tsc` is available.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .helpers import assert_missing, compile_and_run_driver, generate_example


def test_offline_installed_cli_nullable_numeric_wrapper_example_compiles_validates_metadata_and_executes_local_http(tmp_path: Path) -> None:
    temp = tmp_path.resolve()
    consumer = temp / "consumer"
    consumer.mkdir()
    out = consumer / "numbers"
    generated = generate_example("oneof-nullable-numbers.json", out, "numbers")
    assert generated.returncode == 0, generated.stderr
    report = json.loads(generated.stdout)
    assert [a["name"] for a in report["generated"]] == ["composed", "enumexcludes", "fractional", "independent", "integer30", "integer31", "nested", "overlap", "send30", "send31", "zero", "zeroallowed"]
    assert len(report["skipped"]) == 2
    assert re.search(r"oneOf.*sibling minLength", next(s for s in report["skipped"] if s["operationId"] == "unsupported")["reason"])
    assert re.search(r"single type plus null", next(s for s in report["skipped"] if s["operationId"] == "invalidTypes")["reason"])
    assert json.loads((out / "report.json").read_text(encoding="utf-8"))["skipped"] == report["skipped"]
    rejected_out = consumer / "unsupported"
    rejected = generate_example("oneof-nullable-numbers.json", rejected_out, "unsupported", "--include-ops", "unsupported")
    assert rejected.returncode == 1
    assert re.search(r"oneOf.*sibling minLength", rejected.stderr)
    assert_missing(rejected_out)
    invalid_types_out = consumer / "invalid-types"
    invalid_types = generate_example("oneof-nullable-numbers.json", invalid_types_out, "invalid-types", "--include-ops", "invalidTypes")
    assert invalid_types.returncode == 1
    assert re.search(r"single type plus null", invalid_types.stderr)
    assert_missing(invalid_types_out)

    # Actual CLI output resolves the pinned packages (skipped without `npx tsc`).
    compile_and_run_driver(temp, out, "nullable_numeric_package.mjs")
