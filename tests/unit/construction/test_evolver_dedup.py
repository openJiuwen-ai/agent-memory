"""去重决策测试：OrchestratingEvolver 的 _dedup_batch / _dedup_single / _llm_dedup_decide 方法。

覆盖 ADD / NOOP / UPDATE / SUPERSEDE 四种决策 + 降级场景。
使用内联轻量 Store / Plugin 实现，不依赖外部 API 或 conftest。
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    DedupDecision,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.abstractor import Abstractor
from jiuwen_memory.construction.associator import Associator
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.graph import GraphStore
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

# ---------------------------------------------------------------------------
# 内联轻量实现（避免依赖有 import 问题的 fixtures.py）
# ---------------------------------------------------------------------------


class _MemoryKVStore(KVStore):
    """纯内存 KVStore。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, bytes]] = {}

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        return None

    def _sk(self, scope: Scope) -> str:
        return f"{scope.org}/{scope.space}/{scope.user}/{scope.agent}/{scope.session}"

    def insert(self, scope, key, value, ttl=0.0):
        sk = self._sk(scope)
        b = self._data.setdefault(sk, {})
        if key in b:
            raise ConflictError("kv", key)
        b[key] = value

    def update(self, scope, key, value, ttl=0.0):
        sk = self._sk(scope)
        b = self._data.get(sk, {})
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
    """纯内存 VectorStore：暴力 cosine。"""

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
    """确定性 hash 向量（同文本 → cosine=1.0，不同文本 → cosine<1.0）。"""

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
    """Mock LLM：返回预定义字符串列表。"""

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
# 测试辅助
# ---------------------------------------------------------------------------

_DEFAULT_SCOPE = Scope(org="test", user="alice", agent="a1", session="s1")


class NoopExtractor(Extractor):
    """Mock Extractor：返回空列表（去重测试不依赖 Extractor 产出）。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units: list[MemoryUnit], *, context=None) -> list[MemoryUnit]:
        return []


class NoopAbstractor(Abstractor):
    """Mock Abstractor：返回空列表。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        return None

    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        return []


