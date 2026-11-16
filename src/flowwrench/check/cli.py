"""``flowwrench check [PATH ...]``: scan exported workflows, report what will break.

Exit status: 0 no finding at the --fail-on threshold, 1 findings at or above
it, 2 usage or input error (unknown option or check, missing path).
Read-only: no file is written, no workflow runs, no network is used.
"""
from __future__ import annotations

import argparse
from datetime import date
import os
import sys

from .discover import DiscoveryError, discover
from .registry import CHECKS, CheckOptions, run_checks, select
from .report import RENDERERS, Run, render_text

PROG = "flowwrench check"
FAIL_ON = {"error": "error", "warning": "warning", "never": None}

EPILOG = """\
examples:
  flowwrench check                       scan the current directory
  flowwrench check exports/ flows.json   several paths at once
  flowwrench check . --format sarif > flowwrench.sarif
  flowwrench check . --only n8n-refs,schedules --fail-on warning
  flowwrench check --list-checks

exit status: 0 nothing at the --fail-on level, 1 findings at or above it,
2 usage or input error. Nothing is written, executed or sent anywhere.
Docs: https://github.com/tianhaogutum/flowwrench/blob/main/docs/check.md
"""


def _names(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("expected one or more check names, comma separated")
    return names


def _iso_date(value: str) -> str:
    try:
        if len(value) != 10:
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an ISO date, YYYY-MM-DD") from None
    return value


def _positive(value: str) -> int:
    if not value.isdecimal() or not 2 <= int(value) <= 10_000:
        raise argparse.ArgumentTypeError("expected an integer of at least 2")
    return int(value)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Find exported n8n, Node-RED, Activepieces, Windmill, Huginn and crontab files "
                    "under PATH and report what will break before you import or deploy them.")
    p.add_argument("paths", nargs="*", metavar="PATH", help="files or directories to scan (default: .)")
    p.add_argument("--format", choices=("text", "json", "sarif"), default="text",
                   help="report format (default: text); sarif is SARIF 2.1.0 for code scanning")
    p.add_argument("--fail-on", choices=tuple(FAIL_ON), default="error",
                   help="exit 1 when a finding is at least this severe (default: error)")
    p.add_argument("--only", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="run only these checks (repeatable)")
    p.add_argument("--skip", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="do not run these checks (repeatable)")
    p.add_argument("--list-checks", action="store_true", help="list the available checks and exit")
    p.add_argument("--default-timezone", default="UTC", metavar="IANA",
                   help="timezone assumed for schedules that export none (default: UTC)")
    p.add_argument("--from", dest="from_date", type=_iso_date, metavar="DATE",
                   help="UTC start date of the schedule horizons (default: today)")
    p.add_argument("--herd-threshold", type=_positive, default=3, metavar="N",
                   help="report a herd when N or more schedules start in the same minute (default: 3)")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="colour text output (default: auto; honours NO_COLOR)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="text: show a fix hint under each finding and list clean files")
    return p


def list_checks() -> str:
    width = max(len(check.name) for check in CHECKS) + 2
    lines = ["Checks (all run by default; select with --only/--skip):", ""]
    for check in CHECKS:
        lines.append(f"  {check.name.ljust(width)}{check.summary}")
        uses = "built in" if check.tool == "-" else f"uses: flowwrench {check.tool}"
        lines.append(f"  {''.ljust(width)}{uses}; rules: {', '.join(check.rules)}")
    return "\n".join(lines) + "\n"


def _use_color(choice: str) -> bool:
    if choice != "auto":
        return choice == "always"
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _validate_timezone(name: str) -> None:
    from flowwrench.schedules.model import InputError, timezone

    try:
        timezone(name)
    except InputError as exc:
        raise ValueError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_checks:
        sys.stdout.write(list_checks())
        return 0
    try:
        checks = select([n for group in args.only for n in group], [n for group in args.skip for n in group])
        if not checks:
            raise ValueError("--only/--skip leave no check to run")
        _validate_timezone(args.default_timezone)
        options = CheckOptions(default_timezone=args.default_timezone, from_date=args.from_date,
                               herd_threshold=args.herd_threshold)
        paths = args.paths or ["."]
        discovery = discover(paths)
    except (ValueError, DiscoveryError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 2
    findings = run_checks(discovery, checks, options)
    run = Run(paths, discovery.sources, discovery.scanned, checks, findings, FAIL_ON[args.fail_on])
    if args.format == "text":
        output = render_text(run, color=_use_color(args.color), verbose=args.verbose)
    else:
        output = RENDERERS[args.format](run)
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except BrokenPipeError:
        return 1 if run.failed else 0
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
