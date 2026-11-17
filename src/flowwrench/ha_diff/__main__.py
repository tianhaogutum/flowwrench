import sys

from flowwrench.cli import main

raise SystemExit(main(["ha-diff", *sys.argv[1:]]))
