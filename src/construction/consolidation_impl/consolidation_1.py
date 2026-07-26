"""兼容巩固实现：承接原 Evolver 的四态去重与落盘逻辑。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from common.llm.base import LLM, LlmProducer
from common.log import get_logger
from common.type_def import DedupDecision, LifecycleState, MemoryUnit, Segment, memory_key
from common.type_def.chat import ChatMessage
from common.type_def.memory_codec import dumps
from construction.base import OperatorType
from construction.consolidation import Consolidator, ConsolidatorProducer
from construction.dedup import Dedup, DedupProducer
from construction.evolver import EvolveResult
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.kv import KvProducer, KVStore

logger = get_logger(__name__)

_DECISION_PROMPT = """You are a memory consolidation assistant.
For each candidate decide whether it is new information, enriches an existing memory,
replaces an existing memory, or is fully duplicated.
Output ONLY a JSON array with one object per candidate:
[{"candidate_id":"...", "decision":"add|update|supersede|noop"}]
If unsure, use "add". Judge each candidate only against its listed existing memories."""

_MERGE_PROMPT = """Merge the old and new memory into one concise, self-contained statement.
Preserve all non-conflicting details and prefer the new value on conflict.
Output only merged plain text."""


class Consolidation1(Consolidator):
    """相似度短路 + LLM 四态判定的兼容实现。"""

    def __init__(
        self,
        dedup: Dedup,
        index_builder: IndexBuilder,
        kv: KVStore,
        llm: LLM,
        *,
        medium_similarity: float = 0.7,
        high_similarity: float = 0.9,
    ) -> None:
        self._dedup = dedup
        self._index = index_builder
        self._kv = kv
        self._llm = llm
        self._medium_similarity = medium_similarity
        self._high_similarity = high_similarity

    def operator_type(self) -> OperatorType:
        return OperatorType.CONSOLIDATOR

    def health(self) -> None:
        return None

    def consolidate(self, candidates: list[MemoryUnit]) -> EvolveResult:
        result = EvolveResult()
        pending: list[tuple[MemoryUnit, MemoryUnit, float, list[tuple[MemoryUnit, float]]]] = []
        direct: list[tuple[MemoryUnit, DedupDecision, MemoryUnit | None, float]] = []
        for candidate in candidates:
            try:
                hits = self._dedup.recall(candidate)
            except Exception as exc:
                logger.warning("Consolidation1 recall failed, fallback ADD: %s", exc)
                hits = []
            if not hits:
                direct.append((candidate, DedupDecision.ADD, None, 0.0))
                continue
            existing, score = max(hits, key=lambda item: item[1])
            if score < self._medium_similarity:
                direct.append((candidate, DedupDecision.ADD, existing, score))
            elif score >= self._high_similarity:
                direct.append((candidate, DedupDecision.NOOP, existing, score))
            else:
                pending.append((candidate, existing, score, hits))

        decisions = self._decide_batch([(candidate, hits) for candidate, _, _, hits in pending])
        for candidate, decision, existing, score in direct:
            self.apply_decision(candidate, decision, existing, score, result)
        for candidate, existing, score, _hits in pending:
            self.apply_decision(
                candidate,
                decisions.get(candidate.id, DedupDecision.ADD),
                existing,
                score,
                result,
            )
        return result

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        """供策略化实现复用同一召回实例。"""
        try:
            return self._dedup.recall(candidate)
        except Exception as exc:
            logger.warning("Consolidation recall failed for %s: %s", candidate.id, exc)
            return []

    def apply_decision(
        self,
        candidate: MemoryUnit,
        decision: DedupDecision,
        existing: MemoryUnit | None,
        similarity: float,
        result: EvolveResult,
    ) -> None:
        """执行安全落盘原语；缺少 existing 的修改决策降级为 ADD。"""
        if decision == DedupDecision.ADD or (
            decision in {DedupDecision.UPDATE, DedupDecision.SUPERSEDE} and existing is None
        ):
            self._kv.insert(candidate.scope, memory_key(candidate.id), dumps(candidate))
            self._index.build([candidate])
            result.created_ids.append(candidate.id)
            return
        if decision == DedupDecision.NOOP:
            return
        if decision == DedupDecision.UPDATE:
            assert existing is not None
            merged = self._merge_content(existing, candidate)
            if existing.segments:
                existing.segments[0].content = merged
            else:
                existing.segments = [Segment(content=merged)]
            existing.provenance = list(
                set(existing.provenance) | set(candidate.provenance) | {candidate.id}
            )
            existing.metadata.update(candidate.metadata)
            existing.metadata.update(
                {
                    "dedup_decision": "update",
                    "dedup_similarity": str(similarity),
                    "dedup_merged_from": candidate.id,
                }
            )
            self._kv.update(existing.scope, memory_key(existing.id), dumps(existing))
            self._index.update([existing])
            result.updated_ids.append(existing.id)
            return
        assert existing is not None
        candidate.supersedes = existing.id
        candidate.provenance = list(set(candidate.provenance) | {existing.id})
        candidate.metadata.update(
            {
                "dedup_decision": "supersede",
                "dedup_similarity": str(similarity),
                "dedup_superseded": existing.id,
            }
        )
        self._kv.insert(candidate.scope, memory_key(candidate.id), dumps(candidate))
        self._index.build([candidate])
        existing.lifecycle = LifecycleState.SUPERSEDED
        existing.temporal.t_invalid = datetime.now(timezone.utc)
        self._kv.update(existing.scope, memory_key(existing.id), dumps(existing))
        self._index.update([existing])
        result.created_ids.append(candidate.id)
        result.superseded_ids.append(existing.id)

    def _decide_batch(
        self,
        items: list[tuple[MemoryUnit, list[tuple[MemoryUnit, float]]]],
    ) -> dict[str, DedupDecision]:
        if not items:
            return {}
        blocks: list[str] = []
        for candidate, hits in items:
            block = f"[{candidate.id}]: {candidate.content}"
            for existing, score in hits[:3]:
                block += f"\n  e|{existing.id}|{score:.3f}: {existing.content}"
            blocks.append(block)
        messages = [
            ChatMessage(role="system", content=_DECISION_PROMPT),
            ChatMessage(role="user", content="\n---\n".join(blocks)),
        ]
        try:
            response = self._llm.chat(messages, temperature=0, max_tokens=1024)
            parsed = self._parse_json_array(response)
        except Exception as exc:
            logger.warning("Consolidation1 decision failed, fallback ADD: %s", exc)
            return {}
        decisions: dict[str, DedupDecision] = {}
        for item in parsed:
            candidate_id = str(item.get("candidate_id", ""))
            try:
                decisions[candidate_id] = DedupDecision(str(item.get("decision", "add")).lower())
            except ValueError:
                decisions[candidate_id] = DedupDecision.ADD
        return decisions

    @staticmethod
    def _parse_json_array(response: str) -> list[dict]:
        try:
            parsed = json.loads(response.strip())
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", response, re.DOTALL)
            parsed = json.loads(match.group()) if match else []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def _merge_content(self, old: MemoryUnit, new: MemoryUnit) -> str:
        messages = [
            ChatMessage(role="system", content=_MERGE_PROMPT),
            ChatMessage(role="user", content=f"Old:\n{old.content}\n\nNew:\n{new.content}"),
        ]
        try:
            return self._llm.chat(messages, temperature=0, max_tokens=512).strip()
        except Exception:
            return f"{old.content}\n{new.content}"


@ConsolidatorProducer.register("consolidation_1")
def _build(config):
    vector_on = config.get("vector_enabled", True)
    return Consolidation1(
        dedup=DedupProducer.dep(config, default="vector" if vector_on else "keyword"),
        index_builder=IndexBuilderProducer.dep(
            config, "index_builder", default="hybrid" if vector_on else "fulltext"
        ),
        kv=KvProducer.dep(config, default="memory"),
        llm=LlmProducer.dep(config, default="echo"),
        medium_similarity=config.get("dedup_medium_similarity", 0.7),
        high_similarity=config.get("dedup_high_similarity", 0.9),
    )
