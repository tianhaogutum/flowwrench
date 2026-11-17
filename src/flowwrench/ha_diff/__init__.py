"""Compare recorded Home Assistant execution evidence without executing it.

Originally the standalone MIT-licensed tool ha-trace-diff; its license notice is kept in
licenses/ha-trace-diff-MIT.txt. Needs the ``ha`` extra (DeepDiff, jsonpointer). The public
names below are imported on first use, so importing this package stays dependency-free.
"""

from flowwrench import __version__  # noqa: F401

__all__ = ["TraceError", "compare_traces", "load_trace", "parse_trace"]


def __getattr__(name):
    if name == "compare_traces":
        from .compare import compare_traces

        return compare_traces
    if name in ("TraceError", "load_trace", "parse_trace"):
        from . import loader

        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
