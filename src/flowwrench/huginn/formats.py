"""Bounded, data-only JSON/YAML input and explicit, non-overwriting output."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

import yaml
from yaml.events import AliasEvent

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_KIT_BYTES = 20 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 64


class KitError(ValueError):
    """Expected invalid input or unsupported operation, safe to display."""


class LocatedValidationError(KitError):
    """A validation error located in its input document, without data values.

    ``pointer`` is a JSON Pointer into the JSON or YAML data model. In-memory
    callers have no source filename; file loaders supply one before sorting.
    """

    def __init__(self, pointer, reason, *, source=None):
        self.source = str(Path(source).absolute()) if source is not None else None
        self.pointer = pointer
        self.reason = reason
        location = f"{self.source}#{pointer}" if self.source is not None else pointer
        super().__init__(f"{location}: {reason}")


class ReferenceValidationError(LocatedValidationError):
    """A graph error at its original edge or endpoint."""


class AgentValidationError(LocatedValidationError):
    """An agent field or substitution error at its original input location."""


def check_path(path: Path) -> None:
    """Reject symlinks, including ancestor components (no implicit redirection)."""
    for part in [path.absolute(), *path.absolute().parents]:
        if part.is_symlink():
            raise KitError(f"symlink paths are not supported: {part}")


def read_text(path: Path) -> str:
    check_path(path)
    if not path.is_file():
        raise KitError(f"expected a regular file: {path}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise KitError(f"file exceeds 5 MiB limit: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise KitError(f"expected UTF-8: {path}") from None


def json_value(value, depth=0, budget=None):
    if budget is None:
        budget = [MAX_NODES]
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise KitError("document exceeds depth/node limits (64 / 100000)")
    if type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise KitError("unpaired Unicode surrogates are not supported")
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            json_value(item, depth + 1, budget)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, item in value.items():
            if any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                raise KitError("unpaired Unicode surrogates in mapping keys are not supported")
            json_value(item, depth + 1, budget)
        return
    raise KitError("only JSON-compatible values and string mapping keys are supported")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise KitError("mapping keys must be strings")
        if key in result:
            raise KitError("duplicate mapping key")
        result[key] = value
    return result


class DataLoader(yaml.SafeLoader):
    # JSON scalar rules: YAML 1.1's yes/no, dates and octal are ordinary strings.
    yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in rules
              if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:int",
                             "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream):
        super().__init__(stream)
        self.depth_count = 0
        self.node_count = 0

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise KitError("YAML aliases are not supported")
        self.depth_count += 1
        self.node_count += 1
        try:
            if self.depth_count > MAX_DEPTH or self.node_count > MAX_NODES:
                raise KitError("YAML exceeds depth/node limits (64 / 100000)")
            return super().compose_node(parent, index)
        finally:
            self.depth_count -= 1

    def construct_mapping(self, node, deep=False):
        return unique_pairs((self.construct_object(key, deep=deep),
                             self.construct_object(value, deep=deep))
                            for key, value in node.value)


DataLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf"))
DataLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789"))
DataLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"),
    list("-0123456789"),
)


def load_data(path: Path, *, json_only=False):
    raw = read_text(path)
    try:
        if json_only:
            def no_constant(_):
                raise KitError("non-finite JSON numbers are not supported")
            data = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=no_constant)
        else:
            data = yaml.load(raw, Loader=DataLoader)
    except json.JSONDecodeError as exc:
        raise KitError(f"invalid JSON at {path}:{exc.lineno}:{exc.colno}") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f":{mark.line + 1}:{mark.column + 1}" if mark else ""
        raise KitError(f"invalid YAML at {path}{where} ({type(exc).__name__})") from None
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, KitError):
            raise
        raise KitError(f"invalid or excessively nested document: {path}") from None
    json_value(data)
    return data


def yaml_text(data) -> str:
    # YAML counts mapping keys as nodes and its root at depth one. Check before
    # emitting so a valid JSON export cannot produce an unreadable oversized kit.
    stack, count = [(data, 1)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise KitError("generated YAML exceeds depth/node limits (64 / 100000)")
        if type(value) is dict:
            stack.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif type(value) is list:
            stack.extend((item, depth + 1) for item in value)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=True, width=100)


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def write_file(path: Path, content: str):
    check_path(path)
    if path.exists():
        raise KitError(f"output already exists (refusing overwrite): {path}")
    if not path.parent.is_dir():
        raise KitError(f"output parent directory must exist: {path.parent}")
    # O_EXCL also protects a dangling symlink. Restrict permissions for rendered vars.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_directory(path: Path, files: dict[str, str]):
    check_path(path)
    if path.exists():
        raise KitError(f"output already exists (refusing overwrite): {path}")
    if not path.parent.is_dir():
        raise KitError(f"output parent directory must exist: {path.parent}")
    stage = Path(tempfile.mkdtemp(prefix=".hsk-", dir=path.parent))
    try:
        (stage / "agents").mkdir()
        for relative, content in files.items():
            # All names are generated internally; still enforce this boundary here.
            item = Path(relative)
            if item.is_absolute() or ".." in item.parts:
                raise KitError("unsafe output filename")
            write_file(stage / item, content)
        if path.exists() or path.is_symlink():
            raise KitError(f"output already exists: {path}")
        stage.rename(path)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
