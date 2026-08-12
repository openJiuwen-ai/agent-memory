"""回归测试：infer=true 语义派生 MemoryUnit 必须设置 t_valid。

问题：创建 infer=true 语义派生 MemoryUnit 时漏设 temporal.t_valid，
导致派生 t_valid=None 被 _valid_at 当作「无下界/始终有效」，
as_of 时间回溯误命中、版本排序排到最早的版本。
"""

import json
import time
from datetime import datetime, timedelta, timezone

from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl
from jiuwen_memory.control.engine_impl.in_memory_engine import _valid_at, _valid_sort_key
from tests.unit.construction.fixtures import MockLLM


def _make_extractor(llm_responses: list[str] | None = None) -> ExtractorImpl:
    return ExtractorImpl(
        llm=MockLLM(responses=llm_responses),
        min_confidence=0.0,
        retry_max_retries=1,
        retry_backoff_ms=1,
    )


def _create_source_unit(unit_id: str, content: str) -> MemoryUnit:
    """创建正确设置 temporal 字段的源 MemoryUnit（模拟 ingestor 输出）。"""
    now = datetime.now(timezone.utc)
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="test", user="alice"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content, source="text")],
        temporal=Temporal(
            t_event=now,
            t_ingest=now,
            t_valid=now,
        ),
        metadata={"infer": "true"},
    )


def test_infer_true_derived_semantic_unit_must_set_t_valid():
    """回归：infer=true 语义派生 MemoryUnit 漏设 t_valid 导致 as_of 误命中与版本排序错误。

    问题：创建 infer=true 语义派生 MemoryUnit 时漏设 temporal.t_valid，
    使 t_valid=None 被 _valid_at 当作「无下界/始终有效」，造成两个后果：
    1. as_of 时间回溯误命中（派生创建前不该命中却命中）
    2. _valid_sort_key 返回 datetime.min，版本排序排到最早位置
    """
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "tier": "semantic",
                        "content": "Alice 喜欢喝咖啡",
                        "evidence": "喜欢喝咖啡",
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    source = _create_source_unit("u1", "Alice 说她喜欢喝咖啡")

    time.sleep(0.01)
    before_extract = datetime.now(timezone.utc)
    result = extractor.extract([source])
    after_extract = datetime.now(timezone.utc)

    assert len(result) == 1
    derived = result[0]

    assert derived.temporal.t_valid is not None, "派生单元必须设置 t_valid"
    assert before_extract <= derived.temporal.t_valid <= after_extract, (
        f"t_valid 应为派生创建时间，before={before_extract}, "
        f"t_valid={derived.temporal.t_valid}, after={after_extract}"
    )

    one_hour_before = derived.temporal.t_valid - timedelta(hours=1)
    assert _valid_at(derived, one_hour_before) is False, "回溯到派生创建前不应命中"

    assert _valid_at(derived, derived.temporal.t_valid) is True, "创建时刻应该命中"
    assert _valid_at(derived, datetime.now(timezone.utc)) is True, "当前时刻应该命中"

    sort_key = _valid_sort_key(derived)
    epoch_min = datetime.min.replace(tzinfo=timezone.utc)
    assert sort_key > epoch_min, "排序key不能是datetime.min"
    assert (sort_key - derived.temporal.t_valid) < timedelta(seconds=1), "排序key应接近t_valid"
