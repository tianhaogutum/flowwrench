"""Error types shared by the simulator (port of ``lib/errors.js``)."""

from __future__ import annotations

from typing import Any

from .jsvalues import UNDEFINED

NODE_REQUIRED_MESSAGE = "windmill-dryrun needs Node.js 20+ to evaluate JavaScript expressions"


class FormatError(Exception):
    """Invalid input, unsupported field or output failure (CLI exit 2)."""

    name = "FormatError"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StepError(Exception):
    """A runtime failure attributed to a module id (CLI exit 1)."""

    def __init__(self, message: str, step_id: str | None, name: str = "StepError") -> None:
        super().__init__(message)
        self.message = message
        self.step_id = step_id
        self.name = name
        # JavaScript adds these properties only for some errors; UNDEFINED
        # means "property absent" so error_value() can omit them.
        self.result: Any = UNDEFINED
        self.call: Any = UNDEFINED
        self.expected_args: Any = UNDEFINED
        self.actual_args: Any = UNDEFINED


class NodeUnavailableError(RuntimeError):
    """Node.js 20+ is required to evaluate JavaScript expressions."""

    def __init__(self, detail: str | None = None) -> None:
        message = NODE_REQUIRED_MESSAGE if detail is None else f"{NODE_REQUIRED_MESSAGE} ({detail})"
        super().__init__(message)
        self.message = message


def error_message(error: BaseException) -> str:
    """``error.message`` for our errors, ``str(error)`` otherwise."""
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else str(error)


def error_value(error: BaseException) -> dict[str, Any]:
    """Structured, stack-free JSON error record (``errorValue`` in errors.js)."""
    name = getattr(error, "name", None) or type(error).__name__ or "Error"
    step_id = getattr(error, "step_id", None)
    value: dict[str, Any] = {"name": name, "message": error_message(error), "step_id": step_id}
    result = getattr(error, "result", UNDEFINED)
    if result is not UNDEFINED:
        value["result"] = result
    expected_args = getattr(error, "expected_args", UNDEFINED)
    if expected_args is not UNDEFINED:
        value["call"] = getattr(error, "call", UNDEFINED)
        value["expected_args"] = expected_args
        value["actual_args"] = getattr(error, "actual_args", UNDEFINED)
    return value
