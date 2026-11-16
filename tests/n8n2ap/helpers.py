"""Shared helpers for the ``flowwrench n8n2ap`` tests (port of ``test/helpers.js``).

Two Node-only facilities have Python stand-ins here:

* ``runCode`` imported the converter-generated JavaScript module and ran it.
  :func:`run_code` instead recognises each *exact* generated source string and
  emulates it; an unknown source fails the test, so generated code is pinned.
* Ajv cross-checked templates against the JSON Schema. :func:`schema_valid`
  uses ``jsonschema`` (Draft 7) when installed, else a small independent
  validator written for the keywords our schema uses.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = FIXTURES / "golden"
ORACLES = FIXTURES / "oracles"
SRC = HERE.parents[1] / "src"


# --------------------------------------------------------------------------- data builders


def fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.n8n.json"


def fixture(name: str) -> Any:
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


def mapping_path(name: str = "http-basic") -> Path:
    return FIXTURES / f"{name}.connections.json"


def mapping(name: str = "http-basic") -> Any:
    return json.loads(mapping_path(name).read_text(encoding="utf-8"))


def golden_text(name: str) -> str:
    return (GOLDEN / f"{name}.json").read_text(encoding="utf-8")


def oracle(name: str) -> Any:
    return json.loads((ORACLES / f"{name}.json").read_text(encoding="utf-8"))


def clone(value: Any) -> Any:
    """``structuredClone``."""
    return copy.deepcopy(value)


def js(value: Any) -> str:
    """``JSON.stringify(value)`` for test names and substring checks."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def node(name: str, kind: str, parameters: dict[str, Any] | None = None, type_version: float = 1) -> dict[str, Any]:
    return {"id": name, "name": name, "type": "n8n-nodes-base." + kind, "typeVersion": type_version,
            "parameters": {} if parameters is None else parameters}


def edge(name: str, index: int = 0) -> dict[str, Any]:
    return {"node": name, "type": "main", "index": index}


def workflow(*nodes: dict[str, Any]) -> dict[str, Any]:
    connections: dict[str, Any] = {}
    for current, following in zip(nodes, nodes[1:]):
        connections[current["name"]] = {"main": [[edge(following["name"])]]}
    return {"name": "Synthetic test", "nodes": list(nodes), "connections": connections, "settings": {"timezone": "UTC"}}


def start() -> dict[str, Any]:
    """Graph/mapping tests use an explicit clock; preset defaults have their own suite."""
    return node("Start", "scheduleTrigger", {"rule": {"interval": [{"field": "cronExpression", "expression": "*/5 * * * *"}]}}, 1.2)


