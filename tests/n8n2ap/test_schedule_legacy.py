"""Port of ``test/schedule-legacy.test.js``."""

from __future__ import annotations

from typing import Any

import pytest

from flowwrench.n8n2ap import convert

from .helpers import clone, freeze, oracle
from .schedule_legacy_cases import LEGACY_CASES, assert_legacy_result, legacy_source


@pytest.mark.parametrize("case", LEGACY_CASES, ids=[c["name"] for c in LEGACY_CASES])
def test_legacy_cron_contract(case: dict[str, Any]) -> None:
    source = freeze(legacy_source(case))
    before = clone(source)
    result = convert(source)
    assert_legacy_result(result, case)
    assert convert(source) == result
    assert source == before


def test_pinned_legacy_helper_proves_presets_vary_with_random_seconds_custom_expressions_stay_fixed() -> None:
    cases = oracle("legacy-cron")["cases"]
    for mode in ["everyMinute", "everyHour", "everyDay", "everyWeek", "custom"]:
        samples = [c for c in cases if c["rule"].get("mode") == mode]
        assert len(samples) == (8 if mode == "custom" else 4)
        for c in samples:
            assert c["randomCalls"] == 1
            if mode == "custom":
                assert c["sourceCron"] == c["rule"]["cronExpression"]
            else:
                assert c["sourceCron"].split(" ")[0] == str(c["controlledRandomSecond"])
