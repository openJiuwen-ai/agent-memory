# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in adapter from the ordinary pipeline to TemporalEntity results."""

from __future__ import annotations

import math
from dataclasses import replace
from time import perf_counter
from typing import Callable

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import safe_error_message
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import (
    ChannelError,
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    RecallChannel,
    Scope,
    ScoredMemoryUnit,
)
from jiuwen_memory.retrieval.discloser import Discloser
from jiuwen_memory.retrieval.types import (
    DisclosureLevel,
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
)
from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.store_manager import StoreManager

from .formatter import SchemaTemporalFormatter
from .model import SchemaTemporalResult
from .selector import TemporalEntitySelector

logger = get_logger(__name__)

RecordStep = Callable[..., None]


class SchemaTemporalPipelineExtension:
    """Run schema-temporal retrieval after the ordinary public result exists.

    The ordinary pipeline remains authoritative for parsing, recall, fusion,
    reranking, thresholding, top-k and disclosure.  Its final candidates seed
    the Entity path; the independent Property path therefore never consumes
    the ordinary candidate budget.
    """

    def __init__(
        self,
        selector: TemporalEntitySelector,
        discloser: Discloser,
        *,
        formatting_enabled: bool = True,
        formatting_max_chars: int = 16000,
        source_max_chars: int = 1200,
    ) -> None:
        self._selector = selector
        self._discloser = discloser
        self._formatter = SchemaTemporalFormatter()
        self._formatting_enabled = bool(formatting_enabled)
        self._formatting_max_chars = max(1, int(formatting_max_chars))
        self._source_max_chars = max(1, int(source_max_chars))

    def apply(
        self,
        scope: Scope,
        query: RetrievalQuery,
        parsed: ParsedQuery,
        ordinary_candidates: list[ScoredMemoryUnit],
        ordinary_units: dict[str, MemoryUnit],
        ordinary_result: RetrievalResult,
        *,
        user_filters: FilterExpr | None,
        record_step: RecordStep,
    ) -> RetrievalResult:
        """Replace the public result only when the temporal branch is selected."""

        if query.schema_temporal is not None:
            parsed.extensions["schema_temporal"] = query.schema_temporal
        if not self._selector.should_select(parsed):
            return ordinary_result

        started_at = perf_counter()
        try:
            self._selector.select(
                scope,
                parsed,
                ordinary_candidates,
                filters=user_filters,
                limit=query.top_k,
            )
            errors = [*ordinary_result.errors, *self._selector.recall_errors]
            temporal = self._selector.last_result
            record_step(
                "schema_temporal",
                started_at,
                n=(len(temporal.entities) if temporal else 0),
                channel=RecallChannel.TEMPORAL,
            )
        except Exception as exc:
            error = ChannelError(
                RecallChannel.TEMPORAL,
                "TemporalEntitySelector",
                type(exc).__name__,
                safe_error_message(exc),
            )
            logger.warning(
                "Schema temporal retrieval degraded: error_type=%s error=%s",
                error.error_type,
                error.message,
            )
            return replace(ordinary_result, errors=[*ordinary_result.errors, error])

        if temporal is None or not self._formatting_enabled:
            return replace(ordinary_result, errors=errors, schema_temporal=temporal)

        projected = _project_schema_temporal_items(
            temporal,
            formatter=self._formatter,
            limit=query.top_k,
            max_chars=self._formatting_max_chars,
        )
        source_candidates = self._selector.last_source_candidates
        source_items = _disclose_source_candidates(
            self._discloser,
            parsed,
            source_candidates,
            query,
            max_chars=self._source_max_chars,
        )
        if projected:
            output_items = _merge_temporal_and_source_items(
                projected,
                source_items,
                limit=query.top_k,
                source_top_k=self._selector.source_top_k,
                source_ratio=self._selector.source_ratio,
            )
        elif source_items:
            output_items = source_items[: query.top_k]
        else:
            output_items = ordinary_result.items
        record_step(
            "schema_project",
            perf_counter(),
            n=len(output_items),
            channel=RecallChannel.TEMPORAL,
            detail={
                "projected": str(bool(projected)).lower(),
                "source_fallbacks": str(len(source_items)),
            },
        )
        return RetrievalResult(
            items=output_items,
            trajectory=ordinary_result.trajectory,
            errors=errors,
            schema_temporal=temporal,
        )


