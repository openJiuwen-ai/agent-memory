"""ExperienceCurator 的周期整理测试（mem2.0 §2.5.2）。

覆盖：入口筛（生效的环境事实与条件动作）、第 1 步聚合与 supersedes /
merged_into 重定向、第 2/3 步退闲置与有害退休（含置信下界）、第 4 步合并
与删除（三段不全退休、聚簇分桶、三结论、合并条目构造与统计继承、脱敏
检查、簇级容错、提示词可替换）、第 5 步竞争组淘汰、第 6 步控规模（500/100
的「其中」配额）、第 8 步类别计数覆写、运行摘要与失败隔离、以及
OrchestratingEvolver 的 CONSOLIDATE 接线（升华之前先整理、未配置不变）。

模型与向量化用替身驱动：ScriptedLLM 按序返回预置 JSON 结论，TagEmbedder
按正文中的簇标签产 one-hot 向量（同标签余弦 1、异标签 0）——聚簇结果可
由测试文本直接控制。
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.experience import (
    ATTR_HARMFUL,
    ATTR_HELPFUL,
    EVIDENCE_EXTERNAL,
    AttributionEvent,
    StatsRecord,
    UtilityStore,
)
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    memory_key,
)
from jiuwen_memory.common.type_def.memory import LifecycleState, Temporal
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.abstractor import Abstractor
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.curator import Curator
from jiuwen_memory.construction.curator_impl.experience_curator import (
    CATEGORY_CAP,
    CONFIDENCE_Z,
    HARMFUL_ATTRIBUTION_MIN,
    HARMFUL_CONFIDENCE,
    IDLE_RETIRE_DAYS,
    MIN_AGE_DAYS,
    RUN_SUMMARY_TAG,
    UNVERIFIED_CAP,
    ExperienceCurator,
    _difference_lower_bound,
    _wilson_lower,
)
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
)
from jiuwen_memory.construction.prompt_registry import PromptRegistry
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.conftest import make_storage

pytestmark = pytest.mark.unit

EXP_SCOPE = Scope(org="acme", space="shared-experience")


# ---------------------------------------------------------------------------
# 轻量替身与构造
# ---------------------------------------------------------------------------


class RecordingIndexBuilder:
    """IndexBuilder 替身：记录 build / update / remove 的调用与模式。"""

    def __init__(self, *, update_error: Exception | None = None) -> None:
        self.built: list[tuple[list[MemoryUnit], IndexWriteMode]] = []
        self.updated: list[tuple[list[MemoryUnit], IndexWriteMode]] = []
        self.removed: list[tuple[list[MemoryUnit], IndexRemoveMode]] = []
        self._update_error = update_error

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.INDEX_BUILDER

    @staticmethod
    def health() -> None:
        return None

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.built.append((list(units), mode))

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        if self._update_error is not None:
            raise self._update_error
        self.updated.append((list(units), mode))

    def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD):
        self.removed.append((list(units), mode))

    @staticmethod
    def rebuild():
        pass


class BrokenScanUtility(UtilityStore):
    """scan_events 抛异常的 UtilityStore（第 1 步失败场景）。"""

    @staticmethod
    def scan_events(scope):
        raise RuntimeError("scan boom")


class BrokenErasedUtility(UtilityStore):
    """load_erased_authors 抛异常的 UtilityStore（第 0 步失败场景）。"""

    @staticmethod
    def load_erased_authors(scope):
        raise RuntimeError("erased boom")


class ScriptedLLM(LLM):
    """按序返回预置响应；记录每次收到的 messages 与生成参数。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list, dict]] = []

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.LLM

    @staticmethod
    def health() -> None:
        return None

    def chat(self, messages, **options):
        self.calls.append((list(messages), dict(options)))
        if not self._responses:
            raise AssertionError("ScriptedLLM: no scripted response left")
        return self._responses.pop(0)


class TagEmbedder(Embedder):
    """按正文中的簇标签产 one-hot 向量：同标签余弦 1、异标签 0。

    正文不含任何标签时落到第一维——与 clusterA 同簇，用于「单条无伙伴」
    场景之外的兜底。
    """

    TAGS = ("clusterA", "clusterB", "clusterC")

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.EMBEDDER

    @staticmethod
    def health() -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def dimension(self) -> int:
        return len(self.TAGS)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * len(self.TAGS)
        for i, tag in enumerate(self.TAGS):
            if tag in text:
                vector[i] = 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector


def _make_curator(
    *, index: RecordingIndexBuilder | None = None, utility: UtilityStore | None = None,
    **overrides,
) -> tuple[ExperienceCurator, RecordingIndexBuilder, UtilityStore]:
    kv = InMemoryKVStore()
    builder = index or RecordingIndexBuilder()
    store = utility or UtilityStore(kv)
    defaults = dict(
        index_builder=builder,
        utility=store,
    )
    defaults.update(overrides)
    return ExperienceCurator(**defaults), builder, store


def _entry(
    entry_id: str,
    *,
    category: str = "deploy",
    tier: MemoryTier = MemoryTier.SEMANTIC,
    age_days: float = 5,
    supersedes: str = "",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    content: str = "",
    metadata: dict | None = None,
) -> MemoryUnit:
    now = datetime.now(UTC)
    # memory_type=experience：入口筛按它等值过滤（v3 §2.3 隔离规则——
    # 经验与对话、技能可能同空间，非经验条目不参与整理）。
    system_metadata = {"category": category, "memory_type": "experience"}
    system_metadata.update(metadata or {})
    return MemoryUnit(
        id=entry_id,
        scope=EXP_SCOPE,
        tier=tier,
        lifecycle=lifecycle,
        segments=[Segment(content=content or f"条目 {entry_id}")],
        temporal=Temporal(
            t_ingest=now - timedelta(days=age_days),
            t_valid=now - timedelta(days=age_days),
        ),
        supersedes=supersedes,
        system_metadata=system_metadata,
    )


def _tip(
    entry_id: str,
    *,
    condition: str = "服务启动找不到配置",
    capability: str = "cp 命令",
    action: str = "复制示例配置",
    category: str = "deploy",
    age_days: float = 5,
    cluster: str = "clusterA",
) -> MemoryUnit:
    """三段齐全的条件动作（cluster 标签写进正文，控制 TagEmbedder 的聚簇）。"""
    return _entry(
        entry_id,
        tier=MemoryTier.PROCEDURAL,
        category=category,
        age_days=age_days,
        content=f"{cluster} {condition} {action}",
        metadata={"condition": condition, "capability": capability, "action": action},
    )


def _write_attr(
    store: UtilityStore,
    entry_id: str,
    author: str,
    trajectory_id: str,
    verdict: str,
    *,
    evidence: str = EVIDENCE_EXTERNAL,
    days_ago: float = 1,
) -> None:
    occurred = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    store.write_attribution_event(
        EXP_SCOPE,
        entry_id,
        author,
        trajectory_id,
        0,
        AttributionEvent(
            verdict=verdict, evidence=evidence, author=author, occurred_at=occurred
        ),
    )


# ---------------------------------------------------------------------------
# 首版取值（§2.5.2 取值表）
# ---------------------------------------------------------------------------


