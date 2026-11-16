"""Bounded OpenFlow control-flow simulator (port of ``lib/runner.js``).

Every script/rawscript/flow result is mocked; workflow code is never loaded or
executed. JavaScript expressions are delegated to :mod:`.expression`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .errors import FormatError, NodeUnavailableError, StepError, error_message, error_value
from .expression import evaluate_encoded
from .fileio import MAX_BYTES, json_value, require_keys, require_object
from .jsvalues import (
    UNDEFINED,
    clone,
    deep_equal,
    is_safe_integer,
    js_add,
    js_items,
    number_to_string,
    stringify,
)
from .validate import LOOP_TYPES, SCRIPT_TYPES, FlowModel, integer, validate_flow, validate_mocks

OPTION_KEYS = ("unmocked", "maxIterations", "timeoutMs", "maxSteps", "requireAllMocksUsed")
DEFAULT_OPTIONS: dict[str, Any] = {
    "unmocked": "error",
    "maxIterations": 100,
    "timeoutMs": 100,
    "maxSteps": 1000,
    "requireAllMocksUsed": False,
}
_TRACE_BUDGET = 16 * MAX_BYTES
_DEFAULT: Any = object()  # "argument omitted" (JavaScript default parameter)


def resolve_options(options: Any) -> dict[str, Any]:
    """Apply defaults to runner options and validate them."""
    require_keys(options, OPTION_KEYS, "options")
    opts = {**DEFAULT_OPTIONS, **options}
    unmocked = opts["unmocked"]
    if not (isinstance(unmocked, str) and unmocked in ("error", "null", "echo-args")):
        raise FormatError("unmocked must be error, null or echo-args")
    if not isinstance(opts["requireAllMocksUsed"], bool):
        raise FormatError("requireAllMocksUsed: expected boolean")
    integer(opts["maxIterations"], 1, 10000, "maxIterations")
    integer(opts["timeoutMs"], 1, 2000, "timeoutMs")
    integer(opts["maxSteps"], 1, 10000, "maxSteps")
    return opts


@dataclass
class _Env:
    input: Any
    results: dict[str, Any]
    previous: Any
    error: Any


@dataclass
class _Outcome:
    result: Any = None
    stopped: bool = False
    skipped_after: bool = False
    skipped: bool = False


def _retry_delay(multiplier: int | float, seconds: int | float, exponent: int) -> int | float | None:
    """``multiplier * seconds ** exponent`` in double arithmetic, if a safe integer."""
    try:
        power = float(seconds) ** exponent
    except OverflowError:
        power = math.inf
    delay = float(multiplier) * power
    if not is_safe_integer(delay):
        return None
    return int(delay)


class _Simulation:
    def __init__(self, flow: dict[str, Any], input: dict[str, Any], mocks: dict[str, Any],
                 opts: dict[str, Any], model: FlowModel) -> None:
        self.flow = flow
        self.input = input
        self.mocks = mocks
        self.opts = opts
        self.ids = list(model.modules)
        self.branches = model.branches
        self.visited: set[str] = set()
        self.selected: set[str] = set()
        self.calls: dict[str, int] = {}
        self.mock_usage: dict[str, dict[str, int]] = {
            module_id: {
                "expected": len(mock["sequence"]), "calls": 0, "consumed": 0,
                "remaining": len(mock["sequence"]), "extra": 0,
            }
            for module_id, mock in js_items(mocks)
            if isinstance(mock, dict) and mock.get("sequence")
        }
        self.mock_errors: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "status": "ok", "result": None, "results": {}, "trace": [], "executed": [],
            "simulated_delay_seconds": 0, "mock_usage": self.mock_usage,
        }
        self.steps = 0
        self.trace_bytes = 0
        self.root_env = _Env(clone(input), {}, clone(input), None)

    # -- expressions -------------------------------------------------------

    def expr(self, source: str, env: _Env, step_id: str, result: Any = _DEFAULT, error: Any = _DEFAULT) -> Any:
        scope = {
            "flow_input": env.input,
            "results": env.results,
            "previous_result": env.previous,
            "result": env.previous if result is _DEFAULT else result,
            "error": env.error if error is _DEFAULT else error,
        }
        encoded = stringify(scope) or "null"
        if len(encoded.encode("utf-8", errors="surrogatepass")) > MAX_BYTES:
            raise StepError("Expression scope exceeds 1 MiB", step_id, "BudgetExceeded")
        return evaluate_encoded(source, encoded, self.ids, step_id, self.opts["timeoutMs"])

    def predicate(self, source: str, env: _Env, step_id: str, result: Any = _DEFAULT, error: Any = _DEFAULT) -> bool:
        answer = self.expr(source, env, step_id, result, error)
        if not isinstance(answer, bool):
            raise StepError("Predicate must return a boolean", step_id, "ExpressionError")
        return answer

    def transform(self, spec: dict[str, Any], env: _Env, step_id: str) -> Any:
        if spec.get("type") == "static":
            value = spec.get("value")
            return clone(None if value is None else value)
        return self.expr(spec["expr"], env, step_id)

    # -- trace -------------------------------------------------------------

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.steps += 1
        if self.steps > self.opts["maxSteps"]:
            raise StepError(
                f"Execution exceeds max-steps {number_to_string(self.opts['maxSteps'])}", event["module_id"], "BudgetExceeded"
            )
        self.report["trace"].append(event)
        return event

    def finish_event(self, event: dict[str, Any]) -> None:
        self.trace_bytes += len((stringify(event) or "").encode("utf-8", errors="surrogatepass"))
        if self.trace_bytes > _TRACE_BUDGET:
            raise StepError("Trace exceeds 16 MiB", event["module_id"], "BudgetExceeded")

    def enter(self, module_id: str) -> None:
        self.visited.add(module_id)
        self.report["executed"].append(module_id)

    # -- mocks -------------------------------------------------------------

    def mocked(self, module: dict[str, Any], args: dict[str, Any], event: dict[str, Any]) -> Any:
        module_id = module["id"]
        mock = self.mocks.get(module_id)
        inline = module.get("mock")
        inline_enabled = isinstance(inline, dict) and bool(inline.get("enabled"))
        n = self.calls.get(module_id, 0)
        self.calls[module_id] = n + 1
        if mock is not None:
            source = "sequence" if mock.get("sequence") else "external"
        elif inline_enabled:
            source = "inline"
        else:
            source = "missing" if self.opts["unmocked"] == "error" else "fallback"
        event["mock"] = {"source": source, "call": n + 1}
        if mock is None and inline_enabled:
            value = inline.get("return_value")
            return clone(None if value is None else value)
        if mock is None:
            if self.opts["unmocked"] == "null":
                return None
            if self.opts["unmocked"] == "echo-args":
                return clone(args)
            raise StepError(f"Missing mock for module '{module_id}'", module_id, "MissingMock")
        if mock.get("sequence"):
            sequence = mock["sequence"]
            usage = self.mock_usage[module_id]
            usage["calls"] += 1
            usage["consumed"] = min(usage["calls"], usage["expected"])
            usage["remaining"] = usage["expected"] - usage["consumed"]
            usage["extra"] = usage["calls"] - usage["consumed"]
            if n >= len(sequence):
                exhausted = StepError(f"Mock sequence exhausted for '{module_id}' at call {n + 1}", module_id, "MockExhausted")
                self.mock_errors.append(error_value(exhausted))
                raise exhausted
            mock = sequence[n]
            if "expect_args" in mock and not deep_equal(args, mock["expect_args"]):
                mismatch = StepError(
                    f"Mock arguments mismatch for '{module_id}' at call {n + 1}: "
                    f"expected {stringify(mock['expect_args'])}, got {stringify(args)}",
                    module_id,
                    "MockArgsMismatch",
                )
                mismatch.call = n + 1
                mismatch.expected_args = clone(mock["expect_args"])
                mismatch.actual_args = clone(args)
                self.mock_errors.append(error_value(mismatch))
                raise mismatch
        if mock.get("error"):
            raise StepError(mock["error"]["message"], module_id, mock["error"].get("name") or "MockError")
        return clone(args if mock.get("echo_args") else mock.get("result"))

    # -- control flow ------------------------------------------------------

    def stop(self, condition: Any, env: _Env, step_id: str, result: Any) -> bool:
        if not isinstance(condition, dict) or not self.predicate(condition["expr"], env, step_id, result):
            return False
        message = condition.get("error_message")
        if message is not None:
            error = StepError(message or "Stopped with error", step_id, "StopAfterError")
            # A stopping result is retained in trace; no stack is emitted.
            if condition.get("error_include_result"):
                error.result = clone(result)
            raise error
        return True

    def sequence(self, items: list[dict[str, Any]], env: _Env, path: list[str]) -> _Outcome:
        stopped = skipped_after = False
        for module in items:
            value = self.module(module, env, [*path, module["id"]])
            if value.skipped:
                continue
            env.results[module["id"]] = clone(value.result)
            self.report["results"][module["id"]] = clone(value.result)
            env.previous = clone(value.result)
            if value.stopped:
                stopped = True
                skipped_after = value.skipped_after
                break
        return _Outcome(result=env.previous, stopped=stopped, skipped_after=skipped_after)

    def body(self, module: dict[str, Any], env: _Env, path: list[str], event: dict[str, Any]) -> _Outcome:
        module_id = module["id"]
        v = module["value"]
        kind = v["type"]
        if kind in SCRIPT_TYPES:
            args = {key: self.transform(spec, env, module_id) for key, spec in js_items(v["input_transforms"])}
            event["args"] = clone(args)
            return _Outcome(result=self.mocked(module, args, event))
        if kind == "identity":
            return _Outcome(result=clone(env.input if v.get("flow") else env.previous))
        if kind == "branchone":
            index = -1
            for position, branch in enumerate(v["branches"]):
                if self.predicate(branch["expr"], env, module_id):
                    index = position
                    break
            label = "default" if index == -1 else str(index)
            self.selected.add(f"{module_id}:{label}")
            event["branch"] = label
            body = v["default"] if index == -1 else v["branches"][index]["modules"]
            return self.sequence(body, replace(env, results=clone(env.results)), [*path, f"branch:{label}"])
        if kind == "branchall":
            results: list[Any] = []
            stopped = skipped_after = False
            for index, branch in enumerate(v["branches"]):
                self.selected.add(f"{module_id}:{index}")
                try:
                    outcome = self.sequence(branch["modules"], replace(env, results=clone(env.results)), [*path, f"branch:{index}"])
                except StepError as error:
                    if not branch.get("skip_failure") or error.name == "BudgetExceeded":
                        raise
                    results.append(None)
                    continue
                results.append(outcome.result)
                stopped = stopped or outcome.stopped
                skipped_after = skipped_after or outcome.skipped_after
            return _Outcome(result=results, stopped=stopped, skipped_after=skipped_after)
        return self.loop(module, env, path)

    def loop(self, module: dict[str, Any], env: _Env, path: list[str]) -> _Outcome:
        # Loops consume inner stop signals, leaving the outer sequence running.
        module_id = module["id"]
        v = module["value"]
        is_for = v["type"] == "forloopflow"
        values = self.transform(v["iterator"], env, module_id) if is_for else None
        if values is not None and not isinstance(values, list):
            raise StepError("Loop iterator must return an array", module_id, "IteratorError")
        if is_for and values is None:
            raise StepError("Loop iterator must return an array", module_id, "IteratorError")
        max_iterations = self.opts["maxIterations"]
        if values is not None and len(values) > max_iterations:
            raise StepError(
                f"Iterator exceeds max-iterations {number_to_string(max_iterations)}", module_id, "LoopExhausted"
            )
        if values is not None:
            limit: int | float = len(values)
        else:
            limit = min(max_iterations, v.get("max_iterations", max_iterations))
        results: list[Any] = []
        broke = False
        carried = clone(env.results)
        for index in range(int(limit)):
            iteration_env = _Env(
                input={**clone(env.input), "iter": {"index": index, "value": clone(values[index]) if values is not None else index}},
                results=clone(env.results) if values is not None else carried,
                previous=clone(env.previous) if values is not None or index == 0 else clone(results[index - 1]),
                error=env.error,
            )
            try:
                iteration = self.sequence(v["modules"], iteration_env, [*path, f"iter:{index}"])
            except StepError as error:
                if not v.get("skip_failures") or error.name == "BudgetExceeded":
                    raise
                iteration = _Outcome(result=None)
            results.append(clone(iteration.result))
            if iteration.stopped or self.stop(module.get("stop_after_if"), iteration_env, module_id, iteration.result):
                broke = True
                break
        if values is None and not broke:
            raise StepError(f"While loop exceeds max-iterations {number_to_string(limit)}", module_id, "LoopExhausted")
        all_iterations = module.get("stop_after_all_iters_if")
        stopped = self.stop(all_iterations, env, module_id, results)
        skipped_after = bool(all_iterations.get("skip_if_stopped", False)) if isinstance(all_iterations, dict) else False
        return _Outcome(result=results, stopped=stopped, skipped_after=skipped_after)

    def module(self, module: dict[str, Any], env: _Env, path: list[str]) -> _Outcome:
        module_id = module["id"]
        retry = module.get("retry")
        retry = retry if isinstance(retry, dict) else None
        delays: list[int | float] = []
        constant = retry.get("constant") if retry else None
        if isinstance(constant, dict):
            delays.extend(constant["seconds"] for _ in range(int(constant["attempts"])))
        exponential = retry.get("exponential") if retry else None
        if isinstance(exponential, dict):
            for _ in range(int(exponential["attempts"])):
                delay = _retry_delay(exponential["multiplier"], exponential["seconds"], len(delays) + 1)
                if delay is None:
                    raise StepError("Retry delay exceeds safe integer", module_id, "BudgetExceeded")
                delays.append(delay)
        attempt = 0
        entered = False
        while True:
            event = self.add_event({
                "module_id": module_id, "type": module["value"]["type"], "path": path,
                "attempt": attempt + 1, "status": "running",
            })
            try:
                skip_if = module.get("skip_if")
                if isinstance(skip_if, dict) and self.predicate(skip_if["expr"], env, module_id):
                    event["status"] = "skipped"
                    self.finish_event(event)
                    return _Outcome(skipped=True)
                if not entered:
                    self.enter(module_id)
                    entered = True
                outcome = self.body(module, env, path, event)
                # Conditions can use result as well as results.<current-id>.
                event["result"] = clone(outcome.result)
                condition_env = replace(env, results={**env.results, module_id: outcome.result})
                if not outcome.stopped and module["value"]["type"] not in LOOP_TYPES:
                    stop_after = module.get("stop_after_if")
                    outcome.stopped = self.stop(stop_after, condition_env, module_id, outcome.result)
                    outcome.skipped_after = outcome.stopped and (
                        bool(stop_after.get("skip_if_stopped", False)) if isinstance(stop_after, dict) else False
                    )
                event["status"] = "success"
                event["result"] = clone(outcome.result)
                if outcome.stopped:
                    event["stopped"] = True
                    if outcome.skipped_after:
                        event["skipped_after"] = True
                self.finish_event(event)
                return outcome
            except NodeUnavailableError:
                raise
            except Exception as cause:  # noqa: BLE001 - JavaScript catches every error here
                error = cause if isinstance(cause, StepError) else StepError(error_message(cause), module_id)
                event["status"] = "error"
                event["error"] = error_value(error)
                # Mapping errors before entering the body still count as an attempt.
                if not entered:
                    self.enter(module_id)
                    entered = True
                retry_allowed = error.name != "BudgetExceeded" and attempt < len(delays)
                retry_if = retry.get("retry_if") if retry else None
                if retry_allowed and isinstance(retry_if, dict):
                    try:
                        retry_allowed = self.predicate(retry_if["expr"], env, module_id, None, error_value(error))
                    except NodeUnavailableError:
                        raise
                    except Exception as retry_error:  # noqa: BLE001
                        event["error"] = error_value(retry_error)
                        self.finish_event(event)
                        raise
                if retry_allowed:
                    event["retry_delay_seconds"] = delays[attempt]
                    attempt += 1
                    self.report["simulated_delay_seconds"] = js_add(
                        self.report["simulated_delay_seconds"], event["retry_delay_seconds"]
                    )
                    self.finish_event(event)
                    continue
                if module.get("continue_on_error") and error.name != "BudgetExceeded":
                    event["continued"] = True
                    self.finish_event(event)
                    return _Outcome(result={"error": error_value(error)})
                self.finish_event(event)
                raise error

    # -- run ---------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        report = self.report
        try:
            outcome = self.sequence(self.flow["value"]["modules"], self.root_env, [])
            report["result"] = clone(outcome.result)
            if outcome.stopped:
                report["stopped"] = True
                if outcome.skipped_after:
                    report["skipped"] = True
        except StepError as error:
            report["status"] = "error"
            report["error"] = error_value(error)
            report["result"] = {"error": report["error"]}
            failure = self.flow["value"].get("failure_module")
            if isinstance(failure, dict) and error.name != "BudgetExceeded":
                failure_env = replace(
                    self.root_env,
                    input={**clone(self.input), "error": report["error"]},
                    error=report["error"],
                    previous=report["result"],
                )
                try:
                    handled = self.sequence([failure], failure_env, ["failure_module"])
                    report["failure_result"] = clone(handled.result)
                except StepError as failure_error:
                    report["failure_error"] = error_value(failure_error)
        # Account after the handler too. Control-flow recovery cannot hide a
        # mock contract violation; keep any original runtime error as primary.
        if self.opts["requireAllMocksUsed"]:
            for module_id, usage in js_items(self.mock_usage):
                if usage["remaining"]:
                    self.mock_errors.append(error_value(StepError(
                        f"Mock sequence for '{module_id}' has {usage['remaining']} unused response(s): "
                        f"consumed {usage['consumed']} of {usage['expected']}",
                        module_id,
                        "MockUnused",
                    )))
        if self.mock_errors:
            report["mock_errors"] = self.mock_errors
            if report["status"] == "ok":
                report["status"] = "error"
                report["error"] = self.mock_errors[0]
                report["result"] = {"error": report["error"]}
        report["coverage"] = {
            "modules": {
                "executed": sorted(self.visited),
                "total": len(self.ids),
                "missed": sorted(module_id for module_id in self.ids if module_id not in self.visited),
            },
            "branches": {
                "executed": sorted(self.selected),
                "total": len(self.branches),
                "missed": sorted(branch for branch in self.branches if branch not in self.selected),
            },
        }
        return report


def execute_flow(flow: Any, input: Any = UNDEFINED, mocks: Any = UNDEFINED, options: Any = UNDEFINED) -> dict[str, Any]:
    """``runFlow`` with JavaScript argument semantics.

    Only an absent argument (UNDEFINED) takes its default; ``None`` is JSON
    ``null`` and is rejected like in the Node.js tool.
    """
    input = {} if input is UNDEFINED else input
    mocks = {} if mocks is UNDEFINED else mocks
    options = {} if options is UNDEFINED else options
    model = validate_flow(flow)
    require_object(input, "input")
    json_value(input, "input")
    validate_mocks(mocks, model.modules)
    opts = resolve_options(options)
    return _Simulation(flow, input, mocks, opts, model).run()


def run_flow(
    flow: dict[str, Any],
    input: dict[str, Any] | None = None,
    mocks: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Simulate ``flow`` with mocked steps and return the assertable report.

    ``None`` means "use the default" (``{}``). Raises :class:`FormatError` for
    invalid input; runtime failures are reported in the returned report.
    """
    return execute_flow(
        flow,
        UNDEFINED if input is None else input,
        UNDEFINED if mocks is None else mocks,
        UNDEFINED if options is None else options,
    )
