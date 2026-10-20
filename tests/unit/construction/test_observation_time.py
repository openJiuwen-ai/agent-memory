"""Relative dates use the matching source's message date in both extractors."""

from datetime import UTC, datetime

import pytest

from jiuwen_memory.construction.extractor_impl.dynamic_llm_extractor import DynamicLLMExtractor
from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl
from jiuwen_memory.construction.extractor_impl.observation_time import observation_date
from tests.unit.construction.test_dynamic_extraction_consolidation import (
    _FallbackExtractor,
    _ScriptedLLM,
    _unit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("dynamic", [False, True])
def test_each_source_has_its_own_date(dynamic):
    # pylint: disable=protected-access  # 白盒验证内部阶段与异常恢复，不扩大生产接口
    first, second = _unit("first", "昨天迁居北京"), _unit("second", "昨天迁居上海")
    first.temporal.t_message = datetime(2026, 5, 1, tzinfo=UTC)
    second.temporal.t_message = datetime(2026, 6, 1, tzinfo=UTC)
    llm = _ScriptedLLM(["[]"])
    if dynamic:
        extractor = DynamicLLMExtractor(llm, _FallbackExtractor())
        extractor._extract_strategy([first, second], None, "test", "extract facts")
    else:
        extractor = ExtractorImpl(llm, max_attempts=1)
        extractor._llm_extract_batch([first, second])
    prompt = llm.messages[0][1].content
    assert "observation_date: 2026-05-01" in prompt
    assert "observation_date: 2026-06-01" in prompt
    assert prompt.index("2026-05-01") < prompt.index("first")
    assert prompt.index("2026-06-01") < prompt.index("second")


def test_explicit_observation_date_wins_and_preserves_day_precision():
    source = _unit("source", "yesterday")
    source.system_metadata["observation_date"] = "2026-04-09T03:30:00+08:00"
    assert observation_date(source) == "2026-04-09"


def test_source_local_calendar_day_is_not_shifted_to_utc():
    source = _unit("source", "yesterday")
    source.temporal.t_message = datetime.fromisoformat("2026-04-09T00:30:00+08:00")
    assert observation_date(source) == "2026-04-09"