class NoopAssociator(Associator):
    """Mock Associator：返回空列表。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.ASSOCIATOR

    def health(self) -> None:
        return None

    def associate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        return []


class NoopIndexBuilder(IndexBuilder):
    """Mock IndexBuilder：只交付 Storage，不建派生索引。

    IndexBuilder 是记忆写入的唯一入口，替身必须交付 Storage，否则去重测试读不到
    真源；派生索引与去重判定无关，此处省略。
    """

    def __init__(self, storage) -> None:
        self._storage = storage

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        for unit in units:
            self._storage.add(unit.scope, [unit])

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        for unit in units:
            self._storage.update(unit.scope, [unit])

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        pass

    def rebuild(self) -> None:
        pass


class NoopGraphStore(GraphStore):
    """Mock GraphStore：什么都不做。"""

    def store_type(self):
        return StoreType.GRAPH

    def health(self) -> None:
        return None

    def seed_ids(self, scope, tokens):
        return []

    def insert(self, scope, nodes=None, edges=None):
        pass

    def update(self, scope, nodes=None, edges=None):
        pass

    def delete(self, scope, node_ids=None, edge_ids=None):
        pass

    def get(self, scope, node_ids):
        return []

    def search(self, scope, query):
        return []


def _make_evolver(
    kv: KVStore,
    vector_store: VectorStore,
    embedder: Embedder,
    llm: LLM,
    **dedup_kwargs,
) -> OrchestratingEvolver:
    """创建 OrchestratingEvolver 实例（注入 mock 算子）。

    去重召回侧由 VectorDedup 承担（向量召回），阈值拆分：min/top_k/tier/scope
    下沉 recaller，medium/high 留 evolver。
    """
    from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup

    recaller_kwargs = {
        "min_similarity": dedup_kwargs.get("dedup_min_similarity", 0.5),
        "top_k": dedup_kwargs.get("dedup_top_k", 5),
        "tier_filter": dedup_kwargs.get("dedup_tier_filter", True),
        "scope_filter": dedup_kwargs.get("dedup_scope_filter", True),
    }
    evolver_kwargs = {
        k: dedup_kwargs[k]
        for k in ("dedup_medium_similarity", "dedup_high_similarity")
        if k in dedup_kwargs
    }
    storage = CompositeStorage(kv=kv, vector=vector_store, graph=NoopGraphStore())
    dedup = VectorDedup(storage=storage, embedder=embedder, **recaller_kwargs)
    return OrchestratingEvolver(
        extractor=NoopExtractor(),
        abstractor=NoopAbstractor(),
        associator=NoopAssociator(),
        index_builder=NoopIndexBuilder(storage),
        storage=storage,
        message_store=storage.kv,
        dedup=dedup,
        llm=llm,
        **evolver_kwargs,
    )


def _create_stores():
    """创建内存 Store 实例。"""
    return {"kv": _MemoryKVStore(), "vector": _MemoryVectorStore()}


def _make_unit(
    unit_id: str,
    content: str,
    scope: Scope = _DEFAULT_SCOPE,
    tier: MemoryTier = MemoryTier.EPISODIC,
) -> MemoryUnit:
    """创建测试 MemoryUnit 并写入 KVStore + VectorStore。"""
    unit = MemoryUnit(
        id=unit_id,
        scope=scope,
        tier=tier,
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=LifecycleState.ACTIVE,
        temporal=Temporal(),
    )
    return unit


def _index_unit(
    unit: MemoryUnit,
    kv: KVStore,
    vector_store: VectorStore,
    embedder: Embedder,
) -> None:
    """将 unit 写入 KVStore + VectorStore（模拟 IndexBuilder.build 的效果）。"""
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    vector = embedder.embed([unit.content])[0]
    record = VectorRecord(
        id=f"{unit.id}-0",
        vector=vector,
        metadata={"unit_id": unit.id, "tier": unit.tier.value, "seq": "0"},
    )
    vector_store.insert(unit.scope, [record])


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------


class TestDedupAdd:
    """ADD 场景：无相似记忆或相似度低于阈值 → 直接落盘。"""

    @staticmethod
    def test_no_hits_returns_add():
        """VectorStore.search 返回空 → ADD（新事实）。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        assert decision == DedupDecision.ADD
        assert existing is None
        assert similarity == pytest.approx(0.0)

    @staticmethod
    def test_low_similarity_returns_add():
        """相似度低于 medium_threshold → ADD。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_min_similarity=0.3,
            dedup_medium_similarity=0.7,
        )

        # 先索引一条已有记忆
        existing_unit = _make_unit("e1", "Python GIL 机制详解")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        # 候选：与已有记忆不相似（hash embedder 下不同文本 cosine<1.0）
        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        # HashEmbedder 对不同文本产出不同向量，cosine 应低于 medium_threshold
        assert decision == DedupDecision.ADD

    @staticmethod
    def test_add_persisted_to_kv():
        """ADD 决策 → 候选落盘到 KVStore。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        candidates = [_make_unit("c1", "新事实")]
        result = getattr(evolver, "_dedup_batch")(candidates)

        assert result.created_ids == ["c1"]
        # 验证 KVStore 中有记录（evolver 落盘用 memory_key 前缀）
        unit = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("c1")))
        assert unit is not None
        assert unit.id == "c1"


class TestDedupNoop:
    """NOOP 场景：候选与已有记忆完全重叠 → 跳过。"""

    @staticmethod
    def test_high_similarity_llm_noop():
        """高相似度 + LLM 判定 noop → NOOP（不写库）。"""
        stores = _create_stores()
        # HashEmbedder：同文本 → cosine=1.0，不同文本 → cosine<1.0
        # 需要构造同文本场景来触发高相似度
        llm = _MockLLM(responses=[json.dumps({"decision": "noop", "reason": "完全相同"})])
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        # 先索引已有记忆
        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        # 候选：与已有记忆文本完全相同 → cosine=1.0 → LLM 判定 noop
        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        assert decision == DedupDecision.NOOP
        assert existing is not None
        assert similarity >= 0.85

    @staticmethod
    def test_noop_not_written_to_kv():
        """NOOP → 候选不写入 KVStore。"""
        stores = _create_stores()
        llm = _MockLLM(responses=[json.dumps({"decision": "noop", "reason": "完全相同"})])
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        candidates = [_make_unit("c1", "用户偏好简洁回答风格")]
        result = getattr(evolver, "_dedup_batch")(candidates)

        assert result.created_ids == []
        assert result.updated_ids == []
        assert result.superseded_ids == []
        # 候选 id 不在 KVStore 中（NOOP 不落盘）
        with pytest.raises(NotFoundError):
            stores["kv"].get(_DEFAULT_SCOPE, memory_key("c1"))


