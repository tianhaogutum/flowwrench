"""Port of ``test/schedule-custom.test.js``."""

from __future__ import annotations

from typing import Any

import pytest

from flowwrench.n8n2ap import convert

from .helpers import clone, freeze, oracle
from .schedule_custom_cases import CUSTOM_CASES, assert_custom_result, custom_source


@pytest.mark.parametrize("case", CUSTOM_CASES, ids=[c["name"] for c in CUSTOM_CASES])
@pytest.mark.parametrize("kind", ["cron", "scheduleTrigger"])
def test_custom(kind: str, case: dict[str, Any]) -> None:
    source = freeze(custom_source(kind, case))
    before = clone(source)
    result = convert(source)
    assert_custom_result(result, case)
    assert convert(source) == result
    assert source == before


_ORACLE_CASES = oracle("cron-weekdays")["cases"]


@pytest.mark.parametrize("case", _ORACLE_CASES, ids=[c["expression"] for c in _ORACLE_CASES])
def test_pinned_weekday_parser_equivalence(case: dict[str, Any]) -> None:
    for kind in ["cron", "scheduleTrigger"]:
        assert_custom_result(convert(custom_source(kind, case)), {**case, "cron": case["canonical"]})
