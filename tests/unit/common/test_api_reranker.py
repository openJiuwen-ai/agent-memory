"""APIReranker：注入 mock client 验证请求构造 + 按 index 还原输入顺序（零网络）。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.reranker.reranker_impl.api_reranker import APIReranker

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


class _Client:
    """最小 httpx.Client 替身：记录请求、返回预置响应。"""

    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _Resp(self._payload, self._raise)


def _reranker(payload, raise_exc=None):
    return APIReranker(
        "rerank",
        "https://open.bigmodel.cn/api/paas/v4",
        "secret",
        client=_Client(payload, raise_exc),
    )


def _reranker_with_client(payload, raise_exc=None):
    client = _Client(payload, raise_exc)
    reranker = APIReranker(
        "rerank", "https://open.bigmodel.cn/api/paas/v4", "secret", client=client
    )
    return reranker, client


def test_restores_input_order_from_index() -> None:
    # API 按分排序返回（index 乱序），须还原到输入顺序 a,b,c
    payload = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    scores = _reranker(payload).rerank("q", ["a", "b", "c"])

    assert scores == [0.5, 0.1, 0.9]


def test_request_shape_and_auth() -> None:
    reranker, client = _reranker_with_client({"results": []})
    reranker.rerank("咖啡", ["doc1", "doc2"])

    call = client.calls[0]
    assert call["url"].endswith("/rerank")
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"] == {
        "model": "rerank",
        "query": "咖啡",
        "documents": ["doc1", "doc2"],
        "top_n": 2,
    }


def test_missing_result_defaults_zero() -> None:
    # 响应只覆盖 index 0 → 其余保持 0.0
    scores = _reranker({"results": [{"index": 0, "relevance_score": 0.7}]}).rerank("q", ["a", "b"])
    assert scores == [0.7, 0.0]


def test_empty_texts_returns_empty() -> None:
    assert _reranker({"results": []}).rerank("q", []) == []


# -- 防御：空文档 / 空 query 不透传后端（Issue #189 根因 2） -------------------- #


def test_empty_text_elements_excluded_from_request() -> None:
    # 空串/纯空白文本不进请求（透传会被网关 400 拒绝）；返回仍与输入等长同序，
    # 被过滤位置保持 0.0。
    payload = {"results": [{"index": 0, "relevance_score": 0.9}]}
    reranker, client = _reranker_with_client(payload)
    scores = reranker.rerank("q", ["", "abc", "   "])

    assert scores == [0.0, 0.9, 0.0], "空文本应得 0.0 分而非透传后端"
    sent = client.calls[0]["json"]["documents"]
    assert sent == ["abc"], "空文本元素不得出现在请求体中"


def test_all_texts_empty_skips_request_entirely() -> None:
    # 全部文本为空 → 不发请求，直接返回全 0.0（零网络零错误）
    reranker, client = _reranker_with_client({"results": []})
    scores = reranker.rerank("q", ["", "  "])

    assert scores == [0.0, 0.0]
    assert client.calls == [], "全空文本不应发起任何请求"


def test_empty_query_skips_request_entirely() -> None:
    # 空 query 对 rerank 无意义且同样可能被网关拒绝 → 跳过请求返回全 0.0
    reranker, client = _reranker_with_client({"results": []})
    scores = reranker.rerank("   ", ["a", "b"])

    assert scores == [0.0, 0.0]
    assert client.calls == [], "空 query 不应发起任何请求"


def test_backend_error_on_http_failure() -> None:
    reranker = _reranker({}, raise_exc=RuntimeError("502 Bad Gateway"))
    with pytest.raises(BackendError):
        reranker.rerank("q", ["a"])


def test_registered_in_producer() -> None:
    from jiuwen_memory.common.reranker.reranker_impl import RerankerProducer

    assert "api" in RerankerProducer.known()


# -- DashScope（阿里）方言 ---------------------------------------------------- #


def _dashscope(payload):
    client = _Client(payload)
    return APIReranker(
        "gte-rerank-v2",
        "https://dashscope.aliyuncs.com/api/v1",
        "secret",
        dialect="dashscope",
        client=client,
    ), client


def test_dashscope_request_envelope_and_endpoint() -> None:
    # 阿里响应放在 output.results 里
    reranker, client = _dashscope(
        {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }
        }
    )
    scores = reranker.rerank("咖啡", ["a", "b"])

    assert scores == [0.2, 0.8]  # 还原输入顺序
    call = client.calls[0]
    assert call["url"].endswith("/services/rerank/text-rerank/text-rerank")
    assert call["json"] == {
        "model": "gte-rerank-v2",
        "input": {"query": "咖啡", "documents": ["a", "b"]},
        "parameters": {"return_documents": False, "top_n": 2},
    }


def test_unknown_dialect_rejected() -> None:
    with pytest.raises(ValidationError):
        APIReranker("m", "https://x", "k", dialect="bananas", client=_Client({}))