class TestDedupDirectNoopDelta:
    """高相似但有实质差异时，不走 direct_noop，改走 LLM。"""

    @staticmethod
    def test_high_similarity_month_change_routes_to_llm():
        stores = _create_stores()
        llm = _MockLLM(
            responses=[json.dumps({"decision": "supersede", "reason": "月份更新"})]
        )
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=0.9,
        )
        existing_unit = _make_unit("e1", "会议定于3月举行")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])
        candidate = _make_unit("c1", "会议定于5月举行")

        def fake_recall(unit: MemoryUnit):
            return [(existing_unit, 0.95)]

        setattr(getattr(evolver, "_dedup"), "recall", fake_recall)

        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        assert getattr(llm, "_call_count") == 1, "有实质差异时应走 LLM 而非 direct_noop"
        assert decision == DedupDecision.SUPERSEDE
        assert existing.id == "e1"
        assert similarity == pytest.approx(0.95)

    @staticmethod
    def test_high_similarity_month_change_routes_to_llm_in_batch():
        """_dedup_batch 路径：高相似但有月份差异 → 不入 direct_noop，改入 need_llm。"""
        stores = _create_stores()
        llm = _MockLLM(
            responses=[json.dumps({"decision": "supersede", "reason": "月份更新"})]
        )
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=0.9,
        )
        existing_unit = _make_unit("e1", "会议定于3月举行")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])
        candidate = _make_unit("c1", "会议定于5月举行")

        def fake_recall(unit: MemoryUnit):
            return [(existing_unit, 0.95)]

        setattr(getattr(evolver, "_dedup"), "recall", fake_recall)

        result = getattr(evolver, "_dedup_batch")([candidate])

        assert getattr(llm, "_call_count") == 1, "有实质差异时 batch 应走 LLM"
        assert result.superseded_ids == ["e1"]
        assert result.created_ids == ["c1"]
        assert result.updated_ids == []


class TestDedupSupersede:
    """SUPERSEDE 场景：新版替代旧版 → 新版落盘 + 旧版标记 SUPERSEDED。"""

    @staticmethod
    def test_supersede_marks_old_superseded():
        """SUPERSEDE → 新版落盘 + 旧版 lifecycle=SUPERSEDED。

        HashEmbedder 对同文本产出 cosine=1.0，不同文本 cosine 极低。
        所以用同文本触发高相似度，LLM 判定 supersede 来测试。
        """
        stores = _create_stores()
        llm = _MockLLM(responses=[json.dumps({"decision": "supersede", "reason": "新版更完整"})])
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        # 抬高 high 阈值（>1.0），让同文本 cosine=1.0 落入 medium~high 的 LLM 判定区间，
        # 强制走 mock LLM 判 supersede（否则 1.0≥high 会被短路成 NOOP 跳过 LLM）。
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=1.01,
        )

        # 先索引旧版记忆
        old_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(old_unit, stores["kv"], stores["vector"], plugins["embedder"])

        # 候选：与旧版**同文本** → cosine=1.0 → LLM 判定 supersede
        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        result = getattr(evolver, "_dedup_batch")([candidate])

        assert "c1" in result.created_ids
        assert "e1" in result.superseded_ids

        # 验证旧版 lifecycle 已变更
        old_from_kv = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("e1")))
        assert old_from_kv.lifecycle == LifecycleState.SUPERSEDED
        assert old_from_kv.temporal.t_invalid is not None

        # 验证新版已落盘
        new_from_kv = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("c1")))
        assert new_from_kv.supersedes == "e1"


