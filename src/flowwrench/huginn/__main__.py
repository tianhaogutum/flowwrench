import sys

from flowwrench.cli import main

raise SystemExit(main(["huginn", *sys.argv[1:]]))
