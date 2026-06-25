"""最小实现：:class:`~storage.vector.VectorStore` 的纯内存余弦 ANN。

按 scope 原生隔离（scope 折成命名空间键），``search`` 暴力计算查询向量与各行的
余弦相似度返回 top-k。无外部向量库依赖；维度一致性由调用方（同一 Embedder）保证。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Tuple

from common.errors import ConflictError, NotFoundError
from common.type_def import Scope
from storage.base import StoreType
from storage.types import ScoredID, VectorQuery, VectorRecord
from storage.vector import VectorProducer, VectorStore

_ScopeKey = Tuple[str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.user, scope.agent, scope.session)


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
        scored = [
            ScoredID(id=rec.id, score=_cosine(query.vector, rec.vector))
            for rec in self._data[_skey(scope)].values()
        ]
        scored = [s for s in scored if s.score > 0.0]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]


# -- 注册到 VectorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@VectorProducer.register("memory")
def _build(config):
    return InMemoryVectorStore()
