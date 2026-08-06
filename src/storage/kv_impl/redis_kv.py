"""RedisKVStore — 基于 Redis 的 :class:`~storage.kv.KVStore` 实现。

``redis`` 客户端在首次使用时惰性导入与连接，故未安装 ``redis-py`` 或后端未就绪
时，仍可 ``import storage`` 与注册工厂；只有真正访问后端才会触发 ``BackendError``。
``scope`` 入参对 Redis key 做命名空间隔离（``org:space:user:agent:session:<key>``），同一
逻辑 ``key`` 在不同 scope 下互不可见。``ttl`` 为秒（float），``0`` 表示永不过期。

连接串 ``url`` 可经 :class:`~config.config_source.ConfigSource` 晚绑定（key
``kv_store.url``，S08）；URL 变化时丢弃旧客户端并重连。旧库数据不自动迁移。
"""

from __future__ import annotations

from typing import Any

from common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
)
from common.factory.factory import Factory
from common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from storage.kv import KvProducer

from .._support import (
    read_ssl_config,
    reject_url_tls_params,
    require_tls_scheme,
    scope_segments,
    wrap_backend,
)
from ..base import StoreType
from ..kv import KVStore
from ..types import KVMemoryListResult
from .memory_list import list_memory_entries


def _decode_scope_segment(segment: str) -> str:
    return "" if segment == "_" else segment