def steps(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Pre-order walk: a step, its router children, then its ``nextAction`` chain."""
    result: list[dict[str, Any]] = []
    stack: list[Any] = [template["flows"][0]["trigger"]]
    while stack:
        step = stack.pop()
        if not step:
            continue
        result.append(step)
        stack.append(step.get("nextAction"))
        stack.extend(reversed(step.get("children") or []))
    return result


def index_of(items: list[Any], target: Any) -> int:
    """``Array.prototype.indexOf`` by identity."""
    for index, item in enumerate(items):
        if item is target:
            return index
    return -1


def same(a: Any, b: Any) -> bool:
    """``a === b`` for JSON scalars."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def coalesce(value: Any, default: Any) -> Any:
    return default if value is None else value


# --------------------------------------------------------------------------- frozen inputs


class FrozenDict(dict):  # type: ignore[type-arg]
    """``Object.freeze`` stand-in: any mutation raises."""

    def _blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("Cannot modify a frozen object")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _blocked  # type: ignore[assignment]

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        return {key: copy.deepcopy(value, memo) for key, value in self.items()}


class FrozenList(list):  # type: ignore[type-arg]
    def _blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("Cannot modify a frozen array")

    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = sort = reverse = _blocked  # type: ignore[assignment]
    __iadd__ = __imul__ = _blocked  # type: ignore[assignment]

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return [copy.deepcopy(value, memo) for value in self]


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList([freeze(item) for item in value])
    return value


# --------------------------------------------------------------------------- generated code


class JsError(Exception):
    """A JavaScript ``Error`` thrown by emulated generated code."""


_MISSING: Any = object()

PASS_CODE = "export const code = async (inputs) => inputs.data;"
RESPONSE_GUARD_CODE = ("export const code = async (inputs) => { if (inputs.data === null || typeof inputs.data !== 'object' || "
                       "Array.isArray(inputs.data)) throw new Error('n8n2ap: only single JSON object HTTP responses are "
                       "supported'); return inputs.data; };")
IF_GUARD_CODE = ("export const code = async (inputs) => { for (const entry of inputs.checks) { if (typeof entry.value !== "
                 "entry.type || (entry.type === 'number' && !Number.isFinite(entry.value))) throw new Error('n8n2ap: IF "
                 "operand type mismatch'); } return true; };")
WEBHOOK_GUARD_CODE = ("export const code = async (inputs) => { if (inputs.data?.method !== inputs.method) throw new "
                      "Error('n8n2ap: unexpected or missing webhook method'); return { body: inputs.data.body, headers: "
                      "inputs.data.headers, query: inputs.data.queryParams }; };")


def _set_code(initial: str) -> str:
    return "\n".join([
        "export const code = async (inputs) => {",
        f"  const output = {initial};",
        "  for (const entry of inputs.values) {",
        "    const value = entry.value;",
        "    if (entry.type === 'number' && (typeof value !== 'number' || !Number.isFinite(value))) throw new Error('Set expected number');",
        "    if (entry.type === 'boolean' && typeof value !== 'boolean') throw new Error('Set expected boolean');",
        "    if (entry.type === 'object' && (value === null || typeof value !== 'object' || Array.isArray(value))) throw new Error('Set expected object');",
        "    if (entry.type === 'array' && !Array.isArray(value)) throw new Error('Set expected array');",
        "    output[entry.name] = entry.type === 'string' ? String(value) : value;",
        "  }",
        "  return output;",
        "};",
    ])


SET_CODE = _set_code("{}")
SET_KEEP_CODE = _set_code("{ ...inputs.data }")
_FAIL_CODE = re.compile(r'export const code = async \(\) => \{ throw new Error\(("(?:[^"\\]|\\.)*")\); \};')


def _typeof(value: Any) -> str:
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def _string(value: Any) -> str:
    """JavaScript ``String(value)`` for the simple values the tests use."""
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else _string(item) for item in value)
    return "[object Object]"


def _get(value: Any, key: str) -> Any:
    return value.get(key, _MISSING) if isinstance(value, dict) else _MISSING


