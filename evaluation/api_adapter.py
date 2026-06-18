"""评测专用 MemoryAPI adapter。

这是 evaluation scaffold 的确定性 in-memory baseline，实现 ``EvalHarness`` 需要的
最小 ``write`` / ``recall`` 接口，并返回仓库内真实的 ``MemoryUnit`` /
``RetrievalResult`` 类型。

它不是生产运行时 adapter；后续生产 adapter 应把 ``api.MemoryAPI`` 接到真实的
control / storage / retrieval 实现。
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any, Iterable, List, Optional, Sequence

from common.type_def import FilterClause, FilterOp, MemoryUnit, Modality, Scope, Temporal
from retrieval import DisclosureLevel, RecallChannel, RetrievedItem, RetrievalResult, TrajectoryStep

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "did",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "where",
    "who",
    "with",
}


class InMemoryEvaluationAPI:
    """供 evaluation smoke test 和本地 baseline 使用的确定性小型 API。"""

    def __init__(self) -> None:
        self._units: List[MemoryUnit] = []
        self._next_id = 1

    def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        actor: Scope,
        assets: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[dict[str, str]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> List[MemoryUnit]:
        _ = actor
        unit = MemoryUnit(
            id=f"eval-{self._next_id:06d}",
            scope=_copy_scope(scope),
            content=content,
            assets=list(assets or []),
            source=source,
            temporal=Temporal(
                t_event=occurred_at,
                t_ingest=datetime.utcnow(),
                t_valid=occurred_at,
            ),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
        )
        self._next_id += 1
        self._units.append(unit)
        return [unit]

    def recall(
        self,
        query: str,
        scope: Scope,
        *,
        actor: Scope,
        filters: Optional[List[FilterClause]] = None,
        as_of: Optional[datetime] = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        _ = actor
        start = time.perf_counter()
        query_terms = _tokens(query)
        parse_ms = _elapsed_ms(start)

        recall_start = time.perf_counter()
        candidates = []
        for unit in self._units:
            if unit.scope != scope:
                continue
            if not _active_at(unit, as_of):
                continue
            if not _matches_filters(unit, filters or []):
                continue
            candidates.append(unit)
        recall_ms = _elapsed_ms(recall_start)

        fuse_start = time.perf_counter()
        scored = [
            (_score(query_terms, unit), idx, unit)
            for idx, unit in enumerate(candidates)
        ]
        scored.sort(key=lambda row: (-row[0], row[1]))
        limited = scored[: max(top_k, 0)]
        fuse_ms = _elapsed_ms(fuse_start)

        disclose_start = time.perf_counter()
        items = [
            RetrievedItem(
                unit_id=unit.id,
                score=score,
                content=unit.content,
                level=disclosure,
            )
            for score, _, unit in limited
        ]
        disclose_ms = _elapsed_ms(disclose_start)

        trajectory: List[TrajectoryStep] = []
        if with_trajectory:
            trajectory = [
                TrajectoryStep(
                    stage="parse",
                    candidate_count=len(query_terms),
                    cost_ms=parse_ms,
                    detail={"adapter": "in_memory_evaluation"},
                ),
                TrajectoryStep(
                    stage="recall",
                    channel=RecallChannel.KEYWORD,
                    candidate_count=len(candidates),
                    cost_ms=recall_ms,
                    detail={"scope": _scope_key(scope)},
                ),
                TrajectoryStep(
                    stage="fuse",
                    candidate_count=len(limited),
                    cost_ms=fuse_ms,
                    detail={"strategy": "token_overlap"},
                ),
                TrajectoryStep(
                    stage="disclose",
                    candidate_count=len(items),
                    cost_ms=disclose_ms,
                    detail={"level": str(disclosure.value)},
                ),
            ]
        return RetrievalResult(items=items, trajectory=trajectory)


def build_evaluation_api(config: Optional[Any] = None) -> InMemoryEvaluationAPI:
    """兼容 ``EvalHarness(api_factory=...)`` 的工厂函数。"""

    _ = config
    return InMemoryEvaluationAPI()


def _copy_scope(scope: Scope) -> Scope:
    return Scope(
        org=scope.org,
        user=scope.user,
        agent=scope.agent,
        session=scope.session,
    )


def _scope_key(scope: Scope) -> str:
    return "/".join([scope.org, scope.user, scope.agent, scope.session])


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if token not in _STOP_WORDS
    }


def _score(query_terms: set[str], unit: MemoryUnit) -> float:
    if not query_terms:
        return 0.0
    content_terms = _tokens(unit.content)
    overlap = query_terms & content_terms
    if not overlap:
        return 0.0
    coverage = len(overlap) / len(query_terms)
    density = len(overlap) / math.sqrt(max(len(content_terms), 1))
    tag_hits = len(query_terms & {tag.lower() for tag in unit.tags})
    return coverage + density + (0.05 * tag_hits)


def _active_at(unit: MemoryUnit, as_of: Optional[datetime]) -> bool:
    if as_of is None:
        return True
    temporal = unit.temporal
    if temporal.t_valid is not None and temporal.t_valid > as_of:
        return False
    if temporal.t_invalid is not None and temporal.t_invalid <= as_of:
        return False
    return True


def _matches_filters(unit: MemoryUnit, filters: Sequence[FilterClause]) -> bool:
    return all(_matches_filter(unit, clause) for clause in filters)


def _matches_filter(unit: MemoryUnit, clause: FilterClause) -> bool:
    actual = _field_value(unit, clause.field)
    op = clause.op.value if isinstance(clause.op, FilterOp) else str(clause.op)
    expected = clause.value

    if op == FilterOp.EQ.value:
        return actual == expected
    if op == FilterOp.NE.value:
        return actual != expected
    if op == FilterOp.IN.value:
        return actual in _as_iterable(expected)
    if op == FilterOp.NOT_IN.value:
        return actual not in _as_iterable(expected)
    if op == FilterOp.CONTAINS.value:
        return expected in _as_iterable(actual)
    if op == FilterOp.GT.value:
        return actual is not None and actual > expected
    if op == FilterOp.GTE.value:
        return actual is not None and actual >= expected
    if op == FilterOp.LT.value:
        return actual is not None and actual < expected
    if op == FilterOp.LTE.value:
        return actual is not None and actual <= expected
    return False


def _field_value(unit: MemoryUnit, field: str) -> Any:
    if field == "tags":
        return unit.tags
    if field.startswith("metadata."):
        return unit.metadata.get(field.split(".", 1)[1])
    if field in unit.metadata:
        return unit.metadata.get(field)
    if field == "content":
        return unit.content
    if field == "source":
        return unit.source.value
    return getattr(unit, field, None)


def _as_iterable(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return (value,)


def _elapsed_ms(start: float) -> float:
    return max(0.0, (time.perf_counter() - start) * 1000.0)
