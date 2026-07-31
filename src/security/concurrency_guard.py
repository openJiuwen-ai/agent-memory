"""Argon2 verify 的进程级并发上限（security.md §8.1 / 审计 P1-3）。

IP 令牌桶限的是「单地址的请求速率」，限不住「同时在跑的 Argon2 verify 数」--
后者才是 CPU/内存耗尽攻击的真正向量：单 IP 30 个并发错误 key = 30 × 128 MiB
同时驻留。本模块是进程级 ``BoundedSemaphore``，在 ``authenticate`` 之前 acquire，
耗尽即拒（返回 429），是 IP 桶之上的第一层。

**不进 Factory / Producer**：进程级状态按配置实例化多份没有意义--一个进程只有
一份「正在跑的 verify 数」计数器。装配在 :func:`security.bootstrap.register_security`
里算一次默认上限（见 ``default_argon2_guard``），``auth_middleware`` 取它用。

DEV 模式不跑 Argon2（恒返回 ROOT），不需要 guard。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

_LOG = logging.getLogger(__name__)

# Argon2id 单次 verify 内存 128 MiB。默认按「给认证留 ~512 MiB」预算：4 个并发
# 同时最多吃 512 MiB，留出业务内存。可按机器内存调（见 default_argon2_guard）。
_DEFAULT_MAX_CONCURRENT = 4

# 进程级单例：所有请求共享。None 表示「不限」（DEV 模式或显式关闭）。
_guard: "Optional[Argon2Guard]" = None
_guard_lock = threading.Lock()


class Argon2Guard:
    """进程级 Argon2 verify 并发上限。

    acquire 非阻塞：有空槽立刻占用并返回 True，否则返回 False（让中间件
    翻译成 429）。不在 acquire 处阻塞等待--排队会让线程无界堆积，且
    攻击者能用慢请求占满队列把后续正常请求也堵死。
    """

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent 须 >= 1，得到 {max_concurrent}")
        self._max = max_concurrent
        self._sem = threading.BoundedSemaphore(max_concurrent)

    def acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            # release 过多次（acquire 失败后误 release）：不抛，但记一笔--
            # BoundedSemaphore 超过初始值会 ValueError，吞掉会让计数器永久偏。
            _LOG.error("Argon2Guard release 越界（acquire 未成功即 release？）", exc_info=True)

    @property
    def max_concurrent(self) -> int:
        return self._max


def default_argon2_guard(max_concurrent: int | None = None) -> Argon2Guard:
    """取/建进程级 guard 单例。

    第一次调用按 ``max_concurrent``（默认 4）建；后续调用返回同一实例。若后续调用
    传了**不同**的 ``max_concurrent``，抛 ``ValueError``--同进程多 Server / 热重载
    场景下静默忽略配置会让 ``argon2.max_concurrent`` 失效（审计验收 P2-guard）。

    传 ``None`` 用默认上限；显式传 0 是非法（须装配期报错，不能用 ``or`` 吞成默认）。
    """
    global _guard
    effective = _DEFAULT_MAX_CONCURRENT if max_concurrent is None else max_concurrent
    with _guard_lock:
        if _guard is None:
            _guard = Argon2Guard(effective)
            return _guard
        if effective != _guard.max_concurrent:
            raise ValueError(
                f"argon2.max_concurrent 配置冲突：进程已有 guard(max={_guard.max_concurrent})，"
                f"本次请求 max={effective}。进程级 guard 是单例，同进程不可配不同上限。"
            )
        return _guard


def reset_guard() -> None:
    """重置单例（测试隔离用）。"""
    global _guard
    with _guard_lock:
        _guard = None
