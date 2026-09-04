"""EntityIndexAdmissionPolicy 单元测试。

对应提交 093cd82c：实体索引准入策略。原模块按 strategy_type 判定，当前工程
改用 ``MemoryTier``——准入选 A：SEMANTIC / CORE / EPISODIC 准入（含具体实体），
WORKING / ARCHIVAL 跳过。全走公开 API（``decide``），不访问受保护成员。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import (
    EntityIndexAdmissionPolicy,
)

pytestmark = pytest.mark.unit


def _unit(tier: MemoryTier, content: str = "Alice works at Acme") -> MemoryUnit:
    return MemoryUnit(
        id="u1",
        scope=Scope(org="o1", user="u1", agent="a1"),
        tier=tier,
        segments=[Segment(content=content)],
    )


@pytest.mark.parametrize(
    "tier",
    [MemoryTier.SEMANTIC, MemoryTier.CORE, MemoryTier.EPISODIC],
)
def test_admitted_tiers_pass(tier: MemoryTier) -> None:
    policy = EntityIndexAdmissionPolicy()
    result = policy.decide(_unit(tier))
    assert result.admitted is True
    assert result.text == "Alice works at Acme"


@pytest.mark.parametrize(
    "tier",
    [MemoryTier.WORKING, MemoryTier.ARCHIVAL],
)
def test_non_admitted_tiers_skipped(tier: MemoryTier) -> None:
    policy = EntityIndexAdmissionPolicy()
    result = policy.decide(_unit(tier))
    assert result.admitted is False
    assert "tier_not_entity_indexed" in result.reason
    assert tier.value in result.reason


def test_empty_content_not_admitted() -> None:
    policy = EntityIndexAdmissionPolicy()
    # content 来自 segments 折叠；空 segments → content 为空串
    unit = MemoryUnit(
        id="u2",
        scope=Scope(org="o1"),
        tier=MemoryTier.SEMANTIC,
        segments=[],
    )
    result = policy.decide(unit)
    assert result.admitted is False
    assert result.reason == "empty_content"


def test_whitespace_only_content_not_admitted() -> None:
    policy = EntityIndexAdmissionPolicy()
    result = policy.decide(_unit(MemoryTier.SEMANTIC, content="   "))
    assert result.admitted is False
    assert result.reason == "empty_content"


def test_admission_uses_unit_content_not_raw_segment() -> None:
    # text 字段取的是 unit.content（已折叠 segments），跳过 project_memory_text
    policy = EntityIndexAdmissionPolicy()
    unit = MemoryUnit(
        id="u3",
        scope=Scope(org="o1"),
        tier=MemoryTier.CORE,
        segments=[Segment(content="first part"), Segment(content="second part")],
    )
    result = policy.decide(unit)
    assert result.admitted is True
    assert result.text == "first part\nsecond part"


def test_admission_is_deterministic() -> None:
    # 同输入两次判定结果一致（纯函数，无状态）
    policy = EntityIndexAdmissionPolicy()
    unit = _unit(MemoryTier.SEMANTIC)
    assert policy.decide(unit) == policy.decide(unit)
