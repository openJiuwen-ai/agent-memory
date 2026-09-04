"""infer=true 上下文增强抽取测试（见 F02「上下文增强抽取」）。

覆盖：
- evolver EXTRACT 模式下检测 metadata["infer"]=="true" → 内部收集 context
  （recent_originals 从 /messages/ 拉取、related_memories 从 dedup.recall 召回）；
- related_memories 经 prompt 告知大模型已有这些记忆、勿重复抽取（去重靠 prompt
  提示 + _dedup_batch 兜底，evolver 不做向量过滤）；原文不参与去重；
- 非 infer 时 context=None，行为与未扩展前一致；
- engine.write(infer=true) 把原文以规约后的 MemoryUnit 落 /messages/（不建索引），
  同一批 MemoryUnit 喂 evolve。

用 test_evolver_dedup.py 同款内联 Store/Plugin（不依赖外部）。
"""

from __future__ import annotations

import asyncio
import hashlib
import math

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ConflictError, NotFoundError, PermissionDeniedError
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    MESSAGES_KEY_PREFIX,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
    memory_key,
    messages_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import (
    IndexRemoveMode,
    IndexWriteMode,
    ScoredID,
    VectorRecord,
)
from jiuwen_memory.storage.vector import VectorStore

pytestmark = pytest.mark.unit

_DEFAULT_SCOPE = Scope(org="test", user="alice", agent="a1", session="s1")


# ---------------------------------------------------------------------------
# 内联轻量 Store / Plugin（与 test_evolver_dedup.py 同款）
# ---------------------------------------------------------------------------


class _MemoryKVStore(KVStore):
    def __init__(self) -> None:
        self._data: dict[str, dict[str, bytes]] = {}

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def _sk(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope, key, value, ttl=0.0):
        b = self._data.setdefault(self._sk(scope), {})
        if key in b:
            raise ConflictError("kv", key)
        b[key] = value

    def update(self, scope, key, value, ttl=0.0):
        b = self._data.get(self._sk(scope), {})
        if key not in b:
            raise NotFoundError("kv", key)
        b[key] = value

    def delete(self, scope, key):
        self._data.get(self._sk(scope), {}).pop(key, None)

    def get(self, scope, key):
        b = self._data.get(self._sk(scope), {})
        if key not in b:
            raise NotFoundError("kv", key)
        return b[key]

    def mget(self, scope, keys):
        b = self._data.get(self._sk(scope), {})
        out = []
        for key in keys:
            if key not in b:
                raise NotFoundError("kv", key)
            out.append(b[key])
        return out

    def exists(self, scope, key):
        return key in self._data.get(self._sk(scope), {})

    def scan(self, scope, prefix=""):
        b = self._data.get(self._sk(scope), {})
        return [(k, v) for k, v in b.items() if k.startswith(prefix)]

    def list(self, scope, **kwargs):
        raise NotImplementedError

    def scopes(self):
        result = []
        for sk in self._data:
            p = sk.split("/")
            result.append(Scope(org=p[0], space=p[1], user=p[2], agent=p[3], session=p[4]))
        return result


class _MemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        self._data: dict[str, dict[str, VectorRecord]] = {}

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        return None

    def _sk(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope, records):
        b = self._data.setdefault(self._sk(scope), {})
        for r in records:
            if r.id in b:
                raise ConflictError("vector", r.id)
            b[r.id] = r

    def update(self, scope, records):
        b = self._data.get(self._sk(scope), {})
        for r in records:
            if r.id not in b:
                raise NotFoundError("vector", r.id)
            b[r.id] = r

    def delete(self, scope, ids):
        b = self._data.get(self._sk(scope), {})
        for i in ids:
            b.pop(i, None)

    def get(self, scope, ids):
        b = self._data.get(self._sk(scope), {})
        return [b[i] for i in ids if i in b]

    def search(self, scope, query):
        b = self._data.get(self._sk(scope), {})
        q_vec = query.vector
        q_norm = math.sqrt(sum(x * x for x in q_vec))
        if q_norm == 0:
            return []
        results = []
        for rid, rec in b.items():
            r_norm = math.sqrt(sum(x * x for x in rec.vector))
            if r_norm == 0:
                continue
            cosine = sum(a * b for a, b in zip(q_vec, rec.vector)) / (q_norm * r_norm)
            if cosine > 0:
                results.append(ScoredID(id=rid, score=cosine))
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: query.top_k]