def build_schema_temporal_extension(
    config,
    manager: StoreManager,
    domain: DomainStore,
    reranker: Reranker | None,
    discloser: Discloser,
) -> SchemaTemporalPipelineExtension | None:
    """Build the extension only when its global opt-in switch is enabled."""

    enabled = as_bool(
        Factory.cfg_get(config, "schema_temporal_enabled"),
        default=False,
    )
    if not enabled:
        return None
    kv_name = str(Factory.cfg_get(config, "kv_store", "default"))
    kv = manager.kv(kv_name) if manager.has_kv(kv_name) else None
    property_index = SchemaPropertyIndex(kv) if kv is not None else None
    selector = TemporalEntitySelector(
        domain,
        auto_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_auto_enabled"),
            default=False,
        ),
        property_id_lookup=(property_index.lookup if property_index is not None else None),
        entity_fulltext_store=(
            manager.fulltext("schema_entities")
            if manager.has_fulltext("schema_entities")
            else None
        ),
        entity_vector_store=(
            manager.vector("schema_entities")
            if manager.has_vector("schema_entities")
            else None
        ),
        entity_top_k=int(Factory.cfg_get(config, "schema_temporal_entity_top_k", 20)),
        property_top_k=int(Factory.cfg_get(config, "schema_temporal_property_top_k", 50)),
        property_top_n=int(Factory.cfg_get(config, "schema_temporal_property_top_n", 25)),
        rrf_k=int(Factory.cfg_get(config, "schema_temporal_rrf_k", 60)),
        max_properties_per_entity=int(
            Factory.cfg_get(config, "schema_temporal_max_properties_per_entity", 20)
        ),
        property_allocation_min_factor=float(
            Factory.cfg_get(
                config,
                "schema_temporal_property_allocation_min_factor",
                0.5,
            )
        ),
        property_allocation_max_factor=float(
            Factory.cfg_get(
                config,
                "schema_temporal_property_allocation_max_factor",
                1.5,
            )
        ),
        property_extension_step=int(
            Factory.cfg_get(config, "schema_temporal_property_extension_step", 3)
        ),
        property_reranker=reranker,
        property_rerank_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_property_rerank_enabled"),
            default=True,
        ),
        shrink_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_shrink_enabled"),
            default=True,
        ),
        direct_property_rerank_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_direct_property_rerank_enabled"),
            default=False,
        ),
        entity_rerank_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_entity_rerank_enabled"),
            default=False,
        ),
        entity_rerank_max_chars=int(
            Factory.cfg_get(config, "schema_temporal_entity_rerank_max_chars", 4000)
        ),
        source_fallback_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_source_fallback_enabled"),
            default=True,
        ),
        source_policy=str(
            Factory.cfg_get(
                config,
                "schema_temporal_source_policy",
                "missing_or_incomplete",
            )
        ),
        source_top_k=int(Factory.cfg_get(config, "schema_temporal_source_top_k", 20)),
        source_ratio=float(Factory.cfg_get(config, "schema_temporal_source_ratio", 0.3)),
    )
    return SchemaTemporalPipelineExtension(
        selector,
        discloser,
        formatting_enabled=as_bool(
            Factory.cfg_get(config, "schema_temporal_formatting_enabled"),
            default=True,
        ),
        formatting_max_chars=int(
            Factory.cfg_get(config, "schema_temporal_formatting_max_chars", 16000)
        ),
        source_max_chars=int(
            Factory.cfg_get(config, "schema_temporal_source_max_chars", 1200)
        ),
    )


def _project_schema_temporal_items(
    temporal: SchemaTemporalResult,
    *,
    formatter: SchemaTemporalFormatter,
    limit: int,
    max_chars: int,
) -> list[RetrievedItem]:
    items: list[RetrievedItem] = []
    blocks: list[str] = []
    for entity in temporal.entities[: max(1, int(limit))]:
        content = formatter.format_entity(entity)[:max_chars]
        blocks.append(content)
        label = entity.name or entity.entity_id
        if entity.entity_type:
            label = f"{label} ({entity.entity_type})"
        metadata = {
            "retrieval_view": "schema_temporal_entity",
            "schema_entity_id": entity.entity_id,
        }
        if entity.entity_type:
            metadata["schema_entity_type"] = entity.entity_type
        items.append(
            RetrievedItem(
                unit_id=entity.entity_id,
                score=float(temporal.entity_scores.get(entity.entity_id, 0.0)),
                abstract=label,
                overview=content,
                content=content,
                level=DisclosureLevel.L2,
                system_metadata=metadata,
            )
        )
    temporal.formatted_context = "\n\n".join(blocks)
    return items


