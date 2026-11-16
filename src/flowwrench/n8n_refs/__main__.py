import sys

from flowwrench.cli import main

raise SystemExit(main(["n8n-refs", *sys.argv[1:]]))
