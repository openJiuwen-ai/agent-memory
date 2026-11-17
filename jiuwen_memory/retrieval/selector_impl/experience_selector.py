# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExperienceSelector — 经验条目的选取（mem2.0 §2.4）。

经验检索在相关性过滤与截断之间多一个选取阶段，四件事（伪代码见 §2.4）：

1. **复现要求**：可执行的条件动作须已被第二个用户独立复现（统计记录的
   ``authors`` 或复现作者记录至少 2 个，:data:`REPRO_AUTHOR_LIMIT`），
   未满足的淘汰；环境事实与不可执行条目不设此关。
2. **排序分** = 相关性^α × 质量分^β（α=2、β=1；相关性 =
   检索分 ÷ 本次候选最高检索分；质量分 = 0.7 × 有效性 + 0.3 × 新鲜度。
   公式与取值都在 ``common/experience``，本模块只组合）。
3. **探索名额**：归因次数（统计记录 ``covered``）不足阈值的条目算未经验证，
   其中排序分最高的一条先入选，引用值 ``0.e``。
4. **按能力分组**：类别与能力名相同的条目一组，每组先取排序分最高的一条；
   隔离期（落盘不满 7 天）内的条目只经探索名额返回，不参与分组与补满；
   余下名额按排序分从高到低补满。

返回的每个候选换成条目副本：``system_metadata`` 只留类别、三段（条件动作）
与引用值 ``usage_ref``；``score`` 保持检索分——返回给 agent 的分数仍是「与
检索句的匹配程度」，质量分只在排序时用。统计记录与类别基线由周期整理的
聚合步骤写入（Curator），两样都没有时（冷启动）有效性取中性基线
:data:`DEFAULT_BASELINE`，见 F09 决策。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.experience import (
    DEFAULT_BASELINE,
    EXPLORATION_THRESHOLD,
    ISOLATION_PERIOD_DAYS,
    RANK_ALPHA,
    RANK_BETA,
    REPRO_AUTHOR_LIMIT,
    StatsRecord,
    UtilityStore,
    encode_usage_ref,
    freshness,
    quality_score,
    validity,
)
from jiuwen_memory.common.type_def import ScoredMemoryUnit
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.selector import Selector, SelectorProducer
from jiuwen_memory.retrieval.types import RetrievalQuery
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name