def test_first_version_defaults() -> None:
    """首版取值改动应连着 features 文档与写策略一起改。"""
    assert (IDLE_RETIRE_DAYS, MIN_AGE_DAYS) == (60.0, 14.0), "闲置退休与最短存活期"
    assert (HARMFUL_ATTRIBUTION_MIN, HARMFUL_CONFIDENCE) == (5, 0.5), "有害退休门槛"
    assert math.isclose(CONFIDENCE_Z, 1.96), "95% 置信区间"
    assert (CATEGORY_CAP, UNVERIFIED_CAP) == (500, 100), "类别上限与其中归因不足上限"


class TestWilsonMath:
    @staticmethod
    def test_wilson_lower_known_value():
        # 10/11 有害：Wilson 下界 ≈ 0.623 > 0.5（手算可复核的一组数）
        assert _wilson_lower(10.0, 11.0, 1.96) == pytest.approx(0.6226, abs=0.01)

    @staticmethod
    def test_wilson_lower_zero_sample():
        assert math.isclose(_wilson_lower(0.0, 0.0, 1.96), 0.0, abs_tol=1e-12), (
            "无样本返回 0（不触发退休）"
        )

    @staticmethod
    def test_difference_lower_bound_positive():
        assert _difference_lower_bound(0.9, 50.0, 0.2, 50.0, 1.96) > 0, "差距显著"
        assert _difference_lower_bound(0.9, 50.0, 0.85, 50.0, 1.96) < 0, "差距不显著"


# ---------------------------------------------------------------------------
# 入口筛
# ---------------------------------------------------------------------------


class TestEntryFilter:
    @staticmethod
    def test_only_active_semantic_procedural():
        units = [
            _entry("fact", tier=MemoryTier.SEMANTIC),
            _entry("tip", tier=MemoryTier.PROCEDURAL),
            _entry("gone", lifecycle=LifecycleState.FORGOTTEN),
            _entry("summary", tier=MemoryTier.EPISODIC),
            _entry("working", tier=MemoryTier.WORKING),
        ]
        kept = _entry_filter(units)
        assert [u.id for u in kept] == ["fact", "tip"], (
            "只有生效的环境事实与条件动作参与整理"
        )

    @staticmethod
    def test_run_summary_not_touched():
        """运行摘要（EPISODIC）混入下轮读入：被入口筛跳过，不进统计与类别计数。"""
        summary_unit = MemoryUnit(
            id="old-summary",
            scope=EXP_SCOPE,
            tier=MemoryTier.EPISODIC,
            tags=[RUN_SUMMARY_TAG],
            segments=[Segment(content="{}")],
        )
        curator, _index, _store = _make_curator()
        kept = curator.curate([summary_unit, _entry("fact")])
        assert [u.id for u in kept] == ["fact"], "运行摘要被跳过"


# ---------------------------------------------------------------------------
# 第 1 步 聚合
# ---------------------------------------------------------------------------


class TestCurateAggregate:
    @staticmethod
    def test_stats_and_baseline_written():
        curator, _index, store = _make_curator()
        _write_attr(store, "e1", "a1", "t1", ATTR_HELPFUL)
        _write_attr(store, "e1", "a2", "t2", ATTR_HELPFUL)

        kept = curator.curate([_entry("e1")])

        assert [u.id for u in kept] == ["e1"], "有取用的条目留下"
        record = store.load_stats(EXP_SCOPE, "e1")
        assert record is not None and record.covered == 2, "统计记录写回经验空间"
        assert store.load_baseline(EXP_SCOPE).get("deploy", 0) > 0, "类别基线写回"

    @staticmethod
    def test_supersedes_events_redirected():
        curator, _index, store = _make_curator()
        _write_attr(store, "old-1", "a1", "t1", ATTR_HELPFUL)
        replacement = _entry("new-1", age_days=1, supersedes="old-1")

        curator.curate([replacement, _entry("old-1", age_days=30)])

        record = store.load_stats(EXP_SCOPE, "new-1")
        assert record is not None and record.covered == 1, (
            "被取代条目的事件计入取代它的新条目"
        )

    @staticmethod
    def test_aggregate_failure_aborts_round():
        curator, index, _store = _make_curator(utility=BrokenScanUtility(InMemoryKVStore()))

        kept = curator.curate([_entry("e1")])

        assert kept == [], "第 1 步失败：返回空列表，本轮不归纳"
        summary = _last_summary(index)
        assert summary["failed_steps"][0]["step"] == "aggregate", "失败步骤记入运行摘要"

    @staticmethod
    def test_prior_competition_group_carried():
        """prior 的 competition_group 经聚合带过，供第 5 步使用。"""
        curator, _index, store = _make_curator()
        store.save_stats(EXP_SCOPE, "e1", StatsRecord(competition_group="g-1"))
        _write_attr(store, "e1", "a1", "t1", ATTR_HELPFUL)

        curator.curate([_entry("e1")])

        assert store.load_stats(EXP_SCOPE, "e1").competition_group == "g-1", (
            "竞争组从 prior 带过（聚合不产生它）"
        )

    @staticmethod
    def test_author_windowing_counts_once_within_window():
        """user 口径（缺省）按作者限计：同作者窗内两条有害归因只计一次。"""
        curator, _index, store = _make_curator()
        _write_attr(store, "e1", "a1", "t1", ATTR_HARMFUL)
        _write_attr(store, "e1", "a1", "t2", ATTR_HARMFUL)

        curator.curate([_entry("e1")])

        record = store.load_stats(EXP_SCOPE, "e1")
        assert record is not None and record.covered == 1, (
            "同一作者时间窗内的重复归因只计一次（按用户数复现的口径）"
        )

    @staticmethod
    def test_trajectory_space_disables_author_windowing():
        """trajectory 口径的空间不按作者限计：同作者两条各计（按轨迹数复现）。"""
        curator, _index, store = _make_curator(
            trajectory_spaces=frozenset({EXP_SCOPE.space})
        )
        _write_attr(store, "e1", "a1", "t1", ATTR_HARMFUL)
        _write_attr(store, "e1", "a1", "t2", ATTR_HARMFUL)

        curator.curate([_entry("e1")])

        record = store.load_stats(EXP_SCOPE, "e1")
        assert record is not None and record.covered == 2, (
            "trajectory 口径关闭作者限计，两条轨迹各计一次"
        )


# ---------------------------------------------------------------------------
# 第 2 步 退闲置
# ---------------------------------------------------------------------------


class TestRetireIdle:
    @staticmethod
    def test_idle_beyond_threshold_retired():
        curator, index, _store = _make_curator()
        units = [_entry("old", age_days=80), _entry("new", age_days=5)]

        kept = curator.curate(units)

        assert [u.id for u in kept] == ["new"], "80 天无取用且满 14 天的退休"
        assert units[0].lifecycle == LifecycleState.FORGOTTEN, "退休标记 FORGOTTEN"
        assert index.updated and index.updated[0][1] == IndexWriteMode.FORWARD_ONLY, (
            "只回写本体新状态"
        )
        assert index.removed and index.removed[0][1] == IndexRemoveMode.SOFT, (
            "检索索引移出检索（条目与统计保留）"
        )

    @staticmethod
    def test_within_idle_window_kept():
        curator, _index, _store = _make_curator()
        kept = curator.curate([_entry("mid", age_days=30)])
        assert [u.id for u in kept] == ["mid"], "30 天未取用未到 60 天不退休"

    @staticmethod
    def test_recently_used_kept():
        curator, _index, store = _make_curator()
        _write_attr(store, "e1", "a1", "t1", ATTR_HELPFUL, days_ago=3)
        kept = curator.curate([_entry("e1", age_days=80)])
        assert [u.id for u in kept] == ["e1"], "落盘 80 天但 3 天前刚取用的不退休"


