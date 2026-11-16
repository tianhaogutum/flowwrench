"""Deterministic OpenAPI 3.0/3.1 → Activepieces piece scaffold generator.

Python port of the Node.js ``openapi2piece`` 0.1.0 tool.  For the same spec it
emits byte-identical TypeScript piece files; unsupported operations are skipped
with reasons in ``report.json``.

Public API (mirrors ``src/index.js``)::

    spec = load_spec("api.yaml")              # or parse_spec(text)
    files, model = render_piece(spec, name="api")
    write_piece(files, "out/piece-api")       # atomic, never overwrites
"""

from __future__ import annotations

from ._js import UNDEFINED
from .generate import DEPENDENCY_OVERRIDES, VERSIONS, RenderedPiece, render_piece, write_piece
from .model import GenerationError, build_model
from .spec import (
    SpecError,
    identifier,
    load_spec,
    normalize_schema,
    parse_spec,
    property_expression,
    property_type,
    resolve_refs,
)

__version__ = "0.1.0"

__all__ = [
    "DEPENDENCY_OVERRIDES",
    "GenerationError",
    "RenderedPiece",
    "SpecError",
    "UNDEFINED",
    "VERSIONS",
    "__version__",
    "build_model",
    "identifier",
    "load_spec",
    "normalize_schema",
    "parse_spec",
    "property_expression",
    "property_type",
    "render_piece",
    "resolve_refs",
    "write_piece",
]
