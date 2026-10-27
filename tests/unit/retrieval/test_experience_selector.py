"""ExperienceSelector：经验检索的选取算子（mem2.0 §2.4）。

重点覆盖：复现要求（可执行条目须 2 个复现作者）、排序分（相关性平方 × 质量
分）、探索名额（归因不足的条目经 ``0.e`` 返回，至多一条）、按能力分组与补满
的顺序、隔离期（7 天内只经探索名额返回）、副本语义（``system_metadata`` 只留
类别与三段、分数保持检索分、不原地改入参）。时钟经 ``now_fn`` 固定，隔离期与
新鲜度边界才有确定性。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.experience import (
    StatsRecord,
    UtilityStore,
    baseline_key,
    encode_payload,
    stats_key,
)
from jiuwen_memory.common.type_def import (
    MemoryTier,
    Modality,
    RecallChannel,
    Scope,
    ScoredMemoryUnit,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory import MemoryUnit
from jiuwen_memory.retrieval.selector_impl.experience_selector import ExperienceSelector
from jiuwen_memory.retrieval.types import RetrievalQuery
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
EXP_SCOPE = Scope(org="acme", space="shared-experience")
# 落盘 30 天前：一切条目默认已出隔离期（7 天），隔离期用例单独覆盖。


def _days_ago(days: float) -> datetime:
    """``NOW`` 往前推 ``days`` 天的 aware 时刻（t_ingest 是 datetime | None）。"""
    return NOW - timedelta(days=days)


def _unit(
    entry_id: str, meta: dict, *, tier: MemoryTier, t_ingest: datetime | None
) -> MemoryUnit:
    return MemoryUnit(
        id=entry_id,
        scope=EXP_SCOPE,
        tier=tier,
        segments=[Segment(content=f"content of {entry_id}", source=Modality.TEXT)],
        temporal=Temporal(t_ingest=t_ingest, t_message=None),
        system_metadata=dict(meta),
    )


def _fact(
    entry_id: str,
    *,
    score: float = 1.0,
    category: str = "cat-a",
    t_ingest: datetime | None = None,
) -> ScoredMemoryUnit:
    """环境事实：不带三段与可执行标记，tier=SEMANTIC。"""
    meta = {
        "memory_type": "experience",
        "pipeline": "default",
        "category": category,
        "strategy_version": "0",
        "author_principal": "system",
    }
    unit = _unit(
        entry_id, meta, tier=MemoryTier.SEMANTIC, t_ingest=t_ingest or _days_ago(30)
    )
    return ScoredMemoryUnit(unit=unit, score=score, channel=RecallChannel.KEYWORD)


def _tip(
    entry_id: str,
    *,
    score: float = 1.0,
    category: str = "cat-a",
    capability: str = "bash",
    # 默认不可执行：不设复现关，其余各组的用例不必逐个种复现记录；
    # TestReproductionGroup 显式传 executable=True 测关本身。
    executable: bool = False,
    t_ingest: datetime | None = None,
) -> ScoredMemoryUnit:
    """条件动作：带三段与可执行标记，tier=PROCEDURAL。"""
    meta = {
        "memory_type": "experience",
        "pipeline": "default",
        "category": category,
        "strategy_version": "0",
        "author_principal": "system",
        "extraction_strategy": "tip",
        "condition": "cond",
        "capability": capability,
        "action": "act",
        "executable": executable,
    }
    unit = _unit(
        entry_id, meta, tier=MemoryTier.PROCEDURAL, t_ingest=t_ingest or _days_ago(30)
    )
    return ScoredMemoryUnit(unit=unit, score=score, channel=RecallChannel.KEYWORD)


def _seed_stats(kv, entry_id: str, **overrides) -> None:
    """种一条统计记录（本应由周期整理 Curator 聚合写入，检索只读）。"""
    fields: dict = dict(covered=0, helpful={}, harmful={}, authors=[], last_positive_at="")
    fields.update(overrides)
    kv.insert(EXP_SCOPE, stats_key(entry_id), encode_payload(asdict(StatsRecord(**fields))))


def _seed_repro(kv, entry_id: str, authors: list[str]) -> None:
    UtilityStore(kv).save_repro_authors(EXP_SCOPE, entry_id, authors)


def _select(kv, candidates, *, top_k: int = 5) -> list[str]:
    selector = ExperienceSelector(kv, now_fn=lambda: NOW)
    result = selector.select(list(candidates), RetrievalQuery(text="q", top_k=top_k))
    return [c.unit.id for c in result]


def _select_full(kv, candidates, *, top_k: int = 5):
    selector = ExperienceSelector(kv, now_fn=lambda: NOW)
    return selector.select(list(candidates), RetrievalQuery(text="q", top_k=top_k))


# ====================================================================== #
# 复现要求
# ====================================================================== #


class TestReproductionGate:
    @staticmethod
    def test_executable_without_reproduction_dropped():
        kv = InMemoryKVStore()
        result = _select(kv, [_tip("t1", executable=True)])
        assert result == [], "可执行条目无复现记录不返回"

    @staticmethod
    def test_executable_with_two_repro_authors_passes():
        kv = InMemoryKVStore()
        _seed_repro(kv, "t1", ["alice", "bob"])
        assert _select(kv, [_tip("t1", executable=True)]) == ["t1"], "复现作者满 2 个放行"

    @staticmethod
    def test_executable_with_stats_authors_passes():
        kv = InMemoryKVStore()
        _seed_stats(kv, "t1", authors=["alice", "bob"])
        assert _select(kv, [_tip("t1", executable=True)]) == ["t1"], (
            "统计记录 authors 满 2 个放行"
        )

    @staticmethod
    def test_executable_with_single_repro_author_dropped():
        kv = InMemoryKVStore()
        _seed_repro(kv, "t1", ["alice"])
        assert _select(kv, [_tip("t1", executable=True)]) == [], "复现作者只有 1 个不放行"

    @staticmethod
    def test_stats_authors_short_falls_back_to_repro():
        """统计记录未满 2 人时回退读复现作者记录（第二个用户复现即放行，不等聚合）。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "t1", authors=["alice"])
        _seed_repro(kv, "t1", ["alice", "bob"])
        assert _select(kv, [_tip("t1", executable=True)]) == ["t1"], "统计不足时以即时记录为准"

    @staticmethod
    def test_non_executable_passes_without_records():
        kv = InMemoryKVStore()
        assert _select(kv, [_fact("f1")]) == ["f1"], "环境事实不设复现关"

    @staticmethod
    def test_non_executable_tip_passes():
        kv = InMemoryKVStore()
        assert _select(kv, [_tip("t1", executable=False)]) == ["t1"], (
            "不可执行的条件动作不设复现关"
        )


