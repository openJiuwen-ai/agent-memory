"""metadata 驱动的动态 LLM Extractor；无 prompt 时委托旧实现。

metadata 只写 prompt 的 **key**（引用 yml ``prompts.extract`` 段的命名 prompt），运行时
由 :class:`~construction.prompt_registry.PromptRegistry` 按 key 查真实文本发给 LLM。
子类覆盖 :meth:`parse_response` 解析 JSON、XML 等不同响应格式，对下游仍统一返回
``list[MemoryUnit]``。
"""

from __future__ import annotations

from datetime import datetime, timezone

from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import MemoryUnit
from common.type_def.chat import ChatMessage
from construction.base import ExtractContext, OperatorType
from construction.common import parse_tags
from construction.extractor import Extractor, ExtractorProducer
from construction.prompt_registry import PHASE_EXTRACT, PromptRegistry
from construction.prompt_strategy import (
    EXTRACT_PROMPT_PREFIX,
    EXTRACTION_STRATEGY_KEY,
    copy_consolidation_prompts,
    parse_prompt_strategies,
)

from .llm_extractor import (
    _SOURCE_PREFIX,
    ExtractionCandidate,
    ExtractionTarget,
    ExtractorImpl,
    _format_context_block,
    _parse_tier,
    _strip_source_id_shell,
)

logger = get_logger(__name__)


class DynamicLLMExtractor(Extractor):
    """每个 ``_extract_prompt_<strategy>`` 执行一次对应抽取。

    metadata 中 ``_extract_prompt_<strategy>`` 的值是 prompt 的 **key**（引用 yml
    ``prompts.extract`` 段）；运行时按 key 查 :class:`PromptRegistry` 取真实文本作为
    system prompt 发给 LLM。registry 未配置或 key 缺失时回退把值本身当文本用
    （兼容内联文本）。子类覆盖 :meth:`parse_response` 解析 JSON、XML 等不同响应格式，
    对下游仍统一返回 ``list[MemoryUnit]``。
    """

    def __init__(
        self,
        llm: LLM,
        fallback: Extractor,
        *,
        prompt_registry: PromptRegistry | None = None,
        min_confidence: float = 0.5,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        self._llm = llm
        self._fallback = fallback
        self._prompts = prompt_registry or PromptRegistry()
        self._helper = ExtractorImpl(
            llm=llm,
            min_confidence=min_confidence,
            retry_max_retries=retry_max_retries,
            retry_backoff_ms=retry_backoff_ms,
        )
        self._min_confidence = min_confidence

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        self._llm.health()
        self._fallback.health()

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        prompts: list[tuple[str, str]] = []
        for unit in units:
            prompts.extend(parse_prompt_strategies(unit.metadata, EXTRACT_PROMPT_PREFIX))
        if not prompts:
            extracted = self._fallback.extract(units, context=context)
            copy_consolidation_prompts(units, extracted)
            return extracted

        accepted = self._helper.preprocess(units)
        if not accepted:
            return []
        result: list[MemoryUnit] = []
        seen: set[str] = set()
        for strategy, prompt_key in prompts:
            if strategy in seen:
                continue
            seen.add(strategy)
            try:
                built = self._extract_strategy(accepted, context, strategy, prompt_key)
            except Exception as exc:
                logger.warning("Dynamic extractor strategy=%s failed: %s", strategy, exc)
                continue
            for unit in built:
                unit.metadata[EXTRACTION_STRATEGY_KEY] = strategy
            copy_consolidation_prompts(units, built)
            result.extend(built)
        return result

    def _extract_strategy(
        self,
        units: list[MemoryUnit],
        context: ExtractContext | None,
        strategy: str,
        prompt_key: str,
    ) -> list[MemoryUnit]:
        # prompt_key 是引用 yml prompts.extract 段的 key；查 registry 取真实文本。
        # registry 未配置或 key 缺失时回退把 key 本身当文本用（兼容内联文本）。
        prompt = self._prompts.get(PHASE_EXTRACT, prompt_key)
        if prompt is None:
            prompt = prompt_key
        observation_date = next(
            (
                str(unit.metadata.get("observation_date", "")).strip()
                for unit in units
                if unit.metadata.get("observation_date")
            ),
            datetime.now(timezone.utc).isoformat(),
        )
        source_text = "\n".join(
            _SOURCE_PREFIX.format(unit_id=unit.id, unit_content=unit.content) for unit in units
        )
        context_block = _format_context_block(context)
        user_text = (
            f"strategy: {strategy}\nobservation_date: {observation_date}\n\n{source_text}"
            + (f"\n{context_block}" if context_block else "")
        )
        response = self._helper.call_llm_with_retry(
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content=user_text),
            ]
        )
        return self.parse_response(response, units, strategy)

    def parse_response(
        self,
        response: str,
        sources: list[MemoryUnit],
        strategy: str,
    ) -> list[MemoryUnit]:
        """解析一次策略响应并转换为下游统一消费的 MemoryUnit 列表。

        默认实现按 JSON 解析。metadata prompt 负责约束响应格式；子类可解析任意内部
        结构，但必须在本方法边界内完成到 ``list[MemoryUnit]`` 的转换。
        """
        candidates = self._parse_json_candidates(response, sources, strategy)
        return self._helper.build_units(candidates, sources)

    def _parse_json_candidates(
        self,
        response: str,
        sources: list[MemoryUnit],
        strategy: str,
    ) -> list[ExtractionCandidate]:
        unit_map = {unit.id: unit for unit in sources}
        candidates: list[ExtractionCandidate] = []
        for item in self._helper.parse_llm_response(response):
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            source_id = _strip_source_id_shell(str(item.get("source_id", "")))
            content = str(item.get("content", "")).strip()
            if confidence < self._min_confidence or source_id not in unit_map or not content:
                continue
            try:
                target = ExtractionTarget(str(item.get("target", "fact")).lower())
            except ValueError:
                target = ExtractionTarget.FACT
            candidates.append(
                ExtractionCandidate(
                    target=target,
                    content=content,
                    source_unit_id=source_id,
                    confidence=confidence,
                    evidence=str(item.get("evidence", "")),
                    event_date=str(item.get("event_date", "") or ""),
                    tier=_parse_tier(item.get("tier")),
                    tags=parse_tags(item.get("tags")),
                    metadata={EXTRACTION_STRATEGY_KEY: strategy},
                )
            )
        return candidates


@ExtractorProducer.register("dynamic_llm")
def _build(config):
    prompts_data = config.get("prompts")
    registry = (
        PromptRegistry.from_dict(prompts_data) if prompts_data else PromptRegistry()
    )
    return DynamicLLMExtractor(
        llm=LlmProducer.dep(config, default="echo"),
        fallback=ExtractorProducer.dep(config, "fallback", default="keyword"),
        prompt_registry=registry,
        min_confidence=config.get("extractor_min_confidence", 0.5),
        retry_max_retries=config.get("extractor_retry_max", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
    )
