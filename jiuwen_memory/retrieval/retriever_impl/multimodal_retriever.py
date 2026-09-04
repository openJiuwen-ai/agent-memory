# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Parallel native, CLM and ELM retrieval for multimodal memory."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    Scope,
    and_merge,
)
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import (
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
    TrajectoryStep,
)

logger = get_logger(__name__)


class MultimodalRetriever(Retriever):
    """Compose the native retriever with independent CLM and ELM branches."""

    def __init__(
        self,
        base_retriever: Retriever,
        *,
        clip_top_k: int = 10,
        event_top_k: int = 10,
        rrf_k: int = 60,
    ) -> None:
        if clip_top_k <= 0 or event_top_k <= 0:
            raise ValidationError("clip_top_k and event_top_k must be greater than zero")
        if rrf_k <= 0:
            raise ValidationError("rrf_k must be greater than zero")
        self._base = base_retriever
        self._clip_top_k = clip_top_k
        self._event_top_k = event_top_k
        self._rrf_k = rrf_k

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        self._base.health()

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        queries = {
            "native": _with_filters(
                query,
                FilterClause("source", FilterOp.NE, "video"),
                top_k=query.top_k,
            ),
            "multimodal_clip": _with_filters(
                query,
                FilterClause("system_metadata.modal_type", FilterOp.EQ, "multimodal"),
                FilterClause("system_metadata.memory_level", FilterOp.EQ, "clm"),
                top_k=self._clip_top_k,
            ),
            "multimodal_event": _with_filters(
                query,
                FilterClause("system_metadata.modal_type", FilterOp.EQ, "multimodal"),
                FilterClause("system_metadata.memory_level", FilterOp.EQ, "elm"),
                top_k=self._event_top_k,
            ),
        }
        results: dict[str, RetrievalResult] = {}
        degraded: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            futures = {
                executor.submit(self._base.retrieve, scope, branch_query): branch
                for branch, branch_query in queries.items()
            }
            for future in as_completed(futures):
                branch = futures[future]
                try:
                    results[branch] = future.result()
                except Exception as exc:
                    logger.warning(
                        "MultimodalRetriever branch %s failed: %s",
                        branch,
                        exc,
                    )
                    results[branch] = RetrievalResult()
                    degraded[branch] = type(exc).__name__

        branch_order = ("native", "multimodal_clip", "multimodal_event")
        items = _rrf_merge(
            [results[branch].items for branch in branch_order],
            top_k=query.top_k,
            rrf_k=self._rrf_k,
        )
        trajectory: list[TrajectoryStep] = []
        if query.with_trajectory:
            for branch in branch_order:
                trajectory.extend(_branch_trajectory(results[branch].trajectory, branch))
                if branch in degraded:
                    trajectory.append(
                        TrajectoryStep(
                            stage="recall",
                            candidate_count=0,
                            detail={
                                "branch": branch,
                                "degraded": degraded[branch],
                            },
                        )
                    )
            trajectory.append(
                TrajectoryStep(
                    stage="fuse",
                    candidate_count=len(items),
                    detail={
                        "strategy": "rrf",
                        "branches": ",".join(branch_order),
                        "rrf_k": str(self._rrf_k),
                    },
                )
            )
        return RetrievalResult(items=items, trajectory=trajectory)


def _with_filters(
    query: RetrievalQuery,
    *filters: FilterClause,
    top_k: int,
) -> RetrievalQuery:
    return replace(
        query,
        filters=and_merge(query.filters, list(filters)),
        top_k=top_k,
    )


def _rrf_merge(
    branches: list[list[RetrievedItem]],
    *,
    top_k: int,
    rrf_k: int,
) -> list[RetrievedItem]:
    by_id: dict[str, RetrievedItem] = {}
    scores: dict[str, float] = {}
    for branch in branches:
        for rank, item in enumerate(branch, start=1):
            by_id.setdefault(item.unit_id, item)
            scores[item.unit_id] = scores.get(item.unit_id, 0.0) + 1.0 / (rrf_k + rank)
    ranked_ids = sorted(scores, key=scores.get, reverse=True)[:top_k]
    merged: list[RetrievedItem] = []
    for unit_id in ranked_ids:
        item = by_id.get(unit_id)
        if item is not None:
            merged.append(replace(item, score=scores.get(unit_id, 0.0)))
    return merged


def _branch_trajectory(
    steps: list[TrajectoryStep],
    branch: str,
) -> list[TrajectoryStep]:
    return [
        replace(step, detail={**step.detail, "branch": branch})
        for step in steps
    ]


@RetrieverProducer.register("multimodal")
def _build(config):
    return MultimodalRetriever(
        RetrieverProducer.dep(config, "base_retriever", default="pipeline"),
        clip_top_k=int(config.get("clip_top_k", 10)),
        event_top_k=int(config.get("event_top_k", 10)),
        rrf_k=int(config.get("rrf_k", 60)),
    )