class TestDedupUpdate:
    """UPDATE 场景：候选补充已有记忆 → LLM 合成新旧 content → 更新已有 unit。"""

    @staticmethod
    def test_update_merges_content():
        """UPDATE → 已有 unit 的 content 被更新为合成版本。

        HashEmbedder 同文本 → cosine=1.0，LLM 先判定 update 再 merge。
        """
        stores = _create_stores()
        merge_content = "用户偏好简洁回答风格，不喜欢冗长解释"
        llm = _MockLLM(
            responses=[
                json.dumps({"decision": "update", "reason": "候选补充了不喜欢冗长解释"}),
                merge_content,  # 第二次调用：merge content
            ]
        )
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        # 抬高 high 阈值（>1.0），让同文本 cosine=1.0 落入 LLM 判定区间走 update+merge，
        # 否则 1.0≥high 会被短路成 NOOP（merge_content 也就不会被调用）。
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=1.01,
        )

        # 先索引已有记忆
        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        # 候选：与已有记忆**同文本**触发 cosine=1.0 → LLM 判定 update
        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        result = getattr(evolver, "_dedup_batch")([candidate])

        assert "e1" in result.updated_ids
        assert "c1" not in result.created_ids

        # 验证已有 unit 的 content 已被更新
        updated = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("e1")))
        assert merge_content in updated.content

        # 验证 provenance 包含候选 id
        assert "c1" in updated.provenance

    @staticmethod
    def test_update_empty_merge_falls_back_to_concatenation():
        """UPDATE 但合并结果为空串 → 视同合并失败，降级拼接新旧内容（Issue #189）。

        LLM 输出抖动返回 200 + 空 content：降级路径产出拼接文本，
        真源不得被空串静默清空。
        """
        stores = _create_stores()
        llm = _MockLLM(
            responses=[
                json.dumps({"decision": "update", "reason": "候选补充信息"}),
                "",  # 第二次调用（merge content）：HTTP 200 但 content 为空
            ]
        )
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=1.01,
        )

        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        result = getattr(evolver, "_dedup_batch")([candidate])

        # UPDATE 照常执行，但 content 为降级拼接（非空串）
        assert result.updated_ids == ["e1"]
        assert result.created_ids == []

        # 真源内容 = 新旧拼接，未被空串覆写
        kept = loads(stores["kv"].get(_DEFAULT_SCOPE, memory_key("e1")))
        assert kept.content == "用户偏好简洁回答风格\n用户偏好简洁回答风格"


class TestDedupDegradation:
    """降级场景：Embedder/VectorStore/LLM 不可用时的行为。"""

    @staticmethod
    def test_embedder_failure_fallback_add():
        """Embedder 失败 → 全部 ADD（跳过去重）。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        candidate = _make_unit("c1", "测试内容")

        # 手动让 embedder 失败
        def failing_embed(texts):
            raise RuntimeError("Embedder unavailable")

        getattr(getattr(evolver, "_dedup"), "_embedder").embed = failing_embed

        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)
        assert decision == DedupDecision.ADD
        assert similarity == pytest.approx(0.0)

    @staticmethod
    def test_vector_store_failure_fallback_add():
        """VectorStore.search 失败 → 全部 ADD。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        candidate = _make_unit("c1", "测试内容")

        # 手动让 VectorStore.search 失败
        def failing_search(scope, query):
            raise RuntimeError("VectorStore unavailable")

        getattr(getattr(evolver, "_dedup"), "_vector_store").search = failing_search

        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)
        assert decision == DedupDecision.ADD

    @staticmethod
    def test_llm_failure_rule_based_fallback():
        """LLM 失败 → 按 cosine 阈值规则判定（高相似→NOOP，中相似→SUPERSEDE）。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=0.85,
            dedup_medium_similarity=0.7,
        )

        # 索引已有记忆
        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        # 手动让 LLM 失败
        def failing_chat(messages, **options):
            raise RuntimeError("LLM unavailable")

        getattr(evolver, "_llm").chat = failing_chat

        # 候选与已有记忆文本相同 → cosine=1.0 ≥ high_threshold → 降级判定 NOOP
        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        assert decision == DedupDecision.NOOP
        assert similarity >= 0.85

    @staticmethod
    def test_llm_json_parse_failure_fallback_add():
        """LLM 返回非 JSON → 降级为 ADD。"""
        stores = _create_stores()
        llm = _MockLLM(responses=["I think this is a duplicate"])  # 非 JSON
        plugins = {"embedder": _HashEmbedder(), "llm": llm}
        # 抬高 high 阈值（>1.0），让同文本 cosine=1.0 落入 LLM 判定区间，
        # 才会真的调用 LLM 并触发"返回非 JSON → fallback ADD"路径；
        # 否则 1.0≥high 直接短路 NOOP，LLM 根本不被调用，测不到降级。
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
            dedup_high_similarity=1.01,
        )

        # 索引已有记忆
        existing_unit = _make_unit("e1", "用户偏好简洁回答风格")
        _index_unit(existing_unit, stores["kv"], stores["vector"], plugins["embedder"])

        candidate = _make_unit("c1", "用户偏好简洁回答风格")
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        # LLM 返回非 JSON → fallback ADD
        assert decision == DedupDecision.ADD


class TestDedupSelfFilter:
    """候选自身过滤：候选自身的向量记录不应被召回。"""

    @staticmethod
    def test_self_vectors_filtered_from_hits():
        """候选自身的 chunk 向量记录被过滤掉 → 不误判为 NOOP。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}
        evolver = _make_evolver(
            stores["kv"],
            stores["vector"],
            plugins["embedder"],
            plugins["llm"],
        )

        # 先索引候选自身的向量记录（模拟候选已被写入 VectorStore）
        candidate = _make_unit("c1", "测试内容")
        vector = plugins["embedder"].embed([candidate.content])[0]
        record = VectorRecord(
            id=f"{candidate.id}-0",
            vector=vector,
            metadata={"unit_id": candidate.id, "tier": candidate.tier.value},
        )
        stores["vector"].insert(candidate.scope, [record])

        # 候选自身的向量会被过滤掉（因为 id 以 "c1-" 开头）
        decision, existing, similarity = getattr(evolver, "_dedup_single")(candidate)

        assert decision == DedupDecision.ADD
        assert existing is None


