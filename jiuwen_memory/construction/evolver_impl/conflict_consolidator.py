# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Schema 模式记忆冲突巩固的规划器（``EvolveMode.CONFLICT_CONSOLIDATE``）。

把 ACTIVE 的 Schema Property 记忆按 ``(schema_entity_id, schema_property_name)`` 成簇，
再用两段 LLM（关系检测 → 动作规划）产出**无副作用的变更计划**。计划由
``OrchestratingEvolver`` 应用，本模块从不落盘。
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.schema_property import (
    is_schema_property_unit,
    schema_property_entity_id,
)
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory import MEMORY_KEY_PREFIX, Segment
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.schema_prompts import (
    CONFLICT_ACTION_PLANNING_PROMPT,
    CONFLICT_RELATION_DETECTION_PROMPT,
)
from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
from jiuwen_memory.storage.kv import KVStore, load_units

logger = get_logger(__name__)

_ACTION_TYPES = frozenset({"create", "merge", "update", "archive"})
_ISSUE_TYPES = frozenset({"conflict", "duplicate", "near_duplicate", "complementary"})


@dataclass(slots=True)
class Cluster:
    """同 (entity_id, property_name) 的一组 ACTIVE 属性记忆。"""

    entity_id: str
    property_name: str
    entity_name: str = ""
    entity_type: str = ""
    memories: list[MemoryUnit] = field(default_factory=list)


@dataclass(slots=True)
class IssueGroup:
    """LLM #1 输出的一个簇内问题组。"""

    issue_type: str
    memory_ids: list[str] = field(default_factory=list)
    value_hints: dict[str, str] = field(default_factory=dict)
    confidence: str = "medium"
    reason: str = ""


@dataclass(slots=True)
class ConsolidatePlan:
    """一次巩固的完整变更计划（无副作用，由调用方应用）。

    - ``additions``：新建的 ACTIVE 属性记忆（create / merge 产物）；
    - ``replacements``：取代旧版本的新版本（update 产物，``supersedes`` 指向被取代者）；
    - ``retire``：应置 ``SUPERSEDED`` 的旧版本 / 被并源。
    """

    additions: list[MemoryUnit] = field(default_factory=list)
    replacements: list[MemoryUnit] = field(default_factory=list)
    retire: list[MemoryUnit] = field(default_factory=list)
    # 已成功处理的簇成员 id（供调用方打 done 标记）；LLM 失败的簇不加入，留待重试。
    processed_unit_ids: set[str] = field(default_factory=set)
    run_id: str = ""


