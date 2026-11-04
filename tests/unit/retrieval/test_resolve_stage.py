"""resolve 裁决纯函数分支穷举（F10）。

冲突裁决阶梯是确定性的（不靠 LLM），本文件对 ``conflict_winner`` 的每个分支
与 ``adjudicate_resolution`` 的重复/冲突/独立处理做穷举断言。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from jiuwen_memory.common.reranker.base import DuplicateGroup, RerankResolution
from jiuwen_memory.common.reranker.reranker_impl.llm_reranker import (
    adjudicate_resolution,
    conflict_winner,
)
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment, Temporal

pytestmark = pytest.mark.unit


def _unit(
    content: str,
    *,
    tier: MemoryTier = MemoryTier.EPISODIC,
    t_event: datetime | None = None,
    t_ingest: datetime | None = None,
    provenance: list[str] | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=content[:8],
        segments=[Segment(content=content)],
        tier=tier,
        temporal=Temporal(t_event=t_event, t_ingest=t_ingest),
        provenance=list(provenance or []),
    )


# -- conflict_winner 裁决阶梯 ------------------------------------------------- #


def test_t_event_newer_wins() -> None:
    """阶梯 1：双方 t_event 可比，新者胜。"""
    left = _unit("住北京", t_event=datetime(2020, 1, 1))
    right = _unit("搬到上海", t_event=datetime(2021, 1, 1))
    assert conflict_winner(left, right) == 1


def test_t_event_missing_falls_back_to_t_ingest() -> None:
    """阶梯 2：双方 t_event 均缺失，回退 t_ingest，新者胜。"""
    left = _unit("住北京", t_ingest=datetime(2020, 1, 1))
    right = _unit("搬到上海", t_ingest=datetime(2021, 1, 1))
    assert conflict_winner(left, right) == 1


def test_tier_higher_wins_when_time_ties() -> None:
    """阶梯 3：时间判据平局，tier 更高者胜（SEMANTIC > EPISODIC）。"""
    left = _unit("原始片段", tier=MemoryTier.EPISODIC)
    right = _unit("画像事实", tier=MemoryTier.SEMANTIC)
    assert conflict_winner(left, right) == 1


def test_provenance_more_complete_wins_when_tier_ties() -> None:
    """阶梯 4：tier 平局，provenance 更完整者胜（合成记忆优于单源片段）。"""
    left = _unit("单源", tier=MemoryTier.SEMANTIC, provenance=["s1"])
    right = _unit("合成", tier=MemoryTier.SEMANTIC, provenance=["s1", "s2"])
    assert conflict_winner(left, right) == 1


def test_full_tie_keeps_both() -> None:
    """阶梯 5：全平局双保留（不悄悄裁决）。"""
    left = _unit("a")
    right = _unit("b")
    assert conflict_winner(left, right) == -1


# -- adjudicate_resolution ---------------------------------------------------- #


def test_adjudicate_duplicate_cluster_keeps_representative() -> None:
    """duplicate 簇内留代表（score 高者），其余丢弃。"""
    units = [_unit("coffee"), _unit("coffee"), _unit("coffee")]
    scores = [0.9, 0.4, 0.7]
    relations = [
        {"pair": (0, 1), "relation": "duplicate"},
        {"pair": (0, 2), "relation": "duplicate"},
    ]
    resolution = adjudicate_resolution(units, scores, relations)

    assert resolution.kept_indices == [0]
    assert resolution.duplicates == [DuplicateGroup(representative=0, dropped=[1, 2])]
    assert resolution.conflict_dropped == []


def test_adjudicate_conflict_drops_loser_by_time() -> None:
    """conflict 按时间规则裁决，输家进 conflict_dropped。"""
    units = [_unit("住北京", t_event=datetime(2020, 1, 1)),
             _unit("搬到上海", t_event=datetime(2021, 1, 1))]
    scores = [0.5, 0.5]
    relations = [{"pair": (0, 1), "relation": "conflict"}]
    resolution = adjudicate_resolution(units, scores, relations)

    assert resolution.kept_indices == [1]
    assert resolution.conflict_dropped == [0]


def test_adjudicate_conflict_tie_keeps_both() -> None:
    """conflict 平局双保留，不悄悄裁决。"""
    units = [_unit("a"), _unit("b")]
    scores = [0.5, 0.5]
    relations = [{"pair": (0, 1), "relation": "conflict"}]
    resolution = adjudicate_resolution(units, scores, relations)

    assert resolution == RerankResolution.keep_all(2)


def test_adjudicate_independent_keeps_all() -> None:
    """independent 各自保留，不进时间裁决也不丢。"""
    units = [_unit("喜欢爬山", t_event=datetime(2020, 1, 1)),
             _unit("喜欢打球", t_event=datetime(2019, 1, 1))]
    scores = [0.8, 0.8]
    resolution = adjudicate_resolution(units, scores, [])

    assert resolution == RerankResolution.keep_all(2)


def test_adjudicate_multi_relation_implication_keeps_loser() -> None:
    """被多条关系命中的下标视为歧义，保守保护不丢（防单点被多条关系压制）。"""
    units = [
        _unit("new a", t_event=datetime(2025, 1, 1)),
        _unit("old", t_event=datetime(2020, 1, 1)),
        _unit("new b", t_event=datetime(2025, 1, 1)),
    ]
    scores = [0.5, 0.5, 0.5]
    # unit 1 被两条冲突关系命中（都是输家）→ 歧义，保护不丢。
    relations = [
        {"pair": (0, 1), "relation": "conflict"},
        {"pair": (2, 1), "relation": "conflict"},
    ]
    resolution = adjudicate_resolution(units, scores, relations)

    assert resolution.kept_indices == [0, 1, 2]
    assert resolution.conflict_dropped == []
