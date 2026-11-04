# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ValidationError
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
from jiuwen_memory.config.document_flag import resolve_index_builder_default
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.appraiser import Appraisal
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.base import ExtractContext
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dedup_direct_noop import should_direct_noop
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
    _opt_appraiser,
    _resolve_message_store,
)
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.hierarchy_assembly import optional_hierarchy_composer
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
from jiuwen_memory.construction.router import optional_router
from jiuwen_memory.construction.snapshot_inheritance import (
    inherit_infer_snapshots,
    snapshot_for_decision,
)
from jiuwen_memory.storage.store_manager import StoreManagerProducer

logger = get_logger(__name__)


@dataclass
class ConsolidateDecision:
    """consolidate 步的单条判定结果（不落盘）。"""

    candidate: MemoryUnit
    decision: DedupDecision
    existing: MemoryUnit | None
    score: float


class DynamicEvolver(OrchestratingEvolver):
    """EXTRACT 走动态 prompt 四步编排的 Evolver。

    构造参数在父类基础上新增 ``prompt_registry``（consolidate/reflect 步按 key 查
    prompt）与 ``dedup_decisions``（按抽取策略名限定允许的去重结论，未列出的策略
    四种都允许——经验 profile 用它把误判的「更新」挡在合并分支外，§2.2）。
    其余依赖（extractor/abstractor/associator/index_builder/kv/graph/dedup/llm/layer_annotator）
    与父类同构，由装配注入。
    """

    def __init__(
        self,
        *args,
        prompt_registry: PromptRegistry | None = None,
        dedup_decisions: dict[str, set[DedupDecision]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._prompts = prompt_registry or PromptRegistry()
        self._dedup_decisions = dict(dedup_decisions) if dedup_decisions else {}

    def _evolve_extract(self, units: list[MemoryUnit]) -> EvolveResult:
        """动态四步：extract → consolidate(判定) → reflect → 落盘。

        procedural 路径仍走父类行为（不收集 context、不判定、直接落盘）——
        procedural 语义是"把这轮做了什么记成一条 how-to"，无需动态判定。

        评价算子的调用点在过程记忆的转交之后：带 ``procedural=true`` 的写入只经过
        父类的调用点，本类的调用点只覆盖非 procedural 路径（经验请求恒非
        procedural，§2.2）。准入评价在抽取之前，收尾在流程结束（含流程出错时以
        ``None`` 调），与父类 `_evolve_extract` 的接法相同。
        """
        if self._is_procedural(units):
            return super()._evolve_extract(units)

        # 非 procedural：infer 同步抽取 / background EXTRACT
        appraisal = self._appraise(units)
        if appraisal is not None and not appraisal.proceed:
            return EvolveResult()
        try:
            result = self._dynamic_extract(units, appraisal)
        except Exception:
            self._finalize(units, appraisal, None)
            raise
        self._finalize(units, appraisal, result)
        return result

    def _dynamic_extract(
        self, units: list[MemoryUnit], appraisal: Appraisal | None
    ) -> EvolveResult:
        """动态四步的实际管线；评价结论挂进抽取上下文（无评价算子时为 ``None``）。"""
        recent = self._persist_and_maintain_messages(units)
        context = self._maybe_collect_extract_context(units, recent)
        if appraisal is not None:
            if context is None:
                context = ExtractContext()
            context.appraisal = appraisal
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
        units: list[MemoryUnit],
        context: ExtractContext | None,
    ) -> list[MemoryUnit]:
        extracted = self._extractor.extract(units, context=context)
        inherit_infer_snapshots(units, extracted)
        logger.info("DynamicEvolver: EXTRACT extractor returned %d units", len(extracted))
        if not extracted:
            return []
        copy_consolidation_prompts(units, extracted)
        copy_reflect_prompts(units, extracted)
        # 归属判定与父类同一插入点：抽取之后、分层标注与后续判定之前。两个分支各自接线，
        # 因为本类覆盖了 _evolve_extract，父类插入点在本路径上不经过。
        extracted = self._route(units, extracted)
        if not extracted:
            return []
        self._annotate_layers(extracted)
        return extracted

    # ------------------------------------------------------------------
    # 步骤 2：巩固（只判定不落盘）
    # ------------------------------------------------------------------

    def _consolidate_step(
        self,
        candidates: list[MemoryUnit],
    ) -> list[ConsolidateDecision]:
        decisions: list[ConsolidateDecision] = []
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
        hits: list[tuple[MemoryUnit, float]],
    ) -> tuple[DedupDecision, MemoryUnit | None, float]:
        if not hits:
            return DedupDecision.ADD, None, 0.0
        existing, score = max(hits, key=lambda item: item[1])
        if should_direct_noop(score, self._dedup_high_similarity, candidate, existing):
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
            if should_direct_noop(score, self._dedup_high_similarity, candidate, existing):
                return DedupDecision.NOOP, existing, score
            return DedupDecision.ADD, existing, score

    def _resolve_consolidate_prompt(self, candidate: MemoryUnit) -> str | None:
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

    def _dedup_decisions_for(self, candidate: MemoryUnit) -> set[DedupDecision] | None:
        """候选所属抽取策略允许的去重结论；未限定返回 ``None``（四种都允许）。

        经验 profile 的取法（§2.2）：``experience_tip`` 为新增、重复；``experience_fact``
        为新增、重复、取代。范围外的结论一律按新增处理，见 :meth:`_llm_judge`。
        """
        if not self._dedup_decisions:
            return None
        strategy = str(
            candidate.system_metadata.get(EXTRACTION_STRATEGY_KEY, "")
        ).strip()
        if not strategy:
            return None
        declared = self._dedup_decisions.get(strategy)
        return set(declared) if declared else None

    def _llm_judge(
        self,
        candidate: MemoryUnit,
        hits: list[tuple[MemoryUnit, float]],
        prompt: str,
    ) -> tuple[DedupDecision, MemoryUnit | None, float]:
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
        # 去重结论的范围限定（§2.2）：策略声明了允许的结论时，范围外的一律按新增
        # 处理。误判为新增多出一条近似条目，由周期整理合并；误判为重复则复现计数
        # 失真——复现计数是可执行条目放行的依据。范围外的结论跳过 existing_id 校验
        # （按新增处理不需要已有条目）。
        allowed = self._dedup_decisions_for(candidate)
        if allowed is not None and decision not in allowed:
            logger.warning(
                "DynamicEvolver LLM judge returned out-of-scope decision %s for "
                "candidate %s, fallback ADD",
                decision.value, candidate.id[:8],
            )
            return DedupDecision.ADD, None, 0.0
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
        candidates: list[MemoryUnit],
        decisions: list[ConsolidateDecision],
    ) -> list[MemoryUnit]:
        return candidates

    # ------------------------------------------------------------------
    # 步骤 4：落盘（执行 decision）
    # ------------------------------------------------------------------

    def _persist_decisions(
        self,
        candidates: list[MemoryUnit],
        decisions: list[ConsolidateDecision],
    ) -> EvolveResult:
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
        existing: MemoryUnit | None,
        similarity: float,
        result: EvolveResult,
    ) -> int:
        """执行单条决策，返回 noop 计数（0 或 1，与父类签名一致）。"""
        snapshot = snapshot_for_decision(candidate, decision, existing)
        if decision == DedupDecision.ADD or (
            decision in {DedupDecision.UPDATE, DedupDecision.SUPERSEDE}
            and existing is None
        ):
            self._index.build([candidate])
            result.created_ids.append(candidate.id)
            result.created_units.append(candidate)
            return 0
        if decision == DedupDecision.NOOP:
            # 判为重复：把命中的已有条目 id 记入 duplicate_ids，读取方（评价算子的
            # finalize）据此记复现；没有有效已有条目时不填、记警告（§2.2）。
            if existing is not None:
                result.duplicate_ids.append(existing.id)
            else:
                logger.warning(
                    "DynamicEvolver: NOOP without existing for candidate %s, "
                    "duplicate_ids not recorded",
                    candidate.id[:8],
                )
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
            if snapshot is not None:
                existing.hierarchy = snapshot
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
        existing.temporal.t_invalid = datetime.now(UTC)
        self._index.update([existing])
        result.created_ids.append(candidate.id)
        result.created_units.append(candidate)
        result.superseded_ids.append(existing.id)
        return 0

    def _merge_content(self, old: MemoryUnit, new: MemoryUnit) -> str:
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
            merged = self._llm.chat(messages, temperature=0, max_tokens=512).strip()
            if merged:
                return merged
            # 空返回（HTTP 200 但 content 为空，模型输出抖动）视同合并失败，走降级。
            logger.warning(
                "DynamicEvolver._merge_content: LLM merge returned empty for %s→%s, "
                "fallback concatenation",
                old.id[:8], new.id[:8],
            )
        except Exception as exc:
            logger.warning(
                "DynamicEvolver._merge_content: LLM merge failed for %s→%s, "
                "fallback concatenation: %s",
                old.id[:8], new.id[:8], exc,
            )
        # 降级：简单拼接新旧内容
        return f"{old.content}\n{new.content}"


