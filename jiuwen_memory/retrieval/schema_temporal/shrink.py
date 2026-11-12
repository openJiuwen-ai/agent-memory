# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Budgeted, query-aware shrinking of entity property timelines."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from jiuwen_memory.common.plaintext import PlaintextDataError
from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import ParsedQuery

from .model import (
    SchemaPropertyEntry,
    SchemaShrinkDiagnostics,
    SchemaTemporalResult,
    TemporalEntity,
)


class PropertyRecallHit(Protocol):
    """Minimal structural type accepted from a property-local recall path."""

    unit_id: str
    score: float


EntityLocalRecall = Callable[[list[str]], dict[str, list[PropertyRecallHit]]]


class SchemaTemporalShrinker:
    """Apply a bounded, query-aware budget only to hydrated entity timelines."""

    def __init__(
        self,
        *,
        max_properties_per_entity: int = 20,
        allocation_min_factor: float = 0.5,
        allocation_max_factor: float = 1.5,
        reranker: Reranker | None = None,
        rerank_enabled: bool = False,
    ) -> None:
        self._max_per_entity = max(1, int(max_properties_per_entity))
        self._allocation_min_factor = max(0.0, float(allocation_min_factor))
        self._allocation_max_factor = max(
            self._allocation_min_factor,
            float(allocation_max_factor),
        )
        self._reranker = reranker
        self._rerank_enabled = bool(rerank_enabled)

    def shrink(
        self,
        result: SchemaTemporalResult,
        query: ParsedQuery,
        *,
        entity_local_recall: EntityLocalRecall | None = None,
    ) -> SchemaTemporalResult:
        if not result.entities:
            result.shrink_diagnostics = SchemaShrinkDiagnostics()
            return result
        terms = _query_terms(query)
        query_text = query.rewritten or query.raw
        entry_counts = [_entry_count(entity) for entity in result.entities]
        entries_before = sum(entry_counts)
        budgets = self._allocate_budgets(entry_counts)
        entities_reranked = 0
        degraded_errors: set[str] = set()

        pending: list[
            tuple[TemporalEntity, int, int, list[tuple[str, SchemaPropertyEntry]]]
        ] = []
        for entity, original_count, allocated in zip(
            result.entities,
            entry_counts,
            budgets,
            strict=True,
        ):
            budget = min(allocated, original_count)
            if budget <= 0:
                entity.properties = {}
                result.selected.pop(entity.entity_id, None)
                continue
            if original_count <= budget:
                result.selected[entity.entity_id] = entity.selected_payload(result.mode)
                continue
            pending.append((entity, original_count, budget, _flatten(entity)))

        recalled_hits: dict[str, list[PropertyRecallHit]] = {}
        if pending and entity_local_recall is not None:
            try:
                recalled_hits = entity_local_recall(
                    [item[0].entity_id for item in pending],
                )
            except PlaintextDataError:
                raise
            except Exception as exc:
                degraded_errors.add(type(exc).__name__)
                recalled_hits = {}

        prepared: list[
            tuple[
                TemporalEntity,
                int,
                int,
                list[tuple[str, SchemaPropertyEntry]],
                dict[str, float],
            ]
        ] = []
        for entity, original_count, budget, entries in pending:
            local_scores: dict[str, float] = {}
            for hit in recalled_hits.get(entity.entity_id, []):
                local_scores[hit.unit_id] = hit.score
            locally_recalled = [item for item in entries if item[1].unit_id in local_scores]
            if locally_recalled:
                entries = locally_recalled
            prepared.append((entity, original_count, budget, entries, local_scores))

        # Rerank：批量优先（1 次调用覆盖全部超预算实体），仅当批量失败时退化为
        # 逐实体重试——单实体失败只降级该实体，避免一条文本超限拖垮全部。批量成功
        # 时把扁平分数按 span 切回各实体；远程 reranker 下把 N 次 RTT 压回 1 次。
        batch_scores: list[list[float] | None] = [None] * len(prepared)
        degraded_per_entity: list[bool] = [False] * len(prepared)
        if prepared and self._rerank_enabled and self._reranker is not None:
            spans: list[tuple[int, int]] = []
            flat_texts: list[str] = []
            for prepared_item in prepared:
                entries = prepared_item[3]
                start = len(flat_texts)
                flat_texts.extend(f"{name}: {entry.value}" for name, entry in entries)
                spans.append((start, len(flat_texts)))
            batch_ok = False
            if flat_texts:
                try:
                    flat_scores = [
                        float(score)
                        for score in self._reranker.rerank_texts(query_text, flat_texts)
                    ]
                    if len(flat_scores) != len(flat_texts) or not all(
                        math.isfinite(score) for score in flat_scores
                    ):
                        raise ValueError("property reranker returned invalid scores")
                except PlaintextDataError:
                    raise
                except Exception as exc:
                    degraded_errors.add(type(exc).__name__)
                else:
                    for index, (start, end) in enumerate(spans):
                        batch_scores[index] = list(flat_scores[start:end])
                    batch_ok = True
            if not batch_ok:
                for index, prepared_item in enumerate(prepared):
                    entries = prepared_item[3]
                    texts = [f"{name}: {entry.value}" for name, entry in entries]
                    if not texts:
                        continue
                    try:
                        scores = [
                            float(score) for score in self._reranker.rerank_texts(query_text, texts)
                        ]
                        if len(scores) != len(texts) or not all(
                            math.isfinite(score) for score in scores
                        ):
                            raise ValueError("property reranker returned invalid scores")
                    except PlaintextDataError:
                        raise
                    except Exception as exc:
                        degraded_per_entity[index] = True
                        degraded_errors.add(type(exc).__name__)
                    else:
                        batch_scores[index] = scores

        for index, (entity, original_count, budget, entries, local_scores) in enumerate(prepared):
            ranked, reranked = _rank_entries(
                entries,
                terms=terms,
                recall_scores=local_scores,
                rerank_scores=batch_scores[index],
                degraded=degraded_per_entity[index],
            )
            entities_reranked += int(reranked)
            selected = ranked[:budget]
            properties: dict[str, list[SchemaPropertyEntry]] = {}
            for _score, _lexical, name, entry in selected:
                properties.setdefault(name, []).append(entry)
            _sort_timelines(properties)
            entity.properties = properties
            entity.truncated = entity.truncated or len(selected) < original_count
            result.selected[entity.entity_id] = entity.selected_payload(result.mode)

        result.entities = [entity for entity in result.entities if entity.properties]
        retained = {entity.entity_id for entity in result.entities}
        result.selected = {
            entity_id: payload
            for entity_id, payload in result.selected.items()
            if entity_id in retained
        }
        result.entity_scores = {
            entity_id: score
            for entity_id, score in result.entity_scores.items()
            if entity_id in retained
        }
        result.fallback_entity_ids = [
            entity_id for entity_id in result.fallback_entity_ids if entity_id in retained
        ]
        prior = result.shrink_diagnostics or SchemaShrinkDiagnostics()
        result.shrink_diagnostics = SchemaShrinkDiagnostics(
            entries_before=entries_before,
            entries_after=sum(_entry_count(entity) for entity in result.entities),
            entities_reranked=entities_reranked,
            direct_entries_retained=prior.direct_entries_retained,
            timeline_neighbors_added=prior.timeline_neighbors_added,
            degraded_errors=tuple(sorted(degraded_errors)),
        )
        return result

    def _allocate_budgets(self, entry_counts: list[int]) -> list[int]:
        if not entry_counts:
            return []
        minimum = max(1, int(self._max_per_entity * self._allocation_min_factor))
        maximum = max(minimum, int(self._max_per_entity * self._allocation_max_factor))
        total = sum(entry_counts)
        if total <= 0:
            return [self._max_per_entity for _count in entry_counts]
        total_budget = self._max_per_entity * len(entry_counts)
        budgets: list[int] = []
        for count in entry_counts:
            proportional = max(1, int(total_budget * count / total))
            budgets.append(max(minimum, min(maximum, proportional)))
        return budgets


