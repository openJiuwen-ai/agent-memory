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
SUPERSEDE 标记由 Evolver 直接通过 KVStore+IndexBuilder 完成，不依赖 LifecycleManager——
避免 construction 层依赖 control 层。

真源/索引/图由装配注入；演进产物经 :func:`~common.type_def.memory_codec.dumps` 落 kv。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from common.errors import ConflictError
from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import DedupDecision, LifecycleState, MemoryUnit, Relation
from common.type_def.chat import ChatMessage
from common.type_def.memory import Segment
from common.type_def.memory_codec import dumps
from construction.abstractor import Abstractor, AbstractorProducer
from construction.associator import Associator, AssociatorProducer
from construction.base import OperatorType
from construction.dedup import Dedup, DedupProducer
from construction.evolver import EvolveMode, Evolver, EvolveResult, EvolverProducer
from construction.extractor import Extractor, ExtractorProducer
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.graph import GraphProducer, GraphStore
from storage.kv import KvProducer, KVStore
from storage.types import Edge, Node

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 去重判定 Prompt
# ---------------------------------------------------------------------------

_DEDUP_SYSTEM_PROMPT = """You are a memory deduplication assistant. Determine whether a candidate memory
is a new fact, an update to an existing memory, a replacement of an existing memory, or a duplicate
of an existing memory.

Output ONLY a JSON object with:
- "decision": "add" | "update" | "supersede" | "noop"
- "reason": brief explanation (one sentence)

Decision rules:
- "add": The candidate contains new information not covered by any existing memory.
- "update": The candidate adds information to an existing memory (partial overlap, candidate has extra details).
- "supersede": The candidate completely replaces an existing memory (same topic, candidate is more complete/accurate/recent).
- "noop": The candidate is fully covered by an existing memory (no new information).

If unsure, prefer "add" (to avoid losing information)."""

