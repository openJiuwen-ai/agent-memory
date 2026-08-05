"""Redis 分布式锁：``SET NX PX`` 获取，Lua CAS 释放与续期。

三个原语各自的原子性来源：

- **获取**：``SET key token NX PX lease`` 是单条命令，Redis 单线程执行，天然原子。
- **释放**：GET → 比较 → DEL 三步在客户端做非原子——两步之间租约可能过期、他人拿到
  同名锁，此时 DEL 会删掉别人的锁。故整段下推为 Lua 脚本，在服务端一次执行。
- **续期**：同理，必须先确认 token 仍是自己的才 ``PEXPIRE``，否则会给他人的锁续命。

不可用时 fail-closed：异常归一为 ``BackendError`` 向上抛，不提供静默降级为无锁的旁路。
"""

from __future__ import annotations

import uuid
from typing import Any

from common._support import (
    read_ssl_config,
    reject_url_tls_params,
    require_tls_scheme,
    wrap_backend,
)
from common.errors import BackendError
from common.factory.factory import Factory
from common.lock.lock import (
    DEFAULT_LEASE_MS,
    DEFAULT_WAIT_TIMEOUT_MS,
    LockHandle,
    LockProducer,
    LockProvider,
    LockTimeoutError,
    wait_ticks,
)

_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""

_RENEW_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
  return 0
end
"""


class RedisLockProvider(LockProvider):
    """基于 Redis 的 :class:`~common.lock.lock.LockProvider` 实现。

    与 redis KV 后端各自持有连接池、互不复用：KV 是同步客户端，锁是异步客户端，
    且锁的高频短写与业务读写混在同一池里会互相影响尾延迟。
    """

    def __init__(
        self,
        *,
        url: str,
        lease_ms: int = DEFAULT_LEASE_MS,
        wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
        **options: Any,
    ) -> None:
        super().__init__(lease_ms=lease_ms, wait_timeout_ms=wait_timeout_ms)
        self._url = url
        self._options = options
        self._client: Any = None
        self._release_script: Any = None
        self._renew_script: Any = None

    @property
    def client(self) -> Any:
        """惰性建连并注册 Lua 脚本——装配期不连后端，首次用到才连。"""
        if self._client is None:
            try:
                from redis import asyncio as redis_asyncio
            except ImportError as exc:
                raise BackendError("redis client not installed (pip install redis)") from exc
            with wrap_backend("redis lock connect"):
                client = redis_asyncio.Redis.from_url(
                    self._url, decode_responses=True, **self._options
                )
                self._release_script = client.register_script(_RELEASE_LUA)
                self._renew_script = client.register_script(_RENEW_LUA)
                self._client = client
        return self._client

    async def _acquire(self, key: str, *, lease_ms: int, wait_timeout_ms: int) -> LockHandle:
        token = uuid.uuid4().hex
        client = self.client
        async for _ in wait_ticks(wait_timeout_ms):
            with wrap_backend("redis lock acquire"):
                acquired = await client.set(key, token, nx=True, px=lease_ms)
            if acquired:
                return LockHandle(key=key, token=token, lease_ms=lease_ms)
        raise LockTimeoutError(
            f"等待 {wait_timeout_ms}ms 仍未获得锁 {key}（持有者未释放或租约未到期）"
        )

    async def _release(self, handle: LockHandle) -> None:
        client = self.client
        with wrap_backend("redis lock release"):
            await self._release_script(keys=[handle.key], args=[handle.token], client=client)

    async def renew(self, handle: LockHandle, *, lease_ms: int | None = None) -> bool:
        lease = handle.lease_ms if lease_ms is None else int(lease_ms)
        client = self.client
        with wrap_backend("redis lock renew"):
            result = await self._renew_script(
                keys=[handle.key], args=[handle.token, lease], client=client
            )
        return bool(result)

    async def health(self) -> None:
        client = self.client
        with wrap_backend("redis lock ping"):
            await client.ping()


@LockProducer.register("redis")
def _build(config: Any) -> RedisLockProvider:
    url = Factory.require_param(config, "url", backend="redis lock")
    ssl = read_ssl_config(config, backend="redis lock")
    options: dict[str, Any] = {}
    if ssl.verify:
        # 加密开关只在 scheme 上：放行 redis:// 会让证书参数传入却不生效，连接以明文
        # 建立而调用方以为已加密。与 redis KV 后端同一套装配期校验。
        require_tls_scheme(url, expected="rediss", component="redis lock", param="params.url")
        reject_url_tls_params(url, backend="redis lock", param="url")
        options["ssl_ca_certs"] = ssl.ca_cert
    return RedisLockProvider(
        url=url,
        lease_ms=int(Factory.cfg_get(config, "lease_ms", DEFAULT_LEASE_MS)),
        wait_timeout_ms=int(Factory.cfg_get(config, "wait_timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)),
        **options,
    )