class _HashEmbedder(Embedder):
    """确定性 hash 向量：同文本 → cosine=1.0，不同文本 → cosine<1.0。"""

    def __init__(self, dim=64) -> None:
        self._dim = dim

    def plugin_type(self):
        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def dimension(self):
        return self._dim

    def _vec(self, text):
        h = hashlib.sha256(text.encode()).digest()
        vec = [float(h[i % len(h)]) / 255.0 - 0.5 for i in range(self._dim)]
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec


class _MockLLM(LLM):
    def __init__(self, responses=None) -> None:
        self._responses = responses or []
        self._call_count = 0

    def plugin_type(self):
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages, **options):
        r = self._responses[self._call_count % len(self._responses)] if self._responses else ""
        self._call_count += 1
        return r


# ---------------------------------------------------------------------------
# Mock 算子
# ---------------------------------------------------------------------------


class _ScriptedExtractor(Extractor):
    """返回预设候选列表的 Mock Extractor（记录 context 入参供断言）。"""

    def __init__(self, candidates: list[MemoryUnit]) -> None:
        self._candidates = candidates
        self.last_context = None

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units, *, context=None):
        self.last_context = context
        return list(self._candidates)


class _NoopIndexBuilder:
    """只交付 Storage 的替身：IndexBuilder 是写入口，不交付则真源为空。"""

    def __init__(self, storage) -> None:
        self._storage = storage

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.INDEX_BUILDER

    @staticmethod
    def health() -> None:
        return None

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        for unit in units:
            self._storage.add(unit.scope, [unit])

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        for unit in units:
            self._storage.update(unit.scope, [unit])

    def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD):
        pass

    def rebuild(self):
        pass


def _make_unit(
    unit_id: str,
    content: str,
    *,
    scope: Scope = _DEFAULT_SCOPE,
    tier: MemoryTier = MemoryTier.EPISODIC,
    system_metadata: dict | None = None,
    t_ingest=None,
) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        tier=tier,
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=LifecycleState.ACTIVE,
        temporal=Temporal(t_ingest=t_ingest),
        system_metadata=dict(system_metadata or {}),
    )


def _index_related(unit: MemoryUnit, kv: KVStore, vector_store: VectorStore, embedder) -> None:
    """把一条已有 SEMANTIC 记忆写入 KV(/memory/) + 向量索引（供 dedup.recall 召回）。"""
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    vec = embedder.embed([unit.content])[0]
    vector_store.insert(
        unit.scope,
        [
            VectorRecord(
                id=f"{unit.id}-0",
                vector=vec,
                metadata={"unit_id": unit.id, "tier": unit.tier.value, "seq": "0"},
            )
        ],
    )