class RedisKVStore(KVStore):
    """Redis KV 后端；支持构造期 url/host 与运行时 ``kv_store.*`` 晚绑定。"""

    def __init__(
        self,
        *,
        url: str | None = None,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        config_source=None,
        config_namespace: str = "kv_store",
        **options: Any,
    ) -> None:
        # 构造期字段为 ConfigSource 缺失时的回落
        self._fallback_url = url
        self._fallback_host = host
        self._fallback_port = int(port)
        self._fallback_db = int(db)
        self._fallback_password = password
        self._config_source = config_source
        self._config_namespace = config_namespace
        # url 分支走 from_url，不读 host 指纹，故 options 需单独留一份透传（见 client）。
        self._options = dict(options)
        self._client: Any = None
        self._client_fingerprint: object | None = None

    def _resolved_url(self) -> str | None:
        """解析当前应连接的 Redis URL（ConfigSource ``kv_store.url`` 优先）。"""
        from config.binding import resolve_connection_url

        return resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="url",
            fallback=self._fallback_url,
        )

    def _resolved_host_conn(self) -> dict[str, Any]:
        """无 url 时解析 host/port/db/password（ConfigSource 晚绑定）。"""
        from config.active import resolve_bound_value

        host = resolve_bound_value(
            self._config_source,
            namespace=self._config_namespace,
            field="host",
            fallback=self._fallback_host,
        ) or self._fallback_host
        port_raw = resolve_bound_value(
            self._config_source,
            namespace=self._config_namespace,
            field="port",
            fallback=str(self._fallback_port),
        )
        db_raw = resolve_bound_value(
            self._config_source,
            namespace=self._config_namespace,
            field="db",
            fallback=str(self._fallback_db),
        )
        password = resolve_bound_value(
            self._config_source,
            namespace=self._config_namespace,
            field="password",
            fallback=self._fallback_password,
        )
        return {
            "host": host,
            "port": int(port_raw) if port_raw is not None else self._fallback_port,
            "db": int(db_raw) if db_raw is not None else self._fallback_db,
            "password": password if password not in (None, "") else self._fallback_password,
            **self._options,
        }

    def _client_key(self) -> object:
        """当前连接指纹：有 url 用 url；否则用 host 四元组。"""
        url = self._resolved_url()
        if url:
            return ("url", url)
        conn = self._resolved_host_conn()
        return (
            "host",
            conn["host"],
            conn["port"],
            conn["db"],
            conn.get("password"),
        )

    @property
    def client(self) -> Any:
        """惰性创建 Redis 客户端（``decode_responses=False``，值以 bytes 收发）。

        ``kv_store.url`` 或 ``host``/``port``/``db``/``password`` 经 ConfigSource
        晚绑定；指纹变化时重建客户端。
        """
        key = self._client_key()
        if self._client is not None and self._client_fingerprint == key:
            return self._client
        try:
            import redis
        except ImportError as exc:  # 依赖缺失归一为后端不可用
            raise BackendError(
                "redis client not installed (pip install redis)"
            ) from exc
        with wrap_backend("redis connect"):
            if key[0] == "url":
                # url 里的 query 参数优先级高于此处 kwargs（redis-py 解析顺序所致）。
                self._client = redis.Redis.from_url(
                    key[1], decode_responses=False, **self._options
                )
            else:
                conn = self._resolved_host_conn()
                self._client = redis.Redis(decode_responses=False, **conn)
        self._client_fingerprint = key
        return self._client

    @staticmethod
    def _namespaced(scope: Scope, key: str) -> str:
        return ":".join((*scope_segments(scope), key))

    @staticmethod
    def _px(ttl: float) -> int | None:
        return int(ttl * 1000) if ttl and ttl > 0 else None

    def store_type(self) -> StoreType:
        """返回存储类型 ``KV``。"""
        return StoreType.KV

    def health(self) -> None:
        """对 Redis 执行 ``PING``；失败抛 :class:`HealthCheckError`。"""
        try:
            ok = self.client.ping()
        except Exception as exc:
            raise HealthCheckError(f"redis ping failed: {exc}") from exc
        if not ok:
            raise HealthCheckError("redis ping returned falsy")

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """在 ``scope`` 下新建 ``key``；已存在时报冲突。"""
        nk = self._namespaced(scope, key)
        with wrap_backend(f"redis insert {key!r}"):
            ok = self.client.set(nk, value, nx=True, px=self._px(ttl))
        if not ok:  # NX 未写入 = 键已存在
            raise ConflictError(entity="key", key=key)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """覆写 ``scope`` 下已有 ``key``；不存在时报缺失。"""
        nk = self._namespaced(scope, key)
        with wrap_backend(f"redis update {key!r}"):
            ok = self.client.set(nk, value, xx=True, px=self._px(ttl))
        if not ok:  # XX 未写入 = 键不存在
            raise NotFoundError(entity="key", key=key)

    def delete(self, scope: Scope, key: str) -> None:
        """删除 ``scope`` 下的 ``key``（幂等）。"""
        with wrap_backend(f"redis delete {key!r}"):
            self.client.delete(self._namespaced(scope, key))  # 幂等

    def get(self, scope: Scope, key: str) -> bytes:
        """读取 ``scope`` 下 ``key`` 的值；不存在时报缺失。"""
        with wrap_backend(f"redis get {key!r}"):
            value = self.client.get(self._namespaced(scope, key))
        if value is None:
            raise NotFoundError(entity="key", key=key)
        return value

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        """批量读取 ``scope`` 下多个 ``key``；返回与 ``keys`` 下标一一对应。"""
        # 原生 MGET 一次往返召回，返回与 keys 下标一一对应；天然支持重复 key。
        # 缺失位 redis 返回 None，归一为 NotFoundError（与 get 一致）。
        if not keys:
            return []
        namespaced = [self._namespaced(scope, key) for key in keys]
        with wrap_backend(f"redis mget {len(keys)} keys"):
            values = self.client.mget(namespaced)
        out: list[bytes] = []
        for key, value in zip(keys, values):
            if value is None:
                raise NotFoundError(entity="key", key=key)
            out.append(value)
        return out

    def exists(self, scope: Scope, key: str) -> bool:
        """返回 ``scope`` 下 ``key`` 是否存在。"""
        with wrap_backend(f"redis exists {key!r}"):
            return self.client.exists(self._namespaced(scope, key)) > 0

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """扫描 ``scope`` 下全部 ``(key, value)``（可选 ``prefix`` 过滤）。"""
        ns = ":".join(scope_segments(scope)) + ":"  # 该 scope 的命名空间前缀
        with wrap_backend(f"redis scan {prefix!r}"):
            keys = list(self.client.scan_iter(match=f"{ns}{prefix}*"))
            values = self.client.mget(keys) if keys else []
        out: list[tuple[str, bytes]] = []
        for raw, value in zip(keys, values):
            if value is None:  # scan 与 mget 之间过期/删除
                continue
            k = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            out.append((k[len(ns):], value))  # 去掉命名空间前缀还原逻辑 key
        return out

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        """按记忆列表协议分页枚举 ``scope`` 下条目。"""
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        """枚举本存储中已用过的全部 scope（命名空间）。"""
        seen: set[tuple[str, str, str, str, str]] = set()
        with wrap_backend("redis scopes"):
            for raw in self.client.scan_iter(match="*"):
                k = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                parts = k.split(":", 5)
                if len(parts) < 6:  # 非本存储写入的键（不足 5 段 scope + key）
                    continue
                seen.add(tuple(parts[:5]))
        # 还原定长五段：``_`` 占位还原为空维度（与 scope_segments 互逆）
        scopes = []
        for segments in seen:
            scopes.append(
                Scope(
                    org=_decode_scope_segment(segments[0]),
                    space=_decode_scope_segment(segments[1]),
                    user=_decode_scope_segment(segments[2]),
                    agent=_decode_scope_segment(segments[3]),
                    session=_decode_scope_segment(segments[4]),
                )
            )
        return scopes


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -------- #


@KvProducer.register("redis")
def _build(config):
    """装配 Redis KV；注入 default ConfigSource 以便运行时晚绑定 ``kv_store.url``。

    三方库后端：url 必填，未配置即在 build 阶段报错（而非惰性连接时才暴露）。
    """
    from config.config_source import ConfigSourceProducer

    url = Factory.require_param(config, "url", backend="redis KV")
    ssl = read_ssl_config(config, backend="redis KV")
    options: dict[str, Any] = {}
    if ssl.verify:
        require_tls_scheme(
            url, expected="rediss", component="redis KV", param="params.url"
        )
        reject_url_tls_params(url, backend="redis KV", param="url")
        options["ssl_ca_certs"] = ssl.ca_cert
    return RedisKVStore(
        url=url,
        host=Factory.cfg_get(config, "host", "localhost"),
        port=Factory.cfg_get(config, "port", 6379),
        db=Factory.cfg_get(config, "db", 0),
        password=Factory.cfg_get(config, "password"),
        config_source=ConfigSourceProducer.get_cached("default"),
        **options,
    )