def _query_terms(query: ParsedQuery) -> set[str]:
    values = [*query.tokens, *query.keywords]
    values.extend(re.findall(r"[\w\u4e00-\u9fff]+", query.rewritten or query.raw))
    return {value.casefold() for value in values if value.strip()}


def _flatten(entity: TemporalEntity) -> list[tuple[str, SchemaPropertyEntry]]:
    return [(name, entry) for name, timeline in entity.properties.items() for entry in timeline]


def _rank_entries(
    entries: list[tuple[str, SchemaPropertyEntry]],
    *,
    terms: set[str],
    recall_scores: dict[str, float],
    rerank_scores: list[float] | None = None,
    degraded: bool = False,
) -> tuple[list[tuple[float, float, str, SchemaPropertyEntry]], bool]:
    texts = [f"{name}: {entry.value}" for name, entry in entries]
    lexical = [_entry_lexical_score(text, terms) for text in texts]
    relevance = [
        recall_scores.get(entry.unit_id, lexical_score)
        for (_name, entry), lexical_score in zip(entries, lexical, strict=True)
    ]
    reranked = rerank_scores is not None
    if reranked:
        relevance = list(rerank_scores)
    elif degraded:
        relevance = list(lexical)
    ranked = []
    for (name, entry), score, lexical_score in zip(entries, relevance, lexical, strict=True):
        ranked.append((score, lexical_score, name, entry))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3].unit_id))
    return ranked, reranked


def _entry_lexical_score(text: str, terms: set[str]) -> float:
    haystack = text.casefold()
    return float(sum(1 for term in terms if term in haystack))


def _entry_count(entity: TemporalEntity) -> int:
    return sum(len(timeline) for timeline in entity.properties.values())


def _sort_timelines(properties: dict[str, list[SchemaPropertyEntry]]) -> None:
    epoch = datetime.min.replace(tzinfo=UTC)
    for timeline in properties.values():
        timeline.sort(
            key=lambda entry: (
                entry.event_start or entry.event_time or epoch,
                entry.unit_id,
            )
        )
