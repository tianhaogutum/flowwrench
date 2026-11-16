"""Supported OpenFlow subset validation (port of ``lib/validate.js``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, NoReturn

from .errors import FormatError
from .fileio import get, json_value, require_keys, require_object
from .jsvalues import UNDEFINED, is_integer, is_number, js_items, js_length, js_string, js_trim

SCRIPT_TYPES = ("script", "rawscript", "flow")
LOOP_TYPES = ("forloopflow", "whileloopflow")
_MODULE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_FORBIDDEN_IDS = ("constructor", "prototype", "__proto__")
_MODULE_FIELDS = (
    "id", "value", "summary", "skip_if", "stop_after_if", "stop_after_all_iters_if",
    "continue_on_error", "retry", "mock",
)


@dataclass
class FlowModel:
    """Module ids (in validation order) and every branch id of a flow."""

    modules: dict[str, dict[str, Any]] = field(default_factory=dict)
    branches: list[str] = field(default_factory=list)


def _fail(message: str) -> NoReturn:
    raise FormatError(message)


def _bools(value: dict[str, Any], names: tuple[str, ...], label: str) -> None:
    for key in names:
        if key in value and not isinstance(value[key], bool):
            _fail(f"{label}.{key}: expected boolean")


def integer(value: Any, minimum: int, maximum: int, label: str) -> None:
    """``Number.isInteger(value)`` within ``minimum..maximum``."""
    if not is_integer(value) or value < minimum or value > maximum:
        _fail(f"{label}: expected integer {minimum}..{maximum}")


def _expression(expr: Any, label: str) -> None:
    if not isinstance(expr, str) or not js_trim(expr) or js_length(expr) > 16384:
        _fail(f"{label}: expected expression string of 1..16384 characters")


def transform(value: Any, label: str) -> None:
    require_object(value, label)
    kind = value.get("type", UNDEFINED)
    if kind == "static" and isinstance(kind, str):
        require_keys(value, ("type", "value"), label)
    elif kind == "javascript" and isinstance(kind, str):
        require_keys(value, ("type", "expr"), label)
        _expression(value.get("expr", UNDEFINED), f"{label}.expr")
    else:
        _fail(f"{label}: unsupported transform type '{js_string(kind)}'")


def _condition(value: Any, label: str, stop: bool = False) -> None:
    allowed = ("expr", "skip_if_stopped", "error_message", "error_include_result") if stop else ("expr",)
    require_keys(value, allowed, label)
    _expression(value.get("expr", UNDEFINED), f"{label}.expr")
    if stop:
        _bools(value, ("skip_if_stopped", "error_include_result"), label)
        message = value.get("error_message")
        if message is not None and not isinstance(message, str):
            _fail(f"{label}.error_message: expected string or null")
        if message is not None and value.get("skip_if_stopped"):
            _fail(f"{label}: error_message and skip_if_stopped are mutually exclusive")


def _retry(value: Any, label: str) -> None:
    require_keys(value, ("constant", "exponential", "retry_if"), label)
    total: int | float = 0
    for part in ("constant", "exponential"):
        if part not in value:
            continue
        policy = value[part]
        allowed = ("attempts", "seconds") if part == "constant" else ("attempts", "seconds", "multiplier", "random_factor")
        require_keys(policy, allowed, f"{label}.{part}")
        integer(policy.get("attempts", UNDEFINED), 0, 100, f"{label}.{part}.attempts")
        integer(policy.get("seconds", UNDEFINED), 0 if part == "constant" else 1, 86400, f"{label}.{part}.seconds")
        if part == "exponential":
            integer(policy.get("multiplier", UNDEFINED), 0, 86400, f"{label}.exponential.multiplier")
            jitter = policy.get("random_factor")
            if jitter is not None and not (is_number(jitter) and jitter == 0):
                _fail(f"{label}: retry jitter is unsupported; set random_factor to 0")
        total += policy["attempts"]
    if total > 100:
        _fail(f"{label}: at most 100 retries supported")
    if "retry_if" in value:
        _condition(value["retry_if"], f"{label}.retry_if")


def validate_flow(flow: Any) -> FlowModel:
    """Validate the supported OpenFlow subset; return module ids and branch ids."""
    json_value(flow, "flow")
    require_keys(flow, ("summary", "description", "value", "schema"), "flow")
    if not isinstance(flow.get("summary"), str):
        _fail("flow.summary: expected string (OpenFlow wrapper required)")
    value = flow.get("value", UNDEFINED)
    require_keys(value, ("modules", "failure_module", "notes", "groups"), "flow.value")
    model = FlowModel()

    def module_list(items: Any, label: str, depth: int = 0) -> None:
        if not isinstance(items, list):
            _fail(f"{label}: expected module array")
        if depth > 32:
            _fail(f"{label}: module nesting exceeds 32")
        for module in items:
            _module(module, label, depth)

    def _module(module: Any, label: str, depth: int) -> None:
        require_object(module, label)
        module_id = module.get("id", UNDEFINED)
        if not isinstance(module_id, str) or not _MODULE_ID.fullmatch(module_id) or module_id in _FORBIDDEN_IDS:
            _fail(f"{label}: invalid module id")
        if module_id in model.modules:
            _fail(f"duplicate module id '{module_id}' (ids must be globally unique)")
        if len(model.modules) >= 1000:
            _fail("flow exceeds 1000 modules")
        model.modules[module_id] = module
        at = f"module {module_id}"
        require_keys(module, _MODULE_FIELDS, at)
        _bools(module, ("continue_on_error",), at)
        if "skip_if" in module:
            _condition(module["skip_if"], f"{at}.skip_if")
        if "stop_after_if" in module:
            _condition(module["stop_after_if"], f"{at}.stop_after_if", True)
        if "stop_after_all_iters_if" in module:
            _condition(module["stop_after_all_iters_if"], f"{at}.stop_after_all_iters_if", True)
        if "retry" in module:
            _retry(module["retry"], f"{at}.retry")
        if "mock" in module:
            require_keys(module["mock"], ("enabled", "return_value", "preview_only"), f"{at}.mock")
            _bools(module["mock"], ("enabled", "preview_only"), f"{at}.mock")
        v = module.get("value", UNDEFINED)
        require_object(v, f"{at}.value")
        kind = v.get("type", UNDEFINED)
        kind = kind if isinstance(kind, str) else UNDEFINED
        if kind in SCRIPT_TYPES:
            if kind == "rawscript":
                require_keys(v, ("type", "content", "language", "input_transforms", "path", "lock"), f"{at}.value")
                if not isinstance(v.get("content"), str) or not isinstance(v.get("language"), str):
                    _fail(f"{at}: rawscript needs content and language")
            else:
                require_keys(v, ("type", "path", "hash", "input_transforms"), f"{at}.value")
                path = v.get("path")
                if not isinstance(path, str) or not path:
                    _fail(f"{at}: {kind} needs path")
            transforms = v.get("input_transforms", UNDEFINED)
            require_object(transforms, f"{at}.input_transforms")
            for name, item in js_items(transforms):
                transform(item, f"{at}.input_transforms.{name}")
        elif kind == "identity":
            require_keys(v, ("type", "flow"), f"{at}.value")
            _bools(v, ("flow",), at)
        elif kind in LOOP_TYPES:
            require_keys(
                v, ("type", "modules", "iterator", "skip_failures", "parallel", "squash", "max_iterations"), f"{at}.value"
            )
            _bools(v, ("skip_failures", "parallel", "squash"), at)
            if v.get("parallel") or v.get("squash"):
                _fail(f"{at}: parallel loops and squash are unsupported")
            if not isinstance(v.get("skip_failures"), bool):
                _fail(f"{at}: skip_failures must be boolean")
            if kind == "forloopflow":
                transform(v.get("iterator", UNDEFINED), f"{at}.iterator")
                if "max_iterations" in v:
                    _fail(f"{at}: max_iterations belongs to whileloopflow")
            else:
                if "iterator" in v:
                    _fail(f"{at}: whileloopflow has no iterator predicate; use stop_after_if")
                if "max_iterations" in v:
                    integer(v["max_iterations"], 1, 10000, f"{at}.max_iterations")
            module_list(v.get("modules", UNDEFINED), f"{at}.modules", depth + 1)
        elif kind in ("branchone", "branchall"):
            one = kind == "branchone"
            require_keys(v, ("type", "branches", "default") if one else ("type", "branches", "parallel"), f"{at}.value")
            _bools(v, ("parallel",), at)
            if v.get("parallel"):
                _fail(f"{at}: parallel branches are unsupported; use parallel: false")
            branches = v.get("branches", UNDEFINED)
            if not isinstance(branches, list):
                _fail(f"{at}: branches must be an array")
            for index, branch in enumerate(branches):
                branch_label = f"{at}.branches[{index}]"
                require_keys(branch, ("summary", "expr", "modules") if one else ("summary", "skip_failure", "modules"), branch_label)
                if one:
                    _expression(branch.get("expr", UNDEFINED), f"{branch_label}.expr")
                else:
                    _bools(branch, ("skip_failure",), at)
                model.branches.append(f"{module_id}:{index}")
                module_list(branch.get("modules", UNDEFINED), branch_label, depth + 1)
            if one:
                model.branches.append(f"{module_id}:default")
                module_list(v.get("default", UNDEFINED), f"{at}.default", depth + 1)
        else:
            _fail(f"{at}: unsupported module type '{js_string(v.get('type', UNDEFINED))}'")
        if module.get("stop_after_all_iters_if") and kind not in LOOP_TYPES:
            _fail(f"{at}: stop_after_all_iters_if requires a loop")
        mock = module.get("mock")
        if isinstance(mock, dict) and mock.get("enabled") and kind not in SCRIPT_TYPES:
            _fail(f"{at}: inline mocks are only supported on script/rawscript/flow")

    module_list(value.get("modules", UNDEFINED), "flow.value.modules")
    if "failure_module" in value:
        handler = value["failure_module"]
        require_object(handler, "failure_module")
        handler_type = get(handler.get("value"), "type")
        if not (isinstance(handler.get("id"), str) and handler["id"] == "failure") or not (
            isinstance(handler_type, str) and handler_type in ("script", "rawscript")
        ):
            _fail("failure_module must have id failure and type script/rawscript")
        module_list([handler], "failure_module")
    return model


def _outcome(value: Any, label: str, sequence: bool = False) -> None:
    allowed = ("result", "error", "echo_args", "expect_args") if sequence else ("result", "error", "echo_args")
    require_keys(value, allowed, label)
    if sum(1 for key in ("result", "error", "echo_args") if key in value) != 1:
        _fail(f"{label}: expected exactly one of result, error, echo_args")
    if "expect_args" in value:
        require_object(value["expect_args"], f"{label}.expect_args")
    if "echo_args" in value and value["echo_args"] is not True:
        _fail(f"{label}.echo_args must be true")
    if "error" in value:
        error = value["error"]
        require_keys(error, ("message", "name"), f"{label}.error")
        if not isinstance(error.get("message"), str) or ("name" in error and not isinstance(error["name"], str)):
            _fail(f"{label}.error: expected message and optional name strings")


def validate_mocks(mocks: Any, modules: dict[str, dict[str, Any]]) -> None:
    """Validate external mocks against the flow's module ids."""
    json_value(mocks, "mocks")
    require_object(mocks, "mocks")
    for module_id, mock in js_items(mocks):
        if module_id not in modules:
            _fail(f"mock refers to unknown module '{module_id}'")
        if modules[module_id]["value"]["type"] not in SCRIPT_TYPES:
            _fail(f"mock {module_id}: only script/rawscript/flow can be mocked")
        if isinstance(mock, dict) and "sequence" in mock:
            require_keys(mock, ("sequence",), f"mock {module_id}")
            sequence = mock["sequence"]
            if not isinstance(sequence, list) or not sequence:
                _fail(f"mock {module_id}: sequence must be nonempty")
            for index, response in enumerate(sequence):
                _outcome(response, f"mock {module_id}[{index}]", True)
        else:
            _outcome(mock, f"mock {module_id}")
