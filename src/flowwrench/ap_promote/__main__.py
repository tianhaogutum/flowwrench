import sys

from flowwrench.cli import main

raise SystemExit(main(["ap-promote", *sys.argv[1:]]))