def _make_evolver(kv, vector_store, embedder, llm, extractor) -> OrchestratingEvolver:
    # tier_filter=False：让 EPISODIC 本轮 unit 能召回 SEMANTIC 相关记忆（跨 tier）
    storage = CompositeStorage(kv=kv, vector=vector_store)
    dedup = VectorDedup(storage=storage, embedder=embedder, tier_filter=False)
    return OrchestratingEvolver(
        extractor=extractor,
        abstractor=None,  # EXTRACT 不用
        associator=None,
        index_builder=_NoopIndexBuilder(storage),
        storage=storage,
        message_store=storage.raw_port(),
        dedup=dedup,
        llm=llm,
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestInferContextCollection:
    """evolver EXTRACT + infer=true 时内部收集 context。"""

    @staticmethod
    def test_raw_read_permission_denied_is_not_downgraded_to_append():
        """RawDataStore 拒绝读取时，Evolver 不应绕过授权继续追加原文。"""

        from jiuwen_memory.storage.security import StorageAction, StorageSecurity

        class _DenyRawReads(StorageSecurity):
            def authorize(self, access, scope, action, resource):
                if resource == "raw" and action is StorageAction.LIST:
                    raise PermissionDeniedError("raw list")

        kv = _MemoryKVStore()
        storage = CompositeStorage(kv=kv, security=_DenyRawReads())
        evolver = OrchestratingEvolver(
            extractor=None,
            abstractor=None,
            associator=None,
            index_builder=None,
            storage=storage,
            message_store=storage.raw_port(),
            dedup=None,
            llm=None,
        )
        unit = _make_unit("raw-auth-1", "受保护原文", system_metadata={"infer": "true"})

        with pytest.raises(PermissionDeniedError):
            evolver._persist_and_maintain_messages([unit])

        assert kv.scan(_DEFAULT_SCOPE, prefix=MESSAGES_KEY_PREFIX) == []

    @staticmethod
    def test_evolver_rejects_bare_kv_message_store():
        """裸 KV 不能在 Construction 侧重新适配成未授权的原文端口。"""
        storage = CompositeStorage(kv=_MemoryKVStore())

        with pytest.raises(TypeError, match="RawDataStore"):
            OrchestratingEvolver(
                extractor=None,
                abstractor=None,
                associator=None,
                index_builder=None,
                storage=storage,
                message_store=storage.kv,
                dedup=None,
                llm=None,
            )

    @staticmethod
    def test_non_infer_returns_none_context():
        """非 infer 的 EXTRACT → context=None，extractor 收到 None。"""
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        extractor = _ScriptedExtractor([_make_unit("ext-1", "新事实")])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        unit = _make_unit("u1", "普通消息")  # 无 metadata.infer
        evolver.evolve([unit], EvolveMode.EXTRACT)

        assert extractor.last_context is None

    @staticmethod
    def test_infer_collects_recent_originals_and_related():
        """infer=true → context 含 recent 原文和 related memories。"""
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        extractor = _ScriptedExtractor([_make_unit("ext-1", "新事实")])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        # 预置一条已索引的相关记忆（/memory/，供 dedup.recall 召回）
        # 用与本轮相同文本（HashEmbedder 同文本→cosine=1.0，确保超过 min_similarity 召回）
        related = _make_unit("rel-1", "用户偏好 Python 编程", tier=MemoryTier.SEMANTIC)
        _index_related(related, stores["kv"], stores["vector"], plugins["embedder"])

        # 预置一条历史 infer 原文（/messages/，规约后的 MemoryUnit）
        hist = _make_unit(
            "hist-1", "user: 之前聊过猫\nassistant: 嗯", system_metadata={"infer": "true"}
        )
        stores["kv"].insert(_DEFAULT_SCOPE, messages_key(hist.id), dumps(hist))

        # 本轮 infer unit（content 与 related 相同，便于召回）
        cur = _make_unit("cur-1", "用户偏好 Python 编程", system_metadata={"infer": "true"})
        evolver.evolve([cur], EvolveMode.EXTRACT)

        ctx = extractor.last_context
        assert ctx is not None
        # recent_originals：含历史原文，排除本轮自身
        assert len(ctx.recent_originals) == 1
        assert ctx.recent_originals[0].id == "hist-1"
        # related_memories：含召回的相关记忆
        assert len(ctx.related_memories) >= 1
        assert any(m.id == "rel-1" for m in ctx.related_memories)

    @staticmethod
    def test_recent_originals_excludes_current_and_sorts_by_occurred_at():
        """recent 按 occurred_at 降序取最近10条，排除本轮自身。"""
        from datetime import datetime, timezone

        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        extractor = _ScriptedExtractor([])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        # 三条历史原文，不同 t_ingest
        for i, ts in enumerate([3, 1, 2]):
            u = _make_unit(
                f"hist-{i}",
                f"msg-{i}",
                system_metadata={"infer": "true"},
                t_ingest=datetime(2026, 1, ts, tzinfo=timezone.utc),
            )
            stores["kv"].insert(_DEFAULT_SCOPE, messages_key(u.id), dumps(u))

        cur = _make_unit("cur", "本轮", system_metadata={"infer": "true"})
        evolver.evolve([cur], EvolveMode.EXTRACT)

        ctx = extractor.last_context
        assert ctx is not None
        ids = [u.id for u in ctx.recent_originals]
        assert ids == ["hist-0", "hist-2", "hist-1"]  # 按 t_ingest 降序（3,2,1）


class TestRelatedMemoriesDedup:
    """related_memories 经 prompt 告知大模型已有这些记忆、勿重复抽取；_dedup_batch 兜底。"""

    @staticmethod
    def test_related_memories_passed_to_extractor_context():
        """infer=true → related_memories 进 extractor 的 context，供 prompt 提示已有记忆。"""
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        extractor = _ScriptedExtractor([_make_unit("ext-1", "新事实")])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        # related 与本轮同文本（HashEmbedder 同文本→cosine=1.0，确保超过 min_similarity 召回）
        related = _make_unit("rel-1", "用户偏好 Python 编程", tier=MemoryTier.SEMANTIC)
        _index_related(related, stores["kv"], stores["vector"], plugins["embedder"])

        cur = _make_unit("cur-1", "用户偏好 Python 编程", system_metadata={"infer": "true"})
        evolver.evolve([cur], EvolveMode.EXTRACT)

        ctx = extractor.last_context
        assert ctx is not None
        assert any(m.id == "rel-1" for m in ctx.related_memories)

    @staticmethod
    def test_duplicate_of_related_memory_noop_via_dedup():
        """候选与已有记忆同文本 → _dedup_batch 召回判 NOOP → 无新增。

        去重不靠 evolver 向量过滤（已移除），而靠 prompt 提示 + _dedup_batch 兜底。
        """
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        dup_candidate = _make_unit("c-dup", "用户偏好 Python", tier=MemoryTier.SEMANTIC)
        extractor = _ScriptedExtractor([dup_candidate])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        related = _make_unit("rel-1", "用户偏好 Python", tier=MemoryTier.SEMANTIC)
        _index_related(related, stores["kv"], stores["vector"], plugins["embedder"])

        cur = _make_unit("cur-1", "用户偏好 Python 编程", system_metadata={"infer": "true"})
        result = evolver.evolve([cur], EvolveMode.EXTRACT)

        # 候选与 related 同文本 → _dedup_batch 召回 related（cosine=1.0 ≥ high）判 NOOP → 无新增
        assert result.created_ids == []

    @staticmethod
    def test_keeps_new_fact_not_in_related():
        """候选与 related 不相似（不同文本）→ 保留 → ADD 落盘。"""
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        new_candidate = _make_unit("c-new", "用户在做数据库迁移", tier=MemoryTier.SEMANTIC)
        extractor = _ScriptedExtractor([new_candidate])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        related = _make_unit("rel-1", "用户偏好 Python", tier=MemoryTier.SEMANTIC)
        _index_related(related, stores["kv"], stores["vector"], plugins["embedder"])

        cur = _make_unit("cur-1", "用户在做数据库迁移", system_metadata={"infer": "true"})
        result = evolver.evolve([cur], EvolveMode.EXTRACT)

        assert "c-new" in result.created_ids

    @staticmethod
    def test_recent_originals_not_indexed_so_not_recalled():
        """原文（recent_originals）落 /messages/ 不建索引 → dedup.recall 召回不到 → 不参与去重。

        即便候选与原文文本相同，只要没有 related_memories 命中，候选经 _dedup_batch ADD 落盘。
        """
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        # 候选 content 与历史原文相同
        candidate = _make_unit("c-1", "我喜欢猫", tier=MemoryTier.SEMANTIC)
        extractor = _ScriptedExtractor([candidate])
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        # 历史原文（/messages/，无向量索引）——不会被 dedup.recall 召回
        hist = _make_unit("hist-1", "我喜欢猫", system_metadata={"infer": "true"})
        stores["kv"].insert(_DEFAULT_SCOPE, messages_key(hist.id), dumps(hist))

        cur = _make_unit("cur-1", "我在养猫", system_metadata={"infer": "true"})
        result = evolver.evolve([cur], EvolveMode.EXTRACT)

        # 原文不参与去重 → 候选保留 → ADD 落盘
        assert "c-1" in result.created_ids


class TestMessagesUpsertOnRetry:
    """_add_messages upsert：extract 失败后重试同一 unit.id 不撞 KV insert 拒重。"""

    @staticmethod
    def test_evolve_retry_after_extract_failure_does_not_raise_conflict():
        class _FailOnceExtractor(Extractor):
            def __init__(self) -> None:
                self.calls = 0

            def operator_type(self) -> OperatorType:
                return OperatorType.EXTRACTOR

            def health(self) -> None:
                return None

            def extract(self, units, *, context=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("inject failure")
                return []

        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        extractor = _FailOnceExtractor()
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )
        unit = _make_unit(
            "u-retry-1",
            "alice FAIL_INJECT",
            system_metadata={"infer": "true", "middle": "true"},
        )
        with pytest.raises(RuntimeError, match="inject failure"):
            evolver.evolve([unit], EvolveMode.EXTRACT)
        message_store = getattr(evolver, "_message_store")
        assert any(
            record.id == unit.id for record in message_store.list_raw(_DEFAULT_SCOPE, limit=100)
        )

        result = evolver.evolve([unit], EvolveMode.EXTRACT)
        assert result.created_ids == []
        assert extractor.calls == 2


class TestEngineInferPersist:
    """engine.write(infer=true)：原文以规约后的 MemoryUnit 落 /messages/（由 evolver 落盘）。"""

    @staticmethod
    def test_infer_original_persisted_as_memoryunit_under_messages():
        """infer 原文落 /messages/{id}，存 MemoryUnit 字节（不建索引，不落 /memory/）。

        原文落盘在 evolver 内部（_persist_and_maintain_messages），故用真实 OrchestratingEvolver。
        """
        from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
        from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
            PassthroughNormalizer,
        )
        from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor
        from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
        from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        storage = CompositeStorage(kv=stores["kv"], vector=stores["vector"])
        dedup = VectorDedup(storage=storage, embedder=_HashEmbedder(), tier_filter=False)
        extractor = KeywordExtractor(RecursiveChunker(chunk_size_chars=50, overlap_chars=10))
        evolver = OrchestratingEvolver(
            extractor=extractor,
            abstractor=None,
            associator=None,
            index_builder=_NoopIndexBuilder(storage),
            storage=storage,
            message_store=storage.raw_port(),
            dedup=dedup,
            llm=_MockLLM(),
        )

        class _NoopIndex:
            def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD):
                pass

            def rebuild(self):
                pass

        engine = InMemoryEngine(
            ingestor=SimpleIngestor(PassthroughNormalizer()),
            index_builder=_NoopIndex(),
            retriever=None,
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            scheduler=None,
            evolver=evolver,
            lifecycle=None,
        )
        asyncio.run(
            engine.write(
                "user: 你好\nassistant: 你好",
                _DEFAULT_SCOPE,
                system_metadata={"infer": "true"},
            )
        )

        # /messages/ 下应有 1 条 MemoryUnit（规约后字节，由 evolver 落盘）
        msgs = stores["kv"].scan(_DEFAULT_SCOPE, prefix=MESSAGES_KEY_PREFIX)
        assert len(msgs) == 1, f"/messages/ 应有 1 条原文，实际 {len(msgs)}"
        unit = loads(msgs[0][1])
        assert unit is not None
        assert unit.system_metadata.get("infer") == "true"
        assert "你好" in unit.content
        # 派生记忆落 /memory/（真实抽取会产派生 chunk，这里不验派生数量）