class ConflictConsolidator:
    """产出冲突巩固计划；只读真源 + 调 LLM，不落盘。"""

    def __init__(
        self,
        *,
        kv: KVStore,
        llm: LLM,
        min_cluster_size: int = 2,
        fallback_limit: int = 1000,
        max_tokens: int = 2048,
    ) -> None:
        self._kv = kv
        self._llm = llm
        self._property_index = SchemaPropertyIndex(kv)
        self._min_cluster_size = max(1, int(min_cluster_size))
        self._fallback_limit = max(1, int(fallback_limit))
        self._max_tokens = max(1, int(max_tokens))

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def load_fresh(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        """按 id 从真源重读；供落盘侧打 done 标记前取最新对象。"""

        return load_units(self._kv, scope, unit_ids)

    def plan(self, seed_units: list[MemoryUnit]) -> ConsolidatePlan:
        """对一批 seed 记忆产出巩固计划（不落盘）。"""

        result = ConsolidatePlan(run_id=str(uuid.uuid4()))

        # 已 settled（成员都带 consolidation_done_at）的簇不再调 LLM。
        all_clusters = self._filter(self.cluster(seed_units))
        clusters: list[Cluster] = []
        settled = 0
        for cluster in all_clusters:
            if self._is_settled(cluster):
                settled += 1
            else:
                clusters.append(cluster)
        if settled:
            logger.info(
                "ConflictConsolidator: skip %d settled clusters (no new evidence)", settled
            )

        for cluster in clusters:
            kept, superseded = self._dedup_exact(cluster)
            # 确定性去重（content 完全相同）与 LLM 无关，总是生效。
            result.retire.extend(superseded)
            cluster.memories = kept
            if len(cluster.memories) < self._min_cluster_size:
                # 去重后不足两条 → 无需 LLM，该簇已处理完毕。
                result.processed_unit_ids.update(unit.id for unit in kept)
                continue

            # 簇内动作先落局部 plan：任一阶段 LLM 失败则整簇丢弃且不打标记，留待重试。
            local = ConsolidatePlan(run_id=result.run_id)
            groups = self._detect_relations(cluster)
            if groups is None:
                logger.warning(
                    "ConflictConsolidator: cluster (%s, %s) relation detection failed; "
                    "leave unmarked for retry",
                    cluster.entity_id[:8],
                    cluster.property_name,
                )
                continue
            failed = False
            for group in groups:
                actions = self._plan_actions(cluster, group)
                if actions is None:
                    failed = True
                    break
                self._apply_actions_to_plan(cluster, actions, local)
            if failed:
                logger.warning(
                    "ConflictConsolidator: cluster (%s, %s) action planning failed; "
                    "leave unmarked for retry",
                    cluster.entity_id[:8],
                    cluster.property_name,
                )
                continue
            result.additions.extend(local.additions)
            result.replacements.extend(local.replacements)
            result.retire.extend(local.retire)
            # 标记簇内全部 ACTIVE 成员（含非本次 seed 的老成员），否则簇永不 settled。
            result.processed_unit_ids.update(unit.id for unit in cluster.memories)
        logger.info(
            "ConflictConsolidator.plan: clusters=%d settled_skipped=%d processed_units=%d "
            "additions=%d replacements=%d retire=%d",
            len(clusters),
            settled,
            len(result.processed_unit_ids),
            len(result.additions),
            len(result.replacements),
            len(result.retire),
        )
        return result

    # ------------------------------------------------------------------
    # P1 聚类
    # ------------------------------------------------------------------

    def cluster(self, seed_units: list[MemoryUnit]) -> list[Cluster]:
        """按 canonical ``(schema_entity_id, schema_property_name)`` 成簇，无重叠。

        处理范围 = **seed 触及的 ``(实体, 属性)`` 键**：
        - 实体维度：seed 决定哪些实体参与本轮；
        - 属性维度：命中键后从真源**全量**展开同实体记忆，再按 ``(实体, 属性)`` 分组，
          因此「新证据 vs 同属性旧值」的对照始终成立（旧值不依赖 seed）；
        - seed 未触及的属性簇**不处理**——范围由候选源（F04）决定，聚类层不扩散。
        """

        seed_properties = [unit for unit in seed_units if is_schema_property_unit(unit)]
        if not seed_properties:
            return []
        scope = seed_properties[0].scope

        entity_keys: list[str] = []
        seed_keys: set[tuple[str, str]] = set()
        seen_entities: set[str] = set()
        for unit in seed_properties:
            entity_key = schema_property_entity_id(unit)
            property_name = str(
                unit.system_metadata.get("schema_property_name") or ""
            ).strip()
            if not entity_key or not property_name:
                continue
            seed_keys.add((entity_key, property_name))
            if entity_key not in seen_entities:
                seen_entities.add(entity_key)
                entity_keys.append(entity_key)

        groups: dict[tuple[str, str], Cluster] = {}
        for entity_key in entity_keys:
            for unit in self._entity_property_units(scope, entity_key):
                if unit.lifecycle is not LifecycleState.ACTIVE:
                    continue
                property_name = str(
                    unit.system_metadata.get("schema_property_name") or ""
                ).strip()
                if not property_name:
                    continue
                if (entity_key, property_name) not in seed_keys:
                    # 该属性本轮无种子证据，不展开。
                    continue
                cluster = groups.get((entity_key, property_name))
                if cluster is None:
                    cluster = Cluster(
                        entity_id=entity_key,
                        property_name=property_name,
                        entity_name=str(unit.system_metadata.get("schema_entity_name") or ""),
                        entity_type=str(unit.system_metadata.get("schema_entity_type") or ""),
                    )
                    groups[(entity_key, property_name)] = cluster
                cluster.memories.append(unit)
        return list(groups.values())

    def _entity_property_units(self, scope, entity_key: str) -> list[MemoryUnit]:
        """取该实体的全部属性记忆：反向索引优先，缺失则回退全表扫（legacy scope）。"""

        lookup = self._property_index.lookup(scope, entity_key)
        if lookup.indexed:
            return load_units(self._kv, scope, lookup.unit_ids)
        result: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, MEMORY_KEY_PREFIX):
            unit = _load_unit(raw)
            if unit is None or not is_schema_property_unit(unit):
                continue
            if schema_property_entity_id(unit) != entity_key:
                continue
            result.append(unit)
            if len(result) >= self._fallback_limit:
                break
        return result

    # ------------------------------------------------------------------
    # P2 噪声过滤 / P3 确定性去重
    # ------------------------------------------------------------------

    def _filter(self, clusters: list[Cluster]) -> list[Cluster]:
        """只剔除单条簇；不设超大簇上限。"""
        return [
            cluster
            for cluster in clusters
            if len(cluster.memories) >= self._min_cluster_size
        ]

    @staticmethod
    def _is_settled(cluster: Cluster) -> bool:
        """簇内每条记忆都带 ``consolidation_done_at``（上一轮巩固过）→ 可跳过。"""
        return bool(cluster.memories) and all(
            bool(unit.system_metadata.get("consolidation_done_at"))
            for unit in cluster.memories
        )

    @staticmethod
    def _dedup_exact(
        cluster: Cluster,
    ) -> tuple[list[MemoryUnit], list[MemoryUnit]]:
        """归一化 content 现算 hash；一致的多条只留最新，其余退出。"""

        by_hash: dict[str, list[MemoryUnit]] = {}
        for unit in cluster.memories:
            by_hash.setdefault(_content_hash(unit.content), []).append(unit)
        kept: list[MemoryUnit] = []
        superseded: list[MemoryUnit] = []
        epoch = datetime.min.replace(tzinfo=UTC)
        for same in by_hash.values():
            # 时间全缺时用 epoch 兜底，避免 sort 比较 None 抛错。
            same.sort(key=lambda unit: _effective_time(unit) or epoch, reverse=True)
            kept.append(same[0])
            superseded.extend(same[1:])
        return kept, superseded

    # ------------------------------------------------------------------
    # P4 关系检测 / P5 动作规划
    # ------------------------------------------------------------------

    def _detect_relations(self, cluster: Cluster) -> list[IssueGroup] | None:
        """返回 issue group 列表；``None`` 表示本次调用/解析失败（簇应留待重试）。"""
        prompt = CONFLICT_RELATION_DETECTION_PROMPT.format(
            entity_name=cluster.entity_name or cluster.entity_id,
            property_name=cluster.property_name,
            context=self._cluster_context(cluster),
        )
        try:
            response = self._llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            logger.warning("ConflictConsolidator: relation detection failed: %s", exc)
            return None
        return _parse_issue_groups(response, {unit.id for unit in cluster.memories})

    def _plan_actions(self, cluster: Cluster, group: IssueGroup) -> list[dict] | None:
        """返回动作列表；``None`` 表示本次调用/解析失败（簇应留待重试）。"""
        prompt = CONFLICT_ACTION_PLANNING_PROMPT.format(
            issue_type=group.issue_type,
            context=self._group_context(cluster, group),
        )
        try:
            response = self._llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            logger.warning("ConflictConsolidator: action planning failed: %s", exc)
            return None
        return _parse_actions(response)

    # ------------------------------------------------------------------
    # P5 计划映射（不改库）
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_actions_to_plan(
        cluster: Cluster,
        actions: list[dict],
        plan: ConsolidatePlan,
    ) -> None:
        by_id = {unit.id: unit for unit in cluster.memories}
        retired: set[str] = set()

        def _retire(target: MemoryUnit) -> None:
            if target.id in retired:
                return
            retired.add(target.id)
            plan.retire.append(target)

        for action in actions:
            action_type = str(action.get("type") or "").strip().lower()
            if action_type not in _ACTION_TYPES:
                continue
            if action_type == "create":
                content = str(action.get("content") or "").strip()
                evidence = _valid_ids(action.get("evidence_unit_ids"), by_id)
                if not content or not evidence:
                    continue
                plan.additions.append(
                    _make_property_unit(cluster, content, evidence)
                )
            elif action_type == "merge":
                content = str(action.get("target_content") or "").strip()
                sources = [by_id[i] for i in _valid_ids(action.get("source_unit_ids"), by_id)]
                if not content or len(sources) < 2:
                    continue
                for source in sources:
                    _retire(source)
                plan.additions.append(
                    _make_property_unit(
                        cluster,
                        content,
                        [source.id for source in sources],
                        supersedes=[source.id for source in sources],
                    )
                )
            elif action_type == "update":
                content = str(action.get("content") or "").strip()
                target = by_id.get(str(action.get("unit_id") or "").strip())
                if not content or target is None:
                    continue
                _retire(target)
                plan.replacements.append(
                    _make_property_unit(
                        cluster,
                        content,
                        [target.id, *target.provenance],
                        supersedes=target.id,
                    )
                )
            else:  # archive
                target = by_id.get(str(action.get("unit_id") or "").strip())
                if target is not None:
                    _retire(target)

    # ------------------------------------------------------------------
    # 上下文构造
    # ------------------------------------------------------------------

    @staticmethod
    def _cluster_context(cluster: Cluster) -> str:
        lines = [
            f"entity_name: {cluster.entity_name or 'unknown'}",
            f"entity_type: {cluster.entity_type or 'unknown'}",
            f"property_name: {cluster.property_name}",
            f"memory_count: {len(cluster.memories)}",
            "",
        ]
        for index, unit in enumerate(cluster.memories, 1):
            lines.append(
                f"[{index}] memory_id={unit.id} "
                f"time={_format_time(_effective_time(unit))} "
                f"event_time={_format_time(unit.temporal.t_event)} "
                f"ingest_time={_format_time(unit.temporal.t_ingest)} "
                f"content: {unit.content}"
            )
        return "\n".join(lines)

    @staticmethod
    def _group_context(cluster: Cluster, group: IssueGroup) -> str:
        by_id = {unit.id: unit for unit in cluster.memories}
        lines = [
            f"entity_name: {cluster.entity_name or 'unknown'}",
            f"property_name: {cluster.property_name}",
            f"issue_type: {group.issue_type}",
            f"reason: {group.reason}",
            "",
        ]
        for memory_id in group.memory_ids:
            unit = by_id.get(memory_id)
            if unit is None:
                continue
            hint = group.value_hints.get(memory_id, "")
            lines.append(
                f"- memory_id={unit.id} "
                f"time={_format_time(_effective_time(unit))} "
                f"event_time={_format_time(unit.temporal.t_event)} "
                f"ingest_time={_format_time(unit.temporal.t_ingest)} "
                f"value_hint: {hint} content: {unit.content}"
            )
        return "\n".join(lines)


