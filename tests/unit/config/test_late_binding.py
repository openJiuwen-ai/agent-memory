"""同实例晚绑定：model / api_key / base_url / url 在调用路径生效。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.embedder.embedder_impl.openai_embedder import OpenAIEmbedder
from common.llm.llm_impl.openai_llm import OpenAILLM
from common.reranker.reranker_impl.api_reranker import APIReranker
from common.type_def import ChatMessage
from config.config_source_impl.dict_config_source import DictConfigSource
from storage.kv_impl.redis_kv import RedisKVStore


class _EmbResp:
    def __init__(self, texts: list[str], dim: int = 4) -> None:
        self.data = [
            type("D", (), {"index": i, "embedding": [float(i)] * dim})()
            for i, _ in enumerate(texts)
        ]


def test_openai_embedder_late_binds_model_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DictConfigSource(
        {
            "embedder.model": "text-embedding-3-small",
            "embedder.api_key": "key-a",
            "embedder.base_url": "https://a.example/v1",
        }
    )
    clients: list[MagicMock] = []
    ctor_kwargs: list[dict] = []

    def _factory(**kwargs):
        ctor_kwargs.append(kwargs)
        client = MagicMock()
        client.embeddings.create = MagicMock(
            side_effect=lambda **kw: _EmbResp(kw["input"], dim=4)
        )
        clients.append(client)
        return client

    import openai as openai_mod

    monkeypatch.setattr(openai_mod, "OpenAI", _factory)

    emb = OpenAIEmbedder(
        model_name="fallback-model",
        api_key="fallback-key",
        base_url="https://fallback.example/v1",
        dimension=4,
        config_source=cfg,
    )
    emb.embed(["hello"])
    assert ctor_kwargs[0]["api_key"] == "key-a"
    assert ctor_kwargs[0]["base_url"] == "https://a.example/v1"
    assert clients[0].embeddings.create.call_args.kwargs["model"] == "text-embedding-3-small"

    cfg.put("embedder.model", "text-embedding-3-large")
    cfg.put("embedder.api_key", "key-b")
    cfg.put("embedder.base_url", "https://b.example/v1")
    emb.embed(["hello"])
    assert len(clients) == 2
    assert ctor_kwargs[1]["api_key"] == "key-b"
    assert ctor_kwargs[1]["base_url"] == "https://b.example/v1"
    assert clients[1].embeddings.create.call_args.kwargs["model"] == "text-embedding-3-large"


def test_openai_llm_late_binds_model_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DictConfigSource(
        {
            "llm.model": "gpt-4o-mini",
            "llm.api_key": "sk-a",
            "llm.base_url": "https://a.example/v1",
        }
    )

    class _Choice:
        def __init__(self) -> None:
            self.message = type("M", (), {"content": "ok"})()

    class _Resp:
        choices = [_Choice()]

    clients: list[MagicMock] = []
    ctor_kwargs: list[dict] = []

    def _factory(**kwargs):
        ctor_kwargs.append(kwargs)
        client = MagicMock()
        client.chat.completions.create = MagicMock(return_value=_Resp())
        clients.append(client)
        return client

    import openai as openai_mod

    monkeypatch.setattr(openai_mod, "OpenAI", _factory)

    llm = OpenAILLM(
        model_name="gpt-4o",
        api_key="sk-fallback",
        base_url="https://fallback.example/v1",
        config_source=cfg,
    )
    assert llm.chat([ChatMessage(role="user", content="hi")]) == "ok"
    assert ctor_kwargs[0]["api_key"] == "sk-a"
    assert clients[0].chat.completions.create.call_args.kwargs["model"] == "gpt-4o-mini"

    cfg.put("llm.model", "gpt-4o")
    cfg.put("llm.api_key", "sk-b")
    cfg.put("llm.base_url", "https://b.example/v1")
    llm.chat([ChatMessage(role="user", content="hi")])
    assert len(clients) == 2
    assert ctor_kwargs[1]["api_key"] == "sk-b"
    assert clients[1].chat.completions.create.call_args.kwargs["model"] == "gpt-4o"


def test_api_reranker_late_binds_model_key_url() -> None:
    cfg = DictConfigSource(
        {
            "reranker.model": "rerank-v1",
            "reranker.api_key": "rk-a",
            "reranker.base_url": "https://a.example",
        }
    )
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={"results": [{"index": 0, "relevance_score": 0.9}]}
    )
    client.post = MagicMock(return_value=resp)

    rr = APIReranker(
        model_name="fallback",
        base_url="https://fallback.example",
        api_key="rk-fallback",
        client=client,
        config_source=cfg,
    )
    scores = rr.rerank("q", ["doc"])
    assert scores == [0.9]
    assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer rk-a"
    assert client.post.call_args.args[0] == "https://a.example/rerank"
    assert client.post.call_args.kwargs["json"]["model"] == "rerank-v1"

    cfg.put("reranker.model", "rerank-v2")
    cfg.put("reranker.api_key", "rk-b")
    cfg.put("reranker.base_url", "https://b.example")
    rr.rerank("q", ["doc"])
    assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer rk-b"
    assert client.post.call_args.args[0] == "https://b.example/rerank"
    assert client.post.call_args.kwargs["json"]["model"] == "rerank-v2"


def test_redis_kv_late_binds_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DictConfigSource({"kv_store.url": "redis://a.example:6379/0"})
    store = RedisKVStore(url="redis://fallback:6379/0", config_source=cfg)

    created: list[str] = []

    class _FakeRedis:
        @classmethod
        def from_url(cls, url: str, **kwargs):
            created.append(url)
            return MagicMock(ping=MagicMock(return_value=True))

    import sys
    import types

    fake_mod = types.ModuleType("redis")
    fake_mod.Redis = _FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", fake_mod)

    store.health()
    assert created == ["redis://a.example:6379/0"]

    cfg.put("kv_store.url", "redis://b.example:6379/1")
    store.health()
    assert created[-1] == "redis://b.example:6379/1"
