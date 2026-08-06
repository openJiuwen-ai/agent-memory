"""存储后端注册（各 *Producer）与可离线验证的后端（LocalFSStore + 惰性连接语义）测试。

redis/postgres/elasticsearch/milvus 的真实读写需要起后端服务，属集成测试范畴，
这里只覆盖各组件 producer 是否登记了真实后端、``LocalFSStore`` 全链路，以及
第三方库缺失时的惰性 ``BackendError`` 语义。
"""

from __future__ import annotations

import io
from importlib import import_module

import pytest

from common.bootstrap import register_plugins
from common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from common.type_def import Scope
from config import AssemblyContext
from storage.bootstrap import register_backends
from storage.fs import FsProducer
from storage.fs_impl.local_fs import LocalFSStore
from storage.fulltext import FulltextProducer
from storage.fusion import FusionProducer
from storage.graph import GraphProducer
from storage.kv import KvProducer
from storage.kv_impl.postgres_kv import PostgresKVStore
from storage.kv_impl.redis_kv import RedisKVStore
from storage.types import VectorRecord
from storage.vector import VectorProducer
from storage.vector_impl.milvus_vector import MilvusVectorStore
from storage.vector_impl.pgvector_vector import PgVectorStore

register_backends()  # 经接口拿到的 Producer，注册靠 import 实现——测试里显式触发一次
register_plugins()   # in_memory 全文/融合 store 依赖 tokenizer，需插件也注册齐


def _build_store(producer, target, params=None):
    """经 producer 匿名建一个独立实例（接口 1 build，不进缓存）。"""
    return producer.build(target, params or {}, AssemblyContext())


# --------------------------------------------------------------- 工厂注册
def test_real_backends_registered() -> None:
    """各组件 producer 都登记了对应真实后端（导入 *_impl 即自注册）。"""
    assert "redis" in KvProducer.known()
    assert "postgres" in KvProducer.known()
    assert "elasticsearch" in FulltextProducer.known()
    assert "milvus" in VectorProducer.known()
    assert "pgvector" in VectorProducer.known()
    assert "nano_graphrag" in GraphProducer.known()
    assert "local" in FsProducer.known()
    assert "milvus_graph" in FusionProducer.known()


def test_producer_unknown_backend_raises() -> None:
    with pytest.raises(ValidationError):
        KvProducer.build("nope", {}, AssemblyContext())


def test_vector_requires_dim() -> None:
    with pytest.raises(ValidationError):
        MilvusVectorStore(dim=0)


# ------------------------------------------------ 三方库后端的 build 期参数校验
def test_redis_builder_requires_url() -> None:
    """redis 未配置 url 时，在 build 阶段就报错（而非惰性连接时）。"""
    with pytest.raises(ValidationError, match="url"):
        _build_store(KvProducer, "redis")


def test_redis_builder_reads_params() -> None:
    store = _build_store(KvProducer, "redis", {"url": "redis://localhost:6379/0", "db": 2})
    assert isinstance(store, RedisKVStore)


def test_elasticsearch_builder_requires_hosts() -> None:
    with pytest.raises(ValidationError, match="hosts"):
        _build_store(FulltextProducer, "elasticsearch")


def test_milvus_builder_requires_uri() -> None:
    with pytest.raises(ValidationError, match="uri"):
        _build_store(VectorProducer, "milvus", {"dim": 8})


def test_postgres_builders_require_dsn() -> None:
    with pytest.raises(ValidationError, match="dsn"):
        _build_store(KvProducer, "postgres")
    with pytest.raises(ValidationError, match="dsn"):
        _build_store(VectorProducer, "pgvector", {"dim": 8})


def test_postgres_builders_read_params_without_connecting() -> None:
    kv = _build_store(
        KvProducer,
        "postgres",
        {"dsn": "postgresql://example.invalid/db", "table": "custom_kv"},
    )
    vector = _build_store(
        VectorProducer,
        "pgvector",
        {
            "dsn": "postgresql://example.invalid/db",
            "dim": 8,
            "table": "custom_vectors",
            "index_type": "none",
        },
    )

    assert isinstance(kv, PostgresKVStore)
    assert isinstance(vector, PgVectorStore)


def test_pgvector_validates_dim_metric_and_index() -> None:
    with pytest.raises(ValidationError, match="positive"):
        PgVectorStore(dsn="postgresql://example.invalid/db", dim=0)
    with pytest.raises(ValidationError, match="metric_type"):
        PgVectorStore(
            dsn="postgresql://example.invalid/db",
            dim=8,
            metric_type="manhattan",
        )
    with pytest.raises(ValidationError, match="index_type"):
        PgVectorStore(
            dsn="postgresql://example.invalid/db",
            dim=8,
            index_type="ivfflat",
        )


