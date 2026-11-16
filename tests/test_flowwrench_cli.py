"""Top-level `flowwrench` dispatcher: overview, version, routing and error paths."""
import subprocess
import sys

import pytest

import flowwrench
from flowwrench import cli

# Commands whose Python subpackage ships in this repository.
TOOLS = ["n8n-diet", "n8n-refs", "ap-promote", "huginn", "schedules"]
# Registered commands whose modules may arrive later (or are a Node companion).
REGISTERED = ["check", "n8n2ap", "nodered-merge", "nodered-test", "openapi2piece", "windmill-dryrun"]
SUBCOMMANDS = {
    "n8n-diet": ["analyze", "plan", "compare", "reclaim", "reclaim-plan", "verify-copy", "fixture"],
    "n8n-refs": ["check", "compare", "rename"],
    "ap-promote": ["inventory", "verify-roundtrip"],
    "huginn": ["split", "join", "render", "extract-vars", "diff"],
    "schedules": ["extract", "analyze", "dst", "suggest", "overlap", "compare"],
}


def run(*args, module=("-m", "flowwrench")):
    return subprocess.run([sys.executable, *module, *args], capture_output=True, text=True, timeout=60)


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"flowwrench {flowwrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for name in [*TOOLS, *REGISTERED]:
        assert f"\n  {name} " in out


def test_command_table_shape():
    groups = {group: [c.name for c in cli.COMMANDS if c.group == group] for group in cli.GROUPS}
    assert groups == {
        "Check": ["check"],
        "n8n": ["n8n2ap", "n8n-diet", "n8n-refs"],
        "Node-RED": ["nodered-merge", "nodered-test"],
        "Activepieces": ["openapi2piece", "ap-promote"],
        "Windmill": ["windmill-dryrun"],
        "Huginn": ["huginn"],
        "Schedules": ["schedules"],
    }
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "check": "flowwrench.check.cli",
        "n8n2ap": "flowwrench.n8n2ap.cli",
        "n8n-diet": "flowwrench.n8n_diet.cli",
        "n8n-refs": "flowwrench.n8n_refs.cli",
        "nodered-merge": "flowwrench.nodered_merge.cli",
        "nodered-test": None,
        "openapi2piece": "flowwrench.openapi2piece.cli",
        "ap-promote": "flowwrench.ap_promote.cli",
        "windmill-dryrun": "flowwrench.windmill_dryrun.cli",
        "huginn": "flowwrench.huginn.cli",
        "schedules": "flowwrench.schedules.cli",
    }
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    assert set(cli.BY_NAME) == {c.name for c in cli.COMMANDS}


def test_shipped_tools_are_installed():
    for name in TOOLS:
        assert cli._installed(cli.BY_NAME[name]), name


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"flowwrench {flowwrench.__version__}\n"


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    assert f"usage: flowwrench {tool}" in result.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    code, out, _ = _main(["help", tool], capsys)
    assert code == 0
    assert f"usage: flowwrench {tool}" in out


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_version_is_package_version(tool, capsys):
    code, out, _ = _main([tool, "--version"], capsys)
    assert code == 0
    assert out == f"{flowwrench.__version__}\n"


@pytest.mark.parametrize("command,sub", [(c, s) for c, subs in SUBCOMMANDS.items() for s in subs])
def test_subcommand_help_uses_new_program_name(command, sub, capsys):
    code, out, err = _main([command, sub, "--help"], capsys)
    assert code == 0, err
    assert f"usage: flowwrench {command} {sub}" in out


@pytest.mark.parametrize("tool,package", [
    ("n8n-diet", "flowwrench.n8n_diet"), ("n8n-refs", "flowwrench.n8n_refs"),
    ("ap-promote", "flowwrench.ap_promote"), ("huginn", "flowwrench.huginn"),
    ("schedules", "flowwrench.schedules"),
])
def test_subpackage_main_routes_through_dispatcher(tool, package):
    result = run("--help", module=("-m", package))
    assert result.returncode == 0, result.stderr
    assert f"usage: flowwrench {tool}" in result.stdout


def test_tool_exit_codes_pass_through():
    bad = run("ap-promote", "no-such-subcommand")
    assert bad.returncode == 2 and "usage: flowwrench ap-promote" in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["huginnn"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'huginnn'" in err and "did you mean 'huginn'?" in err


@pytest.mark.parametrize("legacy,new", [
    ("n8n-exec-diet", "n8n-diet"), ("n8n-refcheck", "n8n-refs"), ("ap-flow-promote", "ap-promote"),
    ("huginn-scenario-kit", "huginn"), ("cronherd", "schedules"), ("nrflowtest", "nodered-test"),
])
def test_unknown_legacy_name_points_to_new_command(legacy, new, capsys):
    assert cli.main([legacy]) == 2
    assert f"{legacy} is now 'flowwrench {new}'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "flowwrench.ghost_tool.cli", "not shipped", "Check")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    overview = cli.overview()
    assert "ghost" in overview and "(coming soon)" in overview
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("flowwrench: command 'ghost' is not available") and "Traceback" not in err


@pytest.mark.parametrize("argv", [["nodered-test"], ["nodered-test", "run", "--flows", "x.json"],
                                  ["help", "nodered-test"]])
def test_nodered_test_explains_node_companion(argv, capsys):
    code, out, err = _main(argv, capsys)
    assert code == 2 and out == ""
    assert "packages/nrflowtest" in err
    assert "cd packages/nrflowtest && npm ci && node bin/nrflowtest.js run" in err
    assert "Traceback" not in err


def test_nodered_test_is_listed_without_coming_soon(capsys):
    assert cli.main([]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("  nodered-test "))
    assert "(coming soon)" not in line