class TestDedupMiddleFilter:
    """中期记忆过滤：dedup.recall 不召回 metadata.middle=true 的命中 unit。

    场景：Engine.write middle=true 把原文落 /memory/ + tier=WORKING + 立即建索引
    （让原文可召回）。后续 evolver EXTRACT 派生 candidate 走 _dedup_batch，
    dedup.recall 会召回与派生语义接近的中期原文——派生本就是从原文抽取的
    事实陈述，语义必然接近 → LLM dedup 判 NOOP → 派生丢失。

    修复：dedup.recall 聚合阶段过滤 metadata.middle=true 命中——中期原文是
    "待 MiddleToLongJob 处理的缓冲态输入"，不进 dedup 对照池，dedup 只查
    "派生是否与已沉淀长期记忆重复"。
    """

    @staticmethod
    def test_middle_marked_unit_not_in_recall_hits():
        """中期原文（metadata.middle=true）不应进 dedup.recall 的命中列表。"""
        from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup

        stores = _create_stores()
        embedder = _HashEmbedder()
        dedup = VectorDedup(
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            embedder=embedder,
            min_similarity=0.0,  # 不过滤低分，便于断言中期原文是否被召回
            top_k=10,
            tier_filter=False,
            scope_filter=False,
        )

        # 中期原文（被打了 middle=true 标记——Engine.write middle 路径的行为）
        middle_unit = _make_unit("mid-1", "dave enjoys hiking on weekends")
        middle_unit.system_metadata["middle"] = "true"
        _index_unit(middle_unit, stores["kv"], stores["vector"], embedder)

        # 派生 candidate——语义接近中期原文（同人物 + 同事件）
        # 真实 LLM extractor 从原文抽取派生，措辞必然高度重叠
        candidate = _make_unit("c1", "Dave likes to go hiking on weekends.")

        hits = dedup.recall(candidate)

        # 关键断言：中期原文 mid-1 不应出现在 hits 里——被 metadata.middle=true 过滤
        hit_ids = {u.id for u, _ in hits}
        assert "mid-1" not in hit_ids, (
            f"中期原文 mid-1 不应进 dedup 命中——派生与其源原文不应判重，"
            f"got hits={hit_ids}"
        )

    @staticmethod
    def test_long_term_unit_still_in_recall_hits():
        """长期记忆（无 middle 标记）仍应正常进 dedup.recall 命中——修复不应误伤。"""
        from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup

        stores = _create_stores()
        embedder = _HashEmbedder()
        dedup = VectorDedup(
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            embedder=embedder,
            min_similarity=0.0,
            top_k=10,
            tier_filter=False,
            scope_filter=False,
        )

        # 长期记忆（无 middle 标记——已沉淀的派生记忆）
        long_unit = _make_unit("long-1", "Dave likes to go hiking on weekends.")
        _index_unit(long_unit, stores["kv"], stores["vector"], embedder)

        # 派生 candidate——与长期记忆文本完全相同 → 应触发 NOOP（这才是真重复）
        candidate = _make_unit("c1", "Dave likes to go hiking on weekends.")
        hits = dedup.recall(candidate)

        hit_ids = {u.id for u, _ in hits}
        assert "long-1" in hit_ids, (
            f"长期记忆 long-1 应正常进 dedup 命中——这才是要查的真重复，"
            f"got hits={hit_ids}"
        )

    @staticmethod
    def test_keyword_dedup_filters_middle_marked_unit():
        """KeywordDedup 同样应过滤中期记忆——与 VectorDedup 行为一致。"""
        from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
            WhitespaceTokenizer,
        )
        from jiuwen_memory.construction.dedup_impl.keyword_dedup import KeywordDedup
        from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import (
            InMemoryFulltextStore,
        )
        from jiuwen_memory.storage.types import Document

        kv = _MemoryKVStore()
        fulltext = InMemoryFulltextStore(tokenizer=WhitespaceTokenizer())
        dedup = KeywordDedup(
            storage=CompositeStorage(kv=kv, fulltext=fulltext),
            min_similarity=0.0,
            top_k=10,
            tier_filter=False,
            scope_filter=False,
        )

        # 中期原文
        middle_unit = _make_unit("mid-1", "dave enjoys hiking on weekends")
        middle_unit.system_metadata["middle"] = "true"
        kv.insert(middle_unit.scope, memory_key(middle_unit.id), dumps(middle_unit))
        fulltext.insert(
            middle_unit.scope,
            [Document(id=middle_unit.id, text=middle_unit.content)],
        )

        # 派生 candidate
        candidate = _make_unit("c1", "Dave likes to go hiking on weekends.")
        hits = dedup.recall(candidate)

        hit_ids = {u.id for u, _ in hits}
        assert "mid-1" not in hit_ids, (
            f"KeywordDedup 也应过滤中期原文 mid-1，got hits={hit_ids}"
        )


