# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~storage.fulltext.FulltextStore` 的纯内存全文存储。

按完整 scope 隔离，支持词重叠与 Okapi BM25 排序。分词复用注入的
:class:`~common.tokenizer.base.Tokenizer`，与构建侧共享词表。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock

from jiuwen_memory.common.bm25 import inverse_document_frequency, term_score
from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.tokenizer import Tokenizer
from jiuwen_memory.common.tokenizer.base import TokenizerProducer
from jiuwen_memory.common.type_def import (
    FilterExpr,
    Scope,
    evaluate,
    filter_field_metadata_key,
    matches_filter_value,
)
from jiuwen_memory.storage._hierarchy_ties import summary_position
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.types import Document, ScoredID, TextQuery

_ScopeKey = tuple[str, str, str, str, str]


def _skey(scope: Scope) -> _ScopeKey:
    """把 scope 折成可哈希的命名空间键（隔离单位）。"""
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


@dataclass
class _TermIndex:
    postings: dict[str, dict[str, int]] = field(default_factory=dict)
    token_count: int = 0

    def replace(self, doc_id: str, old: list[str], new: list[str]) -> None:
        for term in set(old):
            posting = self.postings[term]
            del posting[doc_id]
            if not posting:
                del self.postings[term]
        for term, frequency in Counter(new).items():
            self.postings.setdefault(term, {})[doc_id] = frequency
        self.token_count += len(new) - len(old)


