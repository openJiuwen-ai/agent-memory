# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~construction.evolver.Evolver`——演进闭环的编排者。

按 ``mode`` 驱动各构建算子，并把新产物落真源 + 建索引（构建层负责落盘）：
- ``EXTRACT``    → :class:`Extractor` 抽取低抽象事实，去重 → 落盘建索引；
- ``CONSOLIDATE``→ :class:`Abstractor` 升华出高抽象画像，去重 → 落盘建索引；
- ``ASSOCIATE``  → :class:`Associator` 发现关联，落 :class:`~storage.graph.GraphStore`
  （为涉及单元建节点、为关联建边），供检索 GRAPH 通道多跳召回；
- ``FORGET``     → 把已被取代（SUPERSEDED）的旧版本标记 FORGOTTEN（非破坏式清理）。

去重流程（Dedup.recall → LLM 语义判定）：
- Step A+B: Dedup.recall() 对候选召回已有相似记忆（向量或倒排，由装配按
  ``vector_enabled`` 选实现——召回 + 阈值过滤 + 加载 + 聚合都在 recaller 内）
- Step D: LLM.chat() 语义判定 → DedupDecision(ADD/UPDATE/SUPERSEDE/NOOP)

不使用 Reranker——LLM 直接做最终语义判定。降级时 LLM 不可用则按 cosine 阈值规则判定。
SUPERSEDE 标记由 Evolver 经 IndexBuilder 完成，不依赖 LifecycleManager——
避免 construction 层依赖 control 层。

IndexBuilder 与 Storage 由装配注入：记忆本体与各索引经 IndexBuilder 统一写入，
原文（/messages/）不是索引形式，不经 IndexBuilder，直接落注入的 ``message_store``。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    MESSAGES_KEY_PREFIX,
    DedupDecision,
    LifecycleState,
    MemoryUnit,
    Relation,
    Scope,
    inherited_system_metadata,
    inherited_user_metadata,
    messages_key,
)
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory import ROUTE_CTX_KEY, Segment
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.abstractor import Abstractor, AbstractorProducer
from jiuwen_memory.construction.associator import Associator, AssociatorProducer
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.dedup import Dedup, DedupProducer
from jiuwen_memory.construction.evolver import EvolveMode, Evolver, EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dedup_direct_noop import should_direct_noop
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer
from jiuwen_memory.construction.prompt_strategy import copy_consolidation_prompts
from jiuwen_memory.construction.router import (
    RouteContext,
    Router,
    apply_decisions,
    degraded_reasons,
    optional_router,
    route_batch,
)
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import Edge, IndexRemoveMode, IndexWriteMode, Node

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 去重判定 Prompt
# ---------------------------------------------------------------------------

_DEDUP_SYSTEM_PROMPT = """You are a memory deduplication assistant. Determine whether a \
candidate memory
is a new fact, an update to an existing memory, a replacement of an existing memory, or a duplicate
of an existing memory.

Output ONLY a JSON object with:
- "decision": "add" | "update" | "supersede" | "noop"
- "reason": brief explanation (one sentence)

Decision rules:
- "add": The candidate contains new information not covered by any existing memory.
- "update": The candidate adds information to an existing memory (partial overlap, candidate \
has extra details).
- "supersede": The candidate completely replaces an existing memory (same topic, candidate is \
more complete/accurate/recent).
- "noop": The candidate is fully covered by an existing memory (no new information).

If unsure, prefer "add" (to avoid losing information)."""

_DEDUP_BATCH_SYSTEM_PROMPT = """You are a memory deduplication assistant. For EACH candidate \
memory, determine whether
it is a new fact, an update to an existing memory, a replacement of an existing memory, or a \
duplicate.

Input format (compact):
  [cid]: <candidate content>
    e|<existing_id>|<cosine_score>: <existing content>
  ---

Output ONLY a JSON array (one entry per candidate, in input order). No explanation, no markdown \
fences.
Each entry:
- "candidate_id": the candidate's id (the value after [ and before ])
- "decision": "add" | "update" | "supersede" | "noop"
- "reason": brief explanation (one sentence)

Decision rules:
- "add": The candidate contains new information not covered by any of its existing similar memories.
- "update": The candidate adds information to an existing memory (partial overlap, candidate \
has extra details).
- "supersede": The candidate completely replaces an existing memory (same topic, candidate is \
more complete/accurate/recent).
- "noop": The candidate is fully covered by an existing memory (no new information).

Judge each candidate independently against its OWN listed existing memories. If unsure, prefer \
"add"."""

_CONTENT_MERGE_SYSTEM_PROMPT = """Combine the old and new memory content into one self-contained
statement. Output ONLY that statement as plain text. No explanation, no labels, no JSON.

- Keep every named entity, quantity, descriptive attribute and date from either version;
  length is no constraint, dropping one to shorten is the failure here.
- Conflict = two different values for the SAME attribute; only there take the newer version
  (the second input). Same value at different precision - keep the precise form. Details that
  merely differ are no conflict; keep both.
- If the two describe different matters, state both - losing either is worse than one item
  covering two.
- Write in the input's language."""

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _route_ctx_of(units: List[MemoryUnit]) -> RouteContext | None:
    """取源单元携带的归属判定上下文；未携带即返回 ``None``（整段不判定）。

    取第一条携带的：一次写入的源单元来自同一次调用，上下文由 API 层统一装配，各条相同。
    取值不是 :class:`RouteContext` 时按未携带处置——该键是内核自己写的瞬态键，出现异常
    取值只可能是调用方伪造，此时按不判定处置比按判定处置安全。
    """
    for unit in units:
        ctx = (unit.system_metadata or {}).get(ROUTE_CTX_KEY)
        if isinstance(ctx, RouteContext):
            return ctx
    return None


