"""策略化巩固：按 write metadata 动态 prompt 判定落盘动作。"""

from __future__ import annotations

import json
import re

from common.llm.base import LlmProducer
from common.log import get_logger
from common.type_def import DedupDecision, MemoryUnit
from common.type_def.chat import ChatMessage
from construction.consolidation import ConsolidatorProducer
from construction.dedup import DedupProducer
from construction.evolver import EvolveResult
from construction.index_builder import IndexBuilderProducer
from construction.prompt_strategy import (
    CONSOLIDATION_PROMPT_PREFIX,
    EXTRACTION_STRATEGY_KEY,
    parse_prompt_strategies,
)
from storage.kv import KvProducer

from .consolidation_1 import Consolidation1

logger = get_logger(__name__)

_OUTPUT_CONTRACT = """

Mandatory output contract:
Output ONLY one JSON object, without markdown:
{"decision":"add|update|supersede|noop","existing_id":"id or empty","reason":"brief"}
Use only an existing_id listed in the input. UPDATE/SUPERSEDE require an existing_id.
If unsure, return {"decision":"add","existing_id":"","reason":"uncertain"}.
"""


class Consolidation2(Consolidation1):
    """使用调用级 prompt；缺失或失败时回退 consolidation_1。"""

    def consolidate(self, candidates: list[MemoryUnit]) -> EvolveResult:
        result = EvolveResult()
        for candidate in candidates:
            selected = self._select_prompt(candidate)
            if selected is None:
                self._merge_result(result, super().consolidate([candidate]))
                continue
            strategy, prompt = selected
            hits = self.recall(candidate)
            try:
                decision, existing, similarity = self._strategy_decision(
                    candidate, hits, strategy, prompt
                )
            except Exception as exc:
                logger.warning(
                    "Consolidation2 strategy=%s failed for %s, fallback consolidation_1: %s",
                    strategy,
                    candidate.id,
                    exc,
                )
                self._merge_result(result, super().consolidate([candidate]))
                continue
            candidate.metadata["_consolidation_strategy"] = strategy
            self.apply_decision(candidate, decision, existing, similarity, result)
        return result

    @staticmethod
    def _select_prompt(candidate: MemoryUnit) -> tuple[str, str] | None:
        prompts = parse_prompt_strategies(candidate.metadata, CONSOLIDATION_PROMPT_PREFIX)
        if not prompts:
            return None
        extraction_strategy = candidate.metadata.get(EXTRACTION_STRATEGY_KEY, "").strip()
        if extraction_strategy:
            for strategy, prompt in prompts:
                if strategy == extraction_strategy:
                    return strategy, prompt
        return prompts[0]

    def _strategy_decision(
        self,
        candidate: MemoryUnit,
        hits: list[tuple[MemoryUnit, float]],
        strategy: str,
        prompt: str,
    ) -> tuple[DedupDecision, MemoryUnit | None, float]:
        hit_map = {unit.id: (unit, score) for unit, score in hits[:5]}
        existing_text = "\n\n".join(
            f"[Memory ID: {unit.id}]\nContent: {unit.content}\n"
            f"Tier: {unit.tier.value}\nSimilarity: {score:.3f}"
            for unit, score in hits[:5]
        )
        messages = [
            ChatMessage(role="system", content=prompt + _OUTPUT_CONTRACT),
            ChatMessage(
                role="user",
                content=(
                    f"Strategy: {strategy}\nCandidate ID: {candidate.id}\n"
                    f"Candidate content: {candidate.content}\n\n"
                    f"Existing memories:\n{existing_text or '(none)'}"
                ),
            ),
        ]
        response = self._llm.chat(messages, temperature=0, max_tokens=512)
        payload = self._parse_object(response)
        decision = DedupDecision(str(payload.get("decision", "add")).strip().lower())
        existing_id = str(payload.get("existing_id", "")).strip()
        existing, score = hit_map.get(existing_id, (None, 0.0))
        if decision in {DedupDecision.UPDATE, DedupDecision.SUPERSEDE} and existing is None:
            raise ValueError("修改型决策必须引用本次召回中的 existing_id")
        return decision, existing, score

    @staticmethod
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

    @staticmethod
    def _merge_result(target: EvolveResult, source: EvolveResult) -> None:
        target.created_ids.extend(source.created_ids)
        target.updated_ids.extend(source.updated_ids)
        target.superseded_ids.extend(source.superseded_ids)
        target.forgotten_ids.extend(source.forgotten_ids)


@ConsolidatorProducer.register("consolidation_2")
def _build(config):
    vector_on = config.get("vector_enabled", True)
    return Consolidation2(
        dedup=DedupProducer.dep(config, default="vector" if vector_on else "keyword"),
        index_builder=IndexBuilderProducer.dep(
            config, "index_builder", default="hybrid" if vector_on else "fulltext"
        ),
        kv=KvProducer.dep(config, default="memory"),
        llm=LlmProducer.dep(config, default="echo"),
        medium_similarity=config.get("dedup_medium_similarity", 0.7),
        high_similarity=config.get("dedup_high_similarity", 0.9),
    )
