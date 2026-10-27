# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExperienceCurator — 经验空间的周期整理（mem2.0 §2.5.2）。

八步流程中只有第 4 步（合并与删除）调用模型：按 (类别, tier) 分桶贪心聚簇
（相似度 ≥ 0.6、每簇至多 20 条），每簇一次调用同时给出三种结论——可合并的
（连同合并后的内容）、互为替代的（竞争组）、过于宽泛的（退休）。第 7 步
（技能，§2.5.7）整理配对的技能空间：效用事件聚合回写、有害退休、试用
转正与旧版取代。整理排在归纳之前：归纳只用整理后留下的条目，并用到第 1 步
刚聚合出的统计记录。

第 4 步的模型与向量化按部署配置可选：LLM 或 Embedder 未装配时该步只做
三段不全的规则退休，聚簇与三结论整段跳过——经验空间的整理不因缺模型
配置而整轮失败。簇级失败隔离：一簇的模型调用重试后仍失败只跳过该簇
（记入运行摘要），不炸整步。

失败隔离（§2.5.2「步骤之间的失败隔离」）：按作者清除（聚合之前的第 0 步）
与第 1 步聚合失败本轮中止（清除没做就把旧事件聚回去、后面各步都依赖统计
记录）；第 2 至 8 步各自捕获异常，失败的一步记入运行摘要并跳过，
其余步骤继续；任一步失败时 :meth:`curate` 返回空列表，本轮不归纳。运行摘要
无论成败都落盘。

退休 = 条目标记 ``FORGOTTEN`` 并退出检索，条目与统计记录保留：先
``update(mode=FORWARD_ONLY)`` 回写本体新状态，再 ``remove(mode=SOFT)`` 移出
检索——与 ``_evolve_forget`` 同款两条指令。改条目状态、写运行摘要与写入
合并产生的新条目都经现有 ``IndexBuilder``，不经它直写存储。

