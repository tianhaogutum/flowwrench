"""Bounded JSON parser with source spans for lossless edits; never evaluates input."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import tempfile
from dataclasses import dataclass
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 200_000


class InvalidInput(ValueError):
    """Invalid or unsupported local input (CLI exit 2)."""


class Refused(ValueError):
    """Valid input that fails a promotion check (CLI exit 1)."""


@dataclass
class Node:
    value: Any
    start: int
    end: int
    # Object member key positions, in source order.
    keys: dict[str, int] | None = None

    def get(self, key: str) -> Node | None:
        return self.value.get(key) if isinstance(self.value, dict) else None

    def text(self, key: str, required: bool = True) -> str | None:
        n = self.get(key)
        if n is None and not required:
            return None
        if n is None or not isinstance(n.value, str) or not n.value:
            raise InvalidInput(f"expected nonempty string field {key}")
        return n.value

    def plain(self) -> Any:
        if isinstance(self.value, dict):
            return {k: v.plain() for k, v in self.value.items()}
        if isinstance(self.value, list):
            return [v.plain() for v in self.value]
        return self.value


def pointer(path: tuple) -> str:
    return ''.join('/' + str(k).replace('~', '~0').replace('/', '~1') for k in path)


def walk(node: Node, path: tuple = ()):
    yield path, node
    if isinstance(node.value, dict):
        for k, v in node.value.items():
            yield from walk(v, path + (k,))
    elif isinstance(node.value, list):
        for k, v in enumerate(node.value):
            yield from walk(v, path + (k,))


def read_text(path: str | Path) -> str:
    try:
        p = Path(path)
        if not stat.S_ISREG(p.stat().st_mode):
            raise InvalidInput("input must be a regular file")
        with p.open('rb') as f:
            raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidInput(f"file exceeds {MAX_BYTES} bytes")
        return raw.decode('utf-8')
    except (OSError, UnicodeError) as e:
        raise InvalidInput(f"cannot read UTF-8 input ({type(e).__name__})") from e


class Document:
    def __init__(self, text: str):
        if len(text.encode('utf-8')) > MAX_BYTES:
            raise InvalidInput(f"file exceeds {MAX_BYTES} bytes")
        self.text = text
        self.pos = 1 if text.startswith('\ufeff') else 0
        self.count = 0
        self.decoder = json.JSONDecoder()
        try:
            self.root = self._parse(0)
            self._ws()
            if self.pos != len(text):
                self._error()
        except (json.JSONDecodeError, RecursionError, OverflowError, ValueError) as e:
            if isinstance(e, InvalidInput):
                raise
            raise InvalidInput(f"invalid JSON at offset {self.pos}") from e

    @classmethod
    def load(cls, path: str | Path) -> Document:
        return cls(read_text(path))

    def _error(self):
        raise InvalidInput(f"invalid JSON at offset {self.pos}")

    def _ws(self):
        while self.pos < len(self.text) and self.text[self.pos] in ' \t\r\n':
            self.pos += 1

    def _string(self):
        if self.pos >= len(self.text) or self.text[self.pos] != '"':
            self._error()
        value, end = self.decoder.raw_decode(self.text, self.pos)
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise InvalidInput(f"unpaired Unicode surrogate at offset {self.pos}")
        self.pos = end
        return value

    def _parse(self, depth: int) -> Node:
        self.count += 1
        if depth > MAX_DEPTH or self.count > MAX_NODES:
            raise InvalidInput("JSON exceeds depth/node limit")
        self._ws()
        start = self.pos
        if self.pos >= len(self.text):
            self._error()
        ch = self.text[self.pos]
        keys = None
        if ch in '{[':
            obj = ch == '{'
            close = '}' if obj else ']'
            value = {} if obj else []
            keys = {} if obj else None
            self.pos += 1
            self._ws()
            if self.pos < len(self.text) and self.text[self.pos] != close:
                while True:
                    self._ws()
                    key_start = self.pos
                    if obj:
                        key = self._string()
                        if key in value:
                            raise InvalidInput(f"duplicate object key at offset {key_start}")
                        self._ws()
                        if self.pos >= len(self.text) or self.text[self.pos] != ':':
                            self._error()
                        self.pos += 1
                    child = self._parse(depth + 1)
                    if obj:
                        value[key] = child
                        keys[key] = key_start
                    else:
                        value.append(child)
                    self._ws()
                    if self.pos >= len(self.text):
                        self._error()
                    if self.text[self.pos] == close:
                        break
                    if self.text[self.pos] != ',':
                        self._error()
                    self.pos += 1
            if self.pos >= len(self.text) or self.text[self.pos] != close:
                self._error()
            self.pos += 1
        elif ch == '"':
            value = self._string()
        else:
            value, self.pos = self.decoder.raw_decode(self.text, self.pos)
            if isinstance(value, float) and not math.isfinite(value):
                self._error()
        return Node(value, start, self.pos, keys)

    def raw_positions(self, node: Node, positions: set[int]) -> dict[int, int]:
        """Translate only requested decoded offsets, in one pass per string token."""
        p = node.start + 1
        decoded = 0
        result = {}
        limit = max(positions)
        while decoded <= limit:
            if decoded in positions:
                result[decoded] = p
            if decoded == limit:
                break
            if self.text[p] == '\\':
                if self.text[p + 1] == 'u':
                    code = int(self.text[p + 2:p + 6], 16)
                    p += 6
                    if 0xD800 <= code <= 0xDBFF:
                        p += 6
                else:
                    p += 2
            else:
                p += 1
            decoded += 1
        return result

    def edited(self, edits: list[tuple[int, int, str]]) -> str:
        parts = []
        pos = 0
        for start, end, replacement in sorted(edits):
            if start < pos or end < start:
                raise InvalidInput("overlapping source edits")
            parts.extend((self.text[pos:start], replacement))
            pos = end
        parts.append(self.text[pos:])
        return ''.join(parts)


def atomic_write(path: str | Path, text: str, protected: tuple[str | Path, ...] = ()):
    """Write only the explicit destination; never derive a path from JSON values."""
    p = Path(path).absolute()
    if any(p.resolve() == Path(x).resolve() for x in protected):
        raise InvalidInput("output must differ from input/catalog/map files")
    for part in (p, *p.parents):
        if part.is_symlink():
            raise InvalidInput("output path must not contain symlinks")
    if p.exists() and not p.is_file():
        raise InvalidInput("output must be a regular file")
    # Parents must exist: typos never create a directory tree.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix='.ap-flow-', dir=p.parent,
                                         delete=False) as f:
            temp = f.name
            f.write(text.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, p)
    except OSError as e:
        raise InvalidInput(f"cannot write output ({type(e).__name__})") from e
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
