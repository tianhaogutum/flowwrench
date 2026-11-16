"""Offline Windmill OpenFlow dry runs: mocked steps, real control flow.

Python port of the Node.js ``openflow-dryrun`` 0.1.0 tool. Every script,
rawscript and subflow result is mocked; ``input_transforms``, branches, loops,
retries and failure handlers are simulated and recorded in an assertable trace
with branch coverage. JavaScript expressions are evaluated by Node.js 20+ in
an isolated child process (see :mod:`flowwrench.windmill_dryrun.expression`).
"""

from __future__ import annotations

__version__ = "0.1.0"

from .bundle import encode_bundle, replay_bundle  # noqa: E402
from .cases import iterate_cases, junit, load_cases, run_case_entries, run_cases  # noqa: E402
from .errors import FormatError, NodeUnavailableError, StepError, error_value  # noqa: E402
from .fileio import checkpoint_outputs, json_value, read_data, write_output  # noqa: E402
from .runner import resolve_options, run_flow  # noqa: E402
from .validate import validate_flow, validate_mocks  # noqa: E402

__all__ = [
    "FormatError",
    "NodeUnavailableError",
    "StepError",
    "__version__",
    "checkpoint_outputs",
    "encode_bundle",
    "error_value",
    "iterate_cases",
    "json_value",
    "junit",
    "load_cases",
    "read_data",
    "replay_bundle",
    "resolve_options",
    "run_case_entries",
    "run_cases",
    "run_flow",
    "validate_flow",
    "validate_mocks",
    "write_output",
]
