"""RedisKVStore — 基于 Redis 的 :class:`~storage.kv.KVStore` 实现。

``redis`` 客户端在首次使用时惰性导入与连接，故未安装 ``redis-py`` 或后端未就绪
时，仍可 ``import storage`` 与注册工厂；只有真正访问后端才会触发 ``BackendError``。
``scope`` 入参对 Redis key 做命名空间隔离（``org:space:user:agent:session:<key>``），同一
逻辑 ``key`` 在不同 scope 下互不可见。``ttl`` 为秒（float），``0`` 表示永不过期。
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
from common.type_def import Scope
from storage.kv import KvProducer

from .._support import scope_segments, wrap_backend
from ..base import StoreType
from ..kv import KVStore


def _decode_scope_segment(segment: str) -> str:
    return "" if segment == "_" else segment


class RedisKVStore(KVStore):
    def __init__(
        self,
        *,
        url: str | None = None,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        **options: Any,
    ) -> None:
        self._url = url
        self._conn = dict(host=host, port=port, db=db, password=password, **options)
        self._client: Any = None

    @property
    def client(self) -> Any:
        """惰性创建 Redis 客户端（``decode_responses=False``，值以 bytes 收发）。"""
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # 依赖缺失归一为后端不可用
                raise BackendError(
                    "redis client not installed (pip install redis)"
                ) from exc
            with wrap_backend("redis connect"):
                if self._url:
                    self._client = redis.Redis.from_url(
                        self._url, decode_responses=False
                    )
                else:
                    self._client = redis.Redis(decode_responses=False, **self._conn)
        return self._client

    @staticmethod
    def _namespaced(scope: Scope, key: str) -> str:
        return ":".join((*scope_segments(scope), key))

    @staticmethod
    def _px(ttl: float) -> int | None:
        return int(ttl * 1000) if ttl and ttl > 0 else None

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        try:
            ok = self.client.ping()
        except Exception as exc:
            raise HealthCheckError(f"redis ping failed: {exc}") from exc
        if not ok:
            raise HealthCheckError("redis ping returned falsy")

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        nk = self._namespaced(scope, key)
        with wrap_backend(f"redis insert {key!r}"):
            ok = self.client.set(nk, value, nx=True, px=self._px(ttl))
        if not ok:  # NX 未写入 = 键已存在
            raise ConflictError(entity="key", key=key)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        nk = self._namespaced(scope, key)
        with wrap_backend(f"redis update {key!r}"):
            ok = self.client.set(nk, value, xx=True, px=self._px(ttl))
        if not ok:  # XX 未写入 = 键不存在
            raise NotFoundError(entity="key", key=key)

    def delete(self, scope: Scope, key: str) -> None:
        with wrap_backend(f"redis delete {key!r}"):
            self.client.delete(self._namespaced(scope, key))  # 幂等

    def get(self, scope: Scope, key: str) -> bytes:
        with wrap_backend(f"redis get {key!r}"):
            value = self.client.get(self._namespaced(scope, key))
        if value is None:
            raise NotFoundError(entity="key", key=key)
        return value

    def exists(self, scope: Scope, key: str) -> bool:
        with wrap_backend(f"redis exists {key!r}"):
            return self.client.exists(self._namespaced(scope, key)) > 0

    def list(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        ns = ":".join(scope_segments(scope)) + ":"  # 该 scope 的命名空间前缀
        with wrap_backend(f"redis list {prefix!r}"):
            keys = list(self.client.scan_iter(match=f"{ns}{prefix}*"))
            values = self.client.mget(keys) if keys else []
        out: list[tuple[str, bytes]] = []
        for raw, value in zip(keys, values):
            if value is None:  # scan 与 mget 之间过期/删除
                continue
            k = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            out.append((k[len(ns):], value))  # 去掉命名空间前缀还原逻辑 key
        return out

    def scopes(self) -> list[Scope]:
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
    # 三方库后端：url 必填，未配置即在 build 阶段报错（而非惰性连接时才暴露）。
    url = Factory.require_param(config, "url", backend="redis KV")
    return RedisKVStore(
        url=url,
        host=Factory.cfg_get(config, "host", "localhost"),
        port=Factory.cfg_get(config, "port", 6379),
        db=Factory.cfg_get(config, "db", 0),
        password=Factory.cfg_get(config, "password"),
    )
