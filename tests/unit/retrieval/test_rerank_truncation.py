"""PipelineRetriever 精排输入限长护栏测试（BUG2026092802777）。

交叉编码器负载 = query 长度 × 候选数，超长 query/doc 会把远程 rerank 服务
顶到 reranker_timeout 才返回。限长上限默认 2000/4000，可经 retriever params
（rerank_max_query_chars / rerank_max_doc_chars）覆盖。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import (
    _MAX_RERANK_DOC_CHARS,
    _MAX_RERANK_QUERY_CHARS,
    PipelineRetriever,
)

pytestmark = pytest.mark.unit


def _retriever(
    query_limit: int | None = None, doc_limit: int | None = None
) -> PipelineRetriever:
    """只装配限长参数的最小实例；其余依赖在 rerank 输入路径之外。"""
    kwargs: dict = {}
    if query_limit is not None:
        kwargs["rerank_max_query_chars"] = query_limit
    if doc_limit is not None:
        kwargs["rerank_max_doc_chars"] = doc_limit
    return PipelineRetriever(None, None, None, None, domain_store=None, **kwargs)


def test_rerank_inputs_default_limits() -> None:
    """默认限长：超长 query/doc 按出厂上限截断，未超限的原样保留。"""
    retriever = _retriever()
    query, docs = retriever._truncate_rerank_inputs(
        "q" * 5000, ["d" * 9000, "short doc"]
    )

    assert query == "q" * _MAX_RERANK_QUERY_CHARS
    assert docs[0] == "d" * _MAX_RERANK_DOC_CHARS
    assert docs[1] == "short doc"


def test_rerank_inputs_configurable_limits() -> None:
    """限长可经构造参数覆盖（与 reranker_timeout 同走 retriever params 通道）。"""
    retriever = _retriever(query_limit=100, doc_limit=50)
    query, docs = retriever._truncate_rerank_inputs("q" * 500, ["d" * 90])

    assert query == "q" * 100
    assert docs == ["d" * 50]
