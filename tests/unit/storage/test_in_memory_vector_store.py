from __future__ import annotations

import threading
import time

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="product", user="alice")


def test_insert_partial_batch_is_atomic_on_conflict() -> None:
    """批量 insert 中途冲突：已校验通过的前半批不得残留（否则重试永久冲突）。"""
    store = InMemoryVectorStore()
    store.insert(_SCOPE, [VectorRecord(id="dup", vector=[1.0])])

    with pytest.raises(ConflictError):
        store.insert(
            _SCOPE,
            [VectorRecord(id="new1", vector=[1.0]), VectorRecord(id="dup", vector=[1.0])],
        )

    assert store.get(_SCOPE, ["new1"]) == []
    store.insert(_SCOPE, [VectorRecord(id="new1", vector=[1.0])])


def test_update_partial_batch_is_atomic_on_missing() -> None:
    """批量 update 中途缺失：前半批不得被改写，保持原子性。"""
    store = InMemoryVectorStore()
    store.insert(_SCOPE, [VectorRecord(id="keep", vector=[1.0])])

    with pytest.raises(NotFoundError):
        store.update(
            _SCOPE,
            [VectorRecord(id="keep", vector=[0.0]), VectorRecord(id="ghost", vector=[1.0])],
        )

    assert store.get(_SCOPE, ["keep"])[0].vector == [1.0]


def test_conflict_error_reports_offending_id() -> None:
    """冲突语义保持：报第一个已存在 id。"""
    store = InMemoryVectorStore()
    store.insert(_SCOPE, [VectorRecord(id="a", vector=[1.0])])

    with pytest.raises(ConflictError) as exc_info:
        store.insert(_SCOPE, [VectorRecord(id="a", vector=[1.0])])
    assert "a" in str(exc_info.value)


def test_concurrent_insert_and_search_is_safe() -> None:
    """并发 insert 与 search 交错：读侧不得抛 dictionary changed size during iteration。"""
    store = InMemoryVectorStore()
    stop = threading.Event()
    reader_errors: list[RuntimeError] = []

    def writer() -> None:
        try:
            for i in range(2000):
                store.insert(_SCOPE, [VectorRecord(id=f"v{i}", vector=[1.0])])
        finally:
            # 异常路径也必须置位，否则 reader 死循环拖住解释器无法退出。
            stop.set()

    def reader() -> None:
        while not stop.is_set():
            try:
                store.search(_SCOPE, VectorQuery(vector=[1.0], top_k=5))
            except RuntimeError as exc:
                reader_errors.append(exc)
                stop.set()
            time.sleep(0.001)  # 极短让步：避免无检索可做时满核忙等抖动 CI。

    threads = [threading.Thread(target=writer, daemon=True)]
    threads += [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # join 超时仍有存活线程：显式置位让 daemon reader 退出，并判定失败。
    alive = [t for t in threads if t.is_alive()]
    stop.set()
    for t in alive:
        t.join(timeout=5)
    assert not alive, "writer/reader 未在超时内结束，疑似死循环"
    assert not reader_errors
    assert stop.is_set()
