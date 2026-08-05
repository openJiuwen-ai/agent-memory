"""最小实现：:class:`~storage.vector.VectorStore` 的纯内存余弦 ANN。

按 scope 原生隔离（scope 折成命名空间键），``search`` 暴力计算查询向量与各行的
余弦相似度返回 top-k。无外部向量库依赖；维度一致性由调用方（同一 Embedder）保证。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Tuple

from common.errors import ConflictError, NotFoundError
from common.log import get_logger
from common.type_def import Scope
from storage.base import StoreType
from storage.types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)

_ScopeKey = Tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryVectorStore(VectorStore):
    """纯内存向量存储：按 scope 隔离，暴力余弦近邻。"""

    def __init__(self) -> None:
        self._data: Dict[_ScopeKey, Dict[str, VectorRecord]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, records: List[VectorRecord]) -> None:
        bucket = self._data[_skey(scope)]
        for rec in records:
            if rec.id in bucket:
                raise ConflictError("vector", rec.id)
            bucket[rec.id] = rec

    def update(self, scope: Scope, records: List[VectorRecord]) -> None:
        bucket = self._data[_skey(scope)]
        for rec in records:
            if rec.id not in bucket:
                raise NotFoundError("vector", rec.id)
            bucket[rec.id] = rec

    def delete(self, scope: Scope, ids: List[str]) -> None:
        bucket = self._data[_skey(scope)]
        for rec_id in ids:
            bucket.pop(rec_id, None)

    def get(self, scope: Scope, ids: List[str]) -> List[VectorRecord]:
        bucket = self._data[_skey(scope)]
        return [bucket[i] for i in ids if i in bucket]

    def search(self, scope: Scope, query: VectorQuery) -> List[ScoredID]:
        bucket = self._data[_skey(scope)]
        scored = [
            ScoredID(
                id=rec.id,
                score=_cosine(query.vector, rec.vector),
                metadata=dict(rec.metadata) if query.return_metadata else None,
            )
            for rec in bucket.values()
        ]
        scored = [s for s in scored if s.score > 0.0]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> List[ScoredHit]:
        # 内存后端无 RTT，recall 即 search 的薄包装：output_fields 只认 "metadata"
        # （归并所需的 unit_id 即在其中），其余值忽略并记日志；空列表/None 不回带。
        fetch_meta = bool(output_fields) and "metadata" in output_fields
        if output_fields:
            unknown = [f for f in output_fields if f != "metadata"]
            if unknown:
                logger.info(
                    "InMemoryVectorStore.recall: output_fields only supports 'metadata', ignoring %s",
                    unknown,
                )
        bucket = self._data[_skey(scope)]
        scored: list[ScoredHit] = []
        for rec in bucket.values():
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


@VectorProducer.register("memory")
def _build(config):
    return InMemoryVectorStore()
