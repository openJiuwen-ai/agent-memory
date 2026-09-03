# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LockProvider — 跨实例互斥横切接口。

锁与 SecurityProvider / AuditLogger 同属横切组件：不继承 :class:`common.base.Plugin`、
不进入 ``PluginType``，但仍用 ``Factory`` 提供注册式装配。

本模块只交付互斥原语，**不在任何业务路径上加锁**——在哪些临界区取锁、锁多大范围，
由各消费方自行论证。设计取舍见 docs/features/common/F06-distributed-lock.md。

契约是异步的（``common`` 层唯一一处）：预期消费方本身是协程，且租约续期在异步下
只需一次 ``create_task``，同步实现则要为每把持有中的锁起守护线程。

**这是基于租约的协调机制，不是共识算法。** 租约到期、进程停顿超过租约、Redis 主从
切换丢失未同步写入，都会导致短暂双持。依赖本锁的业务必须能容忍偶发的互斥失效，
或自备第二道防线（幂等键、唯一约束、乐观并发控制）。
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from typing import AsyncIterator

from .._support import scope_segments
from ..errors import AgentMemoryError, ValidationError
from ..factory.factory import Factory
from ..log import get_logger, redact_for_log
from ..type_def import Scope

logger = get_logger(__name__)

#: 锁键前缀。KV 数据键是裸的五段命名空间，锁与数据共用一个 Redis 库时会撞键，故必须带
#: 前缀区分；``v1`` 供将来键结构变更时并存过渡。
KEY_PREFIX = "am:lock:v1"

DEFAULT_LEASE_MS = 30_000
DEFAULT_WAIT_TIMEOUT_MS = 10_000

#: 竞争等待的退避参数：初值、系数、上限（全抖动在各实现的等待循环内施加）。
BACKOFF_INITIAL_MS = 20.0
BACKOFF_FACTOR = 1.6
BACKOFF_CAP_MS = 200.0


class LockProducer(Factory):
    """LockProvider 的注册式工厂。

    各实现在 ``lock_impl`` 下以 ``@LockProducer.register("<名>")`` 自注册，当前有
    ``redis``（生产）与 ``memory``（单测与本地开发）两个。

    **不设默认实现**：消费方 ``dep(config)`` 时必须显式配置，让「忘了配 Redis」在装配期
    失败，而不是静默退化成不提供跨实例互斥的单机锁。
    """

    TOP_NAME = "lock"


class LockError(AgentMemoryError):
    """所有锁相关异常的基类。"""


class LockTimeoutError(LockError):
    """有界等待耗尽仍未获得锁。"""


class LockLostError(LockError):
    """租约续期失败，持有权已失效。

    本模块不主动抛出——续期失败经 :attr:`LockHandle.lost` 通知，是否升级为异常由
    临界区自行决定。提供该类型是为了让消费方有统一的语义可抛。
    """


@dataclass
class LockHandle:
    """一次持有的凭据。

    ``token`` 是释放与续期做 CAS 的依据：只有 token 匹配才动这把锁，避免租约过期后
    删掉他人重新获得的同名锁。
    """

    key: str
    token: str
    lease_ms: int
    #: True 表示本次是同 task 重入，未真正向后端申请；``guard`` 据此不再起续期任务。
    reentrant: bool = False
    #: 续期失败时置位。临界区可 ``handle.lost.is_set()`` 判断是否已失去持有权。
    lost: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Holding:
    """进程内的持有记账，支撑同 task 重入。"""

    handle: LockHandle
    task: asyncio.Task | None
    depth: int = 1


async def wait_ticks(wait_timeout_ms: int) -> AsyncIterator[int]:
    """竞争等待的节拍：每产出一次表示可以再试一次，时限耗尽即结束迭代。

    首次立即产出（``wait_timeout_ms=0`` 也至少试一次），之后按全抖动指数退避 sleep。
    全抖动而非固定间隔：多个实例同时释放并重试时，固定间隔会让它们持续同频碰撞。

    退避时长同时受剩余时限截断，避免最后一次 sleep 越过 deadline 白等。
    两个实现共用本节拍，保证竞争策略只有一份。
    """
    deadline = time.monotonic() + wait_timeout_ms / 1000.0
    delay_ms = BACKOFF_INITIAL_MS
    attempt = 0
    while True:
        yield attempt
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(random.uniform(0.0, delay_ms) / 1000.0, remaining))
        delay_ms = min(delay_ms * BACKOFF_FACTOR, BACKOFF_CAP_MS)


