import sys

from flowwrench.cli import main

raise SystemExit(main(["schedules", *sys.argv[1:]]))
