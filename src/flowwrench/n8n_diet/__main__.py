import sys

from flowwrench.cli import main

raise SystemExit(main(["n8n-diet", *sys.argv[1:]]))
