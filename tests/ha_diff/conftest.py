import copy
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "upstream"


@pytest.fixture
def testkit():
    return json.loads((FIXTURES / "testkit_trace_sample.json").read_text())


@pytest.fixture
def automation_records():
    return json.loads((FIXTURES / "automation_saved_traces.json").read_text())


@pytest.fixture
def script_records():
    return json.loads((FIXTURES / "script_saved_traces.json").read_text())


@pytest.fixture
def trace(testkit):
    """Real upstream download; callers mutate isolated copies in each test."""
    return copy.deepcopy(testkit["trace"])