# ----------------------------------------------------------------------
# 构造 / 解析 helper
# ----------------------------------------------------------------------


def _make_property_unit(
    cluster: Cluster,
    content: str,
    evidence_ids: list[str],
    *,
    supersedes: str | list[str] = "",
) -> MemoryUnit:
    """构造新的 ACTIVE 属性记忆，**继承簇的 (entity_id, property_name) 归属键**。"""

    representative = cluster.memories[0]
    unit = copy.deepcopy(representative)
    unit.id = str(uuid.uuid4())
    # 内容已改写：清空旧单元的派生字段（L0/L1 摘要、向量），与 schema_update 的约定一致。
    unit.layers.l0 = ""
    unit.layers.l1 = ""
    unit.vectors = []
    unit.segments = [
        Segment(
            content=content,
            assets=list(representative.assets),
            source=representative.source,
        )
    ]
    unit.provenance = _dedupe([*evidence_ids, *representative.provenance])
    if isinstance(supersedes, list):
        unit.supersedes = supersedes[0] if supersedes else ""
    else:
        unit.supersedes = supersedes
    unit.lifecycle = LifecycleState.ACTIVE
    unit.temporal.t_valid = datetime.now(UTC)
    unit.temporal.t_invalid = None
    unit.system_metadata["schema_property_operation"] = "set"
    unit.tags = merge_unit_tags(unit.tags, ["conflict_consolidate"])
    return unit