# ---------------------------------------------------------------------------
# 第 3 步 有害退休
# ---------------------------------------------------------------------------


class TestRetireHarmful:
    @staticmethod
    def _heavy_harmful(store: UtilityStore, entry_id: str) -> None:
        # 10/11 有害：Wilson 下界 ≈ 0.623 > 0.5（6/7 只有 ≈ 0.487，不过线）。
        for i in range(10):
            _write_attr(store, entry_id, f"author-{i}", f"t-{i}", ATTR_HARMFUL)
        _write_attr(store, entry_id, "author-9x", "t-9x", ATTR_HELPFUL)

    def test_harmful_majority_retired(self):
        curator, _index, store = _make_curator()
        self._heavy_harmful(store, "bad")
        _write_attr(store, "good", "a1", "t1", "helpful")

        kept = curator.curate([_entry("bad", age_days=5), _entry("good", age_days=5)])

        assert [u.id for u in kept] == ["good"], (
            "归因满 5 次且有害率置信下界超 0.5 的退休"
        )

    @staticmethod
    def test_insufficient_attribution_kept():
        curator, _index, store = _make_curator()
        for i in range(4):  # 只 4 次，未到 5 次下限
            _write_attr(store, "few", f"author-{i}", f"t-{i}", ATTR_HARMFUL)

        kept = curator.curate([_entry("few", age_days=5)])

        assert [u.id for u in kept] == ["few"], "归因不足 5 次不判有害退休"

    def test_confidence_threshold_moves_boundary(self):
        """同一份数据：阈值调到 0.99 时不退休（阈值是写策略参数）。"""
        curator, _index, store = _make_curator(harmful_confidence=0.99)
        self._heavy_harmful(store, "bad")

        kept = curator.curate([_entry("bad", age_days=5)])

        assert [u.id for u in kept] == ["bad"], "置信下界未过阈值不退休"

    @staticmethod
    def test_helpful_majority_kept():
        curator, _index, store = _make_curator()
        for i in range(6):
            _write_attr(store, "e1", f"author-{i}", f"t-{i}", ATTR_HELPFUL)
        _write_attr(store, "e1", "author-9", "t-9", ATTR_HARMFUL)

        kept = curator.curate([_entry("e1", age_days=5)])

        assert [u.id for u in kept] == ["e1"], "有帮助居多的条目不退休"


# ---------------------------------------------------------------------------
# 第 4 步 合并与删除
# ---------------------------------------------------------------------------


def _verdict(
    *, merges: list[dict] | None = None, competes: list[dict] | None = None,
    too_broad: list[str] | None = None,
) -> str:
    """三结论的 LLM 响应文本。"""
    return json.dumps({
        "merges": merges or [],
        "competes": competes or [],
        "too_broad": too_broad or [],
    })