# ---------------------------------------------------------------------------
# 过程记忆抽取（procedural=true）
# ---------------------------------------------------------------------------


class _ProceduralExtractor(Extractor):
    """Mock Extractor：procedural 模式产 1 条 PROCEDURAL（记录 context 入参）。"""

    def __init__(self, procedural_unit: MemoryUnit) -> None:
        self._procedural_unit = procedural_unit
        self.last_context = "UNSET"  # procedural 应收到 None

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units, *, context=None):
        self.last_context = context
        return [self._procedural_unit]


class TestProceduralExtract:
    """procedural=true：1 条 PROCEDURAL 汇总，原文不落 KV，不走 dedup、不收 context。"""

    @staticmethod
    def test_procedural_skips_context_and_dedup_persists_one():
        """procedural → context=None、不经 _dedup_batch、1 条直接落 /memory/。"""
        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        proc_unit = _make_unit(
            "proc-1",
            "目标：查询订单；步骤：调订单 API；结果：返回订单列表",
            tier=MemoryTier.PROCEDURAL,
        )
        extractor = _ProceduralExtractor(proc_unit)
        evolver = _make_evolver(
            stores["kv"], stores["vector"], plugins["embedder"], plugins["llm"], extractor
        )

        # 预置一条已有记忆（若走 dedup.recall 可能召回——procedural 不应走）
        related = _make_unit("rel-1", "目标：查询订单", tier=MemoryTier.SEMANTIC)
        _index_related(related, stores["kv"], stores["vector"], plugins["embedder"])

        cur = _make_unit(
            "cur-1", "user: 查下订单\nassistant: 已返回列表", system_metadata={"procedural": "true"}
        )
        result = evolver.evolve([cur], EvolveMode.EXTRACT)

        # procedural 收到 context=None（不收集）
        assert extractor.last_context is None
        # 1 条 PROCEDURAL 直接落 /memory/（不经 dedup，即使与 related 相近也不 NOOP）
        assert result.created_ids == ["proc-1"]
        unit = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("proc-1")))
        assert unit.tier == MemoryTier.PROCEDURAL

    @staticmethod
    def test_procedural_original_not_in_kv():
        """procedural 原文不落 KV（/messages/ 和 /memory/ 都无原文）。"""
        from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
            PassthroughNormalizer,
        )
        from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
        from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}

        # evolver 用 keyword_extractor（procedural 降级产 1 条原文 PROCEDURAL）
        from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
        from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor

        storage = CompositeStorage(kv=stores["kv"], vector=stores["vector"])
        dedup = VectorDedup(storage=storage, embedder=_HashEmbedder(), tier_filter=False)
        extractor = KeywordExtractor(RecursiveChunker(chunk_size_chars=50, overlap_chars=10))
        # keyword_extractor 构造需 chunker + normalizer？看签名——只 chunker
        evolver = OrchestratingEvolver(
            extractor=extractor,
            abstractor=None,
            associator=None,
            index_builder=_NoopIndexBuilder(storage),
            storage=storage,
            message_store=storage.raw_port(),
            dedup=dedup,
            llm=_MockLLM(),
        )

        class _NoopIndex:
            def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD):
                pass

            def rebuild(self):
                pass

        engine = InMemoryEngine(
            ingestor=SimpleIngestor(PassthroughNormalizer()),
            index_builder=_NoopIndex(),
            retriever=None,
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            scheduler=None,
            evolver=evolver,
            lifecycle=None,
        )
        derived = asyncio.run(
            engine.write(
                "user: 帮我查订单\nassistant: 已返回",
                _DEFAULT_SCOPE,
                system_metadata={"procedural": "true"},
            )
        )

        # 产 1 条 PROCEDURAL 派生
        assert len(derived) == 1
        assert derived[0].tier == MemoryTier.PROCEDURAL
        # procedural 原文不落 KV → source_ref 不指向任何记录，保持默认空串（不误导溯源）
        assert derived[0].source_ref == ""
        # 原文不落 KV：/messages/ 与 /memory/ 都无 cur 原文（只有派生 proc 在 /memory/）
        msgs = stores["kv"].scan(_DEFAULT_SCOPE, prefix=MESSAGES_KEY_PREFIX)
        assert msgs == [], "procedural 原文不应落 /messages/"

    @staticmethod
    def test_procedural_with_infer_still_no_original_in_kv():
        """procedural=true 且 infer=true 同传时，按 procedural 语义——原文不落 /messages/。

        评审 P2：原实现 `if infer:` 落 /messages/，procedural+infer 同传时违反
        「procedural 原文不落 KV」契约。修正为 `if infer and not procedural:`。
        """
        from jiuwen_memory.common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
        from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
            PassthroughNormalizer,
        )
        from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor
        from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
        from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

        stores = {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}
        storage = CompositeStorage(kv=stores["kv"], vector=stores["vector"])
        dedup = VectorDedup(storage=storage, embedder=_HashEmbedder(), tier_filter=False)
        extractor = KeywordExtractor(RecursiveChunker(chunk_size_chars=50, overlap_chars=10))
        evolver = OrchestratingEvolver(
            extractor=extractor,
            abstractor=None,
            associator=None,
            index_builder=_NoopIndexBuilder(storage),
            storage=storage,
            message_store=storage.raw_port(),
            dedup=dedup,
            llm=_MockLLM(),
        )

        class _NoopIndex:
            def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
                pass

            def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD):
                pass

            def rebuild(self):
                pass

        engine = InMemoryEngine(
            ingestor=SimpleIngestor(PassthroughNormalizer()),
            index_builder=_NoopIndex(),
            retriever=None,
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            scheduler=None,
            evolver=evolver,
            lifecycle=None,
        )
        asyncio.run(
            engine.write(
                "user: 查订单\nassistant: 已返回",
                _DEFAULT_SCOPE,
                system_metadata={"procedural": "true", "infer": "true"},
            )
        )

        # procedural 优先：原文不落 /messages/（即使 infer=true）
        msgs = stores["kv"].scan(_DEFAULT_SCOPE, prefix=MESSAGES_KEY_PREFIX)
        assert msgs == [], "procedural+infer 同传时原文仍不应落 /messages/"