_CONTENT_MERGE_SYSTEM_PROMPT = """You are a memory content merger. Merge the old and new memory contents
into a single coherent statement that preserves all information from both versions.

Output ONLY the merged content as plain text. No explanation, no labels, no JSON.
Rules:
- Preserve all facts/details from both versions.
- If conflicting, prefer the newer version (the second input).
- Keep the result concise and self-contained."""

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    SUPERSEDE 标记由本类直接通过 KVStore.update + IndexBuilder.update 完成，
    不依赖 LifecycleManager（避免 construction 层依赖 control 层）。
    """

    def __init__(
        self,
        extractor: Extractor,
        abstractor: Abstractor,
        associator: Associator,
        index_builder: IndexBuilder,
        kv: KVStore,
        graph: GraphStore,
        dedup: Dedup,
        llm: LLM,
        *,
        dedup_medium_similarity: float = 0.7,
        dedup_high_similarity: float = 0.85,
    ) -> None:
        self._extractor = extractor
        self._abstractor = abstractor
        self._associator = associator
        self._index = index_builder
        self._kv = kv
        self._graph = graph
        self._dedup = dedup
        self._llm = llm
        self.relations: List[Relation] = []  # ASSOCIATE 产物（同时已落图）

        # 去重阈值配置（min/top_k/tier_filter/scope_filter 已下沉到 recaller；
        # medium/high 阈值留本类做 LLM 判定的档位划分）
        self._dedup_medium_similarity = dedup_medium_similarity
        self._dedup_high_similarity = dedup_high_similarity

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    # ------------------------------------------------------------------
    # 落盘辅助
    # ------------------------------------------------------------------

    def _persist(self, units: List[MemoryUnit]) -> List[str]:
        for u in units:
            self._kv.insert(u.scope, u.id, dumps(u))
            self._index.build([u])
        return [u.id for u in units]

    def _persist_graph(self, units: List[MemoryUnit], relations: List[Relation]) -> None:
        """为涉及关联的单元建节点（幂等）、为每条关联建一条边。"""
        if not units:
            return
        scope = units[0].scope
        for u in units:
            node = Node(id=u.id, label=u.tier.value, properties={"content": u.content})
            try:
                self._graph.insert(scope, nodes=[node])
            except ConflictError:
                pass  # 节点已存在
        edges = [
            Edge(
                id=str(uuid.uuid4()),
                source=r.source_id,
                target=r.target_id,
                relation=r.relation,
                properties=dict(r.metadata),
            )
            for r in relations
        ]
        if edges:
            self._graph.insert(scope, edges=edges)

    # ------------------------------------------------------------------
    # 去重核心方法
    # ------------------------------------------------------------------

    def _dedup_batch(self, candidates: List[MemoryUnit]) -> EvolveResult:
        """对一批候选 unit 执行去重判定和后续动作，返回 EvolveResult。

        逐条候选走 Dedup.recall(召回+过滤) → Step D(LLM判定) → 执行。
        """
        result = EvolveResult()
        noop_count = 0

        for candidate in candidates:
            try:
                decision, existing_unit, similarity = self._dedup_single(candidate)
            except Exception as exc:
                logger.warning(
                    "Evolver._dedup: dedup failed for candidate %s, fallback ADD: %s",
                    candidate.id[:8], exc,
                )
                decision = DedupDecision.ADD
                existing_unit = None
                similarity = 0.0

            logger.info(
                "Evolver._dedup: candidate %s → decision=%s existing=%s similarity=%.3f",
                candidate.id[:8], decision.value,
                existing_unit.id[:8] if existing_unit else "None",
                similarity,
            )

            if decision == DedupDecision.ADD:
                self._kv.insert(candidate.scope, candidate.id, dumps(candidate))
                self._index.build([candidate])
                result.created_ids.append(candidate.id)

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
                existing_unit.metadata.update(candidate.metadata)
                existing_unit.metadata["dedup_decision"] = "update"
                existing_unit.metadata["dedup_similarity"] = str(similarity)
                existing_unit.metadata["dedup_merged_from"] = candidate.id
                self._kv.update(existing_unit.scope, existing_unit.id, dumps(existing_unit))
                self._index.update([existing_unit])
                result.updated_ids.append(existing_unit.id)

            elif decision == DedupDecision.SUPERSEDE:
                # 新版落盘
                candidate.supersedes = existing_unit.id
                candidate.provenance = list(
                    set(candidate.provenance) | {existing_unit.id}
                )
                candidate.metadata["dedup_decision"] = "supersede"
                candidate.metadata["dedup_similarity"] = str(similarity)
                candidate.metadata["dedup_superseded"] = existing_unit.id
                self._kv.insert(candidate.scope, candidate.id, dumps(candidate))
                self._index.build([candidate])
                # 旧版标记 SUPERSEDED（直接通过 KVStore，不依赖 LifecycleManager）
                existing_unit.lifecycle = LifecycleState.SUPERSEDED
                existing_unit.temporal.t_invalid = _now()
                self._kv.update(existing_unit.scope, existing_unit.id, dumps(existing_unit))
                self._index.update([existing_unit])
                result.created_ids.append(candidate.id)
                result.superseded_ids.append(existing_unit.id)

            elif decision == DedupDecision.NOOP:
                noop_count += 1
                logger.info(
                    "Evolver._dedup: NOOP candidate %s skipped (duplicate of %s)",
                    candidate.id[:8],
                    existing_unit.id[:8] if existing_unit else "N/A",
                )

        logger.info(
            "Evolver._dedup: total=%d add=%d update=%d supersede=%d noop=%d",
            len(candidates), len(result.created_ids), len(result.updated_ids),
            len(result.superseded_ids), noop_count,
        )
        return result

    def _dedup_single(
        self, candidate: MemoryUnit
    ) -> Tuple[DedupDecision, Optional[MemoryUnit], float]:
        """单条候选的去重判定：Dedup.recall → LLM 语义判定。

        返回 (decision, most_similar_existing_unit, max_similarity_score)。
        召回 + min 阈值过滤 + 加载 + 聚合取 max 全在 recaller 内完成；本方法只做
        medium/high 档位划分与 LLM 判定。
        """
        # Step A+B: 召回已有相似记忆（已滤自身、已按 min_similarity 过滤、已聚合取 max）
        hit_units: List[Tuple[MemoryUnit, float]] = self._dedup.recall(candidate)
        if not hit_units:
            logger.debug("Evolver._dedup_single: no hits for candidate %s → ADD", candidate.id[:8])
            return (DedupDecision.ADD, None, 0.0)

        # 找最高相似度的 hit
        best_unit, best_score = max(hit_units, key=lambda x: x[1])

        # Step D: 语义判定
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
            # 降级：LLM 不可用时按 cosine 阈值规则判定
            if best_score >= self._dedup_high_similarity:
                decision = DedupDecision.NOOP
            else:
                decision = DedupDecision.SUPERSEDE

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

    # ------------------------------------------------------------------
    # Evolver 契约
    # ------------------------------------------------------------------

    def evolve(self, units: List[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        logger.info("Evolver: evolve mode=%s, %d units", mode.value, len(units))
        for u in units:
            logger.info("Evolver: input unit id=%s tier=%s lifecycle=%s provenance=%s content=%s",
                         u.id[:8], u.tier.value, u.lifecycle.value, u.provenance, u.content[:200])
        if mode == EvolveMode.EXTRACT:
            extracted = self._extractor.extract(units)
            logger.info("Evolver: EXTRACT extractor returned %d units", len(extracted))
            result = self._dedup_batch(extracted)
            return result
        if mode == EvolveMode.CONSOLIDATE:
            abstracted = self._abstractor.abstract(units)
            logger.info("Evolver: CONSOLIDATE abstractor returned %d units", len(abstracted))
            result = self._dedup_batch(abstracted)
            return result
        if mode == EvolveMode.ASSOCIATE:
            relations = self._associator.associate(units)
            self.relations.extend(relations)
            self._persist_graph(units, relations)
            logger.info("Evolver: ASSOCIATE found %d relations", len(relations))
            for r in relations:
                logger.info("Evolver: relation %s→%s type=%s score=%.2f metadata=%s",
                             r.source_id[:8], r.target_id[:8], r.relation, r.score,
                             {k: v[:50] if isinstance(v, str) else v for k, v in r.metadata.items()})
            return EvolveResult(updated_ids=[r.target_id for r in relations])
        if mode == EvolveMode.FORGET:
            forgotten: List[str] = []
            for u in units:
                if u.lifecycle == LifecycleState.SUPERSEDED:
                    u.lifecycle = LifecycleState.FORGOTTEN
                    self._kv.update(u.scope, u.id, dumps(u))
                    self._index.remove([u.id])
                    forgotten.append(u.id)
            logger.info("Evolver: FORGET marked %d units as forgotten", len(forgotten))
            return EvolveResult(forgotten_ids=forgotten)
        return EvolveResult()


# -- 注册到 EvolverProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



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
    return OrchestratingEvolver(
        extractor=ExtractorProducer.dep(config, default="keyword"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        kv=KvProducer.dep(config, default="memory"),
        graph=GraphProducer.dep(config, default="memory"),
        dedup=DedupProducer.dep(config, default=dr_default),
        llm=LlmProducer.dep(config, default="echo"),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.85),
    )