class TestMergeAndDelete:
    # -- 三段不全（规则退休，不调模型） ------------------------------- #

    @staticmethod
    def test_incomplete_tip_retired_without_llm():
        """无 LLM / Embedder：第 4 步只做规则退休，其余条目原样留下。"""
        curator, _index, _store = _make_curator()
        broken = _tip("broken", condition="")
        fact = _entry("fact")

        kept = curator.curate([broken, fact])

        assert [u.id for u in kept] == ["fact"], "三段不全的条件动作退休"
        assert broken.lifecycle == LifecycleState.FORGOTTEN, "退休标记 FORGOTTEN"

    @staticmethod
    def test_incomplete_tip_retired_before_clustering():
        """有模型时三段不全仍按规则退休（聚簇之前），孤条目不触发模型。"""
        llm = ScriptedLLM([])
        curator, _index, _store = _make_curator(llm=llm, embedder=TagEmbedder())
        broken = _tip("broken", action="")
        fact = _entry("fact", content="clusterA fact")

        kept = curator.curate([broken, fact])

        assert [u.id for u in kept] == ["fact"], "规则退休先于聚簇"
        assert llm.calls == [], "单条不成簇，不调模型"

    # -- merge：合并条目构造与统计继承 --------------------------------- #

    @staticmethod
    def test_merge_creates_inheriting_unit():
        """合并产生新条目：内容/类别/三段沿用、provenance 记旧 id、落盘取
        最早、统计继承（计数相加、作者并集）、旧条目记 merged_into 并退休。
        """
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "t2"], "content": "合并后的内容"}]),
        ])
        curator, index, store = _make_curator(llm=llm, embedder=TagEmbedder())
        for i in range(2):
            _write_attr(store, "t1", f"a-{i}", f"tr-1-{i}", ATTR_HELPFUL)
            _write_attr(store, "t2", f"b-{i}", f"tr-2-{i}", ATTR_HELPFUL)
        units = [_tip("t1", age_days=5), _tip("t2", age_days=3)]

        kept = curator.curate(units)

        assert len(kept) == 1, "两条合成一条"
        merged_unit = kept[0]
        assert merged_unit.id not in {"t1", "t2"}, "新条目是新 id"
        assert merged_unit.content == "合并后的内容", "内容用模型给的"
        assert merged_unit.tier == MemoryTier.PROCEDURAL, "tier 沿用"
        assert merged_unit.system_metadata["category"] == "deploy", "类别沿用"
        assert merged_unit.system_metadata["condition"], "三段沿用被合并条目"
        assert merged_unit.provenance == ["t1", "t2"], "provenance 记旧 id"
        assert merged_unit.temporal.t_ingest == units[0].temporal.t_ingest, (
            "落盘时间取最早的（不进隔离期）"
        )
        # 新条目经 build 建全索引（运行摘要才是 FORWARD_ONLY）。
        indexed: list[MemoryUnit] = []
        for built, mode in index.built:
            if mode != IndexWriteMode.ALL:
                continue
            indexed.extend(built)
        assert merged_unit in indexed, "新条目进检索索引"
        # 旧条目退休 + 记合并去向。
        assert units[0].lifecycle == LifecycleState.FORGOTTEN, "被合并条目退休"
        assert store.load_stats(EXP_SCOPE, "t1").merged_into == merged_unit.id, (
            "统计记录记合并去向"
        )
        assert store.load_stats(EXP_SCOPE, "t2").merged_into == merged_unit.id, (
            "统计记录记合并去向"
        )
        # 统计继承：t1、t2 各 2 次有帮助 → 新条目 covered 4。
        inherited = store.load_stats(EXP_SCOPE, merged_unit.id)
        assert inherited is not None and inherited.covered == 4, "计数相加"
        assert _last_summary(index)["merged"] == 1, "摘要记合并数"

    @staticmethod
    def test_merged_events_redirect_next_round():
        """下一轮聚合：被合并条目的事件计入合并去向（merged_into 重定向）。"""
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "t2"], "content": "合并后的内容"}]),
        ])
        curator, _index, store = _make_curator(llm=llm, embedder=TagEmbedder())
        _write_attr(store, "t1", "a1", "tr-1", ATTR_HELPFUL)
        _write_attr(store, "t2", "a2", "tr-2", ATTR_HELPFUL)

        first = curator.curate([_tip("t1"), _tip("t2")])
        merged_id = first[0].id
        # 合并后旧条目又收到一次事件（insert-only，事件都在）。
        _write_attr(store, "t1", "a3", "tr-3", ATTR_HELPFUL)

        curator.curate(first)  # 第二轮只读入新条目

        record = store.load_stats(EXP_SCOPE, merged_id)
        # t1 两次 + t2 一次都重定向计入；无重定向时只会是继承的 2。
        assert record is not None and record.covered == 3, (
            "被合并条目的事件计入合并去向"
        )

    @staticmethod
    def test_desensitization_blocks_merge():
        """合并内容命中主体标识：放弃该组，旧条目保留、不产生新条目。"""
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "t2"], "content": "copy alice config"}]),
        ])
        curator, index, store = _make_curator(
            llm=llm, embedder=TagEmbedder(), desensitize_identifiers=["alice"]
        )
        units = [_tip("t1"), _tip("t2")]

        kept = curator.curate(units)

        assert {u.id for u in kept} == {"t1", "t2"}, "旧条目保留"
        assert all(u.lifecycle == LifecycleState.ACTIVE for u in units), "未退休"
        summary = _last_summary(index)
        assert summary["merged"] == 0, "未合并"
        assert summary["blocked_merges"] == 1, "摘要记脱敏拦截"

    @staticmethod
    def test_hallucinated_ids_ignored():
        """模型幻觉 id 丢弃：有效成员不足 2 不合并。"""
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "ghost"], "content": "x"}]),
        ])
        curator, _index, _store = _make_curator(llm=llm, embedder=TagEmbedder())

        kept = curator.curate([_tip("t1"), _tip("t2")])

        assert {u.id for u in kept} == {"t1", "t2"}, "幻觉 id 不影响条目"

    # -- compete / too_broad ------------------------------------------- #

    @staticmethod
    def test_compete_group_written_to_stats():
        """互为替代的条件动作记同一竞争组（组号写统计记录）。"""
        llm = ScriptedLLM([
            _verdict(competes=[{"ids": ["t1", "t2"]}]),
        ])
        curator, _index, store = _make_curator(llm=llm, embedder=TagEmbedder())

        curator.curate([_tip("t1"), _tip("t2")])

        g1 = store.load_stats(EXP_SCOPE, "t1").competition_group
        g2 = store.load_stats(EXP_SCOPE, "t2").competition_group
        assert g1 == g2 and g1.startswith("g-"), "同组同号"

    @staticmethod
    def test_too_broad_retired():
        llm = ScriptedLLM([_verdict(too_broad=["t1"])])
        curator, _index, _store = _make_curator(llm=llm, embedder=TagEmbedder())
        units = [_tip("t1"), _tip("t2")]

        kept = curator.curate(units)

        assert [u.id for u in kept] == ["t2"], "过于宽泛的退休"

    @staticmethod
    def test_fact_ignores_compete_and_too_broad():
        """环境事实只参与 merge：compete / too_broad 结论对其忽略。"""
        llm = ScriptedLLM([
            _verdict(competes=[{"ids": ["f1", "f2"]}], too_broad=["f1"]),
        ])
        curator, _index, store = _make_curator(llm=llm, embedder=TagEmbedder())
        units = [
            _entry("f1", content="clusterA fact one"),
            _entry("f2", content="clusterA fact two"),
        ]

        kept = curator.curate(units)

        assert {u.id for u in kept} == {"f1", "f2"}, "环境事实不受两结论影响"
        record = store.load_stats(EXP_SCOPE, "f1")
        assert record is None or record.competition_group == "", "不进竞争组"

    # -- 聚簇与容错 ----------------------------------------------------- #

    @staticmethod
    def test_cluster_bucketing_by_category_and_tier():
        """聚簇按 (类别, tier) 分桶：同标签也不跨桶，每桶各一次模型调用。"""
        llm = ScriptedLLM([_verdict(), _verdict(), _verdict()])
        embedder = TagEmbedder()
        curator, _index, _store = _make_curator(llm=llm, embedder=embedder)
        units = [
            _entry("f1", content="clusterA fact one"),
            _entry("f2", content="clusterA fact two"),
            _tip("t1", cluster="clusterA"),
            _tip("t2", cluster="clusterA"),
            _tip("c1", category="config", cluster="clusterA"),
            _tip("c2", category="config", cluster="clusterA"),
        ]

        kept = curator.curate(units)

        assert len(llm.calls) == 3, "三个桶各一次调用"
        texts = [messages[1].content for messages, _opts in llm.calls]
        assert 'f1' in texts[0] and 'f2' in texts[0], "SEMANTIC 桶一起判"
        assert 't1' in texts[1] and 't2' in texts[1], "PROCEDURAL 桶一起判"
        assert 'c1' in texts[2] and 'c2' in texts[2], "config 桶一起判"
        assert len(kept) == 6, "空结论：全部保留"

    @staticmethod
    def test_cluster_failure_skips_cluster():
        """一簇模型调用重试后仍失败：跳过该簇，其余步骤照常（不炸整步）。"""
        llm = ScriptedLLM(["not a json object"])
        curator, index, _store = _make_curator(
            llm=llm, embedder=TagEmbedder(), max_attempts=1,
        )

        kept = curator.curate([_tip("t1"), _tip("t2")])

        assert {u.id for u in kept} == {"t1", "t2"}, "簇失败条目保留"
        summary = _last_summary(index)
        assert summary["skipped_clusters"] == 1, "摘要记跳过的簇"
        assert summary["failed_steps"] == [], "第 4 步整体未失败"

    @staticmethod
    def test_prompt_registry_replaces_default():
        """合并提示词可经 PromptRegistry（consolidate / merge）运行时替换。"""
        llm = ScriptedLLM([_verdict()])
        registry = PromptRegistry.from_dict(
            {"consolidate": {"merge": "CUSTOM MERGE PROMPT"}}
        )
        curator, _index, _store = _make_curator(
            llm=llm, embedder=TagEmbedder(), prompt_registry=registry,
        )

        curator.curate([_tip("t1"), _tip("t2")])

        system_prompt = llm.calls[0][0][0].content
        assert system_prompt == "CUSTOM MERGE PROMPT", "系统提示词被替换"
        assert llm.calls[0][1].get("temperature") == 0, "判定调用 temperature=0"


# ---------------------------------------------------------------------------
# 第 5 步 淘汰（竞争组）
# ---------------------------------------------------------------------------


class TestEliminateCompeting:
    @staticmethod
    def test_lower_validity_retired():
        """组内两两比较：都归因满 5 次且有效性差距的置信下界大于 0，退休低者。

        数据取 helpful×20（高）对 helpful×4 + harmful×4（低）：低者的有害率
        Wilson 下界 0.19 未过 0.5（先经第 3 步不退休），有效性差距的置信
        下界约 0.08 > 0（第 5 步淘汰）。
        """
        curator, _index, store = _make_curator()
        store.save_stats(EXP_SCOPE, "high", StatsRecord(competition_group="g-1"))
        store.save_stats(EXP_SCOPE, "low", StatsRecord(competition_group="g-1"))
        for i in range(20):
            _write_attr(store, "high", f"author-h{i}", f"t-h{i}", ATTR_HELPFUL)
        for i in range(4):
            _write_attr(store, "low", f"author-l{i}", f"t-l{i}", ATTR_HELPFUL)
            _write_attr(store, "low", f"author-x{i}", f"t-x{i}", ATTR_HARMFUL)

        kept = curator.curate([_entry("high", age_days=5), _entry("low", age_days=5)])

        assert [u.id for u in kept] == ["high"], (
            "组内都归因满 5 次且有效性差距显著：退休较低的一条"
        )

    @staticmethod
    def test_no_group_no_action():
        curator, _index, store = _make_curator()
        for i in range(6):
            _write_attr(store, "e1", f"author-{i}", f"t-{i}", ATTR_HELPFUL)
            _write_attr(store, "e2", f"author-{i}", f"t-{i}", ATTR_HELPFUL)

        kept = curator.curate([_entry("e1", age_days=5), _entry("e2", age_days=5)])

        assert {u.id for u in kept} == {"e1", "e2"}, "无竞争组（第 4 步空过）不淘汰"