# ---------------------------------------------------------------------------
# OrchestratingEvolver
# ---------------------------------------------------------------------------


class OrchestratingEvolver(Evolver):
    """编排 extract/associate/consolidate/forget，并把产物落真源 + 建索引 + 图。

    EXTRACT/CONSOLIDATE 模式增加去重决策：
    Dedup.recall 召回 → LLM 语义判定 →
    ADD(落盘)/UPDATE(合成)/SUPERSEDE(替代+旧版标记)/NOOP(跳过)。

    召回侧（向量化/Store.search/加载/过滤聚合）下沉到 :class:`Dedup`，
    由装配按 ``vector_enabled`` 选向量/倒排实现；本类只做阈值 + LLM 判定 + 执行。
    SUPERSEDE 标记由本类经 IndexBuilder.update 完成，不依赖 LifecycleManager
    （避免 construction 层依赖 control 层）。
    """

    def __init__(
        self,
        extractor: Extractor,
        abstractor: Abstractor,
        associator: Associator,
        index_builder: IndexBuilder,
        storage: Storage,
        message_store: KVStore,
        dedup: Dedup,
        llm: LLM,
        layer_annotator: LayerAnnotator | None = None,
        router: Router | None = None,
        *,
        dedup_medium_similarity: float = 0.7,
        dedup_high_similarity: float = 0.9,
    ) -> None:
        self._extractor = extractor
        self._abstractor = abstractor
        self._associator = associator
        self._index = index_builder
        # storage 只用于在此取图端口；MemoryUnit 的写入一律经 self._index，故不留字段。
        self._graph = storage.graph if storage.has_graph() else None
        # 原文（/messages/）专用：不是索引形式，不经 IndexBuilder。缺省与正排 KV 同实例。
        self._message_store = message_store
        self._dedup = dedup
        self._llm = llm
        # 分层标注算子：None 表示不标注（向后兼容）；非 None 时在抽取/升华后标注 L0/L1。
        self._layer_annotator = layer_annotator
        # 归属判定算子：None 表示不判定，派生单元沿用源单元的 scope（向后兼容）。
        self._router = router
        self.relations: List[Relation] = []  # ASSOCIATE 产物（同时已落图）

        # 去重阈值配置（min/top_k/tier_filter/scope_filter 已下沉到 recaller；
        # medium/high 阈值留本类做判定档位划分：
        #   < medium → ADD（低相似，新事实）
        #   medium ~ high → LLM 语义判定
        #   ≥ high → 直接 NOOP（确定性重复，跳过 LLM）；LLM 降级时同样规则兜底）
        self._dedup_medium_similarity = dedup_medium_similarity
        self._dedup_high_similarity = dedup_high_similarity

        # infer 上下文收集的条数（见 F02「上下文增强抽取」）
        self._recent_originals_limit = 10
        self._related_memories_top_k = 10

    # ------------------------------------------------------------------
    # infer=true 上下文收集（evolver 内部，evolve 接口不变）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_procedural(units: List[MemoryUnit]) -> bool:
        """本轮是否走过程记忆抽取（metadata["procedural"]=="true"）。"""
        return any(
            str(u.system_metadata.get("procedural", "")).strip().lower() == "true"
            for u in units
        )

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Evolver 契约
    # ------------------------------------------------------------------

    def evolve(
        self,
        units: List[MemoryUnit],
        mode: EvolveMode,
    ) -> EvolveResult:
        logger.info("Evolver: evolve mode=%s, %d units", mode.value, len(units))
        for u in units:
            logger.info("Evolver: input unit id=%s tier=%s lifecycle=%s provenance=%s content=%s",
                         u.id[:8], u.tier.value, u.lifecycle.value, u.provenance, u.content[:200])
        if mode == EvolveMode.EXTRACT:
            return self._evolve_extract(units)
        if mode == EvolveMode.CONSOLIDATE:
            return self._evolve_consolidate(units)
        if mode == EvolveMode.ASSOCIATE:
            return self._evolve_associate(units)
        if mode == EvolveMode.FORGET:
            return self._evolve_forget(units)
        return EvolveResult()

    # ------------------------------------------------------------------
    # 落盘辅助
    # ------------------------------------------------------------------

    def _persist(self, units: List[MemoryUnit]) -> List[str]:
        # 记忆写入只经 IndexBuilder：正排与派生索引由其统一编排。
        self._index.build(units)
        return [u.id for u in units]

    def _persist_graph(self, units: List[MemoryUnit], relations: List[Relation]) -> None:
        """为涉及关联的单元建节点（幂等）、为每条关联建一条边。"""
        if not units or self._graph is None:
            return
        scope = units[0].scope
        for u in units:
            node = Node(id=u.id, label=u.tier.value, properties={"content": u.content})
            try:
                self._graph.insert(scope, nodes=[node])
            except ConflictError:
                pass  # 节点已存在
        edges = []
        for relation in relations:
            edges.append(
                Edge(
                    id=str(uuid.uuid4()),
                    source=relation.source_id,
                    target=relation.target_id,
                    relation=relation.relation,
                    properties=dict(relation.metadata),
                )
            )
        if edges:
            self._graph.insert(scope, edges=edges)

    # ------------------------------------------------------------------
    # 去重核心方法
    # ------------------------------------------------------------------

    def _dedup_batch(self, candidates: List[MemoryUnit]) -> EvolveResult:
        """对一批候选 unit 执行去重判定和后续动作，返回 EvolveResult。

        两段式（批量 LLM 优化）：
        1. 逐条 ``Dedup.recall`` 召回 + 阈值筛选——无命中或低相似(< medium)直接判 ADD；
           分数 ≥ high(默认 0.9)直接判 NOOP（确定性重复，跳过 LLM）；
           medium ~ high 之间的候选收集成 ``need_llm`` 列表。
        2. ``need_llm`` 批量喂一次 LLM 判定（``_llm_dedup_decide_batch``），按返回的
           decision 逐条执行。批量失败则降级为逐条单次 LLM 判定。
        """
        result = EvolveResult()
        noop_count = 0

        # 每条候选的判定上下文：(candidate, best_unit, best_score, hit_units)
        direct_add: List[Tuple[MemoryUnit, Optional[MemoryUnit], float]] = []
        direct_noop: List[Tuple[MemoryUnit, MemoryUnit, float]] = []
        need_llm: List[Tuple[MemoryUnit, MemoryUnit, float, List[Tuple[MemoryUnit, float]]]] = []

        # ---- 第一段：逐条 recall + 阈值筛选（不调 LLM）----
        for candidate in candidates:
            try:
                hit_units = self._dedup.recall(candidate)
            except Exception as exc:
                logger.warning(
                    "Evolver._dedup: recall failed for %s, fallback ADD: %s",
                    candidate.id[:8], exc,
                )
                direct_add.append((candidate, None, 0.0))
                continue

            if not hit_units:
                direct_add.append((candidate, None, 0.0))
                continue

            best_unit, best_score = max(hit_units, key=lambda x: x[1])
            if best_score < self._dedup_medium_similarity:
                # 低相似 → 直接 ADD（不调 LLM）
                direct_add.append((candidate, best_unit, best_score))
            elif best_score >= self._dedup_high_similarity:
                if should_direct_noop(
                    best_score, self._dedup_high_similarity, candidate, best_unit
                ):
                    direct_noop.append((candidate, best_unit, best_score))
                else:
                    need_llm.append((candidate, best_unit, best_score, hit_units))
            else:
                # medium ~ high → 需 LLM 判定
                need_llm.append((candidate, best_unit, best_score, hit_units))

        # ---- 第二段：批量 LLM 判定 ----
        decisions: dict[str, DedupDecision] = {}
        if need_llm:
            batch_items = [(c, hits) for c, _, _, hits in need_llm]
            decisions = self._llm_dedup_decide_batch(batch_items)

        # ---- 第三段：逐条执行 ----
        for candidate, best_unit, best_score in direct_add:
            noop_count += self._apply_decision(
                candidate, DedupDecision.ADD, best_unit, best_score, result
            )

        for candidate, best_unit, best_score in direct_noop:
            noop_count += self._apply_decision(
                candidate, DedupDecision.NOOP, best_unit, best_score, result
            )

        for candidate, best_unit, best_score, _ in need_llm:
            decision = decisions.get(candidate.id, DedupDecision.ADD)
            noop_count += self._apply_decision(
                candidate, decision, best_unit, best_score, result
            )

        logger.info(
            "Evolver._dedup: total=%d add=%d update=%d supersede=%d noop=%d "
            "(direct_noop=%d, need_llm=%d, llm_batches=%d)",
            len(candidates), len(result.created_ids), len(result.updated_ids),
            len(result.superseded_ids), noop_count,
            len(direct_noop), len(need_llm), 1 if need_llm else 0,
        )
        return result

    def _apply_decision(
        self,
        candidate: MemoryUnit,
        decision: DedupDecision,
        existing_unit: Optional[MemoryUnit],
        similarity: float,
        result: EvolveResult,
    ) -> int:
        """执行单条候选的去重决策，更新 result；返回 noop 计数（0 或 1）。"""
        logger.info(
            "Evolver._dedup: candidate %s → decision=%s existing=%s similarity=%.3f",
            candidate.id[:8], decision.value,
            existing_unit.id[:8] if existing_unit else "None",
            similarity,
        )

        # ADD，或 UPDATE/SUPERSEDE 缺 existing_unit 时降级为 ADD（LLM 误判但 recall
        # 无命中——宁可重复不丢失，避免 None.segments/.id 抛 AttributeError）。
        if decision == DedupDecision.ADD or (
            decision in (DedupDecision.UPDATE, DedupDecision.SUPERSEDE)
            and existing_unit is None
        ):
            if decision != DedupDecision.ADD:
                logger.warning(
                    "Evolver._dedup: %s without existing_unit for %s, fallback ADD",
                    decision.value, candidate.id[:8],
                )
            self._index.build([candidate])
            result.created_ids.append(candidate.id)
            result.created_units.append(candidate)

        elif decision == DedupDecision.UPDATE:
            # 合成新旧 content
            merged_content = self._merge_content(existing_unit, candidate)
            # 更新 existing_unit
            if existing_unit.segments:
                existing_unit.segments[0].content = merged_content
            else:
                existing_unit.segments = [Segment(content=merged_content)]
            # 合并 provenance
            existing_unit.provenance = list(
                set(existing_unit.provenance) | set(candidate.provenance) | {candidate.id}
            )
            # 合并 metadata
            existing_unit.system_metadata = inherited_system_metadata(
                [existing_unit, candidate]
            )
            existing_unit.user_metadata = inherited_user_metadata(
                [existing_unit, candidate]
            )
            existing_unit.system_metadata["dedup_decision"] = "update"
            existing_unit.system_metadata["dedup_similarity"] = str(similarity)
            existing_unit.system_metadata["dedup_merged_from"] = candidate.id
            # existing_unit 是已建索引的记忆，经 IndexBuilder 统一回写。
            self._index.update([existing_unit])
            result.updated_ids.append(existing_unit.id)

        elif decision == DedupDecision.SUPERSEDE:
            # 新版落盘
            candidate.supersedes = existing_unit.id
            candidate.provenance = list(
                set(candidate.provenance) | {existing_unit.id}
            )
            candidate.system_metadata["dedup_decision"] = "supersede"
            candidate.system_metadata["dedup_similarity"] = str(similarity)
            candidate.system_metadata["dedup_superseded"] = existing_unit.id
            self._index.build([candidate])
            # 旧版标记 SUPERSEDED（不依赖 LifecycleManager）
            existing_unit.lifecycle = LifecycleState.SUPERSEDED
            existing_unit.temporal.t_invalid = _now()
            self._index.update([existing_unit])
            result.created_ids.append(candidate.id)
            result.created_units.append(candidate)
            result.superseded_ids.append(existing_unit.id)

        elif decision == DedupDecision.NOOP:
            logger.info(
                "Evolver._dedup: NOOP candidate %s skipped (duplicate of %s)",
                candidate.id[:8],
                existing_unit.id[:8] if existing_unit else "N/A",
            )
            return 1

        return 0

    def _dedup_single(
        self, candidate: MemoryUnit
    ) -> Tuple[DedupDecision, Optional[MemoryUnit], float]:
        """单条候选的去重判定：Dedup.recall → 阈值短路 → LLM 语义判定。

        返回 (decision, most_similar_existing_unit, max_similarity_score)。
        召回 + min 阈值过滤 + 加载 + 聚合取 max 全在 recaller 内完成；本方法做
        high/medium 阈值短路与 LLM 判定。
        """
        # Step A+B: 召回已有相似记忆（已滤自身、已按 min_similarity 过滤、已聚合取 max）
        hit_units: List[Tuple[MemoryUnit, float]] = self._dedup.recall(candidate)
        if not hit_units:
            logger.debug("Evolver._dedup_single: no hits for candidate %s → ADD", candidate.id[:8])
            return (DedupDecision.ADD, None, 0.0)

        # 找最高相似度的 hit
        best_unit, best_score = max(hit_units, key=lambda x: x[1])

        # Step D: 语义判定
        if should_direct_noop(
            best_score, self._dedup_high_similarity, candidate, best_unit
        ):
            return (DedupDecision.NOOP, best_unit, best_score)

        # 低相似 → ADD（新事实，不值得深度判定）
        if best_score < self._dedup_medium_similarity:
            return (DedupDecision.ADD, best_unit, best_score)

        # 中/高相似 → LLM 语义判定
        try:
            decision = self._llm_dedup_decide(candidate, hit_units)
        except Exception as exc:
            logger.warning(
                "Evolver._dedup_single: LLM dedup failed for %s, fallback rule-based: %s",
                candidate.id[:8], exc,
            )
            # 降级策略与 _llm_dedup_decide 一致：宁可重复不丢失。
            # high 相似且无实质差异 → NOOP；否则 ADD（不覆盖已有）。
            if should_direct_noop(
                best_score, self._dedup_high_similarity, candidate, best_unit
            ):
                decision = DedupDecision.NOOP
            else:
                decision = DedupDecision.ADD

        return (decision, best_unit, best_score)

    def _llm_dedup_decide(
        self,
        candidate: MemoryUnit,
        hits: List[Tuple[MemoryUnit, float]],
    ) -> DedupDecision:
        """构建去重判定 prompt，LLM.chat() → 解析 JSON → 返回 DedupDecision。"""
        existing_texts = []
        for unit, score in hits[:3]:  # 只取 top-3 减少上下文消耗
            existing_texts.append(
                f"[Memory ID: {unit.id}]\n"
                f"Content: {unit.content}\n"
                f"Tier: {unit.tier.value}\n"
                f"Similarity: {score:.3f}"
            )

        user_prompt = (
            f"Candidate memory to check:\n"
            f"[Memory ID: {candidate.id}]\n"
            f"Content: {candidate.content}\n"
            f"Tier: {candidate.tier.value}\n\n"
            f"Existing similar memories:\n"
            + "\n\n".join(existing_texts)
        )

        messages = [
            ChatMessage(role="system", content=_DEDUP_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_prompt),
        ]

        try:
            response = self._llm.chat(messages, temperature=0, max_tokens=256)
        except Exception as exc:
            raise RuntimeError(f"LLM dedup call failed: {exc}") from exc

        # 解析 JSON
        try:
            result = json.loads(response.strip())
        except json.JSONDecodeError:
            # 尝试提取 JSON 部分（LLM 可能包裹在 markdown fences 中）
            json_match = re.search(r"\{[^}]+\}", response)
            if json_match:
                result = json.loads(json_match.group())
            else:
                logger.warning(
                    "Evolver._llm_dedup_decide: cannot parse LLM response as JSON: %s",
                    response[:200],
                )
                return DedupDecision.ADD

        decision_str = result.get("decision", "add").lower()
        try:
            return DedupDecision(decision_str)
        except ValueError:
            logger.warning(
                "Evolver._llm_dedup_decide: unknown decision '%s', fallback ADD",
                decision_str,
            )
            return DedupDecision.ADD

    def _llm_dedup_decide_batch(
        self,
        items: List[Tuple[MemoryUnit, List[Tuple[MemoryUnit, float]]]],
    ) -> dict[str, DedupDecision]:
        """批量 LLM 判定：一次调用判多条候选，返回 {candidate_id: decision}。

        ``items`` 为 (candidate, hit_units) 列表。prompt 格式（紧凑）：
        [cid]: <candidate content>
          e|<existing_id>|<score>: <existing content>
        候选之间用 ``---`` 分隔。解析失败或缺项的候选回退为单条判定。
        """
        if not items:
            return {}

        # 构建紧凑批量 prompt
        blocks: List[str] = []
        for cand, hits in items:
            cand_block = f"[{cand.id}]: {cand.content}"
            for unit, score in hits[:3]:
                cand_block += f"\n  e|{unit.id}|{score:.3f}: {unit.content}"
            blocks.append(cand_block)
        user_prompt = "\n---\n".join(blocks)

        messages = [
            ChatMessage(role="system", content=_DEDUP_BATCH_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_prompt),
        ]

        try:
            response = self._llm.chat(messages, temperature=0, max_tokens=1024)
        except Exception as exc:
            logger.warning(
                "Evolver._llm_dedup_decide_batch: LLM call failed, "
                "fallback to per-candidate single: %s", exc,
            )
            return self._fallback_single_decide(items)

        # 解析 JSON 数组
        results: dict[str, DedupDecision] = {}
        try:
            parsed = json.loads(response.strip())
        except json.JSONDecodeError:
            # 尝试提取 JSON 片段（LLM 可能裹 markdown fence）
            arr_match = re.search(r"\[.*\]", response, re.DOTALL)
            obj_match = re.search(r"\{.*\}", response, re.DOTALL)
            if arr_match:
                try:
                    parsed = json.loads(arr_match.group())
                except json.JSONDecodeError:
                    parsed = None
            elif obj_match:
                try:
                    parsed = json.loads(obj_match.group())
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = None

        # 兼容：LLM 返回单对象（而非数组）时，包成单元素列表
        if isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            logger.warning(
                "Evolver._llm_dedup_decide_batch: cannot parse LLM response as JSON array, "
                "fallback to per-candidate single: %s", response[:200],
            )
            return self._fallback_single_decide(items)

        # 按候选 id 取 decision
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("candidate_id")
            # 单对象响应（无 candidate_id 字段）且只有一条候选 → 用该候选 id
            if cid is None and len(items) == 1:
                cid = items[0][0].id
            decision_str = str(entry.get("decision", "add")).lower()
            try:
                results[cid] = DedupDecision(decision_str)
            except ValueError:
                logger.warning(
                    "Evolver._llm_dedup_decide_batch: unknown decision '%s' for %s, fallback ADD",
                    decision_str, cid,
                )
                results[cid] = DedupDecision.ADD

        # 缺项候选（LLM 漏了某条）→ 单条补判
        missing = [(c, h) for c, h in items if c.id not in results]
        if missing:
            logger.warning(
                "Evolver._llm_dedup_decide_batch: %d candidates missing in LLM response, "
                "fallback to per-candidate single", len(missing),
            )
            results.update(self._fallback_single_decide(missing))

        return results

    def _fallback_single_decide(
        self,
        items: List[Tuple[MemoryUnit, List[Tuple[MemoryUnit, float]]]],
    ) -> dict[str, DedupDecision]:
        """批量降级：逐条调单条 LLM 判定。某条失败 → 按规则（score 阈值）判定。"""
        results: dict[str, DedupDecision] = {}
        for cand, hits in items:
            best_unit, best_score = max(hits, key=lambda x: x[1]) if hits else (None, 0.0)
            try:
                results[cand.id] = self._llm_dedup_decide(cand, hits)
            except Exception:
                logger.warning(
                    "Evolver._fallback_single_decide: single LLM failed for %s, rule-based",
                    cand.id[:8],
                )
                # 降级策略与 _llm_dedup_decide 一致：宁可重复不丢失。
                if best_unit is not None and should_direct_noop(
                    best_score, self._dedup_high_similarity, cand, best_unit
                ):
                    results[cand.id] = DedupDecision.NOOP
                else:
                    results[cand.id] = DedupDecision.ADD
        return results

    def _merge_content(self, old: MemoryUnit, new: MemoryUnit) -> str:
        """UPDATE 场景：LLM 合成新旧 content 为一条完整陈述。"""
        user_prompt = (
            f"Old version:\n{old.content}\n\n"
            f"New version:\n{new.content}"
        )

        messages = [
            ChatMessage(role="system", content=_CONTENT_MERGE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_prompt),
        ]

        try:
            merged = self._llm.chat(messages, temperature=0, max_tokens=512)
            return merged.strip()
        except Exception as exc:
            logger.warning(
                "Evolver._merge_content: LLM merge failed for %s→%s, fallback concatenation: %s",
                old.id[:8], new.id[:8], exc,
            )
            # 降级：简单拼接新旧内容
            return f"{old.content}\n{new.content}"

    def _maybe_collect_extract_context(
        self, units: List[MemoryUnit], recent: List[MemoryUnit]
    ) -> ExtractContext | None:
        """EXTRACT 模式下：若本轮 units 标记了 infer=true，收集上下文参考项。

        - recent_originals：由调用方经 ``_persist_and_maintain_messages`` 一次 scan 取得的
          最近 N 条原文（做指代/代词消解与语境），只进 prompt，不参与去重。
        - related_memories：用 dedup.recall 召回的相关记忆（MemoryUnit），做去重——
          进 prompt 提示已有事实、勿重复抽取。

        任一步失败降级为空列表，不阻断（仍靠 prompt + _dedup_batch）。非 infer 返回 None。
        """
        infer = any(
            str(u.system_metadata.get("infer", "")).strip().lower() == "true"
            for u in units
        )
        if not infer:
            return None
        related = self._related_memories(units)
        ctx = ExtractContext(
            recent_originals=list(recent), related_memories=related
        )
        logger.info(
            "Evolver._maybe_collect_extract_context: infer=true, recent=%d related=%d",
            len(recent), len(related),
        )
        return ctx

    # -- 原文（/messages/）的直接 KV 读写 ---------------------------------- #
    #
    # 原文既非 MemoryUnit 真源也非索引：不建索引、不参与检索，仅供抽取时做指代消解与
    # 语境补全，条数上限由本类维护。它与索引构建是两件事，故不走 IndexBuilder，也不占
    # Storage 的领域接口，直接用注入的 KVStore。

    def _add_messages(self, scope: Scope, units: List[MemoryUnit]) -> None:
        for unit in units:
            key = messages_key(unit.id)
            value = dumps(unit)
            try:
                self._message_store.insert(scope, key, value)
            except ConflictError:
                # MiddleToLongJob 失败批下轮重试会对同一 unit.id 再调 evolve；extract
                # 失败时 /messages/ 已写入且按契约不回滚，upsert 使重试不再撞 insert 拒重。
                self._message_store.update(scope, key, value)

    def _list_messages(self, scope: Scope) -> List[MemoryUnit]:
        """返回 scope 内全部原文（无序；调用方自行排序）。损坏记录跳过，不阻断整批。"""
        items: List[MemoryUnit] = []
        for _key, raw in self._message_store.scan(scope, prefix=MESSAGES_KEY_PREFIX):
            unit = loads(raw)
            if unit is not None:
                items.append(unit)
        return items

    def _delete_messages(self, scope: Scope, unit_ids: List[str]) -> None:
        for unit_id in unit_ids:
            self._message_store.delete(scope, messages_key(unit_id))

    def _persist_and_maintain_messages(
        self, units: List[MemoryUnit]
    ) -> List[MemoryUnit]:
        """一次扫描完成原文维护：取最近 N 条 → 写入本轮 → 淘汰超出的旧原文。

        顺序（只扫一次 ``/messages/``）：
        1. 读全部历史原文（本轮尚未落盘，故历史中不含本轮）；
        2. 取最近 N 条作 recent；
        3. 写入本轮新原文（原文不建索引，不参与检索）；
        4. 删除超出最近 N 条的旧原文（含本轮后总数 > N 时淘汰最早的）。

        原文不是索引形式，不经 IndexBuilder；它直接落注入的 ``message_store``。该 store
        缺省与正排 KV 是同一实例（``/messages/`` 与 ``/memory/`` 靠 key 前缀分离），
        装配上声明独立的 ``kv_store`` 具名实例即可物理拆开。

        返回 recent（供 extractor prompt 做指代消解/语境）。失败降级为空列表，不阻断。
        """
        if not units:
            return []
        scope = units[0].scope
        try:
            historical = self._list_messages(scope)
        except Exception as exc:
            logger.warning(
                "Evolver._persist_and_maintain_messages: scan failed, empty: %s",
                exc,
            )
            # 读取失败仍落本轮原文（不维护淘汰），recent 为空
            self._add_messages(scope, units)
            return []
        # 1) 本轮新原文并入历史（尚未落盘），统一排序后决定保留/删除
        current_ids = {u.id for u in units}
        ordered = historical + list(units)
        # 2) 一次排序：按 t_ingest 降序（最新在前；本轮刚 ingest 排头部，不会被删）
        ordered.sort(
            key=lambda u: u.temporal.t_ingest or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        # 3) recent = 前 N 条历史原文（排除本轮——本轮已是提取来源，不重复进 context）
        recent = [
            u for u in ordered if u.id not in current_ids
        ][: self._recent_originals_limit]
        # 4) 落本轮新原文 + 淘汰超出 N 条的旧原文（尾部最多 len(ordered)-N 条）
        self._add_messages(scope, units)
        evicted_ids = [u.id for u in ordered[self._recent_originals_limit:]]
        if evicted_ids:
            try:
                self._delete_messages(scope, evicted_ids)
                logger.info(
                    "Evolver._persist_and_maintain_messages: %d old originals evicted "
                    "(kept %d)",
                    len(evicted_ids), self._recent_originals_limit,
                )
            except Exception as exc:
                logger.warning(
                    "Evolver._persist_and_maintain_messages: evict %d originals failed: %s",
                    len(evicted_ids), exc,
                )
        return recent

    def _related_memories(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """用 dedup.recall 召回相关记忆（复用去重向量空间，返回完整 MemoryUnit + score）。

        对本轮每个 unit 调一次 dedup.recall（它已滤自身、滤非 ACTIVE、按 min_similarity
        过滤、聚合取 max），合并去重后取 top-N。dedup.recall 内部异常吞掉返回空，不阻断。
        """
        seen: dict[str, MemoryUnit] = {}
        for unit in units:
            try:
                hits = self._dedup.recall(unit)
            except Exception as exc:
                logger.warning(
                    "Evolver._related_memories: dedup.recall failed for %s: %s",
                    unit.id[:8], exc,
                )
                continue
            for hit_unit, _score in hits:
                if hit_unit.id not in seen:
                    seen[hit_unit.id] = hit_unit
                if len(seen) >= self._related_memories_top_k:
                    break
            if len(seen) >= self._related_memories_top_k:
                break
        return list(seen.values())[: self._related_memories_top_k]

    # ------------------------------------------------------------------
    # 分层标注（抽取/升华后、去重落盘前）
    # ------------------------------------------------------------------

    def _annotate_layers(self, units: List[MemoryUnit]) -> None:
        """对派生候选标注 L0/L1 分层（best effort，不阻断）。

        ``layer_annotator`` 为 None 时跳过（向后兼容）。标注在去重落盘前，保证
        落盘的 unit 已带 layers（见 F01-memory-layer）。annotator 内部按阈值筛选，
        短 content 留空。失败降级为空 layers。
        """
        if self._layer_annotator is None or not units:
            return
        try:
            self._layer_annotator.annotate(units)
        except Exception as exc:
            logger.warning(
                "Evolver._annotate_layers: annotate failed for %d units: %s, "
                "proceeding without layers",
                len(units),
                exc,
            )

    # ------------------------------------------------------------------
    # 归属判定（抽取后、分层标注与去重之前）
    # ------------------------------------------------------------------

    def _route(
        self, sources: List[MemoryUnit], derived: List[MemoryUnit]
    ) -> List[MemoryUnit]:
        """按源单元携带的判定上下文改写派生单元的落点与标签，返回保留下来的单元。

        判定上下文经源单元的瞬态 metadata 键 ``route_ctx`` 传入，取不到即整段跳过——
        后台演进通道从存储重新读取原文，反序列化后该键已不存在，因此后台路径天然不判定。

        插入点在抽取之后、分层标注与去重之前：晚于去重会在源空间比对、在目标空间落盘，
        跨空间的重复内容既查不出也拦不住。

        两个落盘不变量与「落点须在候选集内」的兜底都在
        :func:`~construction.router.route_batch` 内完成，不在本方法重写一份。
        """
        ctx = _route_ctx_of(sources)
        if ctx is None or not derived:
            return derived
        decisions = route_batch(self._router, derived, ctx)
        kept = apply_decisions(decisions)
        spaces = sorted({unit.scope.space for unit in kept})
        degraded = degraded_reasons(decisions)
        if degraded:
            # 落 WARNING 而非审计：派生单元不是调用方提交的对象，本记录的消费者是运维
            # 告警而非合规回放（F07「降级记录按消费者分通道」）。词汇与计数口径与直写
            # 路径共用 ``degraded_reasons``，两处交叉核对得出同一件事。
            logger.warning(
                "Evolver._route: derived=%d kept=%d spaces=%s degraded=%d/%d reasons=%s",
                len(derived),
                len(kept),
                spaces,
                sum(degraded.values()),
                len(decisions),
                dict(degraded.most_common()),
            )
        else:
            logger.info(
                "Evolver._route: derived=%d kept=%d spaces=%s", len(derived), len(kept), spaces
            )
        return kept

    def _evolve_extract(self, units: List[MemoryUnit]) -> EvolveResult:
        """EXTRACT：抽取派生候选 → 分层标注 → 去重判定+落盘（_dedup_batch）。

        子类可覆盖本方法切换 EXTRACT 路径（如 DynamicEvolver 走 extract→consolidate→reflect→落盘）。
        """
        procedural = self._is_procedural(units)
        if procedural:
            # procedural=true：过程记忆抽取——不收集 context（不检索10条、不拉10条）、
            # 不走去重。extractor 把本轮汇总成 1 条 PROCEDURAL 执行历史，直接落 /memory/
            # 建索引。见 F02「过程记忆抽取」。未抽出则直接返回空，不落盘。
            extracted = self._extractor.extract(units, context=None)
            logger.info(
                "Evolver: EXTRACT(procedural) extractor returned %d units", len(extracted)
            )
            if not extracted:
                return EvolveResult()
            copy_consolidation_prompts(units, extracted)
            extracted = self._route(units, extracted)
            if not extracted:
                return EvolveResult()
            self._annotate_layers(extracted)
            created = self._persist(extracted)
            return EvolveResult(created_ids=created, created_units=list(extracted))
        # 非 procedural 的 EXTRACT 路径（infer 同步抽取 / background EXTRACT）：
        recent = self._persist_and_maintain_messages(units)
        context = self._maybe_collect_extract_context(units, recent)
        extracted = self._extractor.extract(units, context=context)
        logger.info("Evolver: EXTRACT extractor returned %d units", len(extracted))
        if not extracted:
            return EvolveResult()
        copy_consolidation_prompts(units, extracted)
        extracted = self._route(units, extracted)
        if not extracted:
            return EvolveResult()
        self._annotate_layers(extracted)
        return self._dedup_batch(extracted)

    def _evolve_consolidate(self, units: List[MemoryUnit]) -> EvolveResult:
        """整合分支：不做归属判定，与后台通道同因。

        判定上下文经瞬态键 ``route_ctx`` 从写入入口传入，只在该次写入的调用链上存在；整合
        由调度器在后台触发，取不到这个上下文。整合产物因此沿用被整合单元的落点。不要在这里
        补判定插入点——没有可用的候选空间集合，判出的落点无从校验是否在调用方的写权范围内。
        """
        abstracted = self._abstractor.abstract(units)
        logger.info("Evolver: CONSOLIDATE abstractor returned %d units", len(abstracted))
        if not abstracted:
            return EvolveResult()
        copy_consolidation_prompts(units, abstracted)
        self._annotate_layers(abstracted)
        return self._dedup_batch(abstracted)

    def _evolve_associate(self, units: List[MemoryUnit]) -> EvolveResult:
        relations = self._associator.associate(units)
        self.relations.extend(relations)
        self._persist_graph(units, relations)
        logger.info("Evolver: ASSOCIATE found %d relations", len(relations))
        for relation in relations:
            metadata_preview = {}
            for key, value in relation.metadata.items():
                metadata_preview[key] = value[:50] if isinstance(value, str) else value
            logger.info(
                "Evolver: relation %s→%s type=%s score=%.2f metadata=%s",
                relation.source_id[:8],
                relation.target_id[:8],
                relation.relation,
                relation.score,
                metadata_preview,
            )
        return EvolveResult(updated_ids=[relation.target_id for relation in relations])

    def _evolve_forget(self, units: List[MemoryUnit]) -> EvolveResult:
        """FORGET 作用于 SUPERSEDED 旧版（建索引记忆，在 /memory/）。

        非破坏式：记忆本体留 FORGOTTEN 状态供审计，仅退出检索。状态判定在本方法内完成，
        据此对索引下两条明确指令——``update(mode=FORWARD_ONLY)`` 只回写本体新状态，
        ``remove(mode=SOFT)`` 让它退出检索。两条指令互不重叠，检索索引不会
        被先重建再删除。IndexBuilder 不解读 lifecycle，只执行被要求的操作。
        """
        targets = [u for u in units if u.lifecycle == LifecycleState.SUPERSEDED]
        if not targets:
            logger.info("Evolver: FORGET marked 0 units as forgotten")
            return EvolveResult(forgotten_ids=[])

        for u in targets:
            u.lifecycle = LifecycleState.FORGOTTEN
        self._index.update(targets, mode=IndexWriteMode.FORWARD_ONLY)  # 只回写本体 FORGOTTEN
        self._index.remove(targets, mode=IndexRemoveMode.SOFT)         # 检索索引移出检索

        forgotten = [u.id for u in targets]
        logger.info("Evolver: FORGET marked %d units as forgotten", len(forgotten))
        return EvolveResult(forgotten_ids=forgotten)


# -- 注册到 EvolverProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



def _resolve_message_store(config) -> KVStore:
    """取原文 store：params 声明了就按引用取，没声明则复用 ``kv_store.default`` 具名实例。

    不能简单用 ``KvProducer.dep(config, "message_store", default="memory")``——``dep`` 的
    ``default`` 分支是**匿名新建**，会造出一个与配置后端无关的进程内 KV，原文从此写进谁也
    看不见的 dict（不落盘、重启即失、``storage.scan`` 查不到、无告警）。

    而字段缺失是常态：``AssemblyContext.merged`` 按具名实例整体覆盖，用户一旦声明
    ``evolver.default``（如 examples/config_template.yml 里启用 dynamic 的写法），内置
    params 里的 ``message_store`` 就随之消失。

    与 :meth:`StorageProducer.resolve` 同款兜底——两者都是**有状态**依赖，新建一个等于换后端。
    """
    if "message_store" in config.params:
        return KvProducer.dep(config, "message_store")
    if "default" in config.ctx.namespaces.get(KvProducer.TOP_NAME, {}):
        return KvProducer.build_named("default", config.ctx)
    return KvProducer.build("memory", {}, config.ctx)


@EvolverProducer.register("orchestrating")
def _build(config):
    """装配 OrchestratingEvolver：经各 Producer ``dep`` 取全部算子 + 去重 dedup。

    各依赖经对应 Producer ``XProducer.dep(config, default=...)`` 取得——具名实例经引用
    （``kv_store`` / ``index_builder`` / ``dedup`` / ``graph_store`` / ``llm`` …）与
    ``InMemoryEngine``、``HybridIndexBuilder`` 等共享同一实例，保证去重检索的是已索引的内容。
    """
    # index_builder / dedup 缺省都随 vector_enabled：向量开走 hybrid+vector，
    # 只倒排走 fulltext+keyword（去重仍可用——向量路在 fulltext-only 下 VectorStore 恒空，
    # 会使去重失效，故此时改用倒排召回）。
    vector_on = config.get("vector_enabled", True)
    ib_default = "hybrid" if vector_on else "fulltext"
    dr_default = "vector" if vector_on else "keyword"
    # layer_annotator 可选：本 evolver params 显式声明 ``layer_annotator`` 时按它取
    # （None/空串 → 显式禁用，视频 profile 用此关闭 L0/L1 标注，对齐 F05；
    # 命名字符串 → 用该具名实例）；键不存在时回退全局 namespace ``default``（向后兼容）。
    # 直接走 build_named 取具名实例，不经 dep（dep 从组件 params 取字段，
    # layer_annotator 是顶层命名空间）。

    def _opt_annotator():
        if "layer_annotator" in config.params:
            name = config.params.get("layer_annotator")
            if not name:  # None / "" → 显式禁用
                return None
            return LayerAnnotatorProducer.build_named(str(name), config.ctx)
        ctx = config.ctx
        ns = ctx.namespaces.get(LayerAnnotatorProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        return LayerAnnotatorProducer.build_named("default", ctx)

    return OrchestratingEvolver(
        extractor=ExtractorProducer.dep(config, default="keyword"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        storage=StorageProducer.resolve(config),
        message_store=_resolve_message_store(config),
        dedup=DedupProducer.dep(config, default=dr_default),
        llm=LlmProducer.dep(config, default="echo"),
        layer_annotator=_opt_annotator(),
        router=optional_router(config),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
    )
