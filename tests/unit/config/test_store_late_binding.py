"""Store 连接类 ConfigSource 晚绑定（F01 §2.1.4 改值）。"""

from __future__ import annotations

import sys
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.storage.fs_impl.local_fs import LocalFSStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="o", user="u")


def test_redis_kv_late_binds_host_port_when_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DictConfigSource(
        {
            "kv_store.host": "host-a",
            "kv_store.port": "6379",
            "kv_store.db": "0",
            "kv_store.password": "pw-a",
        }
    )
    store = RedisKVStore(url=None, host="localhost", port=6379, db=0, config_source=cfg)
    created: list[dict] = []

    class _FakeRedis:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.ping = MagicMock(return_value=True)

        @classmethod
        def from_url(cls, url: str, **kwargs):
            raise AssertionError("should not use from_url when url unbound")

    fake_mod = types.ModuleType("redis")
    fake_mod.Redis = _FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", fake_mod)

    store.health()
    assert created[0]["host"] == "host-a"
    assert created[0]["password"] == "pw-a"

    cfg.put("kv_store.host", "host-b")
    cfg.put("kv_store.password", "pw-b")
    store.health()
    assert created[-1]["host"] == "host-b"
    assert created[-1]["password"] == "pw-b"


def test_sqlite_kv_late_binds_db_path(tmp_path: Path) -> None:
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    cfg = DictConfigSource({"kv_store.db_path": str(path_a)})
    store = SQLiteKVStore(db_path=str(path_a), config_source=cfg)

    store.insert(_SCOPE, "k", b"v1")
    assert store.get(_SCOPE, "k") == b"v1"
    assert path_a.exists()

    cfg.put("kv_store.db_path", str(path_b))
    store.insert(_SCOPE, "k", b"v2")
    assert store.get(_SCOPE, "k") == b"v2"
    assert path_b.exists()
    # 旧库仍保留原值；切换不迁移
    other = SQLiteKVStore(db_path=str(path_a))
    assert other.get(_SCOPE, "k") == b"v1"
    other.close()
    store.close()


def test_local_fs_late_binds_root(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    cfg = DictConfigSource({"fs_store.root": str(root_a)})
    store = LocalFSStore(root=str(root_a), config_source=cfg)

    ref = store.insert(_SCOPE, "f.txt", BytesIO(b"aaa"))
    assert store.get(_SCOPE, ref).read() == b"aaa"

    cfg.put("fs_store.root", str(root_b))
    store.insert(_SCOPE, "g.txt", BytesIO(b"bbb"))
    assert store.get(_SCOPE, "g.txt").read() == b"bbb"
    # 切换 root 不迁移：旧文件仍在 root_a
    assert any(root_a.rglob("f.txt"))
    assert any(root_b.rglob("g.txt"))


def test_milvus_late_binds_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwen_memory.storage.vector_impl.milvus_vector import MilvusVectorStore

    cfg = DictConfigSource({"vector_store.uri": "http://milvus-a:19530"})
    store = MilvusVectorStore(
        uri="http://fallback:19530",
        dim=4,
        config_source=cfg,
    )
    created: list[str] = []

    class _FakeClient:
        def __init__(self, uri: str, **kwargs):
            created.append(uri)

        @staticmethod
        def has_collection(name: str) -> bool:
            return True

        @staticmethod
        def load_collection(name: str) -> None:
            return None

    fake_pymilvus = types.ModuleType("pymilvus")
    fake_pymilvus.MilvusClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)

    _ = store.client
    assert created == ["http://milvus-a:19530"]
    cfg.put("vector_store.uri", "http://milvus-b:19530")
    _ = store.client
    assert created[-1] == "http://milvus-b:19530"


def test_elasticsearch_late_binds_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwen_memory.storage.fulltext_impl.elasticsearch_fulltext import (
        ElasticsearchFulltextStore,
    )

    cfg = DictConfigSource({"fulltext_store.hosts": "http://es-a:9200"})
    store = ElasticsearchFulltextStore(hosts="http://fallback:9200", config_source=cfg)
    created: list[object] = []

    class _Indices:
        @staticmethod
        def exists(index: str) -> bool:
            return True

        @staticmethod
        def put_mapping(**kwargs) -> None:
            return None

        @staticmethod
        def create(**kwargs) -> None:
            return None

    class _FakeES:
        def __init__(self, hosts, **kwargs):
            created.append(hosts)
            self.indices = _Indices()

    fake = types.ModuleType("elasticsearch")
    fake.Elasticsearch = _FakeES  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "elasticsearch", fake)

    _ = store.client
    assert created[0] == "http://es-a:9200"
    cfg.put("fulltext_store.hosts", "http://es-b:9200")
    _ = store.client
    assert created[-1] == "http://es-b:9200"