class TestDedupEvolveExtract:
    """EXTRACT 模式集成：Extractor 产出 → 去重 → EvolveResult。"""

    @staticmethod
    def test_extract_with_dedup():
        """EXTRACT 模式完整流程：extract → dedup → result。"""
        stores = _create_stores()
        plugins = {"embedder": _HashEmbedder(), "llm": _MockLLM()}

        # 使用真实 MockExtractor（返回候选列表）
        class SimpleExtractor(Extractor):
            def operator_type(self) -> OperatorType:
                return OperatorType.EXTRACTOR

            def health(self) -> None:
                return None

            def extract(self, units, *, context=None):
                return [
                    _make_unit("ext-1", "用户偏好简洁回答风格"),
                    _make_unit("ext-2", "Python 的 GIL 机制"),
                ]

        from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup

        dedup = VectorDedup(
            storage=CompositeStorage(kv=stores["kv"], vector=stores["vector"]),
            embedder=plugins["embedder"],
        )
        storage = CompositeStorage(
            kv=stores["kv"], vector=stores["vector"], graph=NoopGraphStore()
        )
        evolver = OrchestratingEvolver(
            extractor=SimpleExtractor(),
            abstractor=NoopAbstractor(),
            associator=NoopAssociator(),
            index_builder=NoopIndexBuilder(storage),
            storage=storage,
            message_store=storage.kv,
            dedup=dedup,
            llm=plugins["llm"],
        )

        # 输入两条原始 unit
        input_units = [
            _make_unit("u1", "用户讨论了简洁回答风格"),
            _make_unit("u2", "用户讨论了 Python GIL"),
        ]

        result = evolver.evolve(input_units, EvolveMode.EXTRACT)

        # Extractor 产出的两条候选走去重，无已有记忆 → 全部 ADD
        assert len(result.created_ids) == 2
        assert "ext-1" in result.created_ids
        assert "ext-2" in result.created_ids
