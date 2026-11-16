"""Port of test/cli.test.js."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from flowwrench.openapi2piece import SpecError, load_spec, parse_spec, render_piece, write_piece

from .helpers import base_spec, run_cli


@pytest.fixture
def temp(tmp_path: Path) -> Path:
    # Output ancestors must be real directories: resolve symlinked temp roots (e.g. macOS /var).
    return tmp_path.resolve()


def generate(out: Path, rest: list[str] | None = None):
    return run_cli(["generate", "--spec", "fixtures/petstore.yaml", "--out", str(out), "--name", "petstore", *(rest or [])])


def test_cli_dry_run_validates_and_prints_plan_without_any_filesystem_changes(temp: Path) -> None:
    before = sorted(os.listdir(temp))
    result = generate(temp / "dry", ["--dry-run", "--include-tags", "pets", "--include-ops", "getPet"])
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["dryRun"] is True
    assert len(report["generated"]) == 1
    assert "src/lib/actions/getpet.ts" in report["files"]
    assert sorted(os.listdir(temp)) == before


def test_cli_writes_real_scaffold_once_refuses_output_overwrite_even_during_dry_run(temp: Path) -> None:
    out = temp / "generated"
    result = generate(out)
    assert result.returncode == 0, result.stderr
    assert (out / "src/index.ts").is_file()
    assert len(json.loads((out / "report.json").read_text(encoding="utf-8"))["generated"]) == 3
    old = (out / "src/index.ts").read_text(encoding="utf-8")
    for opts in ([], ["--dry-run"]):
        repeated = generate(out, opts)
        assert repeated.returncode == 1
        assert re.search(r"already exists", repeated.stderr)
    assert (out / "src/index.ts").read_text(encoding="utf-8") == old


def test_cli_input_and_option_failures_are_nonzero_clear_and_never_create_output(temp: Path) -> None:
    for args, pattern in [
        ([], r"Expected generate"),
        (["generate"], r"Missing --spec"),
        (["generate", "--surprise"], r"Unknown option"),
        (["generate", "--spec", "absent.yaml", "--out", str(temp / "fail"), "--name", "sample"], r"ENOENT"),
    ]:
        result = run_cli(args)
        assert result.returncode == 1
        assert re.search(pattern, result.stderr)
    for opts, pattern in [
        (["--max-actions", "0"], r"positive integer"),
        (["--max-actions", "1.5"], r"positive integer"),
        (["--include-tags", "missing"], r"No operations"),
        (["--name", "../../escape"], r"slug"),
        (["--include-tags", ""], r"nonempty"),
    ]:
        result = generate(temp / "never", opts)
        assert result.returncode == 1
        assert re.search(pattern, result.stderr)
    assert run_cli(["--help"]).returncode == 0
    assert run_cli(["generate", "--help"]).returncode == 0


def test_real_json_input_swagger_unresolved_and_remote_refs_syntax_error_secret_redaction(temp: Path) -> None:
    samples = [
        ({"swagger": "2.0"}, r"Only OpenAPI"),
        (base_spec(components={"schemas": {"X": {"$ref": "#/missing"}}}), r"Unresolved"),
        (base_spec(components={"schemas": {"X": {"$ref": "https://no-network.example.test/schema"}}}), r"Only local"),
    ]
    for doc, pattern in samples:
        path = temp / "bad.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        result = run_cli(["generate", "--spec", str(path), "--out", str(temp / "invalid"), "--name", "sample"])
        assert result.returncode == 1
        assert re.search(pattern, result.stderr)
    secret_file = temp / "secret.yaml"
    secret_file.write_text("openapi: [ DO_NOT_PRINT_CREDENTIAL_123", encoding="utf-8")
    invalid = run_cli(["generate", "--spec", str(secret_file), "--out", str(temp / "invalid"), "--name", "sample"])
    assert invalid.returncode == 1
    assert re.search(r"syntax", invalid.stderr)
    assert not re.search(r"DO_NOT_PRINT", invalid.stderr)
    valid_file = temp / "good.json"
    valid_file.write_text(json.dumps(base_spec()), encoding="utf-8")
    valid = run_cli(["generate", "--spec", str(valid_file), "--out", str(temp / "json"), "--name", "json-piece"])
    assert valid.returncode == 0, valid.stderr


def test_cli_accepts_path_relative_servers_and_emits_required_base_url_without_a_default(temp: Path) -> None:
    for i, url in enumerate(["v2", "./v2"]):
        spec = temp / f"relative-{i}.json"
        spec.write_text(json.dumps(base_spec(servers=[{"url": url}])), encoding="utf-8")
        out = temp / f"relative-{i}"
        result = run_cli(["generate", "--spec", str(spec), "--out", str(out), "--name", "relative"])
        assert result.returncode == 0, result.stderr
        assert len(json.loads(result.stdout)["generated"]) == 1
        source = (out / "src/lib/actions/hello.ts").read_text(encoding="utf-8")
        assert re.search(r'base_url: Property.ShortText\(.*"required":true', source)
        assert not re.search(r'"defaultValue"', source)


def test_filesystem_boundaries_symlink_ancestors_missing_parent_traversal_and_spec_size(temp: Path) -> None:
    link = temp / "link"
    os.symlink(temp, link, target_is_directory=True)
    files = {"src/index.ts": "safe"}
    with pytest.raises(SpecError, match="symlinks"):
        write_piece(files, link / "out")
    with pytest.raises(SpecError, match="symlinks"):
        write_piece(files, link / "out", dry_run=True)
    with pytest.raises(SpecError, match="parent"):
        write_piece(files, temp / "missing" / "out")
    with pytest.raises(SpecError, match="Unsafe"):
        write_piece({"../escape.txt": "bad"}, temp / "safe")
    too_big = temp / "large.json"
    too_big.write_text(" " * (8 * 1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(SpecError, match="8 MiB"):
        load_spec(too_big)
    with pytest.raises(SpecError, match="regular file"):
        load_spec(temp)


def test_untrusted_operation_identifiers_and_descriptions_are_emitted_only_as_quoted_data(temp: Path) -> None:
    doc = base_spec()
    doc["paths"]["/hello"]["get"]["operationId"] = '../../escape"; throw new Error("oops")'
    doc["paths"]["/hello"]["get"]["summary"] = '` ${process.exit(42)} " \\ \n'
    files = render_piece(parse_spec(json.dumps(doc)), name="safe").files
    out = temp / "quoted"
    write_piece(files, out)
    assert (out / "src/lib/actions/escape_throw_new_error_oops.ts").is_file()
    assert all(".." not in p for p in files)