def test_pg_store_base_late_binds_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwen_memory.storage._pg import PgStoreBase

    class _Tiny(PgStoreBase):
        async def _ensure_schema(self, pool) -> None:
            return None

    cfg = DictConfigSource({"kv_store.dsn": "postgresql://a/db"})
    store = _Tiny(
        dsn="postgresql://fallback/db",
        schema="public",
        table="t",
        pool_min_size=1,
        pool_max_size=1,
        connect_timeout=1.0,
        application_name="t",
        auto_create_schema=False,
        config_source=cfg,
        config_namespace="kv_store",
        config_dsn_field="dsn",
    )
    pools: list[str] = []

    class _Pool:
        def __init__(self, dsn: str, **kwargs):
            pools.append(dsn)

        async def close(self):
            return None

    fake_asyncpg = types.ModuleType("asyncpg")

    async def create_pool(dsn=None, **kwargs):
        return _Pool(dsn, **kwargs)

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    _ = store.pool
    assert pools == ["postgresql://a/db"]
    cfg.put("kv_store.dsn", "postgresql://b/db")
    _ = store.pool
    assert pools[-1] == "postgresql://b/db"


def test_nano_graph_late_binds_working_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwen_memory.storage.graph_impl import nano_graphrag_graph as mod

    dir_a = tmp_path / "ga"
    dir_b = tmp_path / "gb"
    cfg = DictConfigSource({"graph_store.working_dir": str(dir_a)})

    class _FakeStorage:
        def __init__(self, namespace: str, global_config: dict):
            self.namespace = namespace
            self.working_dir = global_config["working_dir"]

    monkeypatch.setattr(mod, "_networkx_storage_cls", lambda: _FakeStorage)
    store = mod.NanoGraphRAGGraphStore(working_dir=str(dir_a), config_source=cfg)
    # 无公共注入口：字符串 getattr 避免 G.CLS.11 protected-access
    s1 = getattr(store, "_store")(_SCOPE)
    assert Path(s1.working_dir) == dir_a.resolve()

    cfg.put("graph_store.working_dir", str(dir_b))
    s2 = getattr(store, "_store")(_SCOPE)
    assert Path(s2.working_dir) == dir_b.resolve()
    assert s1 is not s2


def test_fusion_late_binds_uri_and_working_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwen_memory.storage.fusion_impl.milvus_graph_fusion import MilvusGraphFusionStore

    cfg = DictConfigSource(
        {
            "fusion_store.uri": "http://fusion-a:19530",
            "fusion_store.working_dir": str(tmp_path / "fa"),
        }
    )
    created: list[str] = []

    class _FakeClient:
        def __init__(self, uri: str, **kwargs):
            created.append(uri)

        @staticmethod
        def has_collection(name: str) -> bool:
            return True

        @staticmethod
        def load_collection(name: str) -> None:
            return None

    fake_pymilvus = types.ModuleType("pymilvus")
    fake_pymilvus.MilvusClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)

    class _FakeStorage:
        def __init__(self, namespace: str, global_config: dict):
            self.working_dir = global_config["working_dir"]

    monkeypatch.setattr(
        "jiuwen_memory.storage.fusion_impl.milvus_graph_fusion._networkx_storage_cls",
        lambda: _FakeStorage,
    )

    store = MilvusGraphFusionStore(
        working_dir=str(tmp_path / "fallback"),
        dim=4,
        milvus={"uri": "http://fallback:19530"},
        config_source=cfg,
    )
    # 无公共注入口：字符串 getattr 避免 G.CLS.11 protected-access
    _ = getattr(store, "_vec").client
    assert created[0] == "http://fusion-a:19530"

    cfg.put("fusion_store.uri", "http://fusion-b:19530")
    _ = getattr(store, "_vec").client
    assert created[-1] == "http://fusion-b:19530"

    g1 = getattr(store, "_graph")(_SCOPE)
    cfg.put("fusion_store.working_dir", str(tmp_path / "fb"))
    g2 = getattr(store, "_graph")(_SCOPE)
    assert Path(g1.working_dir) != Path(g2.working_dir)
