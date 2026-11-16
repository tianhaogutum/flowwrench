import sys

from flowwrench.cli import main

raise SystemExit(main(["check", *sys.argv[1:]]))