# ---------------------------------------------------------------------------
# 第 6 步 控制规模
# ---------------------------------------------------------------------------


class TestCapCategories:
    @staticmethod
    def _fill(store: UtilityStore, verified: int, unverified: int) -> None:
        for i in range(verified):
            for j in range(5):
                _write_attr(store, f"v-{i}", f"author-{j}", f"t-{i}-{j}", ATTR_HELPFUL)
        # 未验证条目不写事件；有效性差距由 verified 中一条低分条目制造。

    def test_cap_keeps_unverified_quota(self):
        """类别上限 10、其中归因不足至多 3：8 verified + 7 unverified → 留 10。"""
        curator, _index, store = _make_curator(
            category_cap=10, unverified_cap=3, idle_retire_days=10_000
        )
        units = []
        for i in range(8):
            units.append(_entry(f"v-{i}", age_days=5))
        for i in range(7):
            units.append(_entry(f"u-{i}", age_days=1 + i))  # 越新质量分越高
        self._fill(store, verified=8, unverified=7)
        # v-7 拉低有效性：5 次有害 → verified 中质量分最低。
        for j in range(5):
            _write_attr(store, "v-7", f"bad-{j}", f"t-b{j}", ATTR_HARMFUL)

        kept = curator.curate(units)

        kept_ids = {u.id for u in kept}
        assert len(kept) == 10, "总上限 10"
        assert sum(1 for uid in kept_ids if uid.startswith("u-")) == 3, "归因不足至多 3 条"
        assert {"u-0", "u-1", "u-2"} <= kept_ids, "未验证配额给质量分最高（最新）的"
        assert "v-7" not in kept_ids, "验证条目中质量分最低的让位"
        assert sum(1 for uid in kept_ids if uid.startswith("v-")) == 7, "验证条目留 7"

    @staticmethod
    def test_within_cap_untouched():
        curator, _index, _store = _make_curator(category_cap=10, unverified_cap=3)
        units = [_entry(f"e-{i}", age_days=5) for i in range(9)]

        kept = curator.curate(units)

        assert len(kept) == 9, "类别条数未超上限不动"


# ---------------------------------------------------------------------------
# 第 7 步 技能（§2.5.7；v3 §2.5 技能与经验同空间）
# ---------------------------------------------------------------------------


class BrokenSkillScanUtility(UtilityStore):
    """第二次 scan_events 抛异常（第 7 步失败场景，不影响第 1 步）。

    v3 技能与经验同空间后，第 1 步（归因聚合）与第 7 步（技能聚合）的
    scan_events 落在同一空间，无法按 scope 区分——按调用次序区分：单空间
    单批下第 1 次是第 1 步，第 2 次起是第 7 步。
    """

    def __init__(self, kv):
        super().__init__(kv)
        self._scan_calls = 0

    def scan_events(self, scope):
        self._scan_calls += 1
        if self._scan_calls >= 2:
            raise RuntimeError("skill scan boom")
        return super().scan_events(scope)


def _skill(
    unit_id: str,
    name: str,
    *,
    version: int = 1,
    tags: tuple[str, ...] = ("trial",),
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    category: str = "deploy",
) -> MemoryUnit:
    """经验空间里的技能条目（v3 §2.5 技能与经验同空间；metadata 同 skill_abstractor）。"""
    now = datetime.now(UTC)
    return MemoryUnit(
        id=unit_id,
        scope=EXP_SCOPE,
        tier=MemoryTier.PROCEDURAL,
        lifecycle=lifecycle,
        segments=[Segment(content=f"# {name}")],
        tags=list(tags),
        temporal=Temporal(t_ingest=now, t_valid=now),
        system_metadata={
            "memory_type": "skill",
            "skill_name": name,
            "skill_version": version,
            "category": category,
        },
    )


def _write_skill(kv: InMemoryKVStore, unit: MemoryUnit) -> None:
    kv.insert(EXP_SCOPE, memory_key(unit.id), dumps(unit))


def _write_skill_attr(
    store: UtilityStore,
    name: str,
    version: int,
    author: str,
    verdict: str,
    *,
    days_ago: float = 1,
) -> None:
    """在经验空间写一条技能归因事件（键 skill:{名}:{版本}，§2.5.7）。"""
    occurred = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    store.write_attribution_event(
        EXP_SCOPE,
        f"skill:{name}:{version}",
        author,
        f"traj-{author}",
        0,
        AttributionEvent(
            verdict=verdict, evidence=EVIDENCE_EXTERNAL, author=author,
            occurred_at=occurred,
        ),
    )


def _skill_curator(
    kv: InMemoryKVStore, *, index: RecordingIndexBuilder | None = None, **overrides
) -> tuple[ExperienceCurator, RecordingIndexBuilder, UtilityStore]:
    """经验落点空间开启技能沉淀、同源 KV 的 curator（第 7 步生效形态）。"""
    builder = index or RecordingIndexBuilder()
    store = UtilityStore(kv)
    defaults = dict(
        index_builder=builder,
        utility=store,
        message_store=kv,
        skill_spaces=[EXP_SCOPE.space],
    )
    defaults.update(overrides)
    return ExperienceCurator(**defaults), builder, store


def _updated_units(index: RecordingIndexBuilder) -> dict[str, MemoryUnit]:
    """第 7 步回写给 IndexBuilder 的条目（按 id 索引）。

    curator 修改的是从 KV 读出的副本，测试闭包里持有的原对象不会被改动——
    断言取回写的那份。
    """
    return {u.id: u for units, _mode in index.updated for u in units}


def _entry_filter(units: list[MemoryUnit]) -> list[MemoryUnit]:
    """白盒触达入口筛（staticmethod，经小工具绕行受保护访问）。"""
    return getattr(ExperienceCurator, "_entry_filter")(units)


def _handle_skills(curator, scope, units, stats, summary) -> list[MemoryUnit]:
    """白盒触达第 7 步技能处理（经小工具绕行受保护访问）。"""
    return getattr(curator, "_handle_skills")(scope, units, stats, summary)


def _evolver_abstractor(evolver):
    """白盒读取编排器持有的技能抽象器（经小工具绕行受保护访问）。"""
    return getattr(evolver, "_abstractor")


