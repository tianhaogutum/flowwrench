import copy
import json
import os

import pytest

from flowwrench.ha_diff import TraceError, load_trace, parse_trace
from flowwrench.ha_diff.cli import main


def test_download_raw_ws_and_saved_record_formats(trace):
    for payload, expected in [
        (trace, "trace/get"),
        ({"trace": trace, "logbookEntries": []}, "download"),
        ({"id": 1, "type": "result", "success": True, "result": trace}, "websocket"),
        ({"extended_dict": trace, "short_dict": {}}, "saved-record"),
    ]:
        parsed = parse_trace(payload)
        assert parsed.format == expected
        assert len(parsed.events) == 2


def test_pointer_selection_keeps_original_envelope(automation_records):
    parsed = parse_trace(automation_records, select="/data/automation.moon/1")
    assert parsed.parts == ("data", "automation.moon", "1", "extended_dict")
    event = parsed.events["condition/0", 0]
    assert parsed.ref(event.parts)["pointer"].startswith(
        "/data/automation.moon/1/extended_dict/trace/"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.update(domain="unknown"),
        lambda t: t.update(item_id=""),
        lambda t: t.update(run_id=1),
        lambda t: t.update(trace=[]),
        lambda t: t["trace"].update({"action/0": {}}),
        lambda t: t["trace"]["action/0"][0].update(path="action/1"),
        lambda t: t["trace"]["action/0"][0].update(changed_variables=[]),
        lambda t: t["trace"]["action/0"][0].update(result=False),
    ],
)
def test_wrong_shapes_rejected(trace, mutate):
    mutate(trace)
    with pytest.raises(TraceError):
        parse_trace(trace)


@pytest.mark.parametrize(
    "contents",
    [
        '{"x": 1, "x": 2}',
        '{"number": NaN}',
        '{"number": Infinity}',
        '{"number": 1e9999}',
        '{"broken":',
        "[]",
    ],
)
def test_invalid_json_rejected(tmp_path, contents):
    path = tmp_path / "input.json"
    path.write_text(contents)
    with pytest.raises(TraceError):
        load_trace(path)


def test_utf8_bom_accepted(testkit, tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(testkit), encoding="utf-8-sig")
    assert len(load_trace(path).events) == 2


def test_storage_requires_selection(automation_records):
    with pytest.raises(TraceError, match="select one"):
        parse_trace(automation_records)


def test_failed_ws_response_rejected():
    with pytest.raises(TraceError, match="did not succeed"):
        parse_trace({"type": "result", "success": False, "error": {"message": "no"}})


def test_depth_limit_and_non_json_api_inputs(trace):
    for value in (float("nan"), set(), object()):
        trace["unexpected"] = value
        with pytest.raises(TraceError):
            parse_trace(trace)
    value = {}
    node = value
    for _ in range(85):
        node["nested"] = {}
        node = node["nested"]
    with pytest.raises(TraceError, match="nesting"):
        parse_trace(value)


def test_cli_json_exit_codes_and_original_file_protection(testkit, tmp_path, capsys):
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text(json.dumps(testkit))
    other = copy.deepcopy(testkit)
    other["trace"]["trace"]["action/0"][0]["error"] = "different"
    right.write_text(json.dumps(other))
    assert main([str(left), str(right), "--format", "json", "--check"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["first_divergence"] == "action/0#0"
    assert main([str(left), str(left), "--check"]) == 0
    capsys.readouterr()
    original = left.read_bytes()
    assert main([str(left), str(right), "--output", str(left)]) == 2
    assert left.read_bytes() == original


def test_cli_partial_and_io_error_codes(testkit, tmp_path, capsys):
    path = tmp_path / "trace.json"
    testkit["trace"]["trace"] = {}
    path.write_text(json.dumps(testkit))
    assert main([str(path), str(path), "--check"]) == 3
    capsys.readouterr()
    assert main([str(path), str(tmp_path / "missing.json")]) == 2
    error = capsys.readouterr().err
    assert "cannot read" in error and "Traceback" not in error


@pytest.mark.parametrize(
    "contents", ['{"x": "\\ud800"}', '{"\\udfff": 1}', '{"x": ' + "1" * 5000 + "}"]
)
def test_non_scalar_unicode_and_excessive_integer_are_clean_errors(contents, tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text(contents)
    assert main([str(path), str(path)]) == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["\ud800", 10**1000])
def test_unrenderable_values_rejected_even_inside_valid_traces(trace, value):
    trace["trace"]["action/0"][0]["changed_variables"]["response"] = value
    with pytest.raises(TraceError, match="surrogate|integer"):
        parse_trace(trace)


def test_file_size_limit(tmp_path):
    path = tmp_path / "large.json"
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(TraceError, match="exceeds 16 MiB"):
        load_trace(path)


def test_nonregular_files_are_rejected_without_waiting(tmp_path):
    fifo = tmp_path / "trace.pipe"
    os.mkfifo(fifo)
    for path in (tmp_path, fifo):
        with pytest.raises(TraceError, match="regular JSON file"):
            load_trace(path)


def test_cli_handles_a_closed_output_pipe(testkit, tmp_path, monkeypatch):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(testkit))

    def closed_pipe(_value):
        raise BrokenPipeError()

    monkeypatch.setattr("sys.stdout.write", closed_pipe)
    assert main([str(path), str(path)]) == 0


def test_json_object_budget_aborts_during_decode(tmp_path, monkeypatch):
    from flowwrench.ha_diff import loader

    monkeypatch.setattr(loader, "MAX_JSON_OBJECTS", 30)
    path = tmp_path / "many-small-objects.json"
    path.write_text('{"values": [' + ",".join("{}" for _ in range(50)) + "]}")
    with pytest.raises(TraceError, match="object/member safety budget"):
        load_trace(path)


def test_api_node_budget_is_checked_before_a_large_pending_stack(trace, monkeypatch):
    from flowwrench.ha_diff import loader

    monkeypatch.setattr(loader, "MAX_JSON_NODES", 100)
    trace["extra"] = [None] * 150
    with pytest.raises(TraceError, match="node safety budget"):
        parse_trace(trace)


def test_non_utf8_source_label_has_a_clean_error(trace):
    with pytest.raises(TraceError, match="UTF-8 text"):
        parse_trace(trace, source="invalid-\udcff.json")


@pytest.mark.parametrize("path", ["a" * 4097, "/".join(["a"] * 161)])
def test_excessive_paths_are_bounded_before_alignment_or_regex_work(trace, path):
    trace["trace"] = {path: [{"path": path, "timestamp": "2026-10-07T04:57:43.847400+00:00"}]}
    with pytest.raises(TraceError, match="path safety budget"):
        parse_trace(trace)