def test_pgvector_rejects_record_dimension_before_connecting() -> None:
    store = PgVectorStore(dsn="postgresql://example.invalid/db", dim=2)

    with pytest.raises(ValidationError, match="dimension mismatch"):
        store.insert(SCOPE, [VectorRecord(id="bad", vector=[1.0])])


def test_sqlite_builder_reads_db_path(tmp_path) -> None:
    db = str(tmp_path / "x.db")
    store = _build_store(KvProducer, "sqlite", {"db_path": db})
    store.insert(SCOPE, "k", b"v")
    assert store.get(SCOPE, "k") == b"v"


def test_local_fs_builder_requires_root() -> None:
    """root 在构造器中无默认值 → 未配置时 build 阶段报错。"""
    with pytest.raises(ValidationError, match="root"):
        _build_store(FsProducer, "local")


def test_local_fs_builder_forwards_create_root(tmp_path) -> None:
    """create_root 必须能从 config 覆盖：传 False 时不创建 root 目录。"""
    target = tmp_path / "not_created"
    _build_store(FsProducer, "local", {"root": str(target), "create_root": False})
    assert not target.exists()  # create_root=False → build 时不建目录


def test_nano_graphrag_builder_requires_working_dir() -> None:
    with pytest.raises(ValidationError, match="working_dir"):
        _build_store(GraphProducer, "nano_graphrag")


# --------------------------------------------------------------- LocalFSStore
SCOPE = Scope(org="o1", user="u1")
OTHER = Scope(org="o2", user="u2")


@pytest.fixture
def fs(tmp_path) -> LocalFSStore:
    return LocalFSStore(root=str(tmp_path))


def test_fs_insert_get_roundtrip(fs: LocalFSStore) -> None:
    ref = fs.insert(SCOPE, "a/b/x.bin", io.BytesIO(b"hello"))
    assert ref == "a/b/x.bin"
    with fs.get(SCOPE, ref) as fh:
        assert fh.read() == b"hello"
    assert fs.stat(SCOPE, ref).size == 5


def test_fs_update_overwrites(fs: LocalFSStore) -> None:
    fs.insert(SCOPE, "x", io.BytesIO(b"old"))
    fs.update(SCOPE, "x", io.BytesIO(b"newer"))
    with fs.get(SCOPE, "x") as fh:
        assert fh.read() == b"newer"


def test_fs_insert_conflict(fs: LocalFSStore) -> None:
    fs.insert(SCOPE, "x", io.BytesIO(b"a"))
    with pytest.raises(ConflictError):
        fs.insert(SCOPE, "x", io.BytesIO(b"b"))


def test_fs_scope_isolates_same_key(fs: LocalFSStore) -> None:
    """同一逻辑 key 在不同 scope 下互不可见（命名空间隔离）。"""
    fs.insert(SCOPE, "x", io.BytesIO(b"mine"))
    # 另一 scope 下同名 key 不冲突，且读不到对方
    fs.insert(OTHER, "x", io.BytesIO(b"theirs"))
    with fs.get(SCOPE, "x") as fh:
        assert fh.read() == b"mine"
    with fs.get(OTHER, "x") as fh:
        assert fh.read() == b"theirs"


def test_fs_update_missing_raises(fs: LocalFSStore) -> None:
    with pytest.raises(NotFoundError):
        fs.update(SCOPE, "nope", io.BytesIO(b"a"))


def test_fs_get_missing_raises(fs: LocalFSStore) -> None:
    with pytest.raises(NotFoundError):
        fs.get(SCOPE, "nope")


def test_fs_delete_is_idempotent(fs: LocalFSStore) -> None:
    fs.insert(SCOPE, "x", io.BytesIO(b"a"))
    fs.delete(SCOPE, "x")
    fs.delete(SCOPE, "x")  # 再次删除不报错
    assert fs.health() is None  # health 正常时返回 None


def test_fs_blocks_path_traversal(fs: LocalFSStore) -> None:
    with pytest.raises(ValidationError):
        fs.insert(SCOPE, "../escape", io.BytesIO(b"a"))


# --------------------------------------------------------------- 惰性后端
def test_redis_backend_error_when_lib_missing() -> None:
    """未安装 redis 客户端时，构造成功、访问后端抛 BackendError（而非 ImportError）。"""
    try:
        import_module("redis")
    except ImportError:
        kv = RedisKVStore()
        with pytest.raises(BackendError):
            kv.get(SCOPE, "k")
    else:
        pytest.skip("redis installed; lazy-missing path not exercised")


def test_postgres_backend_error_when_lib_missing() -> None:
    try:
        import_module("psycopg")
        import_module("psycopg_pool")
    except ImportError:
        kv = PostgresKVStore(dsn="postgresql://example.invalid/db")
        with pytest.raises(BackendError, match="psycopg"):
            kv.get(SCOPE, "k")
    else:
        pytest.skip("psycopg installed; lazy-missing path not exercised")