class TestSkillsStep:
    @staticmethod
    def test_no_skill_space_no_op():
        """空间未开启技能沉淀（skill_spaces 空）：第 7 步空过，不扫不写。"""
        curator, index, _store = _make_curator()
        summary: dict = {}
        entry = _entry("e1")

        kept = _handle_skills(curator, EXP_SCOPE, [entry], {}, summary)

        assert kept == [entry], "经验条目集原样返回"
        assert summary == {}, "不产生任何技能统计"
        assert not index.updated and not index.removed, "不触发条目回写"

    @staticmethod
    def test_counts_written_back():
        """计数回写（§2.5.7「聚合」）：有帮助/有害（加权）与加载次数进元数据。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        for i in range(1, 6):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HELPFUL)
        for i in (6, 7):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", "neutral")

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        out = _updated_units(index)["s-1"]
        assert out.system_metadata["skill_helpful"] == 5.0, "external 加权 = 条数"
        assert out.system_metadata["skill_harmful"] == 0.0
        assert out.system_metadata["skill_loads"] == 7, "加载 = 有帮助+有害+不计"
        assert index.updated, "改过的条目经 IndexBuilder 回写"
        assert all(
            mode == IndexWriteMode.FORWARD_ONLY for _u, mode in index.updated
        ), "只回写本体（技能不经检索使用）"
        record = store.load_stats(EXP_SCOPE, "skill:deploy-config:1")
        assert record is not None and record.covered == 5, "统计记录落经验空间（技能同空间）"
        assert summary["skills_events"] == 7

    @staticmethod
    def test_trial_promoted():
        """试用转正（§2.5.5）：归因满 5 且有效性不低于基线 → approved。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        for i in range(1, 6):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HELPFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        assert _updated_units(index)["s-1"].tags == ["approved"], "标签 trial → approved"
        assert summary["skills_promoted"] == 1
        assert summary["skills_in_trial"] == 1, "转正前的试用存量"

    @staticmethod
    def test_not_enough_attribution_stays_trial():
        """归因未达下限：保持试用。"""
        kv = InMemoryKVStore()
        curator, _index, store = _skill_curator(kv)
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        for i in (1, 2, 3):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HELPFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        assert skill.tags == ["trial"], "归因 3 次 < 5，保持试用"
        assert summary["skills_promoted"] == 0

    @staticmethod
    def test_validity_below_baseline_not_promoted():
        """有效性低于同类别基线：不转正（也不误退休）。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        store.save_baseline(EXP_SCOPE, {"deploy": 0.9})
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        _write_skill_attr(store, "deploy-config", 1, "d1", ATTR_HELPFUL)
        for i in range(2, 6):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HARMFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        # validity = (1 + 2×0.9) ÷ (5 + 2) ≈ 0.4 < 0.9；有害 4/5 的 Wilson
        # 下界 ≈ 0.45 < 0.5，不到退休。
        out = _updated_units(index)["s-1"]
        assert out.tags == ["trial"], "有效性不足不转正"
        assert out.lifecycle == LifecycleState.ACTIVE, "未到有害退休阈值"
        assert summary["skills_promoted"] == 0 and summary["skills_retired"] == 0

    @staticmethod
    def test_harmful_retired_before_promotion():
        """有害退休（与经验同一规则）优先于转正：退休的不被转正。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        for i in range(1, 6):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HARMFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        out = _updated_units(index)["s-1"]
        assert out.lifecycle == LifecycleState.FORGOTTEN, "有害率下界过阈值 → 退休"
        assert out.tags == ["trial"], "退休的不转正"
        assert summary["skills_retired"] == 1 and summary["skills_promoted"] == 0
        assert index.removed, "退休走 _forget 的移出检索指令"

    @staticmethod
    def test_old_version_superseded():
        """旧版取代（§2.5.7）：同名发布版中版本最大的为现行，旧版 SUPERSEDED。"""
        kv = InMemoryKVStore()
        curator, index, _store = _skill_curator(kv)
        v1 = _skill("s-1", "deploy-config", version=1, tags=("approved",))
        v2 = _skill("s-2", "deploy-config", version=2, tags=("trial",))
        _write_skill(kv, v1)
        _write_skill(kv, v2)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        updated = _updated_units(index)
        assert updated["s-1"].lifecycle == LifecycleState.SUPERSEDED, "旧版被取代"
        assert updated["s-1"].temporal.t_invalid is not None, "取代记失效时间"
        assert "s-2" not in updated, "现行版不动（不在回写集）"
        assert summary["skills_superseded"] == 1

    @staticmethod
    def test_draft_not_involved_in_supersede():
        """draft 不是「旧版」也不算发布版：不触发取代、不被取代。"""
        kv = InMemoryKVStore()
        curator, _index, _store = _skill_curator(kv)
        v1 = _skill("s-1", "deploy-config", version=1, tags=("approved",))
        v3 = _skill("s-3", "deploy-config", version=3, tags=("draft",))
        _write_skill(kv, v1)
        _write_skill(kv, v3)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        assert v1.lifecycle == LifecycleState.ACTIVE, "draft 版本更高不触发取代"
        assert v3.lifecycle == LifecycleState.ACTIVE, "draft 不被取代"
        assert summary["skills_superseded"] == 0

    @staticmethod
    def test_retired_version_not_current():
        """现行版本轮退休后，旧版不再被取代（FORGOTTEN 已退出）。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        v1 = _skill("s-1", "deploy-config", version=1, tags=("approved",))
        v2 = _skill("s-2", "deploy-config", version=2, tags=("trial",))
        _write_skill(kv, v1)
        _write_skill(kv, v2)
        for i in range(1, 6):
            _write_skill_attr(store, "deploy-config", 2, f"d{i}", ATTR_HARMFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        updated = _updated_units(index)
        assert updated["s-2"].lifecycle == LifecycleState.FORGOTTEN, "新版有害退休"
        assert "s-1" not in updated, "旧版不因新版退休被取代（不在回写集）"
        assert summary["skills_superseded"] == 0

    @staticmethod
    def test_in_trial_counts_before_promotion():
        """skills_in_trial 是处理前的试用存量（draft / approved 不计）。"""
        kv = InMemoryKVStore()
        curator, _index, store = _skill_curator(kv)
        for unit_id, tags in (("s-1", ("trial",)), ("s-2", ("trial",)),
                              ("s-3", ("approved",)), ("s-4", ("draft",))):
            _write_skill(kv, _skill(unit_id, f"skill-{unit_id}", tags=tags))
        for i in range(1, 6):
            _write_skill_attr(store, "skill-s-1", 1, f"d{i}", ATTR_HELPFUL)

        summary: dict = {}
        _handle_skills(curator, EXP_SCOPE, [], {}, summary)

        assert summary["skills_in_trial"] == 2, "转正前 2 条试用"

    @staticmethod
    def test_full_curate_with_skills():
        """全链路：配了对的技能空间时 curate 正常跑完八步，技能被处理。"""
        kv = InMemoryKVStore()
        curator, index, store = _skill_curator(kv)
        skill = _skill("s-1", "deploy-config")
        _write_skill(kv, skill)
        for i in range(1, 6):
            _write_skill_attr(store, "deploy-config", 1, f"d{i}", ATTR_HELPFUL)
        entry = _entry("e1")

        kept = curator.curate([entry])

        assert [u.id for u in kept] == ["e1"], "经验条目照常整理返回"
        assert _updated_units(index)["s-1"].tags == ["approved"], "第 7 步在八步流程中生效"
        summary = _last_summary(index)
        assert summary["skills_promoted"] == 1
        assert summary["skills_handled"] == 1

    @staticmethod
    def test_step_failure_isolated():
        """第 7 步失败只跳过本步：其余步骤照跑，本轮不归纳。"""
        kv = InMemoryKVStore()
        utility = BrokenSkillScanUtility(kv)
        curator, index, _store = _skill_curator(kv, utility=utility)

        kept = curator.curate([_entry("e1")])

        assert kept == [], "有一步失败：返回空列表，本轮不归纳"
        summary = _last_summary(index)
        assert summary["failed_steps"][0]["step"] == "handle_skills"
        assert summary["failed_steps"][0]["error"] == "skill scan boom"

    @staticmethod
    def test_skill_stats_key_format():
        """技能的效用事件键：skill:{技能名}:{版本号}（§2.5.5）。"""
        from jiuwen_memory.construction.curator_impl.experience_curator import (
            _skill_stats_key,
        )

        assert _skill_stats_key(_skill("s-1", "deploy-config", version=3)) == (
            "skill:deploy-config:3"
        )


# ---------------------------------------------------------------------------
# 第 8 步 收尾与运行摘要
# ---------------------------------------------------------------------------


class TestRecountAndSummary:
    @staticmethod
    def test_category_counts_overwritten():
        curator, _index, store = _make_curator()
        store.add_category_count(EXP_SCOPE, "deploy", active=99, dropped=7)

        curator.curate([_entry("e1"), _entry("e2"), _entry("e3")])

        counts = store.load_category_count(EXP_SCOPE, "deploy")
        assert counts["active"] == 3, "生效条数按实际覆写"
        assert counts["dropped"] == 7, "丢弃累计值沿用"

    @staticmethod
    def test_categories_independent():
        curator, _index, store = _make_curator()
        curator.curate([_entry("e1", category="deploy"), _entry("e2", category="config")])
        assert store.load_category_count(EXP_SCOPE, "deploy")["active"] == 1, "类别独立"
        assert store.load_category_count(EXP_SCOPE, "config")["active"] == 1, "类别独立"

    @staticmethod
    def test_run_summary_written():
        curator, index, _store = _make_curator()

        curator.curate([_entry("e1", age_days=80)])

        built = [u for units, mode in index.built for u in units]
        summaries = [u for u in built if RUN_SUMMARY_TAG in u.tags]
        assert len(summaries) == 1, "每轮一条运行摘要"
        summary = summaries[0]
        assert summary.tier == MemoryTier.EPISODIC, "运行摘要 tier=EPISODIC"
        assert (
            summary.system_metadata.get("memory_type") == "experience"
        ), "memory_type=experience（v3 §2.5.2，list 按它列出）"
        payload = json.loads(summary.segments[0].content)
        assert payload["retired_idle"] == 1, "步骤统计记入摘要"
        assert payload["started_at"], "轮次开始时间记入摘要"
        assert all(
            mode == IndexWriteMode.FORWARD_ONLY for _units, mode in index.built
        ), "运行摘要只写本体不建索引"

    @staticmethod
    def test_step_failure_recorded_and_no_consolidation():
        curator, index, store = _make_curator(
            index=RecordingIndexBuilder(update_error=RuntimeError("index down"))
        )
        for i in range(6):
            _write_attr(store, "old", f"author-{i}", f"t-{i}", ATTR_HELPFUL, days_ago=70)
        units = [_entry("old", age_days=80)]

        kept = curator.curate(units)

        assert kept == [], "有一步失败：返回空列表，本轮不归纳"
        summary = _last_summary(index)
        assert summary["failed_steps"][0]["step"] == "retire_idle", "失败步骤记入摘要"
        assert summary["failed_steps"][0]["error"] == "index down", "失败原因记入摘要"


# ---------------------------------------------------------------------------
# OrchestratingEvolver 的 CONSOLIDATE 接线
# ---------------------------------------------------------------------------


class _RecordingCurator(Curator):
    """记录 curate 调用；返回去掉指定 id 的条目。"""

    def __init__(self, drop_ids: set[str] | None = None) -> None:
        self.calls: list[list[MemoryUnit]] = []
        self._drop_ids = drop_ids or set()

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.CURATOR

    @staticmethod
    def health() -> None:
        return None

    def curate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        self.calls.append(list(units))
        return [unit for unit in units if unit.id not in self._drop_ids]


class _RecordingAbstractor(Abstractor):
    def __init__(self) -> None:
        self.calls: list[list[MemoryUnit]] = []

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.ABSTRACTOR

    @staticmethod
    def health() -> None:
        return None

    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        self.calls.append(list(units))
        return []


class _NullLLM(LLM):
    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.LLM

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def chat(messages, **options):
        return ""


def _make_evolver(curator: Curator | None):
    kv = InMemoryKVStore()
    storage = make_storage(kv=kv, graph=InMemoryGraphStore())
    return OrchestratingEvolver(
        extractor=object(),  # CONSOLIDATE 路径不触发 extractor
        abstractor=_RecordingAbstractor(),
        associator=object(),  # CONSOLIDATE 路径不触发 associator
        index_builder=RecordingIndexBuilder(),
        storage=storage,
        message_store=storage.kv(),
        dedup=object(),  # 升华产物为空时不走去重
        llm=_NullLLM(),
        curator=curator,
    )


class TestEvolverConsolidateWiring:
    @staticmethod
    def test_curator_runs_before_abstractor():
        curator = _RecordingCurator(drop_ids={"drop-1"})
        evolver = _make_evolver(curator)
        abstractor = _evolver_abstractor(evolver)

        evolver.evolve([_entry("keep-1"), _entry("drop-1")], EvolveMode.CONSOLIDATE)

        assert len(curator.calls) == 1, "CONSOLIDATE 先经整理"
        assert {u.id for u in curator.calls[0]} == {"keep-1", "drop-1"}, (
            "整理收到全部读入条目"
        )
        assert [u.id for u in abstractor.calls[0]] == ["keep-1"], (
            "升华只用整理后留下的条目"
        )

    @staticmethod
    def test_empty_curate_result_skips_abstractor():
        """整理有一步失败（返回空列表）：本轮不归纳，升华不执行。"""
        curator = _RecordingCurator(drop_ids={"keep-1", "drop-1"})
        evolver = _make_evolver(curator)
        abstractor = _evolver_abstractor(evolver)

        evolver.evolve([_entry("keep-1")], EvolveMode.CONSOLIDATE)

        assert abstractor.calls == [], "curate 返回空列表时不归纳"

    @staticmethod
    def test_without_curator_behavior_unchanged():
        evolver = _make_evolver(None)
        abstractor = _evolver_abstractor(evolver)

        evolver.evolve([_entry("keep-1")], EvolveMode.CONSOLIDATE)

        assert [u.id for u in abstractor.calls[0]] == ["keep-1"], (
            "未配置整理算子：照常直接升华（其他 profile 不变）"
        )


# ---------------------------------------------------------------------------
# 空间过滤（mem2.0 §2.5.1「两个算子只处理部署配置中列出的经验空间」）
# ---------------------------------------------------------------------------


class TestExperienceSpaceFilter:
    @staticmethod
    def _incomplete_tip(entry_id: str, *, space: str) -> MemoryUnit:
        """三段不全的 PROCEDURAL——空间被整理时会被规则退休（观察点）。"""
        unit = _entry(
            entry_id,
            tier=MemoryTier.PROCEDURAL,
            metadata={"condition": "服务启动找不到配置"},
        )
        return replace(unit, scope=Scope(org="acme", space=space))

    def test_only_configured_spaces_are_curated(self):
        """清单内空间正常整理；清单外空间不整理、不写摘要、不进返回集。

        返回集是「参与后续归纳」的条目——非经验空间不该被归纳成技能。
        """
        chat_unit = self._incomplete_tip("chat-1", space="chat")
        exp_kept = _tip("exp-kept")  # 三段齐全的条件动作：保留
        exp_retired = self._incomplete_tip("exp-retired", space="shared-experience")

        curator, builder, _store = _make_curator(
            curated_spaces=["shared-experience"]
        )
        kept = curator.curate([chat_unit, exp_kept, exp_retired])

        kept_ids = {unit.id for unit in kept}
        assert "exp-kept" in kept_ids, "清单内的生效条目保留"
        assert "exp-retired" not in kept_ids, "清单内三段不全的被规则退休"
        assert "chat-1" not in kept_ids, "清单外不进返回集（不参与归纳）"
        removed_ids = {u.id for units, _m in builder.removed for u in units}
        assert removed_ids == {"exp-retired"}, "退休只发生在清单内空间"
        summaries: list[MemoryUnit] = []
        for units, _m in builder.built:
            for u in units:
                if RUN_SUMMARY_TAG in u.tags:
                    summaries.append(u)
        assert len(summaries) == 1, "清单外空间不写运行摘要"

    def test_no_filter_without_configuration(self):
        """curated_spaces=None（未配置）不过滤——单测与旧装配路径行为不变。"""
        chat_unit = self._incomplete_tip("chat-1", space="chat")
        exp_retired = self._incomplete_tip("exp-retired", space="shared-experience")

        curator, _builder, _store = _make_curator()
        kept = curator.curate([chat_unit, exp_retired])

        kept_ids = {unit.id for unit in kept}
        assert "chat-1" not in kept_ids and "exp-retired" not in kept_ids, (
            "未配置过滤时两个空间都整理（三段不全均退休）"
        )


# ---------------------------------------------------------------------------
# 第 0 步 按作者清除（mem2.0 §2.5.2「按作者清除」）
# ---------------------------------------------------------------------------


class TestEraseAuthors:
    @staticmethod
    def test_erase_authors_removes_events_and_retires_entries():
        """消费清除标记：删被清除作者的事件、退休「只剩他们复现」的条目。

        复现记录含其他作者的条目保留——其他作者的复现是条目继续生效的
        独立证据。统计随聚合重算：被清除作者的事件不再计入 covered。
        """
        curator, builder, store = _make_curator()
        store.add_erased_author(EXP_SCOPE, "d1")
        store.add_erased_author(EXP_SCOPE, "d2")
        # d1 的两条事件（e1 与 e4 各一）都要删；d3 的事件保留。
        _write_attr(store, "e1", "d1", "t1", ATTR_HELPFUL)
        _write_attr(store, "e4", "d1", "t4", ATTR_HELPFUL)
        _write_attr(store, "e4", "d3", "t4b", ATTR_HELPFUL)
        store.save_repro_authors(EXP_SCOPE, "e1", ["d1"])
        store.save_repro_authors(EXP_SCOPE, "e2", ["d1", "d2"])
        store.save_repro_authors(EXP_SCOPE, "e3", ["d2", "d3"])
        units = [
            _entry("e1"),
            _tip("e2"),  # PROCEDURAL 三段齐全：退休只可能来自清除
            _entry("e3"),
            _entry("e4"),
        ]

        kept = curator.curate(units)

        assert {u.id for u in kept} == {"e3", "e4"}, (
            "只剩被清除作者复现的条目退休；其他作者也复现过的保留"
        )
        assert units[0].lifecycle == LifecycleState.FORGOTTEN, "SEMANTIC 退休"
        assert units[1].lifecycle == LifecycleState.FORGOTTEN, (
            "PROCEDURAL 同样退休——清除是合规操作，不过滤 tier"
        )
        assert store.load_erased_authors(EXP_SCOPE) == [], "标记消费后删除"
        events = store.scan_events(EXP_SCOPE)
        assert all(key.author != "d1" for key, _payload in events), (
            "被清除作者的事件全部删除"
        )
        assert any(key.author == "d3" for key, _payload in events), (
            "其他作者的事件保留"
        )
        record = store.load_stats(EXP_SCOPE, "e4")
        assert record is not None and record.covered == 1, (
            "统计随聚合重算：被清除作者的事件不再计入"
        )
        summary = _last_summary(builder)
        assert summary["erased_authors"] == 2, "摘要记被清除作者数"
        assert summary["erased_events"] == 2, "摘要记删除事件数"
        assert summary["erased_retired"] == 2, "摘要记退休条目数"

    @staticmethod
    def test_erase_authors_noop_without_marks():
        """无清除标记时第 0 步 no-op：组原样返回，摘要不写清除字段。"""
        curator, builder, _store = _make_curator()

        kept = curator.curate([_entry("e1"), _entry("e2")])

        assert {u.id for u in kept} == {"e1", "e2"}, "无标记不退休"
        summary = _last_summary(builder)
        assert "erased_authors" not in summary, "无标记不写清除字段"

    @staticmethod
    def test_erase_authors_failure_aborts_round():
        """清除失败本轮中止（不归纳）：清除没做就把旧事件聚回去等于又算一遍。"""
        curator, builder, _store = _make_curator(utility=BrokenErasedUtility(
            InMemoryKVStore()
        ))

        kept = curator.curate([_entry("e1")])

        assert kept == [], "清除失败返回空，本轮不归纳"
        summary = _last_summary(builder)
        assert summary["failed_steps"] and (
            summary["failed_steps"][0]["step"] == "erase_authors"
        ), "失败步骤记入运行摘要"


class TestMergeReproAuthors:
    """合并产生的条目继承复现作者记录（§2.5.2「合并产生的条目」并集继承）。"""

    @staticmethod
    def test_merge_inherits_repro_authors_union():
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "t2"], "content": "合并后的内容"}]),
        ])
        curator, _index, store = _make_curator(llm=llm, embedder=TagEmbedder())
        store.save_repro_authors(EXP_SCOPE, "t1", ["d1", "d2"])
        store.save_repro_authors(EXP_SCOPE, "t2", ["d2", "d3"])

        kept = curator.curate([_tip("t1"), _tip("t2")])

        # 并集按成员出现序 [d1, d2, d3]，save 内部截 REPRO_AUTHOR_LIMIT=2。
        assert store.load_repro_authors(EXP_SCOPE, kept[0].id) == ["d1", "d2"], (
            "复现作者取被合并条目的并集（内部截上限）"
        )

    @staticmethod
    def test_merge_without_repro_authors_writes_nothing():
        llm = ScriptedLLM([
            _verdict(merges=[{"ids": ["t1", "t2"], "content": "合并后的内容"}]),
        ])
        curator, _index, store = _make_curator(llm=llm, embedder=TagEmbedder())

        kept = curator.curate([_tip("t1"), _tip("t2")])

        assert store.load_repro_authors(EXP_SCOPE, kept[0].id) == [], (
            "成员无复现记录时不写新条目的记录"
        )


# ---------------------------------------------------------------------------
# 小件
# ---------------------------------------------------------------------------


def _last_summary(index: RecordingIndexBuilder) -> dict:
    """取最后一条运行摘要的正文 JSON。"""
    built = [u for units, _mode in index.built for u in units]
    summaries = [u for u in built if RUN_SUMMARY_TAG in u.tags]
    assert summaries, "运行摘要应已写入"
    return json.loads(summaries[-1].segments[0].content)
