# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.vector.VectorStore` 的纯内存余弦 ANN。

按 scope 原生隔离（scope 折成命名空间键），``search`` 暴力计算查询向量与各行的
余弦相似度返回 top-k。无外部向量库依赖；维度一致性由调用方（同一 Embedder）保证。
"""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from threading import Lock

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    Scope,
    evaluate,
    filter_field_metadata_key,
    matches_filter_value,
)
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)

_ScopeKey = tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryVectorStore(VectorStore):
    """纯内存向量存储：按 scope 隔离，暴力余弦近邻。"""

    def __init__(self) -> None:
        self._data: dict[_ScopeKey, dict[str, VectorRecord]] = defaultdict(dict)
        self._lock = Lock()

    def _snapshot(self, scope: Scope) -> list[VectorRecord]:
        # Owned records are replaced, never mutated; scoring needs no store lock.
        with self._lock:
            return list(self._data.get(_skey(scope), {}).values())

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        return None

    def capabilities(self) -> frozenset[StoreCapability]:
        # 内存后端在 top-k 截断前逐条求值 filters，可作共享索引的前置过滤内层。
        return frozenset(
            {StoreCapability.SHARD_PREFILTER, StoreCapability.UNIT_RECORD_LOOKUP}
        )

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            for rec in records:
                if rec.id in bucket:
                    raise ConflictError("vector", rec.id)
                bucket[rec.id] = deepcopy(rec)

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            for rec in records:
                if rec.id not in bucket:
                    raise NotFoundError("vector", rec.id)
                bucket[rec.id] = deepcopy(rec)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            for rec_id in ids:
                bucket.pop(rec_id, None)

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        with self._lock:
            bucket = self._data.get(_skey(scope), {})
            return [deepcopy(bucket[i]) for i in ids if i in bucket]

    def record_ids_for_units(
        self, scope: Scope, unit_ids: list[str]
    ) -> dict[str, list[str]]:
        """按 ``metadata.unit_id`` 找出该记忆的全部记录 id（F09 D16）。"""
        wanted = set(unit_ids)
        out: dict[str, list[str]] = {}
        for rec in self._snapshot(scope):
            unit_id = rec.metadata.get("unit_id")
            if isinstance(unit_id, str) and unit_id in wanted:
                out.setdefault(unit_id, []).append(rec.id)
        return out

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        scored: list[ScoredID] = []
        for rec in self._snapshot(scope):
            if not evaluate(query.filters, lambda clause: matches_filter_value(
                rec.metadata.get(filter_field_metadata_key(clause.field)), clause
            )):
                continue
            score = _cosine(query.vector, rec.vector)
            if score > 0.0:
                scored.append(ScoredID(
                    id=rec.id,
                    score=score,
                    metadata=deepcopy(rec.metadata) if query.return_metadata else None,
                ))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        # 内存后端无 RTT，recall 即 search 的薄包装：output_fields 只认 "metadata"
        # （归并所需的 unit_id 即在其中），其余值忽略并记日志；空列表/None 不回带。
        fetch_meta = bool(output_fields) and "metadata" in output_fields
        if output_fields:
            unknown = [f for f in output_fields if f != "metadata"]
            msg = "InMemoryVectorStore.recall: output_fields only supports 'metadata', ignoring %s"
            if unknown:
                logger.info(msg, unknown)
        scored: list[ScoredHit] = []
        for rec in self._snapshot(scope):
            if not evaluate(query.filters, lambda clause: matches_filter_value(
                rec.metadata.get(filter_field_metadata_key(clause.field)), clause
            )):
                continue
            sim = _cosine(query.vector, rec.vector)
            if sim <= 0.0:
                continue
            scored.append(
                ScoredHit(
                    id=rec.id,
                    score=sim,
                    metadata=deepcopy(rec.metadata) if fetch_meta else {},
                )
            )
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]


# -- 注册到 VectorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@VectorProducer.register(
    "memory", config_inspectors={"at_rest_locations": lambda config: {}}
)
def _build(config):
    return InMemoryVectorStore()
