# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MilvusVectorStore 连接恢复与集合初始化超时单元测试。"""

from __future__ import annotations

import sys
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.types import VectorRecord
from jiuwen_memory.storage.vector_impl import milvus_vector as milvus_module
from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", user="alice")


class _FakeDataType:
    VARCHAR = "VARCHAR"
    FLOAT_VECTOR = "FLOAT_VECTOR"
    JSON = "JSON"


class _FakeSchema:
    @staticmethod
    def add_field(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)


class _FakeIndexParams:
    @staticmethod
    def add_index(**kwargs: Any) -> None:
        _ = kwargs


def _install_fake_pymilvus(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    fake = types.ModuleType("pymilvus")
    fake.MilvusClient = client_cls  # type: ignore[attr-defined]
    fake.DataType = _FakeDataType  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymilvus", fake)


def _store(**kwargs: Any) -> MilvusVectorStore:
    return MilvusVectorStore(uri="http://localhost:19530", dim=4, **kwargs)


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _assert_connect_lock_released(store: MilvusVectorStore) -> None:
    lock = vars(store)["_connect_lock"]
    acquired = lock.acquire(blocking=False)
    assert acquired is True, "_connect_lock must be released after failed initialization"
    lock.release()


class _FlakyEnsureClient:
    """首次 has_collection 抛错模拟瞬时故障，之后恢复正常。"""

    instances: list[_FlakyEnsureClient] = []
    fail_first = True

    def __init__(self, uri: str, **kwargs: Any) -> None:
        _ = kwargs
        type(self).instances.append(self)
        self.uri = uri
        self.closed = False

    @staticmethod
    def load_collection(
        name: str, timeout: float | None = None, _async: bool = False
    ) -> None:
        _ = (name, timeout, _async)

    def has_collection(self, name: str, timeout: float | None = None) -> bool:
        _ = (name, timeout)
        if type(self).fail_first and len(type(self).instances) == 1:
            raise RuntimeError("transient cluster hiccup")
        return True

    def close(self) -> None:
        self.closed = True


class _SlowConnectClient:
    """建连耗时，用于并发首访只建一个连接的断言。"""

    instances: list[_SlowConnectClient] = []

    def __init__(self, uri: str, **kwargs: Any) -> None:
        _ = kwargs
        time.sleep(0.05)
        type(self).instances.append(self)
        self.uri = uri
        self.closed = False

    @staticmethod
    def has_collection(name: str, timeout: float | None = None) -> bool:
        _ = (name, timeout)
        return True

    @staticmethod
    def load_collection(
        name: str, timeout: float | None = None, _async: bool = False
    ) -> None:
        _ = (name, timeout, _async)

    def close(self) -> None:
        self.closed = True


@dataclass
class _ClusterState:
    has_collection: bool = False
    indexes: dict[str, list[str]] = field(default_factory=lambda: {"vector": []})
    describe_seq: list[Any] = field(default_factory=lambda: [{"state": "Finished"}])
    index_state_seq: list[tuple[str, str]] = field(default_factory=lambda: [("Finished", "")])
    load_state: str = "Loaded"
    load_state_seq: list[Any] | None = None
    load_errors: list[Exception | None] = field(default_factory=list)
    load_timeout_error: BaseException | None = None
    has_collection_error: BaseException | None = None
    list_indexes_error: Exception | None = None
    describe_index_error: Exception | None = None
    get_connection_error: Exception | None = None
    load_state_error: Exception | None = None
    load_cost: float = 0.0
    load_poll_cost: float = 0.0
    clock: list[float] = field(default_factory=lambda: [100.0])
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    inserted: list[dict[str, Any]] = field(default_factory=list)
    clients: list[Any] = field(default_factory=list)


class _FakeClusterConn:
    def __init__(self, state: _ClusterState) -> None:
        self._state = state

    def get_index_state(
        self,
        collection_name: str,
        index_name: str,
        timeout: Any = None,
        timestamp: int | None = None,
    ) -> tuple[str, str]:
        _ = collection_name
        idx = sum(1 for name, _ in self._state.calls if name == "get_index_state")
        self._state.calls.append(
            (
                "get_index_state",
                {"index_name": index_name, "timeout": timeout, "timestamp": timestamp},
            )
        )
        return self._state.index_state_seq[min(idx, len(self._state.index_state_seq) - 1)]


class _FakeClusterClient:
    state: _ClusterState

    def __init__(self, uri: str, **kwargs: Any) -> None:
        _ = (uri, kwargs)
        self.closed = False
        self._conn = _FakeClusterConn(type(self).state)
        type(self).state.clients.append(self)

    @staticmethod
    def create_schema(**kwargs: Any) -> _FakeSchema:
        _ = kwargs
        return _FakeSchema()

    @staticmethod
    def prepare_index_params() -> _FakeIndexParams:
        return _FakeIndexParams()

    @staticmethod
    def query(collection_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        _ = (collection_name, kwargs)
        return []

    def has_collection(self, name: str, timeout: float | None = None) -> bool:
        _ = name
        state = type(self).state
        state.calls.append(("has_collection", {"timeout": timeout}))
        if state.has_collection_error is not None:
            raise state.has_collection_error
        return state.has_collection

    def create_collection(self, **kwargs: Any) -> None:
        state = type(self).state
        state.calls.append(("create_collection", dict(kwargs)))
        state.has_collection = True

    def list_indexes(
        self, collection_name: str, field_name: str = "", timeout: float | None = None
    ) -> list[str]:
        _ = collection_name
        state = type(self).state
        state.calls.append(("list_indexes", {"field_name": field_name, "timeout": timeout}))
        if state.list_indexes_error is not None:
            raise state.list_indexes_error
        if field_name:
            return list(state.indexes.get(field_name, []))
        return [idx for group in state.indexes.values() for idx in group]

    def create_index(self, **kwargs: Any) -> None:
        state = type(self).state
        state.calls.append(("create_index", dict(kwargs)))
        state.indexes["vector"] = ["vector"]

    def describe_index(
        self, collection_name: str, index_name: str, timeout: float | None = None
    ) -> Any:
        _ = collection_name
        state = type(self).state
        idx = sum(1 for name, _ in state.calls if name == "describe_index")
        state.calls.append(("describe_index", {"index_name": index_name, "timeout": timeout}))
        if state.describe_index_error is not None:
            raise state.describe_index_error
        item = state.describe_seq[min(idx, len(state.describe_seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def load_collection(
        self, name: str, timeout: float | None = None, _async: bool = False
    ) -> None:
        _ = name
        state = type(self).state
        idx = sum(1 for call_name, _ in state.calls if call_name == "load_collection")
        state.calls.append(("load_collection", {"timeout": timeout, "_async": _async}))
        if idx < len(state.load_errors):
            err = state.load_errors[idx]
            if err is not None:
                raise err
        if not state.indexes.get("vector"):
            raise RuntimeError("index not found[collection=agent_memory_vectors]")
        if state.load_timeout_error is not None:
            raise state.load_timeout_error
        state.clock[0] += state.load_cost
        if not _async and state.load_cost > 0:
            state.clock[0] += state.load_cost
            raise TimeoutError("wait for loading collection timeout")

    def get_load_state(
        self, collection_name: str, timeout: float | None = None
    ) -> dict[str, str]:
        _ = collection_name
        state = type(self).state
        idx = sum(1 for name, _ in state.calls if name == "get_load_state")
        state.calls.append(("get_load_state", {"timeout": timeout}))
        if state.load_state_error is not None:
            raise state.load_state_error
        state.clock[0] += state.load_poll_cost
        if state.load_state_seq is not None:
            item = state.load_state_seq[min(idx, len(state.load_state_seq) - 1)]
            if isinstance(item, BaseException):
                raise item
            return item
        return {"state": state.load_state}

    def insert(
        self, collection_name: str, data: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        _ = (collection_name, kwargs)
        type(self).state.inserted.extend(data)

    def close(self) -> None:
        self.closed = True

    def _get_connection(self) -> _FakeClusterConn:
        state = type(self).state
        state.calls.append(("_get_connection", {}))
        if state.get_connection_error is not None:
            raise state.get_connection_error
        return self._conn


def _make_cluster_client(state: _ClusterState) -> type:
    return type("_BoundClusterClient", (_FakeClusterClient,), {"state": state})


# ------------------------------------------------ 既有连接恢复契约


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
    state = vars(store)
    assert state["_client"] is None
    assert state["_client_uri"] is None

    _FlakyEnsureClient.fail_first = False
    client = store.client
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

    recover = getattr(store, "_recover_connection")
    recover("milvus search", old)
    fresh = store.client
    assert fresh is not old

    recover("milvus search", old)
    assert vars(store)["_client"] is fresh
    assert len(_FlakyEnsureClient.instances) == 2


# ------------------------------------------------ Issue #342 装载超时与恢复


def test_first_insert_load_timeout_releases_lock_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#342 主回归：首次 insert 装载无响应时按有限超时报错释放锁，恢复后重试写入一次。"""
    state = _ClusterState(load_timeout_error=TimeoutError("gRPC DEADLINE_EXCEEDED"))
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(state))
    store = _store(load_timeout=4.0)
    record = VectorRecord(id="r1", vector=[1.0, 0.0, 0.0, 0.0], metadata={"k": "v"})

    with pytest.raises(BackendError) as exc_info:
        store.insert(_SCOPE, [record])
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert state.clients[0].closed is True
    assert vars(store)["_client"] is None
    _assert_connect_lock_released(store)

    by_name = dict(state.calls)
    assert "index_params" not in by_name["create_collection"]
    assert by_name["create_index"]["sync"] is False
    assert by_name["load_collection"]["_async"] is True
    for step in ("has_collection", "create_collection", "create_index", "load_collection"):
        t = by_name[step]["timeout"]
        assert _is_plain_int(t) and 1 <= t <= 4

    # 服务恢复后，同一个 Store 重试 insert 成功写入一次。
    state.load_timeout_error = None
    store.insert(_SCOPE, [record])
    assert len(state.clients) == 2
    assert len(state.inserted) == 1
    assert state.inserted[0]["logical_id"] == "r1"


@pytest.mark.parametrize(
    "branch", ["new_collection", "existing_collection", "missing_index_recovery"]
)
def test_async_load_prevents_sdk_double_wait_across_all_branches(
    monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """三条装载分支均传 _async=True，关闭 SDK 内部重复等待，4s 预算内收敛而非翻倍到 7.5s。"""
    state = _ClusterState(
        has_collection=branch != "new_collection",
        indexes={"vector": ["vector"] if branch == "existing_collection" else []},
        load_state="Loading",
        load_cost=3.75,
        load_poll_cost=0.25,
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: state.clock[0])
    monkeypatch.setattr(
        milvus_module.time, "sleep", lambda dt: state.clock.__setitem__(0, state.clock[0] + dt)
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(state))

    store = _store(load_timeout=4.0)
    started = state.clock[0]
    with pytest.raises(BackendError, match="not loaded within 4.0s"):
        _ = store.client

    assert state.clock[0] - started <= 4.0
    load_calls = [kw for name, kw in state.calls if name == "load_collection"]
    assert load_calls
    assert all(kw["_async"] is True and _is_plain_int(kw["timeout"]) for kw in load_calls)
    load_state_timeouts = [kw["timeout"] for name, kw in state.calls if name == "get_load_state"]
    assert load_state_timeouts == [1] and _is_plain_int(load_state_timeouts[0])
    _assert_connect_lock_released(store)


def test_existing_collection_direct_load_and_missing_vector_index_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有集合默认直接装载（不调 list_indexes）；仅遇缺索引错时按 field_name='vector' 补建。"""
    healthy = _ClusterState(
        has_collection=True,
        indexes={"vector": ["vector"]},
        list_indexes_error=PermissionError("IndexDetail denied"),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(healthy))
    _ = _store(load_timeout=5.0).client
    assert not any(name == "list_indexes" for name, _ in healthy.calls)

    scalar_only = _ClusterState(
        has_collection=True,
        indexes={"logical_id": ["logical_id_index"], "vector": []},
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(scalar_only))
    store = _store(load_timeout=5.0)
    store.insert(_SCOPE, [VectorRecord(id="r2", vector=[0.0, 1.0, 0.0, 0.0], metadata={})])

    list_calls = [kw for name, kw in scalar_only.calls if name == "list_indexes"]
    assert list_calls and list_calls[0]["field_name"] == "vector"
    assert scalar_only.indexes["vector"] == ["vector"]
    assert len(scalar_only.inserted) == 1


def test_index_polling_waits_past_empty_response_and_falls_back_on_2_4_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """describe_index 返空不提前完成；pymilvus 2.4.0 无 state 回退 get_index_state(timestamp=0)。"""
    empty_then_done = _ClusterState(describe_seq=[None, {}, {"state": "Finished"}])
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: empty_then_done.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: empty_then_done.clock.__setitem__(0, empty_then_done.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(empty_then_done))
    _ = _store(load_timeout=5.0).client
    describe_timeouts = [
        kw["timeout"] for name, kw in empty_then_done.calls if name == "describe_index"
    ]
    assert describe_timeouts == [5, 5, 4]
    assert all(_is_plain_int(t) for t in describe_timeouts)
    assert not any(name == "_get_connection" for name, _ in empty_then_done.calls)

    v240 = _ClusterState(
        describe_seq=[{"index_type": "AUTOINDEX", "field_name": "vector"}],
        index_state_seq=[("InProgress", ""), ("Finished", "")],
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: v240.clock[0])
    monkeypatch.setattr(
        milvus_module.time, "sleep", lambda dt: v240.clock.__setitem__(0, v240.clock[0] + dt)
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(v240))
    _ = _store(load_timeout=5.0).client
    state_calls = [kw for name, kw in v240.calls if name == "get_index_state"]
    assert len(state_calls) == 2
    assert sum(1 for name, _ in v240.calls if name == "describe_index") == 1
    assert sum(1 for name, _ in v240.calls if name == "_get_connection") == 1
    assert all(kw["timestamp"] == 0 and _is_plain_int(kw["timeout"]) for kw in state_calls)


def test_retry_after_index_wait_timeout_waits_for_existing_in_progress_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮建索引超时后，次轮进入已有集合分支遇到 code=700 应等待既有索引就绪再装载。"""
    state = _ClusterState(describe_seq=[{"state": "InProgress"}])
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: state.clock[0])
    monkeypatch.setattr(
        milvus_module.time, "sleep", lambda dt: state.clock.__setitem__(0, state.clock[0] + dt)
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(state))
    store = _store(load_timeout=1.0)

    with pytest.raises(BackendError, match="index not ready within 1.0s"):
        _ = store.client
    assert state.has_collection is True
    assert state.indexes["vector"] == ["vector"]
    assert sum(1 for name, _ in state.calls if name == "create_index") == 1
    _assert_connect_lock_released(store)

    # 次轮集合已存在且向量索引仍在构建，首次 load_collection 报 code=700，
    # 随后等待既有向量索引完成后重试装载成功，不重复 create_index。
    prior_describe = sum(1 for name, _ in state.calls if name == "describe_index")
    state.describe_seq = [{"state": "InProgress"}] * (prior_describe + 1) + [{"state": "Finished"}]
    state.load_errors = [RuntimeError("index not found (code=700)"), None]

    client = store.client
    assert client is state.clients[-1]
    assert sum(1 for name, _ in state.calls if name == "create_index") == 1
    assert sum(1 for name, _ in state.calls if name == "list_indexes") == 1
    assert sum(1 for name, _ in state.calls if name == "load_collection") == 2


def test_index_polling_failed_state_transient_700_and_timeout_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引 Failed 立即上抛；短暂 code=700 继续轮询；持续 700 超时保留底层 __cause__。"""
    failed = _ClusterState(describe_seq=[{"state": "Failed", "fail_reason": "index OOM"}])
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: failed.clock[0])
    monkeypatch.setattr(
        milvus_module.time, "sleep", lambda dt: failed.clock.__setitem__(0, failed.clock[0] + dt)
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(failed))
    store_failed = _store(load_timeout=10.0)
    t0 = failed.clock[0]
    with pytest.raises(BackendError, match="index OOM"):
        _ = store_failed.client
    assert sum(1 for name, _ in failed.calls if name == "describe_index") == 1
    assert failed.clock[0] - t0 == 0.0
    _assert_connect_lock_released(store_failed)

    transient_700 = _ClusterState(
        describe_seq=[RuntimeError("index not found (code=700)"), {"state": "Finished"}]
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: transient_700.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: transient_700.clock.__setitem__(0, transient_700.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(transient_700))
    _ = _store(load_timeout=5.0).client
    assert sum(1 for name, _ in transient_700.calls if name == "describe_index") == 2

    always_700 = _ClusterState(describe_seq=[RuntimeError("index not found (code=700)")])
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: always_700.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: always_700.clock.__setitem__(0, always_700.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(always_700))
    with pytest.raises(BackendError, match="index not ready within 1.0s") as exc_info:
        _ = _store(load_timeout=1.0).client
    assert exc_info.value.__cause__ is not None
    assert "code=700" in str(exc_info.value.__cause__)


def test_load_polling_tolerates_101_but_fails_fast_on_connection_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装载轮询容忍短暂 code=101 重试，但遇到连接丢失立即上抛不等待超时。"""
    transient_101 = _ClusterState(
        has_collection=True,
        indexes={"vector": ["vector"]},
        load_state_seq=[RuntimeError("collection not loaded, code=101"), {"state": "Loaded"}],
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: transient_101.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: transient_101.clock.__setitem__(0, transient_101.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(transient_101))
    _ = _store(load_timeout=5.0).client
    assert sum(1 for name, _ in transient_101.calls if name == "get_load_state") == 2

    conn_lost = _ClusterState(
        has_collection=True,
        indexes={"vector": ["vector"]},
        load_state_seq=[RuntimeError("should create connection first")],
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: conn_lost.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: conn_lost.clock.__setitem__(0, conn_lost.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(conn_lost))
    store_lost = _store(load_timeout=30.0)
    t0 = conn_lost.clock[0]
    with pytest.raises(BackendError, match="should create connection first"):
        _ = store_lost.client
    assert sum(1 for name, _ in conn_lost.calls if name == "get_load_state") == 1
    assert conn_lost.clock[0] - t0 == 0.0
    _assert_connect_lock_released(store_lost)


def test_index_polling_tolerates_private_get_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """私有 _get_connection() 异常不阻断 describe_index 轮询，且在 2.4.0 回退时安全降级。"""
    with_state = _ClusterState(
        describe_seq=[{"state": "InProgress"}, {"state": "Finished"}],
        get_connection_error=RuntimeError("should create connection first"),
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: with_state.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: with_state.clock.__setitem__(0, with_state.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(with_state))
    _ = _store(load_timeout=5.0).client
    assert sum(1 for name, _ in with_state.calls if name == "describe_index") == 2
    assert not any(name == "_get_connection" for name, _ in with_state.calls)

    fallback_broken = _ClusterState(
        describe_seq=[{"index_type": "AUTOINDEX", "field_name": "vector"}],
        get_connection_error=RuntimeError("should create connection first"),
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: fallback_broken.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: fallback_broken.clock.__setitem__(0, fallback_broken.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(fallback_broken))
    _ = _store(load_timeout=5.0).client
    assert sum(1 for name, _ in fallback_broken.calls if name == "_get_connection") == 1
    assert not any(name == "get_index_state" for name, _ in fallback_broken.calls)


def test_polling_fails_fast_on_permission_and_param_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引/装载轮询遇到权限拒绝或参数错误时立即上抛并释放锁，不重试到超时。"""
    idx_denied = _ClusterState(
        describe_index_error=RuntimeError("permission denied: DescribeIndex")
    )
    monkeypatch.setattr(milvus_module.time, "monotonic", lambda: idx_denied.clock[0])
    monkeypatch.setattr(
        milvus_module.time,
        "sleep",
        lambda dt: idx_denied.clock.__setitem__(0, idx_denied.clock[0] + dt),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(idx_denied))
    store_idx = _store(load_timeout=30.0)
    t0 = idx_denied.clock[0]
    with pytest.raises(BackendError, match="permission denied"):
        _ = store_idx.client
    assert sum(1 for name, _ in idx_denied.calls if name == "describe_index") == 1
    assert idx_denied.clock[0] - t0 == 0.0
    _assert_connect_lock_released(store_idx)

    load_denied = _ClusterState(
        has_collection=True,
        indexes={"vector": ["vector"]},
        load_state_error=PermissionError("GetLoadState denied"),
    )
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(load_denied))
    store_load = _store(load_timeout=30.0)
    with pytest.raises(BackendError, match="GetLoadState denied"):
        _ = store_load.client
    assert sum(1 for name, _ in load_denied.calls if name == "get_load_state") == 1
    _assert_connect_lock_released(store_load)


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, float("nan"), float("inf"), True, False, None, "abc"],
)
def test_invalid_load_timeout_rejected(bad_value: Any) -> None:
    with pytest.raises(
        ValidationError, match="milvus vector store requires positive 'load_timeout'"
    ):
        _store(load_timeout=bad_value)


def test_base_exception_during_ensure_closes_unpublished_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt 等 BaseException 中断初始化时仍须关闭局部连接并释放锁。"""
    state = _ClusterState(has_collection_error=KeyboardInterrupt("cancelled"))
    _install_fake_pymilvus(monkeypatch, _make_cluster_client(state))
    store = _store(load_timeout=5.0)

    with pytest.raises(KeyboardInterrupt):
        _ = store.client
    assert len(state.clients) == 1 and state.clients[0].closed is True
    assert vars(store)["_client"] is None
    _assert_connect_lock_released(store)
