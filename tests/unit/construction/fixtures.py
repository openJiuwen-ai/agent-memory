"""测试用 fixtures：轻量级 Store / Plugin 实现。

这些不是正式的 jiuwen_memory/ 代码，只在 tests/ 目录中使用。
正式的 Store/Plugin 实现由其他团队提供。
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.chunker.base import Chunker
from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.tokenizer.base import Tokenizer
from jiuwen_memory.common.type_def import (
    ChatMessage,
    Chunk,
    Entity,
    FeatureSet,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.audit import AuditEvent
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.types import Document, ScoredID, TextQuery, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector import VectorStore

# ---------------------------------------------------------------------------
# In-Memory Stores
# ---------------------------------------------------------------------------


class MemoryKVStore(KVStore):
    """内存 KVStore：scope 隔离的 dict 实现。"""

    def __init__(self) -> None:
        # scope_key → {key → value}
        self._data: dict[str, dict[str, bytes]] = {}

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def _scope_key(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.setdefault(sk, {})
        if key in bucket:
            raise ConflictError("kv", key, f"key {key!r} already exists in scope {sk}")
        bucket[key] = value

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        if key not in bucket:
            raise NotFoundError("kv", key, f"key {key!r} not found in scope {sk}")
        bucket[key] = value

    def delete(self, scope: Scope, key: str) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        bucket.pop(key, None)  # 幂等

    def get(self, scope: Scope, key: str) -> bytes:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        if key not in bucket:
            raise NotFoundError("kv", key, f"key {key!r} not found in scope {sk}")
        return bucket[key]

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes | None]:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        out: list[bytes] = []
        for key in keys:
            if key not in bucket:
                raise NotFoundError("kv", key)
            out.append(bucket[key])
        return out

    def exists(self, scope: Scope, key: str) -> bool:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        return key in bucket

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        return [(k, v) for k, v in bucket.items() if k.startswith(prefix)]

    def list(self, scope: Scope, **kwargs):
        raise NotImplementedError

    def scopes(self) -> list[Scope]:
        result = []
        for sk in self._data:
            parts = sk.split("/")
            result.append(
                Scope(
                    org=parts[0],
                    space=parts[1],
                    user=parts[2],
                    agent=parts[3],
                    session=parts[4],
                )
            )
        return result


class MemoryFulltextStore(FulltextStore):
    """内存 FulltextStore：简单 TF 匹配（M1 降级，不用 BM25）。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Document]] = {}

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def health(self) -> None:
        return None

    def _scope_key(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.setdefault(sk, {})
        for doc in docs:
            if doc.id in bucket:
                raise ConflictError(
                    "document", doc.id, f"doc {doc.id!r} already exists in scope {sk}"
                )
            bucket[doc.id] = doc

    def update(self, scope: Scope, docs: list[Document]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        for doc in docs:
            if doc.id not in bucket:
                raise NotFoundError("document", doc.id, f"doc {doc.id!r} not found in scope {sk}")
            bucket[doc.id] = doc

    def delete(self, scope: Scope, ids: list[str]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        for id_ in ids:
            bucket.pop(id_, None)  # 幂等

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        result = []
        for id_ in ids:
            if id_ in bucket:
                result.append(bucket[id_])
        return result

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        """简单 TF 匹配：查询词在文档文本中出现的次数。"""
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})

        # 分词 query（简单按空格/标点分）
        query_terms = set(re.findall(r"\w+", query.text.lower()))
        if not query_terms:
            return []

        results: list[ScoredID] = []
        for doc_id, doc in bucket.items():
            doc_terms = set(re.findall(r"\w+", doc.text.lower()))
            overlap = len(query_terms & doc_terms)
            if overlap > 0:
                score = overlap / len(query_terms)
                results.append(ScoredID(id=doc_id, score=score))

        # 按得分降序
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: query.top_k]


class MemoryVectorStore(VectorStore):
    """内存 VectorStore：暴力 cosine ANN。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, VectorRecord]] = {}

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        return None

    def _scope_key(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.setdefault(sk, {})
        for rec in records:
            if rec.id in bucket:
                raise ConflictError(
                    "vector_record", rec.id, f"record {rec.id!r} already exists in scope {sk}"
                )
            bucket[rec.id] = rec

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        for rec in records:
            if rec.id not in bucket:
                raise NotFoundError(
                    "vector_record", rec.id, f"record {rec.id!r} not found in scope {sk}"
                )
            bucket[rec.id] = rec

    def delete(self, scope: Scope, ids: list[str]) -> None:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        for id_ in ids:
            bucket.pop(id_, None)  # 幂等

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})
        result = []
        for id_ in ids:
            if id_ in bucket:
                result.append(bucket[id_])
        return result

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        """暴力 cosine ANN：计算 query 与所有 record 的 cosine similarity。"""
        sk = self._scope_key(scope)
        bucket = self._data.get(sk, {})

        q_vec = query.vector
        q_norm = math.sqrt(sum(x * x for x in q_vec))
        if q_norm == 0:
            return []

        results: list[ScoredID] = []
        for rec_id, rec in bucket.items():
            r_vec = rec.vector
            r_norm = math.sqrt(sum(x * x for x in r_vec))
            if r_norm == 0:
                continue
            dot = sum(a * b for a, b in zip(q_vec, r_vec))
            cosine = dot / (q_norm * r_norm)
            if cosine > 0:
                results.append(ScoredID(id=rec_id, score=cosine))

        results.sort(key=lambda x: x.score, reverse=True)
        return results[: query.top_k]


# ---------------------------------------------------------------------------
# Test Plugins
# ---------------------------------------------------------------------------


class MockLLM(LLM):
    """MockLLM：返回预定义 JSON，不调外部 API。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        """responses: 依次返回的字符串列表，用完循环。"""
        self._responses = responses or [
            json.dumps(
                [
                    {
                        "target": "preference",
                        "content": "用户偏好用 Python 写代码",
                        "evidence": "偏好 Python",
                        "confidence": 1.0,
                    }
                ]
            )
        ]
        self._call_count = 0

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        response = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        return response


