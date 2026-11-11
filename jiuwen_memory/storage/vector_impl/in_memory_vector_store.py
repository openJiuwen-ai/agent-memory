# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.vector.VectorStore` 的纯内存余弦 ANN。

按 scope 原生隔离（scope 折成命名空间键），``search`` 暴力计算查询向量与各行的
余弦相似度返回 top-k。无外部向量库依赖；维度一致性由调用方（同一 Embedder）保证。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    Scope,
    evaluate,
    filter_field_metadata_key,
    matches_filter_value,
)
from jiuwen_memory.storage.base import StoreType
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
        # 写方法两阶段（先全量校验后全量提交），读方法持锁快照后锁外计算：
        # 无锁时 search 迭代 bucket.values() 与并发 insert 交错会抛
        # "dictionary changed size during iteration"。
        self._lock = threading.Lock()

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            # 先全量校验再提交：中途冲突不得留下部分写入，否则重试会因
            # 前半批已入库而永久冲突。
            for rec in records:
                if rec.id in bucket:
                    raise ConflictError("vector", rec.id)
            for rec in records:
                bucket[rec.id] = rec

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            for rec in records:
                if rec.id not in bucket:
                    raise NotFoundError("vector", rec.id)
            for rec in records:
                bucket[rec.id] = rec

    def delete(self, scope: Scope, ids: list[str]) -> None:
        with self._lock:
            bucket = self._data[_skey(scope)]
            for rec_id in ids:
                bucket.pop(rec_id, None)

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        with self._lock:
            bucket = self._data[_skey(scope)]
            return [bucket[i] for i in ids if i in bucket]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        with self._lock:
            bucket = self._data[_skey(scope)]
            rows = list(bucket.values())
        scored: list[ScoredID] = []
        for rec in bucket.values():
            if not evaluate(query.filters, lambda clause: matches_filter_value(
                rec.metadata.get(filter_field_metadata_key(clause.field)), clause
            )):
                continue
            score = _cosine(query.vector, rec.vector)
            if score > 0.0:
                scored.append(ScoredID(
                    id=rec.id,
                    score=score,
                    metadata=dict(rec.metadata) if query.return_metadata else None,
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
        with self._lock:
            rows = list(self._data[_skey(scope)].values())
        scored: list[ScoredHit] = []
        for rec in rows:
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
                    metadata=dict(rec.metadata) if fetch_meta else {},
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
