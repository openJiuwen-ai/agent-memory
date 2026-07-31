"""存储后端 SSL 配置（``ssl_verify`` / ``ssl_ca_cert``）的读取、校验与翻译测试。

四个后端共用同一组配置参数，但翻译目标各不相同（``ssl_ca_certs`` / ``ca_certs`` /
``sslrootcert`` / ``server_pem_path``），且加密开关的位置分两类：redis 与 elasticsearch
只认连接串 scheme，postgres 与 milvus 有参数形态的真开关。这里覆盖：

- 布尔归一：``${VAR:-false}`` 展开后是字符串 ``"false"``，不得被判为真；
- 缺证书即在装配阶段报错，不静默降级到系统 CA；
- scheme 与 ``ssl_verify`` 不一致时装配期报错，避免"以为加密了其实没有"；
- 开启后证书参数确实抵达客户端（经注入的假客户端观察，不触碰受保护成员）。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from common.errors import BackendError, ValidationError
from config import AssemblyContext
from storage._support import as_bool, read_ssl_config
from storage.bootstrap import register_backends
from storage.fulltext import FulltextProducer
from storage.kv import KvProducer
from storage.vector import VectorProducer

pytestmark = pytest.mark.unit

register_backends()


# -- 布尔归一 ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("false", False),  # ${VAR:-false} 展开后的典型值，直接 if 判定会为真
        ("FALSE", False),
        ("off", False),
        ("0", False),
        ("", False),
        (None, False),
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("1", True),
        (True, True),
    ],
)
def test_as_bool_normalizes_string_config_values(value: Any, expected: bool) -> None:
    assert as_bool(value, default=False) is expected


def test_as_bool_falls_back_to_default_only_for_none() -> None:
    assert as_bool(None, default=True) is True
    assert as_bool("false", default=True) is False


# -- 配置读取 ---------------------------------------------------------------- #


def test_ssl_is_disabled_by_default() -> None:
    """不配置时行为与引入本参数前一致，现有明文部署不受影响。"""
    ssl = read_ssl_config({}, backend="test")
    assert ssl.verify is False
    assert ssl.ca_cert is None


def test_blank_ca_cert_is_normalized_to_none() -> None:
    """``${VAR:-}`` 展开出空串，须归一为 None 而非空路径。"""
    ssl = read_ssl_config({"ssl_ca_cert": "  "}, backend="test")
    assert ssl.ca_cert is None


def test_verify_without_ca_cert_fails_at_assembly() -> None:
    """云厂商多为自签 CA，缺证书时客户端会拿系统 CA 校验失败，故提前拦截。"""
    with pytest.raises(ValidationError, match="ssl_ca_cert"):
        read_ssl_config({"ssl_verify": "true"}, backend="test")


# -- scheme 一致性：redis 与 elasticsearch 的加密开关只存在于连接串 -------------- #


def test_redis_rejects_plaintext_scheme_when_verify_enabled() -> None:
    with pytest.raises(ValidationError, match="rediss://"):
        KvProducer.build(
            "redis",
            {"url": "redis://h:6379/0", "ssl_verify": "true", "ssl_ca_cert": "/certs/ca.crt"},
            AssemblyContext(),
        )


def test_elasticsearch_rejects_plaintext_scheme_when_verify_enabled() -> None:
    with pytest.raises(ValidationError, match="https://"):
        FulltextProducer.build(
            "elasticsearch",
            {"hosts": "http://h:9200", "ssl_verify": "true", "ssl_ca_cert": "/certs/ca.pem"},
            AssemblyContext(),
        )


@pytest.mark.parametrize(
    ("producer", "target", "params"),
    [
        (KvProducer, "redis", {"url": "rediss://h:36379/0"}),
        (KvProducer, "postgres", {"dsn": "postgresql://u@h/db"}),
        (FulltextProducer, "elasticsearch", {"hosts": "https://h:9200"}),
        (VectorProducer, "milvus", {"uri": "https://h:19530", "dim": 8}),
        (VectorProducer, "pgvector", {"dsn": "postgresql://u@h/db", "dim": 8}),
    ],
)
def test_every_backend_requires_ca_cert_when_verify_enabled(
    producer: Any, target: str, params: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="ssl_ca_cert"):
        producer.build(target, {**params, "ssl_verify": "true"}, AssemblyContext())


def test_elasticsearch_accepts_multi_node_https_hosts() -> None:
    """hosts 支持多节点列表，须逐个校验——整体转字符串会误拒合法配置。"""
    FulltextProducer.build(
        "elasticsearch",
        {
            "hosts": ["https://a:9200", "https://b:9200"],
            "ssl_verify": "true",
            "ssl_ca_cert": "/certs/ca.pem",
        },
        AssemblyContext(),
    )


def test_elasticsearch_rejects_plaintext_node_mixed_into_list() -> None:
    with pytest.raises(ValidationError, match="https://"):
        FulltextProducer.build(
            "elasticsearch",
            {
                "hosts": ["https://a:9200", "http://b:9200"],
                "ssl_verify": "true",
                "ssl_ca_cert": "/certs/ca.pem",
            },
            AssemblyContext(),
        )


# -- URL 自带 TLS 参数会覆盖并可能关闭校验，须拒绝 ------------------------------- #


@pytest.mark.parametrize(
    "query",
    ["ssl_cert_reqs=none", "ssl_check_hostname=false", "ssl_ca_certs=/other/ca.crt"],
)
def test_redis_rejects_tls_params_in_url_when_verify_enabled(query: str) -> None:
    with pytest.raises(ValidationError, match="TLS 查询参数"):
        KvProducer.build(
            "redis",
            {
                "url": f"rediss://h:36379/0?{query}",
                "ssl_verify": "true",
                "ssl_ca_cert": "/certs/ca.crt",
            },
            AssemblyContext(),
        )


def test_redis_allows_non_tls_query_params() -> None:
    """只拦 ssl_* 前缀，socket_timeout 等仍可经 URL 配置。"""
    KvProducer.build(
        "redis",
        {
            "url": "rediss://h:36379/0?socket_timeout=5&socket_connect_timeout=5",
            "ssl_verify": "true",
            "ssl_ca_cert": "/certs/ca.crt",
        },
        AssemblyContext(),
    )


def test_redis_url_tls_params_allowed_when_verify_disabled() -> None:
    """ssl_verify=false 是把 TLS 全权交给连接串的逃生舱，此时不干预。"""
    KvProducer.build(
        "redis",
        {"url": "rediss://h:36379/0?ssl_cert_reqs=none", "ssl_verify": "false"},
        AssemblyContext(),
    )


# -- 翻译：证书参数确实抵达客户端 ----------------------------------------------- #


class _FakeRedis:
    """记录 ``from_url`` 收到的 kwargs，用于观察翻译结果。"""

    last_kwargs: dict[str, Any] = {}

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "_FakeRedis":
        cls.last_kwargs = {"url": url, **kwargs}
        return cls()


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRedis]:
    _FakeRedis.last_kwargs = {}
    module = ModuleType("redis")
    setattr(module, "Redis", _FakeRedis)
    monkeypatch.setitem(sys.modules, "redis", module)
    return _FakeRedis


def test_redis_passes_ca_cert_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_redis(monkeypatch)
    store = KvProducer.build(
        "redis",
        {
            "url": "rediss://h:36379/0",
            "ssl_verify": "true",
            "ssl_ca_cert": "/certs/dcs-ca.crt",
        },
        AssemblyContext(),
    )
    store.client  # 惰性建连，触发一次客户端构造

    assert fake.last_kwargs["url"] == "rediss://h:36379/0"
    assert fake.last_kwargs["ssl_ca_certs"] == "/certs/dcs-ca.crt"


def test_redis_passes_no_ssl_kwargs_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭时不得往客户端塞任何 TLS 参数，保持既有明文行为。"""
    fake = _install_fake_redis(monkeypatch)
    store = KvProducer.build("redis", {"url": "redis://h:6379/0"}, AssemblyContext())
    store.client

    assert not [key for key in fake.last_kwargs if key.startswith("ssl_")]