class SimpleTokenizer(Tokenizer):
    """SimpleTokenizer：按空格和标点分词。"""

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.TOKENIZER

    def health(self) -> None:
        return None

    def tokenize(self, text: str) -> list[str]:
        return re.findall(r"\w+", text)


class FixedSizeChunker(Chunker):
    """FixedSizeChunker：按 token 数固定切分。"""

    def __init__(self, tokenizer: Tokenizer, chunk_size: int = 512, overlap: int = 64) -> None:
        self._tokenizer = tokenizer
        self._chunk_size = chunk_size
        self._overlap = overlap

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.CHUNKER

    def health(self) -> None:
        return None

    def chunk(
        self,
        text: str,
        unit_id: str = "",
        metadata: dict[str, str] | None = None,
    ) -> list[Chunk]:
        tokens = self._tokenizer.tokenize(text)
        if not tokens:
            return []

        chunks = []
        start = 0
        seq = 0
        while start < len(tokens):
            end = min(start + self._chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            # 回溯字符位置（简化：按比例估算）
            char_start = int(start * len(text) / max(len(tokens), 1))
            char_end = int(end * len(text) / max(len(tokens), 1))
            chunk_text = text[char_start:char_end] if char_start < len(text) else ""

            chunks.append(
                Chunk(
                    id=str(seq),
                    unit_id=unit_id,
                    seq=seq,
                    text=chunk_text,
                    start=char_start,
                    end=char_end,
                    token_count=len(chunk_tokens),
                    metadata=metadata or {},
                )
            )
            seq += 1
            start += self._chunk_size - self._overlap

        return chunks


class HashEmbedder(Embedder):
    """HashEmbedder：确定性 hash 向量（同文本 → cosine=1.0）。"""

    def __init__(self, dim: int = 128) -> None:
        self._dim = dim

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_vector(t) for t in texts]

    def dimension(self) -> int:
        return self._dim

    def _hash_vector(self, text: str) -> list[float]:
        """确定性 hash → 向量：同文本产出相同向量（cosine similarity = 1.0）。"""
        h = hashlib.sha256(text.encode()).digest()
        # 取 dim 个 float（4 bytes each）
        vec = []
        for i in range(self._dim):
            # 循环使用 hash 字节
            b = h[i % len(h)]
            vec.append(float(b) / 255.0 - 0.5)
        # 归一化
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


class RuleFeatureExtractor(FeatureExtractor):
    """RuleFeatureExtractor：正则关键词 + 简单实体检测。"""

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.FEATURE_EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, text: str) -> FeatureSet:
        # 关键词：提取中英文词汇
        keywords = re.findall(r"[a-zA-Z]{3,}|\w{2,}", text)
        keywords = list(set(keywords))[:10]  # 最多 10 个关键词

        # 实体：简单检测（英文大写词、中文技术名词）
        entities = []
        # 大写开头的英文词（可能是实体）
        for match in re.finditer(r"[A-Z][a-zA-Z]+", text):
            entities.append(Entity(text=match.group(), type="ENTITY", score=0.7))

        # 标签
        labels = {}
        if "偏好" in text or "喜欢" in text or "prefer" in text.lower():
            labels["sentiment"] = "positive"
        if "报错" in text or "错误" in text or "error" in text.lower():
            labels["sentiment"] = "negative"

        return FeatureSet(keywords=keywords, entities=entities, labels=labels)


class MemoryAuditLogger(AuditLogger):
    """MemoryAuditLogger：内存审计日志（记录到 list）。"""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        out: list[AuditEvent] = []
        for event in self._events:
            if filters.get("action") and event.action != filters["action"]:
                continue
            if filters.get("layer") and event.layer != filters["layer"]:
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def get_events(self) -> list[AuditEvent]:
        return list(self._events)


# ---------------------------------------------------------------------------
# Helper: 创建 M1 测试所需的全部组件
# ---------------------------------------------------------------------------


def create_test_stores() -> dict:
    """创建内存 Store 实例。"""
    return {
        "kv": MemoryKVStore(),
        "fulltext": MemoryFulltextStore(),
        "vector": MemoryVectorStore(),
    }


def create_test_plugins(mock_llm_responses: list[str] | None = None) -> dict:
    """创建测试 Plugin 实例。"""
    tokenizer = SimpleTokenizer()
    chunker = RecursiveChunker(
        chunk_size_chars=50,
        overlap_chars=10,
        min_chunk_chars=5,
    )
    return {
        "llm": MockLLM(responses=mock_llm_responses),
        "tokenizer": tokenizer,
        "chunker": chunker,
        "embedder": HashEmbedder(dim=128),
        "feature_extractor": RuleFeatureExtractor(),
        "audit_logger": MemoryAuditLogger(),
    }


def create_test_unit(
    unit_id: str = "u1",
    content: str = "用户偏好用 Python 写代码",
    scope: Scope | None = None,
    tier: MemoryTier = MemoryTier.EPISODIC,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
) -> MemoryUnit:
    """创建测试用的 MemoryUnit。"""
    return MemoryUnit(
        id=unit_id,
        scope=scope or Scope(org="test", user="alice"),
        tier=tier,
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=lifecycle,
        temporal=Temporal(),
    )
