"""集成测试：Milvus(向量) + nano-graphrag(图) 融合的 FusionStore。

验证「构建时同时建图与向量索引」「检索时向量召回后再经图扩展邻居」，以及 CRUD
一致性、scope 隔离、边界。需要真实 Milvus（默认 ``http://localhost:19530``，
``AGENT_MEMORY_TEST_MILVUS_URI`` 覆盖）与 nano-graphrag；不可用时整组 skip。每个用例用
独立 collection + working_dir 隔离，teardown 删 collection。
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope
from jiuwen_memory.storage import StoreType
from jiuwen_memory.storage.fusion import FusionProducer
from jiuwen_memory.storage.fusion_impl.milvus_graph_fusion import MilvusGraphFusionStore
from jiuwen_memory.storage.types import FusionQuery, FusionRecord

MILVUS_URI = os.getenv("AGENT_MEMORY_TEST_MILVUS_URI", "http://localhost:19530")
DIM = 4
SCOPE = Scope(org="itest", user="u1")


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    v = [0.0] * DIM
    for idx, val in nonzero:
        v[idx] = val
    return v


@pytest.fixture
def fusion_factory(tmp_path):
    pytest.importorskip("pymilvus")
    created: list[tuple] = []

    def make(**kw):
        coll = f"ftest_{uuid.uuid4().hex[:10]}"
        store = MilvusGraphFusionStore(
            working_dir=str(tmp_path / coll),
            dim=DIM,
            milvus={"uri": MILVUS_URI, "collection": coll},
            **kw,
        )
        try:
            store.health()
            _ = getattr(store, "_vec").client  # 触发连接 + 建表
        except Exception as exc:
            pytest.skip(f"milvus/nano-graphrag unavailable: {exc}")
        created.append((store, coll))
        return store

    yield make
    for store, coll in created:
        with suppress(Exception):
            getattr(store, "_vec").client.drop_collection(coll)


def _ids(hits) -> set[str]:
    return {h.id for h in hits}


# --------------------------------------------------------------- 工厂
def test_fusion_registered_and_routes(tmp_path):
    assert "milvus_graph" in FusionProducer.known()
    store = MilvusGraphFusionStore(
        working_dir=str(tmp_path),
        dim=DIM,
        milvus={"uri": MILVUS_URI, "collection": f"x_{uuid.uuid4().hex[:8]}"},
    )
    assert isinstance(store, MilvusGraphFusionStore)
    assert store.store_type() is StoreType.FUSION


# --------------------------------------------------------------- 构建 + 正排
def test_fusion_build_and_get_roundtrip(fusion_factory):
    f = fusion_factory()
    f.insert(
        SCOPE,
        [
            FusionRecord(
                id="a",
                vector=_vec((0, 1.0)),
                text="alpha",
                scalars={"kind": "root", "links": ["b"]},
                value=b"\x00\x01BIN",
            ),
            FusionRecord(id="b", vector=_vec((1, 1.0)), text="beta", scalars={"kind": "leaf"}),
        ],
    )
    got = {r.id: r for r in f.get(SCOPE, ["a", "b", "missing"])}
    assert set(got) == {"a", "b"}
    assert got["a"].text == "alpha"
    assert got["a"].value == b"\x00\x01BIN"  # 原始字节正排回读
    assert got["a"].scalars["kind"] == "root"
    assert got["a"].scalars["links"] == ["b"]
    assert f.get(SCOPE, []) == []


# --------------------------------------------------------------- 向量→图检索
def test_fusion_search_vector_then_graph_neighbors(fusion_factory):
    f = fusion_factory()  # neighbor_depth=1
    f.insert(
        SCOPE,
        [
            FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"links": ["b", "c"]}),
            FusionRecord(id="b", vector=_vec((1, 1.0))),
            FusionRecord(id="c", vector=_vec((2, 1.0))),
            FusionRecord(id="z", vector=_vec((3, 1.0))),  # 与 a 无关联
        ],
    )
    res = f.search(SCOPE, FusionQuery(vector=_vec((0, 1.0)), top_k=1))
    assert _ids(res) == {"a", "b", "c"}  # 种子 a + 其邻居 b,c
    assert "z" not in _ids(res)
    by_id = {r.id: r.score for r in res}
    assert by_id["a"] > by_id["b"] and by_id["a"] > by_id["c"]  # 种子分高于衰减邻居
    assert [r.score for r in res] == sorted((r.score for r in res), reverse=True)


def test_fusion_search_multi_hop_depth(fusion_factory):
    f = fusion_factory(neighbor_depth=2)
    f.insert(
        SCOPE,
        [
            FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"links": ["b"]}),
            FusionRecord(id="b", vector=_vec((1, 1.0)), scalars={"links": ["c"]}),
            FusionRecord(id="c", vector=_vec((2, 1.0))),
        ],
    )
    res = f.search(SCOPE, FusionQuery(vector=_vec((0, 1.0)), top_k=1))
    assert _ids(res) == {"a", "b", "c"}  # a(种子) -> b(1跳) -> c(2跳)
    by_id = {r.id: r.score for r in res}
    assert by_id["a"] > by_id["b"] > by_id["c"]  # 跳数越远分越低


def test_fusion_scalar_filter_on_vector_phase(fusion_factory):
    f = fusion_factory()
    f.insert(
        SCOPE,
        [
            FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"kind": "root"}),  # 孤立、无边
            FusionRecord(id="b", vector=_vec((0, 0.9)), scalars={"kind": "leaf", "links": ["c"]}),
            FusionRecord(id="c", vector=_vec((2, 1.0)), scalars={"kind": "leaf"}),
        ],
    )
    # 过滤只作用于向量种子相位：种子限定 kind=leaf -> b,c；图扩展 b<->c 不引入 a
    res = f.search(
        SCOPE,
        FusionQuery(
            vector=_vec((0, 1.0)),
            top_k=10,
            scalar_filters=[FilterClause("kind", FilterOp.EQ, "leaf")],
        ),
    )
    assert _ids(res) == {"b", "c"}
    assert "a" not in _ids(res)  # a 是 root（被过滤掉）且孤立，不会被图召回


# --------------------------------------------------------------- CRUD 语义
def test_fusion_insert_conflict(fusion_factory):
    f = fusion_factory()
    f.insert(SCOPE, [FusionRecord(id="a", vector=_vec((0, 1.0)))])
    with pytest.raises(ConflictError):
        f.insert(SCOPE, [FusionRecord(id="a", vector=_vec((1, 1.0)))])


def test_fusion_update_missing_raises(fusion_factory):
    f = fusion_factory()
    with pytest.raises(NotFoundError):
        f.update(SCOPE, [FusionRecord(id="ghost", vector=_vec((0, 1.0)))])


def test_fusion_update_reflected(fusion_factory):
    f = fusion_factory()
    f.insert(SCOPE, [FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"v": 1})])
    f.update(SCOPE, [FusionRecord(id="a", vector=_vec((1, 1.0)), text="new", scalars={"v": 2})])
    rec = f.get(SCOPE, ["a"])[0]
    assert rec.text == "new" and rec.scalars["v"] == 2
    assert _ids(f.search(SCOPE, FusionQuery(vector=_vec((1, 1.0)), top_k=1))) == {"a"}


def test_fusion_delete_clears_vector_and_graph(fusion_factory):
    f = fusion_factory()
    f.insert(
        SCOPE,
        [
            FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"links": ["b"]}),
            FusionRecord(id="b", vector=_vec((1, 1.0))),
        ],
    )
    f.delete(SCOPE, ["a"])
    f.delete(SCOPE, ["a"])  # 幂等
    assert f.get(SCOPE, ["a"]) == []
    # a 删后，near b 的检索不再把 a 当邻居召回
    res = f.search(SCOPE, FusionQuery(vector=_vec((1, 1.0)), top_k=1))
    assert _ids(res) == {"b"}


def test_fusion_requires_vector(fusion_factory):
    f = fusion_factory()
    with pytest.raises(ValidationError):
        f.insert(SCOPE, [FusionRecord(id="a", vector=None)])
    with pytest.raises(ValidationError):
        f.search(SCOPE, FusionQuery(vector=None))


def test_fusion_empty_inputs_are_noops(fusion_factory):
    f = fusion_factory()
    f.insert(SCOPE, [])
    f.update(SCOPE, [])
    f.delete(SCOPE, [])
    assert f.get(SCOPE, []) == []


# --------------------------------------------------------------- scope 隔离
def test_fusion_scope_isolation(fusion_factory):
    """逻辑 id 只在 Scope 内唯一；跨 Scope 同 id 的检索与正排互相隔离。"""
    f = fusion_factory()
    other = Scope(org="itest", user="u2")
    f.insert(
        SCOPE,
        [
            FusionRecord(id="a", vector=_vec((0, 1.0)), scalars={"links": ["b"]}),
            FusionRecord(id="b", vector=_vec((1, 1.0))),
        ],
    )
    f.insert(other, [FusionRecord(id="a", vector=_vec((0, 1.0)))])
    # 检索不跨 scope
    assert _ids(f.search(SCOPE, FusionQuery(vector=_vec((0, 1.0)), top_k=10))) == {"a", "b"}
    assert _ids(f.search(other, FusionQuery(vector=_vec((0, 1.0)), top_k=10))) == {"a"}
    # 同一逻辑 id 在各 Scope 独立存在，其他 id 不跨 Scope 可见。
    assert _ids(f.get(SCOPE, ["a", "b"])) == {"a", "b"}
    assert _ids(f.get(other, ["a", "b"])) == {"a"}
