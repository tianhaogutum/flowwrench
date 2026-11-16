"""Read only Windmill's top-level scalar metadata. This is NOT a YAML loader.

Nested args/unknown fields are ignored; selected fields reject tags, aliases,
anchors, collections and block scalars. No constructors or evaluation exist.
"""
import json
import re
from .model import InputError

FIELDS = {'schedule', 'cron', 'timezone', 'enabled', 'script_path', 'path'}


def strip_comment(value):
    quote = None
    escaped = False
    for i, c in enumerate(value):
        if quote == '"' and c == '\\' and not escaped:
            escaped = True
            continue
        if c in "\"'" and not escaped:
            if quote is None:
                quote = c
            elif quote == c:
                quote = None
        if c == '#' and quote is None and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
        escaped = False
    return value.strip()


def scalar(value):
    value = strip_comment(value.strip())
    if not value:
        raise InputError('Windmill metadata must have a scalar value')
    if value.startswith('"'):
        try:
            result = json.loads(value)
        except (ValueError, RecursionError):
            raise InputError('invalid double-quoted YAML scalar; use JSON string escapes') from None
        if not isinstance(result, str):
            raise InputError('expected a string scalar')
        return result
    if value.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", value):
            raise InputError('invalid single-quoted YAML scalar')
        return value[1:-1].replace("''", "'")
    if value.startswith(('!', '&', '*', '|', '>', '[', '{', '?', '`', '@')):
        raise InputError('unsupported YAML scalar syntax; quote cron strings')
    if value in ('true', 'false'):
        return value == 'true'
    if value in ('null', '~'):
        return None
    return value


def load_schedule(text):
    result, seen = {}, set()
    started, ended = False, False
    for line in text.splitlines():
        if '\t' in line[:len(line) - len(line.lstrip())]:
            raise InputError('tabs in YAML indentation are unsupported')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line == '---':
            if started or ended:
                raise InputError('multiple YAML documents are unsupported')
            started = True
            continue
        if line == '...':
            ended = True
            continue
        if ended:
            raise InputError('content after YAML document end')
        started = True
        if line[0].isspace():
            continue
        m = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?', line)
        if not m:
            raise InputError('expected a top-level YAML mapping with simple keys')
        key, value = m.groups()
        if key in seen:
            raise InputError('duplicate YAML key')
        seen.add(key)
        if key in FIELDS:
            result[key] = scalar(value or '')
    return result
