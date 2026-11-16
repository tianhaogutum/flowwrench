"""Read-only DOT projection of a concrete GUID graph; no runtime evaluation."""
from __future__ import annotations

import json

from .core import EDGE_FIELDS, required_variables, validate
from .formats import KitError, MAX_FILE_BYTES


def display_text(value):
    """Escape text for a DOT HTML-like label, never interpreting it as markup.

    Numeric references retain Unicode scalars without relying on DOT's quoted
    string escape dialect. Controls and line separators get visible code-point
    markers instead of invalid XML characters or implicit label line breaks.
    """
    parts = []
    for char in value:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029):
            parts.append(f"[U+{code:04X}]")
        elif code > 0x7E or char in "\"'\\&<>":
            parts.append(f"&#x{code:x};")
        else:
            parts.append(char)
    return "".join(parts)


def dot(doc):
    """Return deterministic DOT after full concrete structural validation.

    Unknown string schedules are unevaluated. Only top-level event/control
    links contribute edges; all other fields except display/disabled are omitted.
    The input is never mutated. GUID validation makes quoted JSON GUIDs valid
    DOT identifiers; HTML-like labels keep display data out of DOT syntax.
    """
    validate(doc, check_schedules=False)
    variables = required_variables(doc)
    if variables:
        raise KitError("unresolved variables (use render): " + ", ".join(variables))
    lines = ["digraph scenario {"]
    for agent in sorted(doc["agents"], key=lambda item: item["guid"]):
        label = display_text(agent["name"]) + "<BR/>" + display_text(agent["type"])
        if agent["disabled"]:
            label += "<BR/>(disabled)"
        attributes = f"label=<{label}>"
        if agent["disabled"]:
            attributes += ', style="dashed"'
        lines.append(f'  {json.dumps(agent["guid"])} [{attributes}];')
    for kind, style, label in (("links", "solid", "event"),
                               ("control_links", "dashed", "control")):
        source, target = EDGE_FIELDS[kind]
        for edge in sorted(doc[kind], key=lambda item: (item[source], item[target])):
            lines.append(f'  {json.dumps(edge[source])} -> {json.dumps(edge[target])} '
                         f'[style="{style}", label="{label}"];')
    lines.append("}")
    output = "\n".join(lines) + "\n"
    if len(output.encode("utf-8")) > MAX_FILE_BYTES:
        raise KitError("generated DOT exceeds 5 MiB file limit")
    return output