class ExperienceSelector(Selector):
    """经验条目的选取算子（mem2.0 §2.4）：复现要求、排序分、探索名额、按能力分组。"""

    def __init__(
        self, kv: KVStore, *, now_fn: Callable[[], datetime] | None = None
    ) -> None:
        self._store = UtilityStore(kv)
        # 时钟注入点：单测用它固定「现在」，隔离期与新鲜度才有确定性边界。
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.SELECTOR

    def health(self) -> None:
        return None

    def select(
        self, survivors: list[ScoredMemoryUnit], query: RetrievalQuery
    ) -> list[ScoredMemoryUnit]:
        if not survivors:
            return []
        # 效用记录都在经验空间 scope 下；一次检索的候选同属一个空间（scope 是
        # 召回前置谓词），取首个候选的 scope 即可，接口因此不必带 scope 参数。
        scope = survivors[0].unit.scope
        now = self._now_fn()
        stats = {c.unit.id: self._store.load_stats(scope, c.unit.id) for c in survivors}
        baseline = self._store.load_baseline(scope)

        # 第 0 关：复现要求（可执行条件动作未满 2 个复现作者的淘汰）。
        eligible = [
            c for c in survivors if self._reproduction_ok(scope, c, stats[c.unit.id])
        ]
        if not eligible:
            return []

        ranks = self._rank_scores(eligible, stats, baseline, now)

        # 第 1 步：探索名额——未经验证的候选中排序分最高的一条先入选。
        explore_pick: ScoredMemoryUnit | None = None
        unproven = [
            c for c in eligible if self._covered(stats[c.unit.id]) < EXPLORATION_THRESHOLD
        ]
        if unproven:
            explore_pick = max(unproven, key=lambda c: ranks[c.unit.id])
        rest = [c for c in eligible if c is not explore_pick]

        # 第 2 步：按能力分组——隔离期内的条目不参与（只经探索名额返回）。
        active = [c for c in rest if not self._in_isolation(c, now)]
        active.sort(key=lambda c: ranks[c.unit.id], reverse=True)
        seen_groups: set[tuple[str, str]] = set()
        grouped: list[ScoredMemoryUnit] = []
        fill: list[ScoredMemoryUnit] = []
        for candidate in active:
            group = _group_key(candidate)
            if group not in seen_groups:
                seen_groups.add(group)
                grouped.append(candidate)  # 该组排序分最高的一条
            else:
                fill.append(candidate)

        # 第 3 步：补满——分组余下与各组内部第二名之后都按排序分降序排在尾部。
        ordered = ([] if explore_pick is None else [explore_pick]) + grouped + fill
        return [
            self._copy_with_ref(candidate, explore=candidate is explore_pick)
            for candidate in ordered
        ]

    # -- 各步的实现 ------------------------------------------------------- #

    def _reproduction_ok(
        self, scope, candidate: ScoredMemoryUnit, stats: StatsRecord | None
    ) -> bool:
        """复现要求：可执行条目须有 ≥2 个复现作者（统计记录优先，回退即时记录）。

        统计记录是周期聚合的结果，``authors`` 已满 2 时不读复现作者记录；没有
        统计记录（冷启动）或未满时读沉淀时增量维护的复现作者记录——第二个
        用户的轨迹复现后随即放行，不等周期聚合（§2.4「风险控制」）。
        """
        meta = candidate.unit.system_metadata
        if meta.get("executable") is not True:
            return True
        if stats is not None and len(stats.authors) >= REPRO_AUTHOR_LIMIT:
            return True
        authors = self._store.load_repro_authors(scope, candidate.unit.id)
        return len(authors) >= REPRO_AUTHOR_LIMIT

    @staticmethod
    def _covered(stats: StatsRecord | None) -> int:
        return stats.covered if stats is not None else 0

    @staticmethod
    def _in_isolation(candidate: ScoredMemoryUnit, now: datetime) -> bool:
        """隔离期：落盘不满 7 天只经探索名额返回。

        落盘时间取 ``t_ingest``（``datetime``，记录侧统一 aware）；缺失按已出
        隔离期处理（不因元数据缺失把条目永久锁死，方向见 F09 决策）。
        """
        t_ingest = candidate.unit.temporal.t_ingest
        if t_ingest is None:
            return False
        at = t_ingest if t_ingest.tzinfo else t_ingest.replace(tzinfo=UTC)
        days = max(0.0, (now - at).total_seconds() / 86400.0)
        return days < ISOLATION_PERIOD_DAYS

    def _rank_scores(
        self,
        eligible: list[ScoredMemoryUnit],
        stats: dict[str, StatsRecord | None],
        baseline: dict[str, float],
        now: datetime,
    ) -> dict[str, float]:
        """逐候选算排序分：相关性^α × 质量分^β。

        相关性按本次参与排序的候选（已过复现要求）的最高检索分归一；最高分
        不大于 0（全零分）时相关性取 1——排序退化为按质量分。质量分与有效性、
        新鲜度的公式在 ``common/experience``。
        """
        max_score = max(c.score for c in eligible)
        ranks: dict[str, float] = {}
        for candidate in eligible:
            relevance = 1.0 if max_score <= 0.0 else candidate.score / max_score
            record = stats[candidate.unit.id] or StatsRecord()
            base = baseline.get(
                str(candidate.unit.system_metadata.get("category", "")), DEFAULT_BASELINE
            )
            entry_validity = validity(record, base)
            # 新鲜度无最近正向时刻时回落落盘时间；t_ingest 是 datetime，转 ISO 进公式。
            t_ingest = candidate.unit.temporal.t_ingest
            entry_freshness = freshness(
                record.last_positive_at,
                fallback_at=t_ingest.isoformat() if t_ingest is not None else "",
                now=now,
            )
            ranks[candidate.unit.id] = (
                relevance**RANK_ALPHA * quality_score(entry_validity, entry_freshness) ** RANK_BETA
            )
        return ranks

    @staticmethod
    def _copy_with_ref(candidate: ScoredMemoryUnit, *, explore: bool) -> ScoredMemoryUnit:
        """换条目副本：``system_metadata`` 只留类别与三段，加引用值；分数保持检索分。"""
        meta = _stripped_metadata(candidate)
        meta["usage_ref"] = encode_usage_ref(explore=explore)
        unit = replace(candidate.unit, system_metadata=meta)
        return replace(candidate, unit=unit)


def _group_key(candidate: ScoredMemoryUnit) -> tuple[str, str]:
    """按能力分组的组键：(类别, 能力名)。

    环境事实不带能力名，capability 取空串——同类别下自成一组（§2.4「名额」：
    有环境事实通过过滤时至少返回 1 条的保证即来自此）。
    """
    meta = candidate.unit.system_metadata
    return (str(meta.get("category", "")), str(meta.get("capability", "")))


def _stripped_metadata(candidate: ScoredMemoryUnit) -> dict[str, object]:
    """条目副本的 ``system_metadata``：只留类别与三段（§2.4「返回的内容」）。

    写入侧落盘的 memory_type / pipeline / 抽取策略 / 作者标记 / 可执行标记等
    内部元数据不外发；三段（condition / capability / action）只有条件动作带。
    """
    source = candidate.unit.system_metadata
    meta: dict[str, object] = {}
    if "category" in source:
        meta["category"] = source["category"]
    for key in ("condition", "capability", "action"):
        if key in source:
            meta[key] = source[key]
    return meta


# -- 注册到 SelectorProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@SelectorProducer.register("experience")
def _build(config):
    # 效用记录与条目真源共用经验空间的 KV：统计记录、类别基线与复现作者记录
    # 都键点读自这里（§3.2），与索引/构建侧同实例。
    manager = StoreManagerProducer.resolve(config)
    kv_name = resolve_name(config, "kv_store")
    if not manager.has_kv(kv_name):
        raise ValidationError(
            "experience selector 需要 kv_store 读取效用记录（统计/基线/复现作者），"
            "请检查该 retriever 组件的 kv_store 配置"
        )
    return ExperienceSelector(manager.kv(kv_name))