class TestProceduralSourceRef:
    """procedural 产出 source_ref 为空（原文不落 KV，ref 不指向不存在的记录）。"""

    @staticmethod
    def test_llm_extractor_procedural_source_ref_empty():
        """llm_extractor 的 procedural 产出 source_ref 为空（与 keyword 对齐）。"""
        import json as _json

        from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl

        # MockLLM 返回 procedural JSON（content 字段）
        class _ProcLLM(LLM):
            @staticmethod
            def plugin_type():
                return PluginType.LLM

            @staticmethod
            def health():
                return None

            def chat(self, messages, **options):
                return _json.dumps({"content": "目标:查单;步骤:调API;结果:返回列表"})

        extractor = ExtractorImpl(
            llm=_ProcLLM(),
            min_confidence=0.5,
        )
        source = MemoryUnit(
            id="cur-1",
            scope=Scope(org="t", user="u"),
            segments=[Segment(content="user: 查单\nassistant: 已返", source=Modality.TEXT)],
            lifecycle=LifecycleState.ACTIVE,
            temporal=Temporal(),
            system_metadata={"procedural": "true"},
        )
        result = extractor.extract([source])
        assert len(result) == 1
        assert result[0].tier.value == "procedural"
        assert result[0].source_ref == "", "llm procedural source_ref 应为空（原文不落 KV）"
        assert result[0].provenance == ["cur-1"]
