"""Port of test/package.test.js.

``npm pack`` + offline ``npm install`` have no Python equivalent; the port checks
the packaged resources the CLI needs (``templates/runtime.ts`` and friends, no
tests or node_modules inside the package) and runs the CLI as a real subprocess.
The TypeScript/runtime half compiles the CLI output with ``npx tsc`` in the
original order and then runs the original JavaScript assertions from
node/package.mjs; it is skipped unless ``npx tsc`` is available.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from .helpers import (
    HERE,
    NODE_DIR,
    assert_missing,
    fixture_json,
    generate_example,
    link_node_modules,
    log_command,
    require_node_toolchain,
    run_cli,
    run_node,
    run_tsc,
)


def _walk(node: Traversable, prefix: str = "") -> list[str]:
    paths = []
    for child in node.iterdir():
        path = f"{prefix}{child.name}"
        paths.append(path)
        if child.is_dir():
            paths.extend(_walk(child, path + "/"))
    return paths


def test_packed_cli_installs_offline_with_only_runtime_dependencies_and_executes_real_generation(tmp_path: Path) -> None:
    temp = tmp_path.resolve()
    package = resources.files("flowwrench.openapi2piece")
    packaged = _walk(package)
    for template in ("templates/runtime.ts", "templates/LICENSE", "templates/THIRD_PARTY.md", "templates/README.md.in"):
        assert template in packaged, template
    assert all(not p.startswith("tests/") and not p.startswith("node_modules/") for p in packaged)
    consumer = temp / "consumer"
    consumer.mkdir()

    run = generate_example("petstore.yaml", consumer / "generated", "installed")
    assert run.returncode == 0, run.stderr
    assert len(json.loads(run.stdout)["generated"]) == 3
    alternatives = generate_example("request-alternatives.json", consumer / "alternatives", "alternatives")
    assert alternatives.returncode == 0, alternatives.stderr
    report = json.loads(alternatives.stdout)
    assert len(report["generated"]) == 1
    assert report["generated"][0]["name"] == "notify"
    assert re.search(r"incompatible framework properties: object, string", report["skipped"][0]["reason"])
    rejected = consumer / "rejected"
    failure = generate_example("request-alternatives.json", rejected, "rejected", "--include-ops", "unsupported")
    assert failure.returncode == 1
    assert re.search(r"incompatible framework properties: object, string", failure.stderr)
    assert_missing(rejected)

    auth_out = consumer / "authchoices"
    auth_run = generate_example("auth-alternatives.json", auth_out, "authchoices")
    assert auth_run.returncode == 0, auth_run.stderr
    auth_report = json.loads(auth_run.stdout)
    assert [a["name"] for a in auth_report["generated"]] == ["optional", "compound", "items", "keyonly", "public"]
    assert len(auth_report["skipped"]) == 3
    assert json.loads((auth_out / "report.json").read_text(encoding="utf-8"))["skipped"] == auth_report["skipped"]
    no_auth_out = consumer / "unsupported-auth"
    no_auth = generate_example("auth-alternatives.json", no_auth_out, "unsupported", "--include-ops", "unsupported")
    assert no_auth.returncode == 1
    assert re.search(r"Unsupported security scheme", no_auth.stderr)
    assert_missing(no_auth_out)

    colliding_out = consumer / "colliding-auth"
    colliding = generate_example("auth-alternatives.json", colliding_out, "colliding", "--include-ops", "colliding")
    assert colliding.returncode == 1
    assert re.search(r"same credential location", colliding.stderr)
    assert_missing(colliding_out)

    constrained_out = consumer / "constrained"
    constrained_run = generate_example("oneof-constraints.json", constrained_out, "constrained")
    assert constrained_run.returncode == 0, constrained_run.stderr
    constrained_report = json.loads(constrained_run.stdout)
    assert [a["name"] for a in constrained_report["generated"]] == ["notify"]
    assert len(constrained_report["skipped"]) == 1
    assert re.search(r"unsupported schema keyword minProperties", constrained_report["skipped"][0]["reason"])
    no_constraints_out = consumer / "unsupported-constraints"
    no_constraints = generate_example("oneof-constraints.json", no_constraints_out, "unsupported", "--include-ops", "unsupported")
    assert no_constraints.returncode == 1
    assert re.search(r"unsupported schema keyword minProperties", no_constraints.stderr)
    assert_missing(no_constraints_out)

    strings_out = consumer / "strings"
    strings_run = generate_example("oneof-strings.json", strings_out, "strings")
    assert strings_run.returncode == 0, strings_run.stderr
    strings_report = json.loads(strings_run.stdout)
    assert [a["name"] for a in strings_report["generated"]] == ["overlap", "sendstring"]
    assert len(strings_report["skipped"]) == 1
    assert re.search(r"oneOf.*sibling minimum", strings_report["skipped"][0]["reason"])
    no_strings_out = consumer / "unsupported-strings"
    no_strings = generate_example("oneof-strings.json", no_strings_out, "unsupported", "--include-ops", "unsupported")
    assert no_strings.returncode == 1
    assert re.search(r"oneOf.*sibling minimum", no_strings.stderr)
    assert_missing(no_strings_out)

    widgets_out = consumer / "widgets"
    widget_run = generate_example("request-projection.json", widgets_out, "widgets")
    assert widget_run.returncode == 0, widget_run.stderr
    widget_report = json.loads(widget_run.stdout)
    assert [a["name"] for a in widget_report["generated"]] == ["createwidget", "getwidget"]
    assert widget_report["skipped"] == []
    newer_spec = fixture_json("request-projection.json")
    newer_spec["openapi"] = "3.1.0"
    newer_path = consumer / "widgets31.json"
    newer_path.write_text(json.dumps(newer_spec), encoding="utf-8")
    no_widgets_out = consumer / "unsupported-widgets31"
    no_widgets_args = ["generate", "--spec", str(newer_path), "--out", str(no_widgets_out), "--name", "widgets31", "--include-ops", "createWidget"]
    no_widgets = run_cli(no_widgets_args)
    log_command(["flowwrench", "openapi2piece", *no_widgets_args], no_widgets)
    assert no_widgets.returncode == 1
    assert re.search(r"readOnly request schemas are unsupported", no_widgets.stderr)
    assert_missing(no_widgets_out)

    # The CLI's actual output compiles and executes against the pinned packages;
    # no registry is needed (skipped without `npx tsc`).
    toolchain = require_node_toolchain()
    link_node_modules(temp, toolchain)
    compiled = run_tsc(toolchain, ["-p", str(auth_out / "tsconfig.json")], cwd=temp)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    for out in (constrained_out, strings_out, widgets_out):
        for extra in (["--noEmit"], []):
            compiled = run_tsc(toolchain, [*extra, "-p", str(out / "tsconfig.json")], cwd=temp)
            assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = run_node(
        toolchain,
        NODE_DIR / "package.mjs",
        cwd=HERE,
        env={
            "OPENAPI2PIECE_AUTH_OUT": str(auth_out),
            "OPENAPI2PIECE_CONSTRAINED_OUT": str(constrained_out),
            "OPENAPI2PIECE_STRINGS_OUT": str(strings_out),
            "OPENAPI2PIECE_WIDGETS_OUT": str(widgets_out),
        },
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
