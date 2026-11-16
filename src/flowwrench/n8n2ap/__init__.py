"""``flowwrench n8n2ap``: offline n8n workflow -> Activepieces template converter.

Python port of the Node tool ``n8n2ap`` 0.1.0. It converts one exported n8n
workflow into an Activepieces flow template plus an honest migration report.
No workflow step or network call is ever executed.
"""

from __future__ import annotations

from .connections import UNSET
from .converter import ConversionResult, convert, report_markdown
from .expressions import translate_expression
from .input import LIMITS, ConversionError, InputError
from .validate import load_template_schema, template_schema, validate_template

__version__ = "0.1.0"

__all__ = [
    "LIMITS",
    "UNSET",
    "ConversionError",
    "ConversionResult",
    "InputError",
    "__version__",
    "convert",
    "load_template_schema",
    "report_markdown",
    "template_schema",
    "translate_expression",
    "validate_template",
]