def _parse_object(response: str) -> dict:
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
    ``dedup_decisions`` 按抽取策略名声明允许的去重结论（§2.2），值非法时装配失败。
    """
    from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer

    vector_on = config.get("vector_enabled", True)
    ib_default = resolve_index_builder_default(config)
    dr_default = "vector" if vector_on else "keyword"

    def _opt_annotator():
        # 先读实例参数：置空即本实例不标注，与 OrchestratingEvolver 的装配一致
        # （此前只看全局 default，实例无法单独关闭）。
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

    def _parse_dedup_decisions(raw):
        if not raw:
            return {}
        if not isinstance(raw, Mapping):
            raise ValidationError(
                f"dedup_decisions 须是「策略名 → 结论列表」的映射，得到 {type(raw).__name__}"
            )
        parsed: dict[str, set[DedupDecision]] = {}
        for strategy, decisions in raw.items():
            if not isinstance(decisions, (list, tuple)) or not decisions:
                raise ValidationError(
                    f"dedup_decisions[{strategy!r}] 须是非空列表（add/update/supersede/noop）"
                )
            try:
                parsed[str(strategy)] = {DedupDecision(str(item)) for item in decisions}
            except ValueError as exc:
                raise ValidationError(
                    f"dedup_decisions[{strategy!r}] 含非法结论：{exc}"
                ) from exc
        return parsed

    prompts_data = config.get("prompts")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    # 与 build_kernel 注入的 default ConfigSource 共享，使 prompts.* 可运行时切换
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )

    hierarchy_index = IndexBuilderProducer.dep(config, "index_builder", default=ib_default)
    hierarchy_llm = LlmProducer.dep(config, default="echo")
    return DynamicEvolver(
        extractor=ExtractorProducer.dep(config, default="dynamic_llm"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=hierarchy_index,
        storage=StoreManagerProducer.resolve(config),
        message_store=_resolve_message_store(config),
        dedup=DedupProducer.dep(config, default=dr_default),
        llm=hierarchy_llm,
        hierarchy_composer=optional_hierarchy_composer(
            config, index_builder=hierarchy_index, llm=hierarchy_llm,
        ),
        layer_annotator=_opt_annotator(),
        router=optional_router(config),
        appraiser=_opt_appraiser(config),
        prompt_registry=registry,
        dedup_decisions=_parse_dedup_decisions(config.get("dedup_decisions")),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
        recent_originals_limit=config.get("recent_originals_limit", 10),
        related_memories_top_k=config.get("related_memories_top_k", 10),
    )