class _Recorded(Exception):
    """哨兵：客户端构造参数已记录，就地中断，不触发真实连接。"""


def _recorder(sink: dict[str, Any]):
    def factory(*args: Any, **kwargs: Any) -> Any:
        sink.update(kwargs)
        raise _Recorded
    return factory


def test_elasticsearch_passes_ca_cert_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    elasticsearch = pytest.importorskip("elasticsearch")
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(elasticsearch, "Elasticsearch", _recorder(recorded))

    store = FulltextProducer.build(
        "elasticsearch",
        {
            "hosts": "https://h:9200",
            "ssl_verify": "true",
            "ssl_ca_cert": "/certs/css-ca.pem",
        },
        AssemblyContext(),
    )
    with pytest.raises(BackendError):
        store.client  # 构造边界抛哨兵，经 wrap_backend 归一为 BackendError

    assert recorded["ca_certs"] == "/certs/css-ca.pem"


def test_milvus_passes_server_pem_path_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """单向 TLS 落 server_pem_path；ca_pem_path 属双向分支，单独给会静默失效。"""
    pymilvus = pytest.importorskip("pymilvus")
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(pymilvus, "MilvusClient", _recorder(recorded))

    store = VectorProducer.build(
        "milvus",
        {
            "uri": "https://h:19530",
            "dim": 8,
            "ssl_verify": "true",
            "ssl_ca_cert": "/certs/milvus-ca.pem",
        },
        AssemblyContext(),
    )
    with pytest.raises(BackendError):
        store.client

    assert recorded["secure"] is True
    assert recorded["server_pem_path"] == "/certs/milvus-ca.pem"
    assert "ca_pem_path" not in recorded