入口筛（§2.5.2「输入的筛选」）：参与整理、归纳与类别计数的只有生效
（``ACTIVE``）的环境事实（SEMANTIC）与条件动作（PROCEDURAL）——运行摘要
（EPISODIC）被 tier 过滤跳过。条目按 (org, space) 分组，每组独立跑八步
（统计记录、类别计数与运行摘要都按空间隔离）；只处理部署配置
``curated_spaces`` 列出的周期整理空间（v3 §2.5.1）。
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.experience import (
    ATTRIBUTION_WINDOW_DAYS,
    DEFAULT_BASELINE,
    StatsRecord,
    UtilityStore,
    aggregate_utility,
    compute_baseline,
    days_since,
    freshness,
    now_iso,
    quality_score,
    validity,
    weighted_count,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory import LifecycleState, MemoryTier, Segment, Temporal
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.config.document_flag import resolve_index_builder_default
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.common import (
    resolve_message_store,
    validate_llm_attempt_policy,
)
from jiuwen_memory.construction.curator import Curator, CuratorProducer
from jiuwen_memory.construction.experience import (
    MEMORY_TYPE_EXPERIENCE,
    desensitization_hit,
)
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.construction.prompt_registry import PHASE_CONSOLIDATE, PromptRegistry
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

if TYPE_CHECKING:
    from jiuwen_memory.common.embedder import Embedder
    from jiuwen_memory.common.llm import LLM

logger = get_logger(__name__)

# 首版取值（§2.5.2 的取值表，属写策略，随版本调整）。
IDLE_RETIRE_DAYS = 60.0        # 闲置退休：60 天没有被取用
MIN_AGE_DAYS = 14.0            # 最短存活期：落盘已满 14 天
HARMFUL_ATTRIBUTION_MIN = 5    # 有害退休 / 竞争组比较 / 控规模共用的归因次数下限
HARMFUL_CONFIDENCE = 0.5       # 有害退休：有害率的置信下界阈值
CONFIDENCE_Z = 1.96            # 95% 置信区间
CATEGORY_CAP = 500             # 每类别生效条目上限
UNVERIFIED_CAP = 100           # 其中归因不足条目的上限
CLUSTER_SIMILARITY = 0.6       # 第 4 步聚簇的相似度阈值
CLUSTER_SIZE_LIMIT = 20        # 第 4 步每簇的条目上限
RUN_SUMMARY_TAG = "run-summary"

# 条件动作的三段（§2.3；system_metadata 里的键，与写入/检索两侧同款）。
TIP_KEYS = ("condition", "capability", "action")

# 技能条目（§2.5.4）：memory_type 标记与已发布标签。常量语义与
# skill_abstractor 同款——各实现模块自持副本，不跨模块 import 私有常量。
MEMORY_TYPE_SKILL = "skill"
SKILL_PUBLISHED_TAGS = ("approved", "trial")  # 已发布 = 通过 或 试用（§2.5.4 表）

# 第 4 步的合并判定提示词（PHASE_CONSOLIDATE / "merge"，可经部署配置替换）。
_MERGE_SYSTEM_PROMPT = """\
You judge a cluster of similar experience entries from a software engineering memory.

Decisions, all in one response:

- "merge": the entries state the same knowledge. Give the merged content that
  keeps every distinct detail (file paths, commands, error types) and drops
  duplicated wording;
- "compete": the entries give different actions for the same situation. Only
  for entries that have a condition and an action;
- "too_broad": the entry applies to almost any situation because its condition
  or action names no concrete file, command, or error. Only for entries that
  have a condition and an action.

Environmental facts take part in merge only: judge them by whether the content
is the same. Never mark a fact as compete or too_broad.

Output ONLY a JSON object, no markdown fences:
{"merges": [{"ids": ["<id>", "..."], "content": "<merged content>"}, ...],
 "competes": [{"ids": ["<id>", "..."]}, ...],
 "too_broad": ["<id>", ...]}
Leave entries not mentioned in any group unchanged. Use the entry ids exactly
as given.
"""


class ExperienceCurator(Curator):
    """经验空间的周期整理算子（§2.5.2 的八步）。"""

    def __init__(
        self,
        index_builder: IndexBuilder,
        utility: UtilityStore,
        *,
        llm: LLM | None = None,
        embedder: Embedder | None = None,
        prompt_registry: PromptRegistry | None = None,
        desensitize_identifiers: list[str] | None = None,
        curated_spaces: Sequence[str] | None = None,
        skill_spaces: Sequence[str] | None = None,
        message_store: KVStore | None = None,
        idle_retire_days: float = IDLE_RETIRE_DAYS,
        min_age_days: float = MIN_AGE_DAYS,
        attribution_min: int = HARMFUL_ATTRIBUTION_MIN,
        harmful_confidence: float = HARMFUL_CONFIDENCE,
        confidence_z: float = CONFIDENCE_Z,
        category_cap: int = CATEGORY_CAP,
        unverified_cap: int = UNVERIFIED_CAP,
        attribution_window_days: float | None = ATTRIBUTION_WINDOW_DAYS,
        trajectory_spaces: frozenset[str] | None = None,
        cluster_similarity: float = CLUSTER_SIMILARITY,
        cluster_size_limit: int = CLUSTER_SIZE_LIMIT,
        max_attempts: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._index = index_builder
        self._utility = utility
        # 第 4 步的可选依赖：LLM 与 Embedder 任一缺省时只做规则退休。
        self._llm = llm
        self._embedder = embedder
        self._prompts = prompt_registry
        # 合并内容的脱敏检查标识（§2.5.2「合并产生的条目」）：被合并条目落盘
        # 时已各自通过检查，这里是防模型在重组正文时引入主体标识；空表 = 无
        # 信号不拦截。主体标识由部署配置注入（curator 节点 ``desensitize_identifiers``）。
        self._identifiers = [str(name) for name in (desensitize_identifiers or [])]
        # 只处理部署配置列出的周期整理空间（v3 §2.5.1：周期任务可能经覆盖全
        # 组织的注册读入全库条目，未列入 curated_spaces 的空间不属于本算子的
        # 整理对象）。None = 未配置，不过滤——单测与旧装配路径行为不变。
        self._curated_spaces = (
            frozenset(curated_spaces) if curated_spaces is not None else None
        )
        # 第 7 步的技能维护开关（v3 §2.5.4）：curated_spaces 中 ``skill`` 为真
        # 的空间名集合。技能与来源经验在同一空间，第 7 步在本空间扫描技能条
        # 目。空集 = 未开启技能沉淀，第 7 步空过。message_store 与
        # UtilityStore 同源（技能的条目与效用事件都在同一 KV）。
        self._skill_spaces = frozenset(skill_spaces) if skill_spaces else frozenset()
        self._message_store = message_store
        self._idle_retire_days = float(idle_retire_days)
        self._min_age_days = float(min_age_days)
        self._attribution_min = int(attribution_min)
        self._harmful_confidence = float(harmful_confidence)
        self._confidence_z = float(confidence_z)
        self._category_cap = int(category_cap)
        self._unverified_cap = int(unverified_cap)
        # user 口径的作者限计窗；trajectory 口径的空间按轨迹数复现，不按作者
        # 限计（§3.2：限计会把单用户的复现轨迹压到时间窗内至多一条）。
        # ``trajectory_spaces`` 是装配期把 ``repro_unit`` 解析到底后的空间集
        # （auto 按落点选口径，见 ExperienceConfig.resolved_repro_unit）。
        self._attribution_window_days = attribution_window_days
        self._trajectory_spaces = trajectory_spaces
        self._cluster_similarity = float(cluster_similarity)
        self._cluster_size_limit = max(2, int(cluster_size_limit))
        self._max_attempts, self._retry_backoff_ms = validate_llm_attempt_policy(
            max_attempts, retry_backoff_ms,
        )

    def operator_type(self) -> OperatorType:
        return OperatorType.CURATOR

    def health(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    # 入口与编排
    # ------------------------------------------------------------------ #

    def curate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        started = datetime.now(UTC)
        candidates = self._entry_filter(units)
        kept: list[MemoryUnit] = []
        all_ok = True
        groups: dict[tuple[str, str], tuple[Scope, list[MemoryUnit]]] = {}
        for unit in candidates:
            key = (unit.scope.org, unit.scope.space)
            if key not in groups:
                groups[key] = (unit.scope, [])
            groups[key][1].append(unit)
        for scope, group in groups.values():
            # 空间过滤（v3 §2.5.1「周期任务只处理 curated_spaces 列出的空间」）：
            # 覆盖全组织的注册会读入全库条目，未列入 curated_spaces 的空间不属
            # 于本算子的整理对象，也不进返回集——返回集是「参与后续归纳」的
            # 条目，别的空间不该被归纳。不整理、不写运行摘要。
            if (
                self._curated_spaces is not None
                and scope.space not in self._curated_spaces
            ):
                continue
            summary: dict[str, Any] = {
                "started_at": now_iso(),
                "read_units": len(units),
                "candidate_units": len(group),
                "failed_steps": [],
            }
            group_kept, ok = self._curate_scope(scope, group, summary)
            kept.extend(group_kept)
            all_ok = all_ok and ok
            summary["elapsed_seconds"] = round(
                (datetime.now(UTC) - started).total_seconds(), 3
            )
            self._write_run_summary(scope, summary)
        if not all_ok:
            logger.warning("ExperienceCurator: some step failed, no consolidation this round")
            return []
        return kept

    @staticmethod
    def _entry_filter(units: list[MemoryUnit]) -> list[MemoryUnit]:
        """入口筛：只有生效的经验条目参与整理（§2.5.2「输入的筛选」）。

        固定逻辑，由单测固定：``memory_type`` 为 ``experience`` + ``ACTIVE``
        生命周期 + tier ∈ {SEMANTIC, PROCEDURAL}。经验与对话、技能可能同在
        一个空间（v3 §2.3 的隔离规则），对话条目（不带 ``memory_type``）与
        技能条目（第 7 步自行维护）都被排除；运行摘要（EPISODIC）与其他
        tier 的条目被跳过。
        """
        kept: list[MemoryUnit] = []
        for unit in units:
            # 逐条三筛（G.EXP.04 推导式不承载多子句条件）：生命周期 / 类型 / tier。
            if unit.lifecycle != LifecycleState.ACTIVE:
                continue
            if str(unit.system_metadata.get("memory_type", "")) != MEMORY_TYPE_EXPERIENCE:
                continue
            if unit.tier not in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL):
                continue
            kept.append(unit)
        return kept

    def _curate_scope(
        self, scope: Scope, group: list[MemoryUnit], summary: dict[str, Any]
    ) -> tuple[list[MemoryUnit], bool]:
        """一个经验空间的八步；返回 (留下的条目, 是否全部步骤成功)。"""
        # 第 0 步 按作者清除（§2.5.2）：失败本轮中止——清除没做就把被清除
        # 作者的旧事件聚回去，等于把他们的影响又算一遍。
        try:
            group = self._erase_authors(scope, group, summary)
        except Exception as exc:  # noqa: BLE001 —— 失败隔离：记摘要、本轮不归纳
            logger.warning("ExperienceCurator: erase_authors failed: %s", exc)
            summary["failed_steps"].append({"step": "erase_authors", "error": str(exc)})
            return [], False

        # 第 1 步 聚合：失败本轮中止，后面各步都依赖统计记录。
        try:
            stats = self._aggregate(scope, group, summary)
        except Exception as exc:  # noqa: BLE001 —— 失败隔离：记摘要、本轮不归纳
            logger.warning("ExperienceCurator: aggregate failed: %s", exc)
            summary["failed_steps"].append({"step": "aggregate", "error": str(exc)})
            return [], False

        remaining = list(group)
        ok = True
        steps = (
            ("retire_idle", self._retire_idle),
            ("retire_harmful", self._retire_harmful),
            ("merge_and_delete", self._merge_and_delete),
            ("eliminate_competing", self._eliminate_competing),
            ("cap_categories", self._cap_categories),
            ("handle_skills", self._handle_skills),
            ("recount_categories", self._recount_categories),
        )
        for name, step in steps:
            try:
                remaining = step(scope, remaining, stats, summary)
            except Exception as exc:  # noqa: BLE001 —— 失败隔离：跳过本步继续
                logger.warning("ExperienceCurator: step %s failed: %s", name, exc)
                summary["failed_steps"].append({"step": name, "error": str(exc)})
                ok = False
        return remaining, ok

    # ------------------------------------------------------------------ #
    # 第 0 步 按作者清除
    # ------------------------------------------------------------------ #

    def _erase_authors(
        self, scope: Scope, group: list[MemoryUnit], summary: dict[str, Any]
    ) -> list[MemoryUnit]:
        """消费清除标记：删被清除作者的效用事件，退休「只剩他们复现」的条目。

        用户空间删除后，删除回调把该空间 owners 的作者摘要写进本组织每个
        经验空间（``/erased-authors/`` 标记，见 ``construction.experience.
        make_erasure_callback``）；本步骤在聚合之前消费：删这些作者的效用
        事件（统计随聚合重算——聚合从剩余事件全量重算，被清除作者的计数
        自然消失）、复现作者只剩被清除作者的条目退休（其他作者也复现过的
        不动，他们的复现是条目继续生效的独立证据）、删标记。退休不过滤
        tier：清除是合规操作，与条目类别无关。
        """
        digests = self._utility.load_erased_authors(scope)
        if not digests:
            return group
        wanted = set(digests)
        erased_events = self._utility.delete_events_by_authors(scope, wanted)
        retired: list[MemoryUnit] = []
        for unit in group:
            authors = self._utility.load_repro_authors(scope, unit.id)
            if authors and set(authors) <= wanted:
                retired.append(unit)
        self._forget(retired)
        for digest in digests:
            self._utility.drop_erased_author(scope, digest)
        summary["erased_authors"] = len(digests)
        summary["erased_events"] = erased_events
        summary["erased_retired"] = len(retired)
        return _without(group, retired)

    # ------------------------------------------------------------------ #
    # 第 1 步 聚合
    # ------------------------------------------------------------------ #

    def _window_days_for(self, space: str) -> float | None:
        """该空间聚合的作者限计窗：trajectory 口径的空间不按作者限计（§3.2）。"""
        if self._trajectory_spaces and space in self._trajectory_spaces:
            return None
        return self._attribution_window_days

    def _aggregate(
        self, scope: Scope, group: list[MemoryUnit], summary: dict[str, Any]
    ) -> dict[str, StatsRecord]:
        """效用事件 → 统计记录 + 类别基线（§3.2），写回经验空间。"""
        events = self._utility.scan_events(scope)
        summary["events"] = len(events)
        # prior 带过 merged_into / competition_group / sealed（聚合不产生它们）。
        involved = {key.entry_id for key, _payload in events}
        involved.update(unit.id for unit in group)
        prior: dict[str, StatsRecord] = {}
        for entry_id in involved:
            record = self._utility.load_stats(scope, entry_id)
            if record is not None:
                prior[entry_id] = record
        # 被取代条目的事件计入取代它的新条目（§3.2）：重定向映射按本轮生效条目
        # 的 supersedes 字段构造，并入 prior 的 merged_into（被合并条目的事件
        # 计入合并去向）；supersedes 在后覆盖——本轮生效条目的取代关系更权威。
        # 不在本轮读入的条目事件计入自身；链式与环保护见 _resolve_redirect。
        redirects: dict[str, str] = {
            entry_id: record.merged_into
            for entry_id, record in prior.items()
            if record.merged_into
        }
        redirects.update(
            {unit.supersedes: unit.id for unit in group if unit.supersedes}
        )
        stats = aggregate_utility(
            events,
            redirects=redirects,
            window_days=self._window_days_for(scope.space),
            prior=prior,
            watermark=now_iso(),
        )
        for entry_id, record in stats.items():
            self._utility.save_stats(scope, entry_id, record)
        summary["aggregated_entries"] = len(stats)
        # 类别基线：各类别条目有效性的中位数。无事件的条目也参与（有效性回退
        # 上一轮基线）；本轮基线用上一轮的算（迭代收敛，破 §2.4 的循环依赖）。
        stats_for_baseline = dict(stats)
        for unit in group:
            if unit.id not in stats_for_baseline:
                stats_for_baseline[unit.id] = prior.get(unit.id) or StatsRecord()
        categories = {unit.id: _category_of(unit) for unit in group}
        baseline = compute_baseline(
            stats_for_baseline,
            categories,
            prior_baseline=self._utility.load_baseline(scope),
        )
        self._utility.save_baseline(scope, baseline)
        return stats

    # ------------------------------------------------------------------ #
    # 第 2、3 步 退休
    # ------------------------------------------------------------------ #

    def _retire_idle(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """退闲置：60 天没有被取用、且落盘已满 14 天的。

        从未被取用的条目（统计记录无 last_used_at）闲置从落盘起算——落盘即
        最后一次接触。时间不可解析的条目不判闲置（保守，不退休）。
        """
        now = datetime.now(UTC)
        retired: list[MemoryUnit] = []
        for unit in units:
            record = stats.get(unit.id)
            last_used = record.last_used_at if record is not None else ""
            ingest_at = _iso_of(unit.temporal.t_ingest)
            idle_days = days_since(last_used or ingest_at, now=now)
            age_days = days_since(ingest_at, now=now)
            if idle_days is None or age_days is None:
                continue
            if idle_days >= self._idle_retire_days and age_days >= self._min_age_days:
                retired.append(unit)
        self._forget(retired)
        summary["retired_idle"] = len(retired)
        return _without(units, retired)

    def _retire_harmful(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """有害退休：归因满 5 次、有害率的 95% 置信下界超过 0.5 的。"""
        retired: list[MemoryUnit] = []
        for unit in units:
            record = stats.get(unit.id)
            if record is None or record.covered < self._attribution_min:
                continue
            positive = weighted_count(record.helpful)
            negative = weighted_count(record.harmful)
            lower = _wilson_lower(negative, positive + negative, self._confidence_z)
            if lower > self._harmful_confidence:
                retired.append(unit)
        self._forget(retired)
        summary["retired_harmful"] = len(retired)
        return _without(units, retired)

    # ------------------------------------------------------------------ #
    # 第 4 步 合并与删除（模型聚簇与三结论）
    # ------------------------------------------------------------------ #

    def _merge_and_delete(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """按 (类别, tier) 聚簇、每簇一次模型：合并 / 竞争组 / 过于宽泛退休。

        三段不全的条件动作先按规则退休（不调模型）；LLM 或 Embedder 未装配时
        到此为止。三结论同一簇一次调用给出；结论重叠时过于宽泛优先于合并、
        合并优先于竞争（过于宽泛的条目不值得合并，被合并的条目退出竞争）。
        """
        # 三段不全的条件动作按规则判定退休（§2.5.2「三段不全」）。
        incomplete = [
            unit for unit in units
            if unit.tier == MemoryTier.PROCEDURAL and not _tip_complete(unit)
        ]
        self._forget(incomplete)
        remaining = _without(units, incomplete)
        summary["retired_incomplete"] = len(incomplete)

        summary["merged"] = 0
        summary["competed"] = 0
        summary["retired_too_broad"] = 0
        if self._llm is None or self._embedder is None:
            return remaining
        summary["skipped_clusters"] = 0
        summary["blocked_merges"] = 0

        retired: list[MemoryUnit] = []
        # 合并产生的新条目汇入 remaining，供第 5/6/8 步把新条目纳入竞争组
        # 比较、类别计数（继承的统计已在 stats 里）。新条目不在任何簇内，
        # 不会与其他簇的退休集合冲突。
        created: list[MemoryUnit] = []
        for cluster in self._cluster_units(remaining):
            try:
                verdict = self._judge_cluster(cluster)
            except Exception as exc:  # noqa: BLE001 —— 簇级容错：跳过该簇
                logger.warning("ExperienceCurator: cluster judge failed: %s", exc)
                summary["skipped_clusters"] += 1
                continue
            retired_ids = self._apply_verdict(scope, cluster, verdict, stats, summary, created)
            retired.extend(unit for unit in cluster if unit.id in retired_ids)
        remaining = _without(remaining, retired)
        remaining.extend(created)
        return remaining

    def _apply_verdict(
        self,
        scope: Scope,
        cluster: list[MemoryUnit],
        verdict: Mapping[str, Any],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
        created: list[MemoryUnit],
    ) -> set[str]:
        """执行一簇的三结论；返回本簇内被退休（含被合并）的条目 id 集。

        模型返回的 id 逐一对照簇内成员（幻觉 id 丢弃）；环境事实只参与
        merge——compete / too_broad 里的 SEMANTIC 条目忽略（§2.5.2「模型调用」）。
        """
        by_id = {unit.id: unit for unit in cluster}
        retired_ids: set[str] = set()

        for raw_id in _id_list(verdict.get("too_broad")):
            unit = by_id.get(raw_id)
            if unit is None or unit.id in retired_ids:
                continue
            if unit.tier != MemoryTier.PROCEDURAL:
                continue
            retired_ids.add(unit.id)
            summary["retired_too_broad"] += 1

        for group in _group_list(verdict.get("merges")):
            ids = [i for i in _id_list(group.get("ids")) if i not in retired_ids]
            members: list[MemoryUnit] = []
            for entry_id in ids:
                unit = by_id.get(entry_id)
                if unit is not None and unit.id not in {m.id for m in members}:
                    members.append(unit)
            content = str(group.get("content", "")).strip()
            if len(members) < 2 or not content:
                continue
            new_unit = self._apply_merge(scope, members, content, stats, summary)
            if new_unit is not None:
                retired_ids.update(member.id for member in members)
                created.append(new_unit)

        for group in _group_list(verdict.get("competes")):
            members: list[MemoryUnit] = []
            for entry_id in _id_list(group.get("ids")):
                # 三筛展开成顺序 if（G.EXP.04）：存在 / 未被本轮退休 / PROCEDURAL。
                if entry_id not in by_id or entry_id in retired_ids:
                    continue
                if by_id[entry_id].tier != MemoryTier.PROCEDURAL:
                    continue
                members.append(by_id[entry_id])
            if len(members) < 2:
                continue
            self._apply_compete(scope, members, stats)
            summary["competed"] += 1
        return retired_ids

    def _apply_merge(
        self,
        scope: Scope,
        members: list[MemoryUnit],
        content: str,
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> MemoryUnit | None:
        """落盘一条合并产生的新条目；旧条目退休、统计由新条目继承。

        新条目经 ``build``（本体 + 检索索引）进入检索；类别沿用被合并条目、
        ``provenance`` 记被合并条目的 id、落盘时间取最早的（不进隔离期）。
        合并内容写入前做脱敏检查（§2.5.2「合并产生的条目」），命中即放弃该
        组合并且旧条目保留。返回 ``None`` 表示该组合未执行。
        """
        if self._identifiers and desensitization_hit(content, self._identifiers):
            logger.warning(
                "ExperienceCurator: merged content blocked by desensitization check"
            )
            summary["blocked_merges"] += 1
            return None
        now = datetime.now(UTC)
        template = members[0]
        earliest = min(
            (unit.temporal.t_ingest for unit in members if unit.temporal.t_ingest),
            default=now,
        )
        tags: list[str] = []
        for unit in members:
            for tag in unit.tags:
                if tag and tag not in tags:
                    tags.append(tag)
        new_unit = MemoryUnit(
            id=str(uuid.uuid4()),
            scope=scope,
            tier=template.tier,
            segments=[Segment(content=content)],
            temporal=Temporal(t_ingest=earliest, t_valid=now),
            provenance=[unit.id for unit in members],
            tags=tags,
            system_metadata=dict(template.system_metadata),
        )
        # 统计继承：计数相加、作者取并集、时间取 max（§2.5.2）；merged_into /
        # competition_group 不继承——新条目刚诞生无去向，合并即退出竞争。
        inherited = _inherit_stats(
            [stats.get(unit.id) or StatsRecord() for unit in members],
            watermark=now_iso(),
        )
        stats[new_unit.id] = inherited
        self._utility.save_stats(scope, new_unit.id, inherited)
        # 复现作者由新条目继承（§2.5.2「合并产生的条目」）：记录取被合并条目
        # 的并集（save 内部截 REPRO_AUTHOR_LIMIT）——按作者限计读合并后的
        # 记录判断「已有两个独立作者复现」。
        repro_authors: list[str] = []
        for unit in members:
            for author in self._utility.load_repro_authors(scope, unit.id):
                if author and author not in repro_authors:
                    repro_authors.append(author)
        if repro_authors:
            self._utility.save_repro_authors(scope, new_unit.id, repro_authors)
        summary["merged"] += 1
        # 旧条目：统计记录记合并去向（下一轮聚合据此重定向事件），条目退休。
        for unit in members:
            record = stats.get(unit.id)
            if record is None:
                record = StatsRecord(watermark=now_iso())
                stats[unit.id] = record
            record.merged_into = new_unit.id
            self._utility.save_stats(scope, unit.id, record)
        self._forget(members)
        self._index.build([new_unit])
        return new_unit

    def _apply_compete(
        self, scope: Scope, members: list[MemoryUnit], stats: dict[str, StatsRecord]
    ) -> None:
        """一组互为替代的条件动作记同一竞争组（组号写统计记录）。"""
        group_id = f"g-{uuid.uuid4().hex[:8]}"
        for unit in members:
            record = stats.get(unit.id)
            if record is None:
                record = StatsRecord(watermark=now_iso())
                stats[unit.id] = record
            record.competition_group = group_id
            self._utility.save_stats(scope, unit.id, record)

    def _cluster_units(self, units: list[MemoryUnit]) -> list[list[MemoryUnit]]:
        """按 (类别, tier) 分桶后贪心聚簇，返回 ≥2 条的簇。

        环境事实与条件动作不同桶（「环境事实只参与 merge」由分桶保证一半）。
        贪心：顺序遍历，与某簇心余弦相似度 ≥ 阈值则入簇（簇满即封），否则
        自成一簇；簇心取簇内向量的增量均值。
        """
        if self._embedder is None:  # _merge_and_delete 已挡；这里为类型窄化
            raise RuntimeError("embedder is not configured")
        buckets: dict[tuple[str, str], list[MemoryUnit]] = {}
        for unit in units:
            buckets.setdefault((_category_of(unit), unit.tier.value), []).append(unit)
        clusters: list[list[MemoryUnit]] = []
        for members in buckets.values():
            if len(members) < 2:
                continue
            vectors = self._embedder.embed([unit.content for unit in members])
            centers: list[list[float]] = []
            groups: list[list[int]] = []
            for i, vector in enumerate(vectors):
                for ci, center in enumerate(centers):
                    if len(groups[ci]) >= self._cluster_size_limit:
                        continue
                    if _cosine(vector, center) >= self._cluster_similarity:
                        groups[ci].append(i)
                        n = len(groups[ci])
                        for j, x in enumerate(vector):  # 簇心增量均值
                            center[j] += (x - center[j]) / n
                        break
                else:
                    centers.append(list(vector))
                    groups.append([i])
            clusters.extend(
                [members[i] for i in group] for group in groups if len(group) >= 2
            )
        return clusters

    def _judge_cluster(self, cluster: list[MemoryUnit]) -> dict:
        """一簇一次模型调用（含重试），返回三结论的 JSON object。"""
        lines = []
        for unit in cluster:
            entry: dict[str, str] = {"id": unit.id, "content": unit.content}
            if unit.tier == MemoryTier.PROCEDURAL:
                entry.update(
                    (key, str(unit.system_metadata.get(key, "")).strip())
                    for key in TIP_KEYS
                )
            lines.append(json.dumps(entry, ensure_ascii=False))
        prompt = _MERGE_SYSTEM_PROMPT
        if self._prompts is not None:
            prompt = self._prompts.get(PHASE_CONSOLIDATE, "merge") or prompt
        return self._call_llm(prompt, "\n".join(lines))

    def _call_llm(self, system_prompt: str, user_text: str) -> dict:
        """一次模型调用（含指数退避重试），返回解析后的 JSON object。

        与 ``ExperienceAppraiser._call_llm`` 同构（ChatMessage / temperature=0 /
        花括号兜底解析）；两处出现同构先接受，第三处出现时再抽公共。
        """
        if self._llm is None:  # _merge_and_delete 已挡；这里为类型窄化
            raise RuntimeError("LLM is not configured")
        from jiuwen_memory.common.type_def.chat import ChatMessage

        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_text),
        ]
        last_exc: Exception | None = None
        response = ""
        for attempt in range(self._max_attempts):
            try:
                response = self._llm.chat(messages, temperature=0, max_tokens=2048)
                return _parse_object(response)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    wait = self._retry_backoff_ms * (2**attempt) / 1000.0
                    logger.warning(
                        "ExperienceCurator: LLM call failed (attempt %d), retry in %.1fs",
                        attempt + 1, wait,
                    )
                    time.sleep(wait)
        logger.warning(
            "ExperienceCurator: LLM response unparsable after %d attempts: %s",
            self._max_attempts,
            (response or "")[:400],
        )
        raise last_exc if last_exc else RuntimeError("LLM call did not execute")

    # ------------------------------------------------------------------ #
    # 第 5 步 淘汰（竞争组内两两比较）
    # ------------------------------------------------------------------ #

    def _eliminate_competing(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """竞争组内两两比较：都归因满下限且有效性差距的 95% 置信下界大于 0
        的，退休较低的一条（竞争组由第 4 步的模型结论给出）。"""
        baseline = self._utility.load_baseline(scope)
        by_group: dict[str, list[MemoryUnit]] = {}
        for unit in units:
            record = stats.get(unit.id)
            group_id = record.competition_group if record is not None else ""
            if group_id:
                by_group.setdefault(group_id, []).append(unit)
        retired_ids: set[str] = set()

        def _validity_of(unit: MemoryUnit) -> float:
            record = stats[unit.id]
            return validity(record, baseline.get(_category_of(unit), DEFAULT_BASELINE))

        def _sample_of(unit: MemoryUnit) -> float:
            record = stats[unit.id]
            return weighted_count(record.helpful) + weighted_count(record.harmful)

        for members in by_group.values():
            ordered = sorted(members, key=_validity_of, reverse=True)
            for i, high in enumerate(ordered):
                if high.id in retired_ids:
                    continue
                rh = stats[high.id]
                if rh.covered < self._attribution_min:
                    continue
                vh, nh = _validity_of(high), _sample_of(high)
                for low in ordered[i + 1:]:
                    if low.id in retired_ids:
                        continue
                    rl = stats[low.id]
                    if rl.covered < self._attribution_min:
                        continue
                    vl, nl = _validity_of(low), _sample_of(low)
                    if _difference_lower_bound(vh, nh, vl, nl, self._confidence_z) > 0:
                        retired_ids.add(low.id)
        retired = [unit for unit in units if unit.id in retired_ids]
        self._forget(retired)
        summary["eliminated_competing"] = len(retired)
        return _without(units, retired)

    # ------------------------------------------------------------------ #
    # 第 6 步 控制规模
    # ------------------------------------------------------------------ #

    def _cap_categories(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """每类别留质量分最高的 500 条，其中归因不足的至多 100 条（§2.5.2）。

        归因不足的条目另留配额是给新条目攒归因的机会（探索名额的检索侧同款
        考量，§2.4）。质量分 = 0.7×有效性（用上一轮基线）+ 0.3×新鲜度。
        """
        now = datetime.now(UTC)
        baseline = self._utility.load_baseline(scope)
        by_category: dict[str, list[MemoryUnit]] = {}
        for unit in units:
            by_category.setdefault(_category_of(unit), []).append(unit)
        retired: list[MemoryUnit] = []
        scaled: dict[str, int] = {}
        for category, members in by_category.items():
            if len(members) <= self._category_cap:
                continue

            def _score(unit: MemoryUnit, _category: str = category) -> float:
                # _category 经默认参数绑定 def 时的循环值（G.FNM.02）：_score 只在
                # 本轮迭代内使用，但闭包晚绑定仍是静态检查的告警源。
                record = stats.get(unit.id)
                base = baseline.get(_category, DEFAULT_BASELINE)
                entry_validity = validity(record, base) if record is not None else base
                last_positive = record.last_positive_at if record is not None else ""
                entry_freshness = freshness(
                    last_positive, _iso_of(unit.temporal.t_ingest), now=now
                )
                return quality_score(entry_validity, entry_freshness)

            members.sort(key=_score, reverse=True)
            unverified: list[MemoryUnit] = []
            for unit in members:
                # 海象换显式循环（G.OPR.04）：无统计或归因不足即未验证。
                record = stats.get(unit.id)
                if record is None or record.covered < self._attribution_min:
                    unverified.append(unit)
            unverified_ids = {unit.id for unit in unverified}
            verified = [unit for unit in members if unit.id not in unverified_ids]
            keep_unverified = unverified[: self._unverified_cap]
            keep_verified = verified[: self._category_cap - len(keep_unverified)]
            keep_ids = {unit.id for unit in keep_unverified} | {
                unit.id for unit in keep_verified
            }
            dropped = [unit for unit in members if unit.id not in keep_ids]
            retired.extend(dropped)
            if dropped:
                scaled[category] = len(dropped)
        self._forget(retired)
        summary["scaled_down"] = len(retired)
        summary["scaled_down_by_category"] = scaled
        return _without(units, retired)

    # ------------------------------------------------------------------ #
    # 第 7 步 技能（§2.5.7）
    # ------------------------------------------------------------------ #

    def _handle_skills(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """技能的聚合回写与状态维护（§2.5.7）：计数回写、有害退休、试用转正、
        旧版取代。

        技能与来源经验在同一空间（v3 §2.5.4），技能的聚合与退休由本空间的
        周期任务一并处理。技能条目不经 ``units`` 进来（``curate`` 入口的
        ``_entry_filter`` 按 ``memory_type`` 只放行经验条目），本步自行扫描
        本空间的技能条目；本空间未开启技能沉淀（``curated_spaces`` 中该空间
        ``skill`` 不为真）时空过，经验条目集原样返回。

        技能的效用事件与统计记录以 ``skill:{名}:{版本}`` 作键写在本空间
        （§2.5.5：条目标识改变时使用反馈不中断），聚合规则与经验相同。试用
        转正的有效性基线取本空间的类别基线——技能的类别继承自来源经验
        （skill_abstractor 落草稿时带过），只按技能条目算不出稳定的中位数。
        基线无该类别时取冷启动默认（与检索侧质量分同款）。
        """
        if scope.space not in self._skill_spaces or self._message_store is None:
            return units
        skills = self._load_skills(scope)
        skill_stats = self._aggregate_skills(scope, summary)
        baseline = self._utility.load_baseline(scope)

        summary["skills_handled"] = len(skills)
        # 计数回写（§2.5.7「聚合」）：有帮助、有害与加载次数写进条目元数据，
        # 审核人用 get 可见。有帮助/有害用加权次数（与有害退休判定同口径），
        # 加载次数 = 归因事件总数（covered 只计有帮助与有害，加 neutral）。
        rewritten: list[MemoryUnit] = []
        for unit in skills:
            record = skill_stats.get(_skill_stats_key(unit))
            if record is None:
                continue
            unit.system_metadata["skill_helpful"] = weighted_count(record.helpful)
            unit.system_metadata["skill_harmful"] = weighted_count(record.harmful)
            unit.system_metadata["skill_loads"] = record.covered + record.neutral
            rewritten.append(unit)

        # 处理开始时的试用存量（§2.5.5「运行摘要记下本轮自动发布……的条数」：
        # 新发布的试用在整理之后的归纳阶段才写入，第 7 步看到的是存量口径）。
        summary["skills_in_trial"] = sum(
            1
            for unit in skills
            if unit.lifecycle == LifecycleState.ACTIVE and "trial" in unit.tags
        )

        # 有害退休：与经验同一规则（§2.5.7），作用于全部生效技能——已发布的
        # 才有加载途径，draft 凑不满归因下限，统一按规则判定不加标签过滤。
        retired: list[MemoryUnit] = []
        for unit in skills:
            if unit.lifecycle != LifecycleState.ACTIVE:
                continue
            record = skill_stats.get(_skill_stats_key(unit))
            if record is None or record.covered < self._attribution_min:
                continue
            positive = weighted_count(record.helpful)
            negative = weighted_count(record.harmful)
            lower = _wilson_lower(negative, positive + negative, self._confidence_z)
            if lower > self._harmful_confidence:
                retired.append(unit)
        self._forget(retired)
        summary["skills_retired"] = len(retired)

        # 试用转正（§2.5.5）：归因满下限且有效性不低于同类别基线 → approved。
        promoted: list[MemoryUnit] = []
        for unit in skills:
            if unit.lifecycle != LifecycleState.ACTIVE or "trial" not in unit.tags:
                continue
            if unit in retired:
                continue
            record = skill_stats.get(_skill_stats_key(unit))
            if record is None or record.covered < self._attribution_min:
                continue
            base = baseline.get(_category_of(unit), DEFAULT_BASELINE)
            if validity(record, base) >= base:
                unit.tags = [
                    "approved" if tag == "trial" else tag for tag in unit.tags
                ]
                promoted.append(unit)
        summary["skills_promoted"] = len(promoted)

        # 旧版取代（§2.5.7）：同名已发布的版本最大的为现行版，其余同名已发布
        # 旧版标记 SUPERSEDED（与 evolver 的 SUPERSEDE 同款：lifecycle + 失效
        # 时间 + update）。draft 不参与——待审的新版不是「旧版」，发布后才触发
        # 取代；已退休的不再参与（FORGOTTEN 已退出，无需再取代）。
        by_name: dict[str, list[MemoryUnit]] = {}
        for unit in skills:
            if (
                unit.lifecycle == LifecycleState.ACTIVE
                and _is_published_skill(unit)
                and unit not in retired
            ):
                by_name.setdefault(_skill_name_of(unit), []).append(unit)
        superseded: list[MemoryUnit] = []
        for members in by_name.values():
            if len(members) < 2:
                continue
            members.sort(key=_skill_version_of, reverse=True)
            for unit in members[1:]:
                unit.lifecycle = LifecycleState.SUPERSEDED
                unit.temporal.t_invalid = datetime.now(UTC)
                superseded.append(unit)
        summary["skills_superseded"] = len(superseded)

        # 落盘：计数回写、转正与取代都改了条目本体，经 IndexBuilder 回写
        # （FORWARD_ONLY 只写本体——技能不经检索使用，正文索引无读取方，
        # §2.5.4）。退休走 _forget 的两条指令，已在上文落盘。
        changed: dict[str, MemoryUnit] = {}
        for unit in rewritten + promoted + superseded:
            changed[unit.id] = unit
        if changed:
            self._index.update(list(changed.values()), mode=IndexWriteMode.FORWARD_ONLY)
        return units

    def _load_skills(self, scope: Scope) -> list[MemoryUnit]:
        """读本空间的全部技能条目（与 skill_abstractor._load_skills 同款）。"""
        skills: list[MemoryUnit] = []
        for _key, raw in self._message_store.scan(scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None:
                continue
            if str(unit.system_metadata.get("memory_type", "")) == MEMORY_TYPE_SKILL:
                skills.append(unit)
        return skills

    def _aggregate_skills(
        self, scope: Scope, summary: dict[str, Any]
    ) -> dict[str, StatsRecord]:
        """本空间的技能效用事件 → 统计记录写回本空间（§2.5.7「聚合」）。

        聚合规则与经验相同（同一条轨迹取最新修订、归因按作者限计），但不带
        prior、不做重定向：技能没有合并与竞争组（第 4 步只整理经验条目），
        技能也豁免按作者清除（§2.5.2「不清除的内容」），聚合每次从本空间
        的全量事件重算即可。
        """
        events = self._utility.scan_events(scope)
        summary["skills_events"] = len(events)
        records = aggregate_utility(
            events,
            window_days=self._window_days_for(scope.space),
            watermark=now_iso(),
        )
        for entry_id, record in records.items():
            self._utility.save_stats(scope, entry_id, record)
        return records

    # ------------------------------------------------------------------ #
    # 第 8 步 收尾
    # ------------------------------------------------------------------ #

    def _recount_categories(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        stats: dict[str, StatsRecord],
        summary: dict[str, Any],
    ) -> list[MemoryUnit]:
        """各类别的生效条目数覆写类别计数记录，沉淀的写入上限读它（§2.5.2）。"""
        active: dict[str, int] = {}
        for unit in units:
            category = _category_of(unit)
            active[category] = active.get(category, 0) + 1
        for category, count in active.items():
            dropped = self._utility.load_category_count(scope, category).get("dropped", 0)
            self._utility.save_category_count(
                scope, category, {"active": count, "dropped": dropped}
            )
        summary["categories_active"] = active
        return units

    # ------------------------------------------------------------------ #
    # 公共小件
    # ------------------------------------------------------------------ #

    def _forget(self, units: list[MemoryUnit]) -> None:
        """退休：``FORGOTTEN`` 并退出检索（与 ``_evolve_forget`` 同款两条指令）。

        条目与统计记录保留；``IndexBuilder`` 不解读 lifecycle，只执行被要求的
        操作。
        """
        if not units:
            return
        for unit in units:
            unit.lifecycle = LifecycleState.FORGOTTEN
        self._index.update(units, mode=IndexWriteMode.FORWARD_ONLY)  # 只回写本体新状态
        self._index.remove(units, mode=IndexRemoveMode.SOFT)         # 检索索引移出检索

    def _write_run_summary(self, scope: Scope, summary: Mapping[str, Any]) -> None:
        """运行摘要（§2.5.2）：EPISODIC 条目、标签 ``run-summary``、正文 JSON。

        只写本体不建索引（``FORWARD_ONLY``），``search`` 取不到；``memory_type``
        为 ``experience``（v3 §2.5.2 形式行），编辑者用 ``list`` 按
        ``memory_type=experience`` 与标签列出、``get`` 查看。归纳的统计由升华
        算子（SkillAbstractor）补进同一条。
        """
        now = datetime.now(UTC)
        unit = MemoryUnit(
            id=str(uuid.uuid4()),
            scope=scope,
            tier=MemoryTier.EPISODIC,
            segments=[Segment(content=json.dumps(summary, ensure_ascii=False))],
            tags=[RUN_SUMMARY_TAG],
            temporal=Temporal(t_ingest=now, t_valid=now),
            system_metadata={"memory_type": MEMORY_TYPE_EXPERIENCE},
        )
        self._index.build([unit], mode=IndexWriteMode.FORWARD_ONLY)


# -- 模块级小件 -------------------------------------------------------- #


def _category_of(unit: MemoryUnit) -> str:
    """条目的类别（经验条目落盘时写进 system_metadata["category"]，§2.3）。"""
    return str(unit.system_metadata.get("category", "")).strip()


def _skill_name_of(unit: MemoryUnit) -> str:
    """技能条目的技能名（system_metadata["skill_name"]，§2.5.4 落草稿时写入）。"""
    return str(unit.system_metadata.get("skill_name", "")).strip()


def _skill_version_of(unit: MemoryUnit) -> int:
    """技能条目的版本号（非法值按 1——保守取首版，不因元数据脏而炸整步）。"""
    try:
        return int(unit.system_metadata.get("skill_version", 1))
    except (TypeError, ValueError):
        return 1


def _skill_stats_key(unit: MemoryUnit) -> str:
    """技能的效用事件与统计记录的键：``skill:{技能名}:{版本号}``（§2.5.5）。"""
    return f"skill:{_skill_name_of(unit)}:{_skill_version_of(unit)}"


def _is_published_skill(unit: MemoryUnit) -> bool:
    """技能是否已发布：标签含 approved（已通过）或 trial（试用，§2.5.4）。"""
    return any(tag in SKILL_PUBLISHED_TAGS for tag in unit.tags)


def _tip_complete(unit: MemoryUnit) -> bool:
    """条件动作三段（condition/capability/action）是否齐全（§2.3 落盘时写入
    system_metadata；键与写入/检索两侧同款）。"""
    metadata = unit.system_metadata
    return all(str(metadata.get(key, "")).strip() for key in TIP_KEYS)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（任一零向量返回 0，即永不相似）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _inherit_stats(records: list[StatsRecord], *, watermark: str) -> StatsRecord:
    """被合并条目的统计记录由新条目继承（§2.5.2）：计数相加、作者取并集、
    时间取 max（空串最小，不参与比较）。merged_into / competition_group /
    watermark 不从旧记录带过。"""
    merged = StatsRecord(watermark=watermark)
    for record in records:
        for level, count in record.helpful.items():
            merged.helpful[level] = merged.helpful.get(level, 0) + count
        for level, count in record.harmful.items():
            merged.harmful[level] = merged.harmful.get(level, 0) + count
        merged.covered += record.covered
        merged.neutral += record.neutral
        for author in record.authors:
            if author and author not in merged.authors:
                merged.authors.append(author)
        for author, count in record.author_success.items():
            if author:
                merged.author_success[author] = (
                    merged.author_success.get(author, 0) + count
                )
        merged.strong_success += record.strong_success
        merged.last_used_at = max(merged.last_used_at, record.last_used_at)
        merged.last_positive_at = max(merged.last_positive_at, record.last_positive_at)
        for key, count in record.sealed.items():
            merged.sealed[key] = merged.sealed.get(key, 0) + count
    return merged


def _id_list(raw: Any) -> list[str]:
    """模型结论里的 id 列表：只收非空字符串，保持顺序、去重。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        entry_id = str(item).strip() if item is not None else ""
        if entry_id and entry_id not in out:
            out.append(entry_id)
    return out


def _group_list(raw: Any) -> list[Mapping[str, Any]]:
    """模型结论里的分组列表（merges / competes）：只收 dict。"""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _parse_object(response: str) -> dict:
    """解析模型返回的 JSON object；失败再尝试提取最外层花括号段。

    与 ``experience_extractor._parse_object`` / ``experience_appraiser._parse_object``
    同构——三处出现各自持有副本，不跨实现模块 import 私有函数。
    """
    text = (response or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise
        payload = json.loads(match.group())
    if not isinstance(payload, dict):
        raise ValueError("curate 响应必须是 JSON object")
    return payload


def _iso_of(at: datetime | None) -> str:
    """datetime 转 ISO 字符串（时间字段缺 None 时给空串）。"""
    return at.isoformat() if at is not None else ""


def _without(units: list[MemoryUnit], retired: list[MemoryUnit]) -> list[MemoryUnit]:
    """从 units 中去掉 retired（按 id，保持原顺序）。"""
    retired_ids = {unit.id for unit in retired}
    return [unit for unit in units if unit.id not in retired_ids]


def _wilson_lower(successes: float, total: float, z: float) -> float:
    """Wilson score 区间下界（单比例；第 3 步有害退休用，total 非正返回 0）。"""
    if total <= 0:
        return 0.0
    p_hat = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p_hat + z2 / (2.0 * total)) / denom
    half = (z / denom) * math.sqrt(
        p_hat * (1.0 - p_hat) / total + z2 / (4.0 * total * total)
    )
    return center - half


def _difference_lower_bound(
    p_high: float, n_high: float, p_low: float, n_low: float, z: float
) -> float:
    """两比例之差的置信下界（Wald；第 5 步竞争组淘汰用，样本非正返回 0）。"""
    if n_high <= 0 or n_low <= 0:
        return 0.0
    stderr = math.sqrt(
        p_high * (1.0 - p_high) / n_high + p_low * (1.0 - p_low) / n_low
    )
    return (p_high - p_low) - z * stderr


# -- 注册到 CuratorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@CuratorProducer.register("experience")
def _build(config):
    """装配 ExperienceCurator：IndexBuilder 经引用与 evolver 共享同一实例，
    KV 与经验评价/抽取算子同源（事件、统计与类别计数都在同一空间的 KV）。

    LLM 与 Embedder 按 curator 节点的 ``llm`` / ``embedder`` 字段可选：配置了
    引用或内联才装配（``Factory.dep`` 缺省即报错，可选依赖先查字段再取）；
    任一缺省时第 4 步只做规则退休，整理不因缺模型配置而整轮失败。

    部署配置（v3 §2 / §2.5.1）：``globals.experience`` 的 ``curated_spaces``
    给出周期整理空间清单（只处理清单内的空间）与各空间的 ``repro_unit``——
    装配期解析到底（``auto`` 按落点选口径），trajectory 口径的空间逐空间
    关闭作者限计（§3.2：两种计数法的限计错误代价不对称，误关只是轻度高估，
    误开会让单用户部署的复现轨迹数被压到时间窗内至多一条）。
    ``desensitize_identifiers`` 是部署在 curator 节点声明的
    主体标识清单，合并重组的内容命中即拦截。各空间的 ``skill`` 字段给出
    技能沉淀开关（§2.5.4），第 7 步只维护开启的空间；无任何空间开启时第
    7 步空过。
    """
    from jiuwen_memory.common.embedder.base import EmbedderProducer
    from jiuwen_memory.common.llm.base import LlmProducer
    from jiuwen_memory.config.config_source import ConfigSourceProducer
    from jiuwen_memory.construction.experience import (
        REPRO_UNIT_TRAJECTORY,
        parse_experience_config,
        space_naming_of,
    )

    experience = parse_experience_config(config.get("experience"))
    if experience is None:
        raise ValidationError(
            "globals.experience 未配置：经验 profile 装配失败（经验沉淀以配置了"
            "这一项为前提，v3 §2）"
        )
    prompts_data = config.get("prompts")
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )
    llm = LlmProducer.dep(config) if config.get("llm") is not None else None
    embedder = (
        EmbedderProducer.dep(config) if config.get("embedder") is not None else None
    )
    # repro_unit 装配期解析到底（§2.4）：auto 按落点选口径——经验落在写入者
    # 自己的空间（fallback 模板形态）按轨迹、其余按用户；无判定表时按
    # default_space。trajectory 口径的空间逐空间关闭作者限计。
    naming = space_naming_of(config)
    trajectory_spaces = frozenset(
        item.space
        for item in experience.curated_spaces
        if experience.resolved_repro_unit(item.space, naming) == REPRO_UNIT_TRAJECTORY
    )
    identifiers = config.get("desensitize_identifiers")
    message_store = resolve_message_store(config)
    return ExperienceCurator(
        index_builder=IndexBuilderProducer.dep(
            config, "index_builder", default=resolve_index_builder_default(config)
        ),
        utility=UtilityStore(message_store),
        message_store=message_store,
        skill_spaces=experience.skill_space_names,
        llm=llm,
        embedder=embedder,
        prompt_registry=registry,
        desensitize_identifiers=(
            [str(name) for name in identifiers] if identifiers else None
        ),
        curated_spaces=experience.curated_space_names,
        attribution_window_days=ATTRIBUTION_WINDOW_DAYS,
        trajectory_spaces=trajectory_spaces,
        max_attempts=config.get("curator_max_attempts", 3),
        retry_backoff_ms=config.get("curator_retry_backoff", 1000),
    )
