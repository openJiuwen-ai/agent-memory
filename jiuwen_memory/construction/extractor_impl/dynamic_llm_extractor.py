"""metadata 驱动的动态 LLM Extractor；无 prompt 时委托旧实现。

metadata 只写 prompt 的 **key**（引用 yml ``prompts.extract`` 段的命名 prompt），运行时
由 :class:`~construction.prompt_registry.PromptRegistry` 按 key 查真实文本发给 LLM。
子类覆盖 :meth:`parse_response` 解析 JSON、XML 等不同响应格式，对下游仍统一返回
``list[MemoryUnit]``。
"""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer
from jiuwen_memory.construction.prompt_registry import PHASE_EXTRACT, PromptRegistry
from jiuwen_memory.construction.prompt_strategy import (
    EXTRACT_PROMPT_PREFIX,
    EXTRACTION_STRATEGY_KEY,
    copy_consolidation_prompts,
    parse_prompt_strategies,
)

from .llm_extractor import (
    _SOURCE_PREFIX,
    ExtractionCandidate,
    ExtractorImpl,
    _format_context_block,
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

    def operator_type(self) -> OperatorType:
        """返回算子类型 ``EXTRACTOR``。"""
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        """探活：检查 LLM 与 fallback Extractor。"""
        self._llm.health()
        self._fallback.health()

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        """按 metadata 中的 prompt 策略抽取；无策略时委托 fallback。"""
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
        successful_strategies = 0
        last_error: Exception | None = None
        for strategy, prompt_key in prompts:
            if strategy in seen:
                continue
            seen.add(strategy)
            try:
                built = self._extract_strategy(accepted, context, strategy, prompt_key)
            except Exception as exc:
                logger.warning("Dynamic extractor strategy=%s failed: %s", strategy, exc)
                last_error = exc
                continue
            successful_strategies += 1
            for unit in built:
                unit.metadata[EXTRACTION_STRATEGY_KEY] = strategy
            copy_consolidation_prompts(units, built)
            result.extend(built)
        if successful_strategies == 0 and last_error is not None:
            raise last_error
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
        items = self._helper.parse_llm_response(response)
        candidates = self._helper.build_candidates(items, sources)
        for candidate in candidates:
            candidate.metadata[EXTRACTION_STRATEGY_KEY] = strategy
        return candidates


@ExtractorProducer.register("dynamic_llm")
def _build(config):
    """装配 DynamicLLMExtractor；PromptRegistry 挂接共享 ConfigSource 以支持 prompt 晚绑定。"""
    prompts_data = config.get("prompts")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )
    return DynamicLLMExtractor(
        llm=LlmProducer.dep(config, default="echo"),
        fallback=ExtractorProducer.dep(config, "fallback", default="keyword"),
        prompt_registry=registry,
        min_confidence=config.get("extractor_min_confidence", 0.5),
        retry_max_retries=config.get("extractor_retry_max", 3),
        retry_backoff_ms=config.get("extractor_retry_backoff", 1000),
    )
