"""DynamicEvolver：EXTRACT 走动态 prompt 四步编排的 Evolver 实现。

继承 :class:`OrchestratingEvolver`，只覆盖 ``_evolve_extract``：
``extract → consolidate(判定) → reflect → 落盘``。其余三模式
（CONSOLIDATE/ASSOCIATE/FORGET）继承父类行为。

与父类 legacy EXTRACT（``_dedup_batch`` 判定+落盘耦合）的区别：
- consolidate 步只产出 :class:`ConsolidateDecision`，不落盘；
- 落盘延后到 reflect 之后统一执行；
- consolidate/reflect 的 prompt 从 :class:`PromptRegistry` 按 key 查
  （metadata 只写 prompt key，引用 yml ``prompts`` 段）。

注册名 ``dynamic``，与 ``orchestrating`` 平级，同属 ``evolver`` 顶层命名空间。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    DedupDecision,
    LifecycleState,
    MemoryUnit,
    Segment,
    inherited_system_metadata,
    inherited_user_metadata,
)
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.base import ExtractContext
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
    _resolve_message_store,
)
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.construction.prompt_registry import (
    PHASE_CONSOLIDATE,
    PromptRegistry,
)
from jiuwen_memory.construction.prompt_strategy import (
    CONSOLIDATION_PROMPT_PREFIX,
    EXTRACTION_STRATEGY_KEY,
    copy_consolidation_prompts,
    copy_reflect_prompts,
    parse_prompt_strategies,
)
from jiuwen_memory.storage.storage import StorageProducer

logger = get_logger(__name__)


@dataclass
class ConsolidateDecision:
    """consolidate 步的单条判定结果（不落盘）。"""

    candidate: MemoryUnit
    decision: DedupDecision
    existing: Optional[MemoryUnit]
    score: float


class DynamicEvolver(OrchestratingEvolver):
    """EXTRACT 走动态 prompt 四步编排的 Evolver。

    构造参数在父类基础上新增 ``prompt_registry``（consolidate/reflect 步按 key 查 prompt）。
    其余依赖（extractor/abstractor/associator/index_builder/kv/graph/dedup/llm/layer_annotator）
    与父类同构，由装配注入。
    """

    def __init__(
        self,
        *args,
        prompt_registry: PromptRegistry | None = None,
        **kwargs,
    ) -> None:
        """初始化 DynamicEvolver。

        Args:
            prompt_registry: 参数 prompt_registry（PromptRegistry | None）。
            *args: 参数 args。
            **kwargs: 参数 kwargs。
        """
        super().__init__(*args, **kwargs)
        self._prompts = prompt_registry or PromptRegistry()

    def _evolve_extract(self, units: List[MemoryUnit]) -> EvolveResult:
        """动态四步：extract → consolidate(判定) → reflect → 落盘。

        procedural 路径仍走父类行为（不收集 context、不判定、直接落盘）——
        procedural 语义是"把这轮做了什么记成一条 how-to"，无需动态判定。
        """
        if self._is_procedural(units):
            return super()._evolve_extract(units)

        # 非 procedural：infer 同步抽取 / background EXTRACT
        recent = self._persist_and_maintain_messages(units)
        context = self._maybe_collect_extract_context(units, recent)
        candidates = self._extract_step(units, context)
        if not candidates:
            return EvolveResult()
        decisions = self._consolidate_step(candidates)
        reflected = self._reflect_step(candidates, decisions)
        return self._persist_decisions(reflected, decisions)

    # ------------------------------------------------------------------
    # 步骤 1：抽取
    # ------------------------------------------------------------------

    def _extract_step(
        self,
        units: List[MemoryUnit],
        context: Optional[ExtractContext],
    ) -> List[MemoryUnit]:
        """执行 `extract_step` 操作。

        Args:
            units: 参数 units（List[MemoryUnit]）。
            context: 参数 context（Optional[ExtractContext]）。

        Returns:
            返回 List[MemoryUnit]。
        """
        extracted = self._extractor.extract(units, context=context)
        logger.info("DynamicEvolver: EXTRACT extractor returned %d units", len(extracted))
        if not extracted:
            return []
        copy_consolidation_prompts(units, extracted)
        copy_reflect_prompts(units, extracted)
        self._annotate_layers(extracted)
        return extracted

    # ------------------------------------------------------------------
    # 步骤 2：巩固（只判定不落盘）
    # ------------------------------------------------------------------

    def _consolidate_step(
        self,
        candidates: List[MemoryUnit],
    ) -> List[ConsolidateDecision]:
        """执行 `consolidate_step` 操作。

        Args:
            candidates: 参数 candidates（List[MemoryUnit]）。

        Returns:
            返回 List[ConsolidateDecision]。
        """
        decisions: List[ConsolidateDecision] = []
        for candidate in candidates:
            try:
                hits = self._dedup.recall(candidate)
            except Exception as exc:
                logger.warning(
                    "DynamicEvolver recall failed for %s, fallback ADD: %s",
                    candidate.id[:8],
                    exc,
                )
                hits = []
            decision, existing, score = self._judge(candidate, hits)
            decisions.append(
                ConsolidateDecision(
                    candidate=candidate,
                    decision=decision,
                    existing=existing,
                    score=score,
                )
            )
        return decisions

    def _judge(
        self,
        candidate: MemoryUnit,
        hits: List[Tuple[MemoryUnit, float]],
    ) -> Tuple[DedupDecision, Optional[MemoryUnit], float]:
        """执行 `judge` 操作。

        Args:
            candidate: 参数 candidate（MemoryUnit）。
            hits: 参数 hits（List[Tuple[MemoryUnit, float]]）。

        Returns:
            返回 Tuple[DedupDecision, Optional[MemoryUnit], float]。
        """
        if not hits:
            return DedupDecision.ADD, None, 0.0
        existing, score = max(hits, key=lambda item: item[1])
        if score >= self._dedup_high_similarity:
            return DedupDecision.NOOP, existing, score
        if score < self._dedup_medium_similarity:
            return DedupDecision.ADD, existing, score
        prompt = self._resolve_consolidate_prompt(candidate)
        if prompt is None:
            return DedupDecision.ADD, existing, score
        try:
            return self._llm_judge(candidate, hits, prompt)
        except Exception as exc:
            logger.warning(
                "DynamicEvolver LLM judge failed for %s, fallback rule: %s",
                candidate.id[:8],
                exc,
            )
            if score >= self._dedup_high_similarity:
                return DedupDecision.NOOP, existing, score
            return DedupDecision.ADD, existing, score

    def _resolve_consolidate_prompt(self, candidate: MemoryUnit) -> Optional[str]:
        """解析并返回目标配置或资源。

        Args:
            candidate: 参数 candidate（MemoryUnit）。

        Returns:
            返回 Optional[str]。
        """
        prompts = parse_prompt_strategies(
            candidate.system_metadata, CONSOLIDATION_PROMPT_PREFIX
        )
        if not prompts:
            return None
        extraction_strategy = str(candidate.system_metadata.get(
            EXTRACTION_STRATEGY_KEY, ""
        )).strip()
        if extraction_strategy:
            for _strategy, prompt_key in prompts:
                if _strategy == extraction_strategy:
                    return self._prompts.get(PHASE_CONSOLIDATE, prompt_key)
        _strategy, prompt_key = prompts[0]
        return self._prompts.get(PHASE_CONSOLIDATE, prompt_key)

    def _llm_judge(
        self,
        candidate: MemoryUnit,
        hits: List[Tuple[MemoryUnit, float]],
        prompt: str,
    ) -> Tuple[DedupDecision, Optional[MemoryUnit], float]:
        """执行 `llm_judge` 操作。

        Args:
            candidate: 参数 candidate（MemoryUnit）。
            hits: 参数 hits（List[Tuple[MemoryUnit, float]]）。
            prompt: 参数 prompt（str）。

        Returns:
            返回 Tuple[DedupDecision, Optional[MemoryUnit], float]。

        Raises:
            ValueError: 执行失败时抛出。
        """
        hit_map = {unit.id: (unit, score) for unit, score in hits[:5]}
        existing_text = "\n\n".join(
            f"[Memory ID: {unit.id}]\nContent: {unit.content}\n"
            f"Tier: {unit.tier.value}\nSimilarity: {score:.3f}"
            for unit, score in hits[:5]
        )
        messages = [
            ChatMessage(role="system", content=prompt),
            ChatMessage(
                role="user",
                content=(
                    f"Candidate ID: {candidate.id}\n"
                    f"Candidate content: {candidate.content}\n\n"
                    f"Existing memories:\n{existing_text or '(none)'}"
                ),
            ),
        ]
        response = self._llm.chat(messages, temperature=0, max_tokens=512)
        payload = _parse_object(response)
        decision = DedupDecision(
            str(payload.get("decision", "add")).strip().lower()
        )
        existing_id = str(payload.get("existing_id", "")).strip()
        existing, score = hit_map.get(existing_id, (None, 0.0))
        if decision in {DedupDecision.UPDATE, DedupDecision.SUPERSEDE} and existing is None:
            raise ValueError("修改型决策必须引用本次召回中的 existing_id")
        return decision, existing, score

    # ------------------------------------------------------------------
    # 步骤 3：反思（默认 no-op，子类可覆盖）
    # ------------------------------------------------------------------

    def _reflect_step(
        self,
        candidates: List[MemoryUnit],
        decisions: List[ConsolidateDecision],
    ) -> List[MemoryUnit]:
        """执行 `reflect_step` 操作。

        Args:
            candidates: 参数 candidates（List[MemoryUnit]）。
            decisions: 参数 decisions（List[ConsolidateDecision]）。

        Returns:
            返回 List[MemoryUnit]。
        """
        return candidates

    # ------------------------------------------------------------------
    # 步骤 4：落盘（执行 decision）
    # ------------------------------------------------------------------

    def _persist_decisions(
        self,
        candidates: List[MemoryUnit],
        decisions: List[ConsolidateDecision],
    ) -> EvolveResult:
        """执行 `persist_decisions` 操作。

        Args:
            candidates: 参数 candidates（List[MemoryUnit]）。
            decisions: 参数 decisions（List[ConsolidateDecision]）。

        Returns:
            返回 EvolveResult。
        """
        result = EvolveResult()
        for decision in decisions:
            self._apply_decision(
                decision.candidate,
                decision.decision,
                decision.existing,
                decision.score,
                result,
            )
        return result

    def _apply_decision(
        self,
        candidate: MemoryUnit,
        decision: DedupDecision,
        existing: Optional[MemoryUnit],
        similarity: float,
        result: EvolveResult,
    ) -> int:
        """执行单条决策，返回 noop 计数（0 或 1，与父类签名一致）。"""
        if decision == DedupDecision.ADD or (
            decision in {DedupDecision.UPDATE, DedupDecision.SUPERSEDE}
            and existing is None
        ):
            self._index.build([candidate])
            result.created_ids.append(candidate.id)
            return 0
        if decision == DedupDecision.NOOP:
            return 1
        if decision == DedupDecision.UPDATE:
            if existing is None:
                raise ValueError("UPDATE 决策必须提供 existing memory")
            merged = self._merge_content(existing, candidate)
            if existing.segments:
                existing.segments[0].content = merged
            else:
                existing.segments = [Segment(content=merged)]
            existing.provenance = list(
                set(existing.provenance) | set(candidate.provenance) | {candidate.id}
            )
            existing.system_metadata = inherited_system_metadata([existing, candidate])
            existing.user_metadata = inherited_user_metadata([existing, candidate])
            existing.system_metadata.update(
                {
                    "dedup_decision": "update",
                    "dedup_similarity": str(similarity),
                    "dedup_merged_from": candidate.id,
                }
            )
            self._index.update([existing])
            result.updated_ids.append(existing.id)
            return 0
        if existing is None:
            raise ValueError("SUPERSEDE 决策必须提供 existing memory")
        candidate.supersedes = existing.id
        candidate.provenance = list(set(candidate.provenance) | {existing.id})
        candidate.system_metadata.update(
            {
                "dedup_decision": "supersede",
                "dedup_similarity": str(similarity),
                "dedup_superseded": existing.id,
            }
        )
        self._index.build([candidate])
        existing.lifecycle = LifecycleState.SUPERSEDED
        existing.temporal.t_invalid = datetime.now(timezone.utc)
        self._index.update([existing])
        result.created_ids.append(candidate.id)
        result.superseded_ids.append(existing.id)
        return 0

    def _merge_content(self, old: MemoryUnit, new: MemoryUnit) -> str:
        """执行 `merge_content` 操作。

        Args:
            old: 参数 old（MemoryUnit）。
            new: 参数 new（MemoryUnit）。

        Returns:
            返回 str。
        """
        user_prompt = f"Old:\n{old.content}\n\nNew:\n{new.content}"
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "Merge the old and new memory into one concise, self-contained "
                    "statement. Preserve all non-conflicting details and prefer the "
                    "new value on conflict. Output only merged plain text."
                ),
            ),
            ChatMessage(role="user", content=user_prompt),
        ]
        try:
            return self._llm.chat(messages, temperature=0, max_tokens=512).strip()
        except Exception:
            return f"{old.content}\n{new.content}"


def _parse_object(response: str) -> dict:
    """解析输入数据并返回结构化结果。

    Args:
        response: 参数 response（str）。

    Returns:
        返回 dict。

    Raises:
        ValueError: 执行失败时抛出。
    """
    try:
        payload = json.loads(response.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match is None:
            raise
        payload = json.loads(match.group())
    if not isinstance(payload, dict):
        raise ValueError("consolidation 响应必须是 JSON object")
    return payload


@EvolverProducer.register("dynamic")
def _build(config):
    """装配 DynamicEvolver：复用 OrchestratingEvolver 的全部依赖，额外注入 PromptRegistry。

    prompts 从 ``ctx.globals["prompts"]`` 加载（yml 顶层 ``prompts`` 段）；
    ``config.get("prompts")`` 在 params 无 prompts 时回退 globals。
    """
    vector_on = config.get("vector_enabled", True)
    ib_default = "hybrid" if vector_on else "fulltext"
    dr_default = "vector" if vector_on else "keyword"

    def _opt_annotator():
        """执行 `opt_annotator` 操作。"""
        from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer

        ctx = config.ctx
        ns = ctx.namespaces.get(LayerAnnotatorProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        return LayerAnnotatorProducer.build_named("default", ctx)

    prompts_data = config.get("prompts")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    # 与 build_kernel 注入的 default ConfigSource 共享，使 prompts.* 可运行时切换
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )

    return DynamicEvolver(
        extractor=ExtractorProducer.dep(config, default="dynamic_llm"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        storage=StorageProducer.resolve(config),
        message_store=_resolve_message_store(config),
        dedup=DedupProducer.dep(config, default=dr_default),
        llm=LlmProducer.dep(config, default="echo"),
        layer_annotator=_opt_annotator(),
        prompt_registry=registry,
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
    )
