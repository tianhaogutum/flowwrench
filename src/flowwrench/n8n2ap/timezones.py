"""Time-zone name check standing in for ``new Intl.DateTimeFormat('en', { timeZone })``.

V8/ICU accepts IANA zone and link names case-insensitively (ASCII only) and
throws ``RangeError`` otherwise. Python has no ICU, so the IANA database seen by
:mod:`zoneinfo` (system tzdata or the ``tzdata`` package) is used instead.
"""

from __future__ import annotations

import zoneinfo
from functools import lru_cache

_ALWAYS = ("UTC", "Etc/UTC", "GMT", "Etc/GMT")


def _ascii_upper(text: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in text)


@lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    try:
        names = set(zoneinfo.available_timezones())
    except Exception:  # pragma: no cover - broken tzdata installations
        names = set()
    names.update(_ALWAYS)
    return frozenset(_ascii_upper(name) for name in names)


def is_valid_timezone(name: str) -> bool:
    return bool(name) and _ascii_upper(name) in _zones()
