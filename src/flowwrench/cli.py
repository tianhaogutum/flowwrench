"""Top-level ``flowwrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``flowwrench --help`` stays fast and a broken or missing
tool only affects its own command. ``nodered-test`` is not a Python module: it
needs the Node-RED runtime and ships as a Node.js companion in packages/nrflowtest/.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "flowwrench"
REPO_URL = "https://github.com/tianhaogutum/flowwrench"


@dataclass(frozen=True)
class Command:
    name: str
    # Dotted path of the module exposing ``main(argv)``; None for a non-Python companion.
    module: str | None
    summary: str
    group: str
    # Repository directory of a non-Python companion (only when ``module`` is None).
    companion: str | None = None


GROUPS = ("Check", "n8n", "Node-RED", "Activepieces", "Windmill", "Huginn", "Schedules")

COMMANDS: tuple[Command, ...] = (
    Command("check", "flowwrench.check.cli",
            "Scan a folder of exported workflows and report what will break before you import or deploy",
            "Check"),
    Command("n8n2ap", "flowwrench.n8n2ap.cli",
            "Convert an n8n workflow export into an Activepieces flow, with a migration report", "n8n"),
    Command("n8n-diet", "flowwrench.n8n_diet.cli",
            "Measure n8n SQLite execution storage, simulate pruning, reclaim a verified compact copy", "n8n"),
    Command("n8n-refs", "flowwrench.n8n_refs.cli",
            "Check n8n data references between nodes (SARIF for CI) and rename nodes safely", "n8n"),
    Command("nodered-merge", "flowwrench.nodered_merge.cli",
            "Semantic diff and three-way Git merge driver for Node-RED flows.json", "Node-RED"),
    Command("nodered-test", None,
            "Run headless tests against real Node-RED flow exports (Node.js companion)", "Node-RED",
            companion="packages/nrflowtest"),
    Command("openapi2piece", "flowwrench.openapi2piece.cli",
            "Generate an editable Activepieces piece from an OpenAPI 3.0/3.1 file", "Activepieces"),
    Command("ap-promote", "flowwrench.ap_promote.cli",
            "Inventory, remap and pre-check Activepieces connections when promoting flows", "Activepieces"),
    Command("windmill-dryrun", "flowwrench.windmill_dryrun.cli",
            "Dry-run Windmill OpenFlow mappings and control flow offline against mocks", "Windmill"),
    Command("huginn", "flowwrench.huginn.cli",
            "Split, render, diff, lint and combine Huginn scenarios as git-friendly YAML", "Huginn"),
    Command("schedules", "flowwrench.schedules.cli",
            "Inventory schedules across n8n, Activepieces, Huginn and cron; find herds and DST hazards",
            "Schedules"),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "n8n-exec-diet": "n8n-diet", "n8n-refcheck": "n8n-refs", "ap-flow-promote": "ap-promote",
    "huginn-scenario-kit": "huginn", "cronherd": "schedules", "nrflowtest": "nodered-test",
    "nrflow-merge": "nodered-merge", "openflow-dryrun": "windmill-dryrun",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    if command.module is None:
        return True
    return _present(command.module.rpartition(".")[0])


def companion_message(command: Command) -> str:
    """How to run a non-Python companion such as ``nodered-test`` (nrflowtest)."""
    where = command.companion
    return "\n".join([
        f"{PROG} {command.name}: this command is not part of the Python package.",
        "It runs flows on the real Node-RED runtime, so it ships as the Node.js companion",
        f"nrflowtest in {where}/ of the {PROG} repository (Node.js >= 22.9 and npm required).",
        "",
        "From a checkout of the repository:",
        "",
        f"  cd {where} && npm ci && node bin/nrflowtest.js run \\",
        "    --flows fixtures/orders/flows.json --tests 'fixtures/orders/*.test.yaml'",
        "",
        "Then point it at your own export:",
        "",
        "  node bin/nrflowtest.js run --flows flows.json --tests 'tests/*.yaml'",
        "",
        f"Docs: {REPO_URL}/tree/main/{where}",
    ]) + "\n"


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline checks and fixes for n8n, Node-RED, Activepieces, "
        "Windmill and Huginn workflows",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = "" if _installed(command) else "  (coming soon)"
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if command.module is None:
        sys.stderr.write(companion_message(command))
        return 2
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    module = importlib.import_module(command.module)
    result = module.main(argv)
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
