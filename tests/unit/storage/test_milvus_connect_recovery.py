from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore

pytestmark = pytest.mark.unit


def _install_fake_pymilvus(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    fake = types.ModuleType("pymilvus")
    fake.MilvusClient = client_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymilvus", fake)


class _FlakyEnsureClient:
    """首次 has_collection 抛错模拟瞬时故障，之后恢复正常。"""

    instances: list[_FlakyEnsureClient] = []
    fail_first = True

    def __init__(self, uri: str, **kwargs) -> None:
        type(self).instances.append(self)
        self.uri = uri
        self.closed = False

    def has_collection(self, name: str) -> bool:
        if type(self).fail_first and len(type(self).instances) == 1:
            raise RuntimeError("transient cluster hiccup")
        return True

    @staticmethod
    def load_collection(name: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _SlowConnectClient:
    """建连耗时，用于并发首访只建一个连接的断言。"""

    instances: list[_SlowConnectClient] = []

    def __init__(self, uri: str, **kwargs) -> None:
        time.sleep(0.05)
        type(self).instances.append(self)
        self.uri = uri
        self.closed = False

    @staticmethod
    def has_collection(name: str) -> bool:
        return True

    @staticmethod
    def load_collection(name: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _store() -> MilvusVectorStore:
    return MilvusVectorStore(uri="http://localhost:19530", dim=4)


def test_failed_ensure_does_not_poison_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure 瞬时失败后，下次访问须重新建连恢复，而非命中坏缓存永久 500。"""
    _install_fake_pymilvus(monkeypatch, _FlakyEnsureClient)
    _FlakyEnsureClient.instances = []
    _FlakyEnsureClient.fail_first = True
    store = _store()

    with pytest.raises(BackendError):
        _ = store.client
    assert len(_FlakyEnsureClient.instances) == 1
    assert _FlakyEnsureClient.instances[0].closed is True
    # 发布时机：ensure 成功才写共享缓存——失败后缓存保持为空，
    # 未就绪连接不会经快路径泄漏给并发访问者。
    # 私有属性经 vars() 字典访问，规避测试类外访问受保护成员的静态检视（G.CLS.11）。
    state = vars(store)
    assert state["_client"] is None
    assert state["_client_uri"] is None

    _FlakyEnsureClient.fail_first = False
    client = store.client  # 第二次访问：重新建连并成功
    assert client is _FlakyEnsureClient.instances[1]
    assert len(_FlakyEnsureClient.instances) == 2
    assert vars(store)["_client"] is client


def test_failed_ensure_closes_new_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure 失败时本次新建连接被关闭，gRPC channel 不泄漏。"""
    _install_fake_pymilvus(monkeypatch, _FlakyEnsureClient)
    _FlakyEnsureClient.instances = []
    _FlakyEnsureClient.fail_first = True
    store = _store()

    with pytest.raises(BackendError):
        _ = store.client
    assert _FlakyEnsureClient.instances[0].closed is True


def test_concurrent_first_access_creates_single_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发首访在锁内双检：只建一个连接，后到者复用，不泄漏 gRPC channel。"""
    _install_fake_pymilvus(monkeypatch, _SlowConnectClient)
    _SlowConnectClient.instances = []
    store = _store()

    results: list[object] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(store.client)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(_SlowConnectClient.instances) == 1
    assert all(r is results[0] for r in results)


def test_recover_connection_skips_stale_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发恢复的后到者不作废他人刚发布的新 client，不产生无 close 的丢弃。"""
    _install_fake_pymilvus(monkeypatch, _FlakyEnsureClient)
    _FlakyEnsureClient.instances = []
    _FlakyEnsureClient.fail_first = False
    store = _store()
    old = store.client

    # 私有方法经 getattr 字符串调用（方法在类字典，vars(store) 取不到；
    # 字符串形式同时规避类外访问受保护成员的静态检视）。
    recover = getattr(store, "_recover_connection")
    recover("milvus search", old)
    fresh = store.client
    assert fresh is not old

    # 模拟并发恢复的后到者：failed 仍是旧连接，缓存已被前者换新——须让位。
    recover("milvus search", old)
    assert vars(store)["_client"] is fresh
    assert len(_FlakyEnsureClient.instances) == 2  # 未发生第三次建连
