"""最小实现：:class:`~storage.fusion.FusionStore`——向量·倒排·正排合一。

同一 id 一行同时承载向量 / 文本 / 标量 / 原始值；``search`` 在一次调用内做
「向量近邻 +（可选）文本相关性」按 ``vector_weight`` 混合、受 ``scalar_filters``
约束，``get`` 即正排读取。按 scope 原生隔离。无外部依赖。

这是分离的 fulltext+vector 之外的**另一种存储形态**（一次召回免 join）；分词复用
注入的 Tokenizer 算文本相关性。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope, evaluate, filter_field_metadata_key
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fusion import FusionProducer, FusionStore
from jiuwen_memory.storage.types import FusionQuery, FusionRecord, ScoredID

_ScopeKey = Tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _passes(scalars: Dict[str, Any], clause: FilterClause) -> bool:
    val = scalars.get(filter_field_metadata_key(clause.field))
    if isinstance(val, (list, tuple, set)):
        if clause.op == FilterOp.CONTAINS:
            return clause.value in val
        if clause.op in (FilterOp.NE, FilterOp.NOT_IN):
            return True
        return False
    if val is None:
        return clause.op in (FilterOp.NE, FilterOp.NOT_IN)
    if clause.op == FilterOp.EQ:
        return val == clause.value
    if clause.op == FilterOp.NE:
        return val != clause.value
    if clause.op == FilterOp.IN:
        return val in (clause.value or [])
    if clause.op == FilterOp.NOT_IN:
        return val not in (clause.value or [])
    if clause.op == FilterOp.CONTAINS:
        return False
    if clause.op == FilterOp.GT:
        return val > clause.value
    if clause.op == FilterOp.GTE:
        return val >= clause.value
    if clause.op == FilterOp.LT:
        return val < clause.value
    if clause.op == FilterOp.LTE:
        return val <= clause.value
    return True


class InMemoryFusionStore(FusionStore):
    """纯内存融合存储：向量近邻 + 文本相关性混合 + 标量过滤，一次召回。"""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer
        self._data: Dict[_ScopeKey, Dict[str, FusionRecord]] = defaultdict(dict)
        self._tokens: Dict[_ScopeKey, Dict[str, List[str]]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        return StoreType.FUSION

    def health(self) -> None:
        return None

    def _index_tokens(self, sk: _ScopeKey, rec: FusionRecord) -> None:
        self._tokens[sk][rec.id] = self._tokenizer.tokenize(rec.text) if rec.text else []

    def insert(self, scope: Scope, records: List[FusionRecord]) -> None:
        sk = _skey(scope)
        for rec in records:
            if rec.id in self._data[sk]:
                raise ConflictError("fusion", rec.id)
            self._data[sk][rec.id] = rec
            self._index_tokens(sk, rec)

    def update(self, scope: Scope, records: List[FusionRecord]) -> None:
        sk = _skey(scope)
        for rec in records:
            if rec.id not in self._data[sk]:
                raise NotFoundError("fusion", rec.id)
            self._data[sk][rec.id] = rec
            self._index_tokens(sk, rec)

    def delete(self, scope: Scope, ids: List[str]) -> None:
        sk = _skey(scope)
        for rec_id in ids:
            self._data[sk].pop(rec_id, None)
            self._tokens[sk].pop(rec_id, None)

    def get(self, scope: Scope, ids: List[str]) -> List[FusionRecord]:
        bucket = self._data[_skey(scope)]
        return [bucket[i] for i in ids if i in bucket]

    def search(self, scope: Scope, query: FusionQuery) -> List[ScoredID]:
        sk = _skey(scope)
        q_tokens = set(self._tokenizer.tokenize(query.text)) if query.text else set()
        scored: List[ScoredID] = []
        for rec_id, rec in self._data[sk].items():
            if not evaluate(query.scalar_filters, lambda c: _passes(rec.scalars, c)):
                continue
            vscore = _cosine(query.vector or [], rec.vector or [])
            toks = self._tokens[sk].get(rec_id, [])
            tscore = (
                sum(1 for t in toks if t in q_tokens) / len(toks)
                if toks and q_tokens
                else 0.0
            )
            mixed = query.vector_weight * vscore + (1.0 - query.vector_weight) * tscore
            if mixed > 0.0:
                scored.append(ScoredID(id=rec_id, score=mixed))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]


# -- 注册到 FusionProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@FusionProducer.register("memory")
def _build(config):
    return InMemoryFusionStore(TokenizerProducer.dep(config, default="whitespace"))
