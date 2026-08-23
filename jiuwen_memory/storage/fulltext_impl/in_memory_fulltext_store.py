"""最小实现：:class:`~storage.fulltext.FulltextStore` 的纯内存全文存储。

按 scope 原生隔离（scope 折成命名空间键），用 Okapi BM25 计分做 top-k 召回。
分词复用注入的 :class:`~common.tokenizer.base.Tokenizer`，与构建侧同一实例即
同词表。无外部依赖。

IDF 与 avgdl 每次 ``search`` 从 scope 的词表现算，不做增量维护：``insert`` /
``update`` / ``delete`` 后不可能与索引失配，代价是每次查询 O(N)（N = scope 内
文档数）。纯内存后端本就按小规模 scope 设计，正确性优先于查询开销；大规模语料
请用 ``elasticsearch`` 后端。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Tuple

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.types import Document, ScoredID, TextQuery

_ScopeKey = Tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """把 scope 折成可哈希的命名空间键（隔离单位）。"""
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


class InMemoryFulltextStore(FulltextStore):
    """纯内存全文存储：按 scope 隔离，Okapi BM25 计分的 top-k 召回。"""

    def __init__(self, tokenizer: Tokenizer, *, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 < 0:
            raise ValueError(f"k1 must be >= 0, got {k1}")
        if not 0.0 <= b <= 1.0:
            raise ValueError(f"b must be in [0, 1], got {b}")
        self._tokenizer = tokenizer
        self._k1 = k1
        self._b = b
        self._docs: Dict[_ScopeKey, Dict[str, Document]] = defaultdict(dict)
        self._tokens: Dict[_ScopeKey, Dict[str, List[str]]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, docs: List[Document]) -> None:
        key = _skey(scope)
        bucket = self._docs[key]
        for doc in docs:
            if doc.id in bucket:
                raise ConflictError("document", doc.id)
            bucket[doc.id] = doc
            self._tokens[key][doc.id] = self._tokenizer.tokenize(doc.text)

    def update(self, scope: Scope, docs: List[Document]) -> None:
        key = _skey(scope)
        bucket = self._docs[key]
        for doc in docs:
            if doc.id not in bucket:
                raise NotFoundError("document", doc.id)
            bucket[doc.id] = doc
            self._tokens[key][doc.id] = self._tokenizer.tokenize(doc.text)

    def delete(self, scope: Scope, ids: List[str]) -> None:
        key = _skey(scope)
        for doc_id in ids:
            self._docs[key].pop(doc_id, None)
            self._tokens[key].pop(doc_id, None)

    def get(self, scope: Scope, ids: List[str]) -> List[Document]:
        bucket = self._docs[_skey(scope)]
        return [bucket[i] for i in ids if i in bucket]

    def search(self, scope: Scope, query: TextQuery) -> List[ScoredID]:
        key = _skey(scope)
        q_tokens = self._tokenizer.tokenize(query.text)
        if not q_tokens:
            return []

        docs = {doc_id: tokens for doc_id, tokens in self._tokens[key].items() if tokens}
        if not docs:
            return []
        n = len(docs)
        avgdl = sum(len(tokens) for tokens in docs.values()) / n

        # 只统计 query 内的词：query 外的词对得分无贡献，无需计 df。
        q_terms = set(q_tokens)
        df: dict[str, int] = defaultdict(int)
        for tokens in docs.values():
            for term in q_terms.intersection(tokens):
                df[term] += 1

        scored: List[ScoredID] = []
        for doc_id, tokens in docs.items():
            tf: dict[str, int] = defaultdict(int)
            for token in tokens:
                if token in q_terms:
                    tf[token] += 1
            if not tf:
                continue
            dl = len(tokens)
            score = 0.0
            for term, freq in tf.items():
                # Lucene 口径的 IDF：log 内的 +1 保证非负——缺了它，出现在半数以上
                # 文档中的词 IDF 为负，反而把含该词的文档往下压。
                idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denom = freq + self._k1 * (1.0 - self._b + self._b * dl / avgdl)
                score += idf * (freq * (self._k1 + 1.0)) / denom
            scored.append(ScoredID(id=doc_id, score=score))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]


# -- 注册到 FulltextProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@FulltextProducer.register("memory")
def _build(config):
    # Tokenizer 经 TokenizerProducer 自取（缺省 whitespace），与索引/查询侧共享同一实例。
    # k1 / b 为 BM25 标准缺省，一般无需调整。
    return InMemoryFulltextStore(
        TokenizerProducer.dep(config, default="whitespace"),
        k1=float(config.get("fulltext_bm25_k1", 1.5)),
        b=float(config.get("fulltext_bm25_b", 0.75)),
    )