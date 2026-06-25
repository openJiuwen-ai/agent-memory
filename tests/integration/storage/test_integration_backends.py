"""真实后端集成测试：Redis(kv) 与 Milvus(vector)。

覆盖「多轮增删改查后的数据一致性」与「边界/稳定性」场景。需要真实后端：

- Redis：默认 ``localhost:6300``（``AGENT_MEMORY_TEST_REDIS_PORT`` 覆盖）
- Milvus：默认 ``http://localhost:19530``（``AGENT_MEMORY_TEST_MILVUS_URI`` 覆盖）

后端不可达时整组自动 skip；每个用例用唯一 scope / collection 隔离并自清理，
互不污染、可重复运行。
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from common.errors import ConflictError, NotFoundError
from common.type_def import FilterClause, FilterOp, Scope
from storage._support import scope_segments
from storage.kv_impl.redis_kv import RedisKVStore
from storage.types import VectorQuery, VectorRecord
from storage.vector_impl.milvus_vector import MilvusVectorStore

REDIS_PORT = int(os.getenv("AGENT_MEMORY_TEST_REDIS_PORT", "6300"))
MILVUS_URI = os.getenv("AGENT_MEMORY_TEST_MILVUS_URI", "http://localhost:19530")


# ============================================================ Redis / KVStore
@pytest.fixture
def kv():
    """连到真实 Redis 的 KV store + 唯一 scope；teardown 清掉该 scope 命名空间。"""
    pytest.importorskip("redis")
    store = RedisKVStore(host="localhost", port=REDIS_PORT)
    try:
        store.health()
    except Exception as exc:
        pytest.skip(f"redis unreachable on :{REDIS_PORT}: {exc}")
    scope = Scope(org="itest", user=uuid.uuid4().hex)
    yield store, scope
    prefix = ":".join(scope_segments(scope)) + ":*"
    for key in store.client.scan_iter(match=prefix):
        store.client.delete(key)


def test_kv_full_crud_lifecycle(kv):
    store, scope = kv
    store.insert(scope, "k", b"v1")
    assert store.exists(scope, "k") is True
    assert store.get(scope, "k") == b"v1"
    store.update(scope, "k", b"v2")
    assert store.get(scope, "k") == b"v2"
    store.delete(scope, "k")
    assert store.exists(scope, "k") is False
    with pytest.raises(NotFoundError):
        store.get(scope, "k")


def test_kv_insert_conflict_keeps_original(kv):
    store, scope = kv
    store.insert(scope, "k", b"first")
    with pytest.raises(ConflictError):
        store.insert(scope, "k", b"second")
    assert store.get(scope, "k") == b"first"  # 冲突不改原值


def test_kv_update_missing_raises(kv):
    store, scope = kv
    with pytest.raises(NotFoundError):
        store.update(scope, "ghost", b"x")


def test_kv_delete_idempotent(kv):
    store, scope = kv
    store.insert(scope, "k", b"x")
    store.delete(scope, "k")
    store.delete(scope, "k")  # 再删不报错
    assert store.exists(scope, "k") is False


def test_kv_scope_isolation(kv):
    store, scope = kv
    other = Scope(org="itest", user=uuid.uuid4().hex)
    try:
        store.insert(scope, "shared", b"mine")
        store.insert(other, "shared", b"theirs")  # 同名 key 不冲突
        assert store.get(scope, "shared") == b"mine"
        assert store.get(other, "shared") == b"theirs"
        store.delete(scope, "shared")
        assert store.exists(scope, "shared") is False
        assert store.get(other, "shared") == b"theirs"  # 互不影响
    finally:
        store.delete(other, "shared")


def test_kv_many_ops_consistency(kv):
    """50 次插入 → 改偶数项 → 删 5 的倍数项 → 校验最终态逐键一致。"""
    store, scope = kv
    n = 50
    for i in range(n):
        store.insert(scope, f"k{i}", f"v{i}".encode())
    for i in range(0, n, 2):
        store.update(scope, f"k{i}", f"v{i}-upd".encode())
    deleted = set(range(0, n, 5))
    for i in deleted:
        store.delete(scope, f"k{i}")

    for i in range(n):
        if i in deleted:
            assert not store.exists(scope, f"k{i}")
            with pytest.raises(NotFoundError):
                store.get(scope, f"k{i}")
        else:
            expected = f"v{i}-upd".encode() if i % 2 == 0 else f"v{i}".encode()
            assert store.get(scope, f"k{i}") == expected


def test_kv_binary_and_empty_and_large_values(kv):
    store, scope = kv
    payloads = {
        "binary": bytes(range(256)),  # 非 UTF-8 全字节
        "empty": b"",  # 空值
        "large": b"x" * (1 << 20),  # 1MB
        "newlines": b"a\r\nb\x00c",  # 控制字符
    }
    for key, val in payloads.items():
        store.insert(scope, key, val)
    for key, val in payloads.items():
        assert store.get(scope, key) == val


def test_kv_ttl_expires(kv):
    store, scope = kv
    store.insert(scope, "ephemeral", b"x", ttl=1.0)
    assert store.exists(scope, "ephemeral") is True
    time.sleep(1.3)
    assert store.exists(scope, "ephemeral") is False
    with pytest.raises(NotFoundError):
        store.get(scope, "ephemeral")


def test_kv_update_refreshes_ttl_to_persistent(kv):
    store, scope = kv
    store.insert(scope, "k", b"x", ttl=1.0)
    store.update(scope, "k", b"y")  # ttl=0 → 转永久
    time.sleep(1.3)
    assert store.get(scope, "k") == b"y"  # 未过期


# ========================================================== Milvus / VectorStore
DIM = 8


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    v = [0.0] * DIM
    for idx, val in nonzero:
        v[idx] = val
    return v


@pytest.fixture
def vec():
    """连到真实 Milvus 的 vector store（唯一 collection）+ scope；teardown 删 collection。"""
    pytest.importorskip("pymilvus")
    collection = f"itest_{uuid.uuid4().hex[:12]}"
    store = MilvusVectorStore(uri=MILVUS_URI, collection=collection, dim=DIM, metric_type="COSINE")
    try:
        store.health()
        _ = store.client  # 触发连接 + 建表
    except Exception as exc:
        pytest.skip(f"milvus unreachable on {MILVUS_URI}: {exc}")
    scope = Scope(org="itest", user=uuid.uuid4().hex)
    yield store, scope
    try:
        store.client.drop_collection(collection)
    except Exception:  # 清理尽力而为
        pass


def test_vec_full_crud_lifecycle(vec):
    store, scope = vec
    rec = VectorRecord(id="a", vector=_vec((0, 1.0)), metadata={"color": "red"})
    store.insert(scope, [rec])

    got = store.get(scope, ["a"])
    assert len(got) == 1
    assert got[0].id == "a"
    assert got[0].vector == pytest.approx(_vec((0, 1.0)), abs=1e-5)
    assert got[0].metadata == {"color": "red"}

    hits = store.search(scope, VectorQuery(vector=_vec((0, 1.0)), top_k=5))
    assert [h.id for h in hits] == ["a"]

    store.update(scope, [VectorRecord(id="a", vector=_vec((1, 1.0)), metadata={"color": "blue"})])
    got = store.get(scope, ["a"])
    assert got[0].vector == pytest.approx(_vec((1, 1.0)), abs=1e-5)
    assert got[0].metadata == {"color": "blue"}

    store.delete(scope, ["a"])
    assert store.get(scope, ["a"]) == []
    assert store.search(scope, VectorQuery(vector=_vec((1, 1.0)), top_k=5)) == []


def test_vec_insert_conflict(vec):
    store, scope = vec
    store.insert(scope, [VectorRecord(id="dup", vector=_vec((0, 1.0)))])
    with pytest.raises(ConflictError):
        store.insert(scope, [VectorRecord(id="dup", vector=_vec((1, 1.0)))])


def test_vec_update_missing_raises(vec):
    store, scope = vec
    with pytest.raises(NotFoundError):
        store.update(scope, [VectorRecord(id="ghost", vector=_vec((0, 1.0)))])


def test_vec_delete_idempotent(vec):
    store, scope = vec
    store.insert(scope, [VectorRecord(id="a", vector=_vec((0, 1.0)))])
    store.delete(scope, ["a"])
    store.delete(scope, ["a"])  # 再删不报错
    assert store.get(scope, ["a"]) == []


def test_vec_empty_inputs_are_noops(vec):
    store, scope = vec
    store.insert(scope, [])
    store.update(scope, [])
    store.delete(scope, [])
    assert store.get(scope, []) == []
    assert store.get(scope, ["nope"]) == []  # 不存在的 id 省略


def test_vec_search_topk_ordering(vec):
    store, scope = vec
    store.insert(
        scope,
        [
            VectorRecord(id="x", vector=_vec((0, 1.0))),
            VectorRecord(id="y", vector=_vec((1, 1.0))),
            VectorRecord(id="z", vector=_vec((0, 0.9), (1, 0.1))),  # 偏向 x
        ],
    )
    hits = store.search(scope, VectorQuery(vector=_vec((0, 1.0)), top_k=3))
    assert hits[0].id == "x"  # 最近邻在首位
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)  # 得分降序
    assert {h.id for h in hits} == {"x", "y", "z"}


def test_vec_metadata_filters(vec):
    store, scope = vec
    store.insert(
        scope,
        [
            VectorRecord(
                id="r1",
                vector=_vec((0, 1.0)),
                metadata={"color": "red", "n": 1, "tags": ["a", "b"]},
            ),
            VectorRecord(
                id="r2",
                vector=_vec((0, 0.9)),
                metadata={"color": "blue", "n": 9, "tags": ["b", "c"]},
            ),
            VectorRecord(
                id="r3", vector=_vec((0, 0.8)), metadata={"color": "red", "n": 5, "tags": ["c"]}
            ),
        ],
    )

    def query_ids(filters):
        query = VectorQuery(vector=_vec((0, 1.0)), top_k=10, filters=filters)
        return {h.id for h in store.search(scope, query)}

    assert query_ids([FilterClause("color", FilterOp.EQ, "red")]) == {"r1", "r3"}
    assert query_ids([FilterClause("n", FilterOp.GT, 4)]) == {"r2", "r3"}
    assert query_ids([FilterClause("color", FilterOp.IN, ["blue"])]) == {"r2"}
    assert query_ids([FilterClause("tags", FilterOp.CONTAINS, "b")]) == {"r1", "r2"}
    # 组合谓词 AND
    assert query_ids(
        [FilterClause("color", FilterOp.EQ, "red"), FilterClause("n", FilterOp.GTE, 5)]
    ) == {"r3"}


def test_vec_scope_isolation(vec):
    store, scope = vec
    other = Scope(org="itest", user=uuid.uuid4().hex)
    store.insert(scope, [VectorRecord(id="mine", vector=_vec((0, 1.0)))])
    store.insert(other, [VectorRecord(id="theirs", vector=_vec((0, 1.0)))])

    # get 按 scope 过滤：跨 scope 的 id 读不到
    assert {r.id for r in store.get(scope, ["mine", "theirs"])} == {"mine"}
    # search 不跨 scope
    assert {h.id for h in store.search(scope, VectorQuery(vector=_vec((0, 1.0)), top_k=10))} == {
        "mine"
    }
    # delete 不跨 scope：在 scope 删 theirs 无效
    store.delete(scope, ["theirs"])
    assert {r.id for r in store.get(other, ["theirs"])} == {"theirs"}


def test_vec_many_ops_consistency(vec):
    """30 条插入 → 删 3 的倍数 → 改 2 的倍数(余下) → 校验最终态。"""
    store, scope = vec
    n = 30
    recs = [
        VectorRecord(id=f"v{i}", vector=_vec((i % DIM, 1.0)), metadata={"n": i}) for i in range(n)
    ]
    store.insert(scope, recs)
    assert len({r.id for r in store.get(scope, [f"v{i}" for i in range(n)])}) == n

    deleted = {i for i in range(n) if i % 3 == 0}
    store.delete(scope, [f"v{i}" for i in deleted])
    updated = {i for i in range(n) if i % 2 == 0 and i not in deleted}
    store.update(
        scope,
        [
            VectorRecord(id=f"v{i}", vector=_vec((0, 2.0)), metadata={"n": i, "upd": True})
            for i in updated
        ],
    )

    survivors = [i for i in range(n) if i not in deleted]
    got = {r.id: r for r in store.get(scope, [f"v{i}" for i in range(n)])}
    assert set(got) == {f"v{i}" for i in survivors}  # 删除的不在
    for i in survivors:
        rec = got[f"v{i}"]
        if i in updated:
            assert rec.metadata.get("upd") is True
            assert rec.vector == pytest.approx(_vec((0, 2.0)), abs=1e-5)
        else:
            assert "upd" not in rec.metadata
            assert rec.metadata["n"] == i