def _spread(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return {str(i): item for i, item in enumerate(value)}
    if isinstance(value, str):
        return {str(i): char for i, char in enumerate(value)}
    return {}


def _run_set(inputs: dict[str, Any], keep: bool) -> dict[str, Any]:
    output = _spread(_get(inputs, "data")) if keep else {}
    for entry in inputs["values"]:
        value = _get(entry, "value")
        kind = _get(entry, "type")
        if kind == "number" and (_typeof(value) != "number" or not math.isfinite(value)):
            raise JsError("Set expected number")
        if kind == "boolean" and _typeof(value) != "boolean":
            raise JsError("Set expected boolean")
        if kind == "object" and (value is None or _typeof(value) != "object" or isinstance(value, list)):
            raise JsError("Set expected object")
        if kind == "array" and not isinstance(value, list):
            raise JsError("Set expected array")
        output[entry["name"]] = _string(value) if kind == "string" else value
    return output


def run_code(step: dict[str, Any], inputs: Any = _MISSING) -> Any:
    """Emulate one converter-generated CODE step (never the original n8n jsCode)."""
    code = step["settings"]["sourceCode"]["code"]
    if inputs is _MISSING:
        inputs = step["settings"]["input"]
    if code == PASS_CODE:
        return _get(inputs, "data")
    failure = _FAIL_CODE.fullmatch(code)
    if failure:
        raise JsError(json.loads(failure.group(1)))
    if code == RESPONSE_GUARD_CODE:
        data = _get(inputs, "data")
        if data is None or _typeof(data) != "object" or isinstance(data, list):
            raise JsError("n8n2ap: only single JSON object HTTP responses are supported")
        return data
    if code == IF_GUARD_CODE:
        for entry in inputs["checks"]:
            value = _get(entry, "value")
            kind = _get(entry, "type")
            if _typeof(value) != kind or (kind == "number" and not math.isfinite(value)):
                raise JsError("n8n2ap: IF operand type mismatch")
        return True
    if code == WEBHOOK_GUARD_CODE:
        data = _get(inputs, "data")
        method = _get(data, "method")
        expected = _get(inputs, "method")
        if not (method is expected or (isinstance(method, str) and isinstance(expected, str) and method == expected)):
            raise JsError("n8n2ap: unexpected or missing webhook method")
        return {"body": data.get("body"), "headers": data.get("headers"), "query": data.get("queryParams")}
    if code == SET_CODE:
        return _run_set(inputs, keep=False)
    if code == SET_KEEP_CODE:
        return _run_set(inputs, keep=True)
    raise AssertionError(f"unrecognised generated code: {code!r}")


# --------------------------------------------------------------------------- schema cross-check


def _load_schema() -> dict[str, Any]:
    from flowwrench.n8n2ap.validate import template_schema

    return template_schema


def schema_valid(template: Any) -> bool:
    """Independent JSON Schema check (Ajv in the Node suite)."""
    schema = _load_schema()
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return _MiniValidator(schema).valid(template, schema)
    return bool(jsonschema.Draft7Validator(schema).is_valid(template))


class _MiniValidator:
    """Draft-07 subset: $ref (local), anyOf, const, enum, type, pattern, properties,
    required, additionalProperties, items, minItems and maxItems."""

    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root

    def _resolve(self, ref: str) -> dict[str, Any]:
        target: Any = self.root
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        return target

    @staticmethod
    def _type_ok(value: Any, kind: str) -> bool:
        if kind == "null":
            return value is None
        if kind == "boolean":
            return isinstance(value, bool)
        if kind == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if kind == "string":
            return isinstance(value, str)
        if kind == "array":
            return isinstance(value, list)
        if kind == "object":
            return isinstance(value, dict)
        raise AssertionError(kind)

    def valid(self, value: Any, schema: dict[str, Any]) -> bool:
        if "$ref" in schema:
            return self.valid(value, self._resolve(schema["$ref"]))
        if "anyOf" in schema and not any(self.valid(value, s) for s in schema["anyOf"]):
            return False
        if "const" in schema and not same(value, schema["const"]):
            return False
        if "enum" in schema and not any(same(value, e) for e in schema["enum"]):
            return False
        if "type" in schema and not self._type_ok(value, schema["type"]):
            return False
        if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
            return False
        if isinstance(value, dict) and schema.get("type") == "object":
            if any(key not in value for key in schema.get("required", [])):
                return False
            properties = schema.get("properties", {})
            for key, item in value.items():
                if key in properties:
                    if not self.valid(item, properties[key]):
                        return False
                elif schema.get("additionalProperties") is False:
                    return False
        if isinstance(value, list) and schema.get("type") == "array":
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
                return False
            if "items" in schema and not all(self.valid(item, schema["items"]) for item in value):
                return False
        return True


# --------------------------------------------------------------------------- CLI


@dataclass
class Completed:
    status: int
    stdout: str
    stderr: str


def run_cli(*args: Any, cwd: Path | str | None = None) -> Completed:
    """Spawn ``python -m flowwrench.n8n2ap`` (``node bin/n8n2ap.js`` in the Node suite)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run([sys.executable, "-m", "flowwrench.n8n2ap", *(str(a) for a in args)],
                          capture_output=True, timeout=60, env=env, cwd=cwd)
    return Completed(proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8"))