class LockProvider(ABC):
    """跨实例互斥能力。

    ``acquire`` / ``release`` / ``guard`` 在本层给出实现，只有后端原语
    （``_acquire`` / ``_release`` / ``renew``）下放给各实现。这偏离
    `common/AGENTS.md` 铁律 1 的「接口模块零依赖实现」，理由是重入记账与 guard 组合
    属于**契约级行为**而非后端细节：下沉会在两个实现里重复，且两份重入语义可能分叉。
    """

    def __init__(
        self,
        *,
        lease_ms: int = DEFAULT_LEASE_MS,
        wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    ) -> None:
        self._lease_ms = int(lease_ms)
        self._wait_timeout_ms = int(wait_timeout_ms)
        self._held: dict[str, _Holding] = {}

    # -- 键 ------------------------------------------------------------------ #

    @staticmethod
    def build_key(scope: Scope, name: str) -> str:
        """拼锁键：``am:lock:v1:{org}:{space}:{user}:{agent}:{session}:{name}``。

        **粒度由调用方决定，本组件不预设**：要用户级互斥就传一个 ``agent`` /
        ``session`` 置空的 ``Scope``，要更细的区分维度就写进 ``name``。粒度是业务
        判断，锁原语不替调用方选。

        ``name`` 是键的末段，不做转义——末段无歧义边界，段内出现 ``:`` 也不会与其他
        scope 的键折叠。
        """
        text = str(name or "").strip()
        if not text:
            raise ValidationError(
                "锁名 name 不能为空——它是锁键的末段，用于区分同一 scope 下的不同锁"
            )
        return ":".join((KEY_PREFIX, *scope_segments(scope), text))

    # -- 获取与释放 ----------------------------------------------------------- #

    async def acquire(
        self,
        scope: Scope,
        name: str,
        *,
        lease_ms: int | None = None,
        wait_timeout_ms: int | None = None,
    ) -> LockHandle:
        """获取锁，有界等待；超时抛 :class:`LockTimeoutError`。

        ``wait_timeout_ms=0`` 表示只试一次、不等待。

        **重入以 ``asyncio.current_task()`` 为身份边界**：同一 task 内嵌套获取同一键时
        递增计数并直接返回（``reentrant=True``），``create_task`` 派生的子任务不视为
        重入、会正常参与竞争。这条语义决定了消费方能否在同一调用栈内安全嵌套。

        重入记账是**进程内的调用嵌套记录，与租约是否仍然有效正交**：外层持有期间租约已
        过期时，重入仍会成功并返回同一 handle——此时该 handle 的 ``lost`` 已由看门狗置位，
        持有权状态一律以 ``lost`` 为准，不由重入路径二次判断。
        """
        key = self.build_key(scope, name)
        current = asyncio.current_task()
        holding = self._held.get(key)
        if holding is not None and holding.task is current:
            holding.depth += 1
            return replace(holding.handle, reentrant=True)

        handle = await self._acquire(
            key,
            lease_ms=self._lease_ms if lease_ms is None else int(lease_ms),
            wait_timeout_ms=(
                self._wait_timeout_ms if wait_timeout_ms is None else int(wait_timeout_ms)
            ),
        )
        self._held[key] = _Holding(handle=handle, task=current)
        return handle

    async def release(self, handle: LockHandle) -> None:
        """释放锁；重入时只递减计数，归零才真正释放。

        token 不匹配说明这把锁已被他人重新获得（本方租约过期），此时仍下发一次带 CAS 的
        释放——后端会因 token 不符而不动它，是安全的空操作。
        """
        holding = self._held.get(handle.key)
        if holding is not None and holding.handle.token == handle.token:
            holding.depth -= 1
            if holding.depth > 0:
                return
            self._held.pop(handle.key, None)
        await self._release(handle)

    @asynccontextmanager
    async def guard(
        self,
        scope: Scope,
        name: str,
        *,
        lease_ms: int | None = None,
        wait_timeout_ms: int | None = None,
        auto_renew: bool = True,
    ) -> AsyncIterator[LockHandle]:
        """获取 / 自动续期 / 释放的组合，推荐入口。

        ``auto_renew`` 默认开启：以 ``lease_ms / 3`` 为周期续期，临界区可长于租约。
        续期失败置位 ``handle.lost`` 并停止续期——不主动中断临界区，由持有者自行判断。
        """
        handle = await self.acquire(
            scope, name, lease_ms=lease_ms, wait_timeout_ms=wait_timeout_ms
        )
        renewer: asyncio.Task | None = None
        if auto_renew and not handle.reentrant:
            renewer = asyncio.create_task(self._renew_loop(handle))
        try:
            yield handle
        finally:
            # 先停续期再释放：反过来会让续期循环把刚释放的锁重新续上一个租约周期。
            if renewer is not None:
                renewer.cancel()
                with suppress(asyncio.CancelledError):
                    await renewer
            await self.release(handle)

    @abstractmethod
    async def renew(self, handle: LockHandle, *, lease_ms: int | None = None) -> bool:
        """按 token 做 CAS 续期；返回 False 表示已失去持有权。"""

    async def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则由实现抛出异常。

        与其余组件的同步 ``health()`` 不一致——本组件整体异步，探测需要往后端发一次
        往返。消费方级联调用时须 ``await``。
        """
        return None

    async def _renew_loop(self, handle: LockHandle) -> None:
        """看门狗：周期性续期，一旦失败就置位 ``handle.lost`` 并退出。

        必须通知临界区——续期失败意味着持有权已经丢失，若无通知，持有者会在无锁状态下
        把临界区执行完。
        """
        interval = max(handle.lease_ms / 3.0, 1.0) / 1000.0
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.renew(handle)
            except Exception as exc:  # 续期失败一律视为失去持有权，不向上抛断掉临界区
                logger.warning(
                    "lock renew failed key=%s: error_type=%s",
                    redact_for_log(handle.key),
                    type(exc).__name__,
                )
                renewed = False
            if not renewed:
                logger.warning("lock lost key=%s", redact_for_log(handle.key))
                handle.lost.set()
                return

    # -- 后端原语 ------------------------------------------------------------- #

    @abstractmethod
    async def _acquire(self, key: str, *, lease_ms: int, wait_timeout_ms: int) -> LockHandle:
        """向后端申请锁，含有界等待与退避；超时抛 :class:`LockTimeoutError`。"""

    @abstractmethod
    async def _release(self, handle: LockHandle) -> None:
        """向后端释放锁，必须按 token 做 CAS。"""