class _FakePool:
    """记录 ConnectionPool 构造参数；open 抛哨兵以免真实建连。"""

    recorded: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).recorded = dict(kwargs)

    def open(self, **_: Any) -> None:
        raise _Recorded

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("producer", "target", "params"),
    [
        (KvProducer, "postgres", {"dsn": "postgresql://u@h/db"}),
        (VectorProducer, "pgvector", {"dsn": "postgresql://u@h/db", "dim": 8}),
    ],
)
def test_postgres_family_passes_sslmode_and_root_cert(
    monkeypatch: pytest.MonkeyPatch, producer: Any, target: str, params: dict[str, Any]
) -> None:
    psycopg_pool = pytest.importorskip("psycopg_pool")
    _FakePool.recorded = {}
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)

    store = producer.build(
        target,
        {**params, "ssl_verify": "true", "ssl_ca_cert": "/certs/rds-ca.pem"},
        AssemblyContext(),
    )
    with pytest.raises(BackendError):
        store.pool

    connect_kwargs = _FakePool.recorded["kwargs"]
    assert connect_kwargs["sslmode"] == "verify-full"
    assert connect_kwargs["sslrootcert"] == "/certs/rds-ca.pem"


def test_postgres_leaves_dsn_untouched_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭时不得注入 sslmode，避免覆盖 dsn 里既有的 TLS 设定。"""
    psycopg_pool = pytest.importorskip("psycopg_pool")
    _FakePool.recorded = {}
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)

    store = KvProducer.build(
        "postgres", {"dsn": "postgresql://u@h/db?sslmode=require"}, AssemblyContext()
    )
    with pytest.raises(BackendError):
        store.pool

    connect_kwargs = _FakePool.recorded["kwargs"]
    assert "sslmode" not in connect_kwargs
    assert "sslrootcert" not in connect_kwargs