# ====================================================================== #
# 排序分与顺序
# ====================================================================== #


class TestRankOrder:
    @staticmethod
    def test_higher_relevance_ranks_first():
        """同组两条：检索分高的在前（相关性平方主导，质量同为基础值）。"""
        kv = InMemoryKVStore()
        result = _select(kv, [_tip("low", score=0.5), _tip("high", score=1.0)])
        assert result == ["high", "low"], "检索分高者在前，低者在补满区"

    @staticmethod
    def test_quality_breaks_relevance_ties():
        """同检索分：有帮助记录的条目质量分高，排前（两条都已验证，不走探索）。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "bad", covered=6, harmful={"external": 6})
        _seed_stats(
            kv,
            "good",
            covered=6,
            helpful={"external": 6},
            last_positive_at=_days_ago(1).isoformat(),
        )
        result = _select(kv, [_tip("bad"), _tip("good")])
        assert result == ["good", "bad"], "质量分高者在前"

    @staticmethod
    def test_zero_scores_do_not_crash():
        """全零检索分：相关性取 1，不抛除零。"""
        kv = InMemoryKVStore()
        result = _select(kv, [_tip("a", score=0.0), _fact("b", score=0.0)])
        assert sorted(result) == ["a", "b"], "零分候选照常返回"

    @staticmethod
    def test_baseline_used_when_no_stats():
        """无统计记录的条目取类别基线；基线高者质量分高、排前。"""
        kv = InMemoryKVStore()
        kv.insert(
            EXP_SCOPE, baseline_key(), encode_payload({"cat-a": 0.1, "cat-b": 0.9})
        )
        result = _select(kv, [_tip("in-b", category="cat-b"), _tip("in-a", category="cat-a")])
        assert result == ["in-b", "in-a"], "高基线类别的条目排前（冷启动按基线）"


# ====================================================================== #
# 探索名额
# ====================================================================== #


class TestExploration:
    @staticmethod
    def test_unproven_best_rank_gets_explore_ref():
        """归因不足的条目中排序分最高的一条经探索名额返回，排最前。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "unproven", covered=3, helpful={"external": 3})
        _seed_stats(kv, "proven", covered=6, helpful={"external": 2})
        result = _select_full(kv, [_tip("proven"), _tip("unproven")])
        assert [c.unit.id for c in result] == ["unproven", "proven"], (
            "探索条目排最前（否则截断时可能被挤掉）"
        )
        assert result[0].unit.system_metadata["usage_ref"] == "0.e", "探索条目的引用值"
        assert result[1].unit.system_metadata["usage_ref"] == "0.n", "其余条目按排序分"

    @staticmethod
    def test_all_proven_no_explore_ref():
        kv = InMemoryKVStore()
        _seed_stats(kv, "a", covered=5)
        _seed_stats(kv, "b", covered=5)
        result = _select_full(kv, [_tip("a"), _tip("b")])
        refs = {c.unit.system_metadata["usage_ref"] for c in result}
        assert refs == {"0.n"}, "全部已验证时无探索名额"

    @staticmethod
    def test_explore_quota_is_one():
        """多个未验证条目也只有 1 条经探索名额返回。"""
        kv = InMemoryKVStore()
        result = _select_full(kv, [_tip("u1"), _tip("u2"), _tip("u3")])
        explore_refs = [
            c for c in result if c.unit.system_metadata["usage_ref"] == "0.e"
        ]
        assert len(explore_refs) == 1, "探索名额至多一条"

    @staticmethod
    def test_explore_picks_highest_rank_among_unproven():
        """未验证条目中挑排序分最高的：有部分归因的高于零归因的。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "some", covered=3, helpful={"external": 3})
        result = _select_full(kv, [_tip("none"), _tip("some")])
        assert result[0].unit.id == "some", "探索名额给未验证中排序分最高的"
        assert result[0].unit.system_metadata["usage_ref"] == "0.e", "该条经探索返回"


# ====================================================================== #
# 按能力分组与补满
# ====================================================================== #


class TestGrouping:
    @staticmethod
    def test_same_capability_only_one_leads():
        """同 (类别, 能力) 的两条：组内最优在前排，另一条落补满区。"""
        kv = InMemoryKVStore()
        result = _select(kv, [_tip("second", score=0.9), _tip("first", score=1.0)])
        assert result == ["first", "second"], "组内最优领先，其余按排序分补满"

    @staticmethod
    def test_different_capabilities_each_lead():
        """不同能力各自成组：两组的最优都进前排。"""
        kv = InMemoryKVStore()
        result = _select(
            kv,
            [
                _tip("bash-2", capability="bash", score=0.9),
                _tip("bash-1", capability="bash", score=1.0),
                _tip("web-2", capability="web", score=0.8),
                _tip("web-1", capability="web", score=0.85),
            ],
        )
        # 探索名额给排序分最高的 bash-1；组间按各组最优的排序分降序（bash-2 >
        # web-1）；组内次优进补满区（web-2）。
        assert result == ["bash-1", "bash-2", "web-1", "web-2"], (
            "探索名额一条 + 各组最优在前 + 补满在尾"
        )

    @staticmethod
    def test_fact_forms_own_group():
        """环境事实不带能力名，与条件动作不同组：各出一条领先。"""
        kv = InMemoryKVStore()
        result = _select(kv, [_tip("tip-2", score=0.9), _fact("fact-1", score=0.5)])
        assert result == ["tip-2", "fact-1"], "环境事实自成一组，不被条件动作挤掉"

    @staticmethod
    def test_facts_of_same_category_form_one_group():
        """同类别多条环境事实同组：只有一条领先，其余补满。"""
        kv = InMemoryKVStore()
        result = _select(kv, [_fact("f2", score=0.9), _fact("f1", score=1.0)])
        assert result == ["f1", "f2"], "同类别环境事实一组，组内最优领先"


# ====================================================================== #
# 隔离期
# ====================================================================== #


class TestIsolation:
    @staticmethod
    def test_isolated_entry_omitted_without_exploration():
        """隔离期内且已验证（无探索资格）的条目完全不出现。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "young", covered=6)
        young = _tip("young", t_ingest=_days_ago(3))
        old = _tip("old", t_ingest=_days_ago(30))
        assert _select(kv, [young, old]) == ["old"], "隔离期条目不参与分组与补满"

    @staticmethod
    def test_isolated_entry_returns_via_exploration():
        """隔离期内的未验证条目仍经探索名额返回。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "young", covered=3, helpful={"external": 3})
        young = _tip("young", t_ingest=_days_ago(3))
        old = _tip("old", t_ingest=_days_ago(30))
        result = _select_full(kv, [young, old])
        assert [c.unit.id for c in result] == ["young", "old"], "隔离条目经探索名额排最前"
        assert result[0].unit.system_metadata["usage_ref"] == "0.e", "标记为探索返回"

    @staticmethod
    def test_isolation_boundary_is_seven_days():
        """落盘恰好满 7 天：已出隔离期，参与分组。"""
        kv = InMemoryKVStore()
        boundary = _tip("boundary", t_ingest=_days_ago(7))
        assert _select(kv, [boundary]) == ["boundary"], "满 7 天即出隔离期"

    @staticmethod
    def test_missing_t_ingest_treated_as_released():
        """落盘时间缺失：按已出隔离期处理，不把条目永久锁死。"""
        kv = InMemoryKVStore()
        no_time = _tip("no-time", t_ingest=None)
        assert _select(kv, [no_time]) == ["no-time"], "缺落盘时间不隔离"


# ====================================================================== #
# 副本语义
# ====================================================================== #


class TestCopySemantics:
    @staticmethod
    def test_tip_metadata_stripped_to_category_segments_and_ref():
        """条件动作副本只留类别、三段与引用值；内部元数据不外发。"""
        kv = InMemoryKVStore()
        _seed_repro(kv, "t1", ["alice", "bob"])
        # 无统计记录 → 未验证 → 经探索名额返回，引用值为 0.e（唯一候选必占名额）。
        result = _select_full(kv, [_tip("t1", executable=True)])
        assert result[0].unit.system_metadata == {
            "category": "cat-a",
            "condition": "cond",
            "capability": "bash",
            "action": "act",
            "usage_ref": "0.e",
        }, "副本 system_metadata 只含类别、三段与引用值"

    @staticmethod
    def test_fact_copy_has_category_and_ref_only():
        kv = InMemoryKVStore()
        result = _select_full(kv, [_fact("f1")])
        assert result[0].unit.system_metadata == {
            "category": "cat-a",
            "usage_ref": "0.e",
        }, "环境事实副本只含类别与引用值（未验证经探索名额）"

    @staticmethod
    def test_score_kept_as_retrieval_score():
        """返回分数保持检索分，不掺质量分（排序分只用于排序）。"""
        kv = InMemoryKVStore()
        _seed_stats(kv, "good", covered=6, helpful={"external": 6})
        result = _select_full(kv, [_tip("good", score=0.42)])
        assert result[0].score == pytest.approx(0.42), "分数仍是检索分"

    @staticmethod
    def test_input_not_mutated():
        """选取不原地改候选：入参的条目不带引用值（存储可能缓存同一对象）。"""
        kv = InMemoryKVStore()
        candidate = _fact("f1")
        _select_full(kv, [candidate])
        assert "usage_ref" not in candidate.unit.system_metadata, "入参条目未被改写"


# ====================================================================== #
# 边界
# ====================================================================== #


class TestBoundaries:
    @staticmethod
    def test_empty_survivors_returns_empty():
        assert _select(InMemoryKVStore(), []) == [], "空候选返回空"

    @staticmethod
    def test_all_dropped_returns_empty():
        kv = InMemoryKVStore()
        dropped = [_tip("t1", executable=True), _tip("t2", executable=True)]
        assert _select(kv, dropped) == [], "全部被复现要求拦下返回空"

    @staticmethod
    def test_scope_taken_from_candidates():
        """效用记录的 scope 取自候选条目（经验空间），无需调用方传。"""
        kv = InMemoryKVStore()
        # 统计种在 EXP_SCOPE（与条目 scope 一致）；scope 不符则读不到、探索名额失效。
        _seed_stats(kv, "t1", covered=3, helpful={"external": 3})
        result = _select_full(kv, [_tip("t1")])
        assert result[0].unit.system_metadata["usage_ref"] == "0.e", "按条目 scope 读到记录"