def _parse_issue_groups(response: str, valid_ids: set[str]) -> list[IssueGroup] | None:
    """解析关系检测响应；``None`` = 响应不可解析/结构非法（该簇留待重试）。"""
    payload = _load_json_object(response)
    if payload is None:
        return None
    raw_groups = payload.get("issue_groups")
    if not isinstance(raw_groups, list):
        return None
    groups: list[IssueGroup] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        memory_ids = _valid_ids(raw.get("memory_ids"), None)
        memory_ids = [mid for mid in memory_ids if mid in valid_ids]
        if len(memory_ids) < 2:
            continue
        issue_type = str(raw.get("issue_type") or "").strip().lower()
        if issue_type not in _ISSUE_TYPES:
            continue
        value_hints = raw.get("value_hints")
        hints = (
            {str(k): str(v) for k, v in value_hints.items()}
            if isinstance(value_hints, dict)
            else {}
        )
        groups.append(
            IssueGroup(
                issue_type=issue_type,
                memory_ids=memory_ids,
                value_hints=hints,
                confidence=str(raw.get("confidence") or "medium").strip().lower(),
                reason=str(raw.get("reason") or ""),
            )
        )
    return groups


def _parse_actions(response: str) -> list[dict] | None:
    """解析动作规划响应；``None`` = 响应不可解析/结构非法（该簇留待重试）。"""
    payload = _load_json_object(response)
    if payload is None:
        return None
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        return None
    return [action for action in raw_actions if isinstance(action, dict)]