class InMemoryFulltextStore(FulltextStore):
    """按 scope 维护文档、分词和可选的 BM25 倒排索引。"""

    def __init__(self, tokenizer: Tokenizer, *, scoring: str = "overlap",
                 stable_hierarchy_ties: bool = False) -> None:
        if scoring not in ("overlap", "bm25"):
            raise ValidationError("memory fulltext scoring must be overlap or bm25")
        if type(stable_hierarchy_ties) is not bool:
            raise ValidationError("stable_hierarchy_ties must be bool")
        self._stable_hierarchy_ties = stable_hierarchy_ties
        self._tokenizer = tokenizer
        self._bm25 = scoring == "bm25"
        self._terms: dict[_ScopeKey, _TermIndex] = defaultdict(_TermIndex)
        self._lock = RLock()
        self._docs: dict[_ScopeKey, dict[str, Document]] = defaultdict(dict)
        self._tokens: dict[_ScopeKey, dict[str, list[str]]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        self._write(_skey(scope), docs, insert=True)

    def update(self, scope: Scope, docs: list[Document]) -> None:
        self._write(_skey(scope), docs, insert=False)

    def _write(self, key: _ScopeKey, docs: list[Document], *, insert: bool) -> None:
        with self._lock:
            bucket, tokens = self._docs[key], self._tokens[key]
            staged = {}
            for doc in docs:
                if insert and (doc.id in bucket or doc.id in staged):
                    raise ConflictError("document", doc.id)
                if not insert and doc.id not in bucket:
                    raise NotFoundError("document", doc.id)
                staged[doc.id] = (doc, self._tokenizer.tokenize(doc.text))
            for uid, (doc, new_tokens) in staged.items():
                if self._bm25:
                    self._terms[key].replace(uid, tokens.get(uid, []), new_tokens)
                bucket[uid], tokens[uid] = doc, new_tokens

    def delete(self, scope: Scope, ids: list[str]) -> None:
        key = _skey(scope)
        with self._lock:
            for doc_id in ids:
                old = self._tokens[key].pop(doc_id, None)
                if old is not None and self._bm25:
                    self._terms[key].replace(doc_id, old, [])
                self._docs[key].pop(doc_id, None)

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        with self._lock:
            bucket = self._docs[_skey(scope)]
            return [bucket[i] for i in ids if i in bucket]

    def rebuild_keyword_statistics(self) -> None:
        """Rebuild derived postings after importing a historical token-only snapshot."""
        if self._bm25:
            with self._lock:
                rebuilt: dict[_ScopeKey, _TermIndex] = defaultdict(_TermIndex)
                for key, bucket in self._tokens.items():
                    for uid, tokens in bucket.items():
                        rebuilt[key].replace(uid, [], tokens)
                self._terms = rebuilt

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        key = _skey(scope)
        q_tokens = self._tokenizer.tokenize(query.text)
        if not q_tokens:
            return []
        with self._lock:
            if self._bm25:
                return self._bm25_search(key, query, q_tokens)
            return self._overlap_search(key, query, q_tokens)

    def _bm25_search(self, key: _ScopeKey, query: TextQuery, q_tokens: list[str]) -> list[ScoredID]:
        index = self._terms.get(key)
        if not index or not index.token_count:
            return []
        documents = self._tokens[key]
        average_length = index.token_count / len(documents)
        scores: dict[str, float] = defaultdict(float)
        for term, weight in Counter(q_tokens).items():
            posting = index.postings.get(term)
            if not posting:
                continue
            idf = inverse_document_frequency(len(documents), len(posting))
            for uid, frequency in posting.items():
                length_penalty = 1.2 * (0.25 + 0.75 * len(documents[uid]) / average_length)
                scores[uid] += weight * term_score(idf, frequency, length_penalty, 1.2)
        eligible = []
        for uid, score in scores.items():
            metadata = self._docs[key][uid].metadata
            if evaluate(query.filters, lambda clause: matches_filter_value(
                metadata.get(filter_field_metadata_key(clause.field)), clause
            )):
                eligible.append(ScoredID(id=uid, score=score))
        if self._stable_hierarchy_ties:
            documents = self._docs[key]

            def position(item):
                doc = documents[item.id]
                source = summary_position(doc.metadata)
                if source is None:
                    return -item.score, (1,), "", item.id
                return -item.score, (0, *source), sha256(doc.text.encode()).hexdigest(), item.id

            eligible.sort(key=position)
        else:
            eligible.sort(key=lambda item: (-item.score, item.id))
        return eligible[:query.top_k]

    def _overlap_search(
        self, key: _ScopeKey, query: TextQuery, q_tokens: list[str],
    ) -> list[ScoredID]:
        q_set = set(q_tokens)
        scored: list[ScoredID] = []
        for doc_id, tokens in self._tokens[key].items():
            if not tokens:
                continue
            metadata = self._docs[key][doc_id].metadata
            if not evaluate(query.filters, lambda clause: matches_filter_value(
                metadata.get(filter_field_metadata_key(clause.field)), clause
            )):
                continue
            hits = sum(1 for t in tokens if t in q_set)
            if hits:
                scored.append(ScoredID(id=doc_id, score=hits / len(tokens)))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]

    def count_units(self, scope: Scope, filters: FilterExpr | None = None,
                    *, descendants: bool = False) -> int:
        with self._lock:
            return self._count_units(scope, filters, descendants)

    def _count_units(self, scope: Scope, filters: FilterExpr | None, descendants: bool) -> int:
        boundary, units = _skey(scope), set()
        for skey, documents in self._docs.items():
            if descendants:
                if skey[:2] != boundary[:2] or any(
                        value and skey[i] != value for i, value in enumerate(boundary) if i >= 2):
                    continue
            elif skey != boundary:
                continue
            for document in documents.values():
                metadata = document.metadata
                uid = metadata.get("unit_id")
                if isinstance(uid, str) and uid and evaluate(filters,
                        lambda clause: matches_filter_value(
                            metadata.get(filter_field_metadata_key(clause.field)), clause)):
                    units.add((*skey, uid))
        return len(units)


# -- 注册到 FulltextProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@FulltextProducer.register(
    "memory", config_inspectors={"at_rest_locations": lambda config: {}}
)
def _build(config):
    # Tokenizer 经 TokenizerProducer 自取（缺省 whitespace），与索引/查询侧共享同一实例。
    scoring = config.params.get("scoring", config.get("memory_fulltext_scoring", "overlap"))
    stable_ties = config.params.get("stable_hierarchy_ties",
                                   config.get("memory_stable_hierarchy_ties", False))
    return InMemoryFulltextStore(TokenizerProducer.dep(config, default="whitespace"),
                                scoring=scoring, stable_hierarchy_ties=stable_ties)
