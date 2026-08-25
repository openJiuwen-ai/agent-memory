"""最小实现：:class:`~storage.fulltext.FulltextStore` 的纯内存全文存储。

按 scope 原生隔离（scope 折成命名空间键），用词重叠计分模拟 BM25 的 top-k
召回。分词复用注入的 :class:`~common.tokenizer.base.Tokenizer`，与构建侧同一
实例即同词表。无外部依赖。
"""

from __future__ import annotations

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
    """纯内存全文存储：按 scope 隔离，词重叠计分模拟 BM25 的 top-k 召回。"""

    def __init__(self, tokenizer: Tokenizer) -> None:
        """初始化 InMemoryFulltextStore。

        Args:
            tokenizer: 参数 tokenizer（Tokenizer）。
        """
        self._tokenizer = tokenizer
        self._docs: Dict[_ScopeKey, Dict[str, Document]] = defaultdict(dict)
        self._tokens: Dict[_ScopeKey, Dict[str, List[str]]] = defaultdict(dict)

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.FULLTEXT

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def insert(self, scope: Scope, docs: List[Document]) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            docs: 参数 docs（List[Document]）。

        Raises:
            ConflictError: 执行失败时抛出。
        """
        key = _skey(scope)
        bucket = self._docs[key]
        for doc in docs:
            if doc.id in bucket:
                raise ConflictError("document", doc.id)
            bucket[doc.id] = doc
            self._tokens[key][doc.id] = self._tokenizer.tokenize(doc.text)

    def update(self, scope: Scope, docs: List[Document]) -> None:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            docs: 参数 docs（List[Document]）。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        key = _skey(scope)
        bucket = self._docs[key]
        for doc in docs:
            if doc.id not in bucket:
                raise NotFoundError("document", doc.id)
            bucket[doc.id] = doc
            self._tokens[key][doc.id] = self._tokenizer.tokenize(doc.text)

    def delete(self, scope: Scope, ids: List[str]) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            ids: 参数 ids（List[str]）。
        """
        key = _skey(scope)
        for doc_id in ids:
            self._docs[key].pop(doc_id, None)
            self._tokens[key].pop(doc_id, None)

    def get(self, scope: Scope, ids: List[str]) -> List[Document]:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            ids: 参数 ids（List[str]）。

        Returns:
            返回 List[Document]。
        """
        bucket = self._docs[_skey(scope)]
        return [bucket[i] for i in ids if i in bucket]

    def search(self, scope: Scope, query: TextQuery) -> List[ScoredID]:
        """检索与查询匹配的结果。

        Args:
            scope: 参数 scope（Scope）。
            query: 参数 query（TextQuery）。

        Returns:
            返回 List[ScoredID]。
        """
        key = _skey(scope)
        q_tokens = self._tokenizer.tokenize(query.text)
        if not q_tokens:
            return []
        q_set = set(q_tokens)
        scored: List[ScoredID] = []
        for doc_id, tokens in self._tokens[key].items():
            if not tokens:
                continue
            hits = sum(1 for t in tokens if t in q_set)
            if hits:
                scored.append(ScoredID(id=doc_id, score=hits / len(tokens)))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: query.top_k]


# -- 注册到 FulltextProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@FulltextProducer.register("memory")
def _build(config):
    # Tokenizer 经 TokenizerProducer 自取（缺省 whitespace），与索引/查询侧共享同一实例。
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return InMemoryFulltextStore(TokenizerProducer.dep(config, default="whitespace"))
