"""进程内锁：字典加租约，供单测与无 Redis 的本地开发使用。

**不提供跨实例互斥**——多个记忆服务实例各持一份字典，互不可见。生产部署必须配
``redis`` 实现。之所以仍保留租约与 token CAS 语义，是为了让单测覆盖到与 Redis 实现
一致的行为（过期后可被他人获得、token 不符不释放、续期失败返回 False）。

check-and-set 之间不 await，故在单个事件循环内是原子的。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

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


class InMemoryLockProvider(LockProvider):
    """进程内的 :class:`~common.lock.lock.LockProvider` 实现，不跨实例。"""

    def __init__(
        self,
        *,
        lease_ms: int = DEFAULT_LEASE_MS,
        wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    ) -> None:
        super().__init__(lease_ms=lease_ms, wait_timeout_ms=wait_timeout_ms)
        # key -> (token, 过期时刻)；用 monotonic 计时，不受系统时钟调整影响
        self._entries: dict[str, tuple[str, float]] = {}

    def _held_token(self, key: str, now: float) -> str | None:
        """返回当前有效持有者的 token；无人持有或已过期则 None（顺带清理）。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[1] <= now:
            self._entries.pop(key, None)
            return None
        return entry[0]

    async def _acquire(self, key: str, *, lease_ms: int, wait_timeout_ms: int) -> LockHandle:
        token = uuid.uuid4().hex
        async for _ in wait_ticks(wait_timeout_ms):
            now = time.monotonic()
            if self._held_token(key, now) is None:
                self._entries[key] = (token, now + lease_ms / 1000.0)
                return LockHandle(key=key, token=token, lease_ms=lease_ms)
        raise LockTimeoutError(
            f"等待 {wait_timeout_ms}ms 仍未获得锁 {key}（持有者未释放或租约未到期）"
        )

    async def _release(self, handle: LockHandle) -> None:
        if self._held_token(handle.key, time.monotonic()) == handle.token:
            self._entries.pop(handle.key, None)

    async def renew(self, handle: LockHandle, *, lease_ms: int | None = None) -> bool:
        now = time.monotonic()
        if self._held_token(handle.key, now) != handle.token:
            return False
        lease = handle.lease_ms if lease_ms is None else int(lease_ms)
        self._entries[handle.key] = (handle.token, now + lease / 1000.0)
        return True


@LockProducer.register("memory")
def _build(config: Any) -> InMemoryLockProvider:
    return InMemoryLockProvider(
        lease_ms=int(Factory.cfg_get(config, "lease_ms", DEFAULT_LEASE_MS)),
        wait_timeout_ms=int(Factory.cfg_get(config, "wait_timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)),
    )