def _load_unit(raw) -> MemoryUnit | None:
    """反序列化真源记录；格式不符时返回 None。"""
    try:
        return loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _load_json_object(response: str) -> dict | None:
    text = (response or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        logger.warning("ConflictConsolidator: LLM response is not valid JSON")
        return None
    return payload if isinstance(payload, dict) else None


def _valid_ids(values: object, by_id: dict[str, MemoryUnit] | None) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in result:
            continue
        if by_id is not None and text not in by_id:
            continue
        result.append(text)
    return result


def _content_hash(content: str) -> str:
    normalized = " ".join(str(content or "").casefold().split())
    return normalized


def _effective_time(unit: MemoryUnit):
    """「最新」判据的有效时间：t_event > t_message > t_ingest > t_valid。

    ``t_valid`` 是抽取时刻，仅在前三者全缺时兜底。返回值统一为 aware
    （naive 视作 UTC），否则同簇内混用两种时间会在排序时抛 TypeError。
    """
    value = (
        unit.temporal.t_event
        or unit.temporal.t_message
        or unit.temporal.t_ingest
        or unit.temporal.t_valid
    )
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _format_time(value) -> str:
    return value.isoformat() if value is not None else "unknown"


def _dedupe(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = [
    "Cluster",
    "ConflictConsolidator",
    "ConsolidatePlan",
    "IssueGroup",
]