def _disclose_source_candidates(
    discloser: Discloser,
    parsed: ParsedQuery,
    candidates: list[ScoredMemoryUnit],
    query: RetrievalQuery,
    *,
    max_chars: int,
) -> list[RetrievedItem]:
    if not candidates:
        return []
    units = {candidate.unit_id: candidate.unit for candidate in candidates}
    items = discloser.disclose(
        parsed,
        candidates,
        units,
        query.disclosure,
        max_tokens=query.max_tokens,
    )
    return [
        _format_source_item(item, units[item.unit_id], max_chars=max_chars)
        for item in items
        if item.unit_id in units
    ]


def _format_source_item(
    item: RetrievedItem,
    unit: MemoryUnit,
    *,
    max_chars: int,
) -> RetrievedItem:
    message_time = unit.temporal.t_message
    message_label = message_time.isoformat() if message_time is not None else "unknown"
    prefix = (
        "[Source evidence; "
        f"message_time={message_label}; property_event_time=not_structurally_extracted]\n"
    )
    content = f"{prefix}{item.content}"[:max_chars]
    system_metadata = dict(item.system_metadata)
    system_metadata["retrieval_view"] = "schema_source_fallback"
    return replace(
        item,
        abstract=item.abstract or "Source evidence",
        overview=content,
        content=content,
        system_metadata=system_metadata,
    )


def _merge_temporal_and_source_items(
    temporal_items: list[RetrievedItem],
    source_items: list[RetrievedItem],
    *,
    limit: int,
    source_top_k: int,
    source_ratio: float,
) -> list[RetrievedItem]:
    result_limit = max(1, int(limit))
    if not source_items or source_top_k <= 0 or source_ratio <= 0:
        return temporal_items[:result_limit]
    if not temporal_items:
        return source_items[: min(result_limit, source_top_k)]

    source_budget = min(
        len(source_items),
        max(1, int(source_top_k)),
        max(1, math.ceil(result_limit * min(1.0, source_ratio))),
    )
    if result_limit > 1:
        source_budget = min(source_budget, result_limit - 1)
    else:
        return source_items[:1]
    temporal_budget = result_limit - source_budget
    selected_temporal = temporal_items[:temporal_budget]
    selected_sources = source_items[:source_budget]
    remaining = result_limit - len(selected_temporal) - len(selected_sources)
    if remaining > 0:
        selected_temporal.extend(
            temporal_items[len(selected_temporal) : len(selected_temporal) + remaining]
        )
        remaining = result_limit - len(selected_temporal) - len(selected_sources)
    if remaining > 0:
        selected_sources.extend(
            source_items[len(selected_sources) : len(selected_sources) + remaining]
        )
    return _interleave_items(
        selected_temporal,
        selected_sources,
        limit=result_limit,
        source_ratio=source_ratio,
    )


def _interleave_items(
    primary: list[RetrievedItem],
    secondary: list[RetrievedItem],
    *,
    limit: int,
    source_ratio: float,
) -> list[RetrievedItem]:
    output: list[RetrievedItem] = []
    primary_index = 0
    secondary_index = 0
    source_credit = 0.0
    ratio = min(1.0, max(0.0, source_ratio))
    while len(output) < limit and (
        primary_index < len(primary) or secondary_index < len(secondary)
    ):
        source_credit += ratio
        use_secondary = secondary_index < len(secondary) and (
            primary_index >= len(primary) or source_credit >= 1.0
        )
        if use_secondary:
            output.append(secondary[secondary_index])
            secondary_index += 1
            source_credit = max(0.0, source_credit - 1.0)
            continue
        output.append(primary[primary_index])
        primary_index += 1
    return output
