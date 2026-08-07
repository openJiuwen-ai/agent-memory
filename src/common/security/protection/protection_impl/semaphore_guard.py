"""进程内信号量预算：昂贵安全操作的并发上限（F05 §Protection §WorkloadGuard）。

``BoundedSemaphore`` 非阻塞 acquire：有空槽立刻占用，耗尽即返回 ``False`` 让调用方
翻译成 429。不排队——排队会把资源耗尽从 CPU/内存转移到线程与请求队列。

**已知限制**：进程内计数，多副本部署 N 个副本 = N 倍实际并发。故
:meth:`supports_distributed_budget` 继承默认的 ``False``，装配期据此判断能否宣称
集群级预算，而不是靠 target 名推断。
"""

from __future__ import annotations

import logging
import threading

from common.errors import ValidationError
from common.security.protection.workload_guard import WorkloadGuard, WorkloadGuardProducer

_LOG = logging.getLogger(__name__)

# Argon2id 单次 verify 内存 128 MiB。默认按「给安全操作留 ~512 MiB」预算：4 个并发
# 同时最多吃 512 MiB，留出业务内存。可按机器内存调（params.max_concurrent）。
_DEFAULT_MAX_CONCURRENT = 4


class SemaphoreWorkloadGuard(WorkloadGuard):
    """进程内并发预算，基于 ``threading.BoundedSemaphore``。"""

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
            # release 过多次（acquire 失败后误 release）：不抛，但记一笔——
            # BoundedSemaphore 超过初始值会 ValueError，吞掉会让计数器永久偏。
            _LOG.error("WorkloadGuard release 越界（acquire 未成功即 release？）", exc_info=True)

    @property
    def max_concurrent(self) -> int:
        return self._max

    def health(self) -> None:
        return None


@WorkloadGuardProducer.register("semaphore")
def _build(config):
    """装配 SemaphoreWorkloadGuard；参数非法在**装配期**报错。

    ``max_concurrent=0`` 会让服务拒绝一切昂贵操作（认证全挂），必须在启动时炸，
    不能等到第一个请求进来才暴露。
    """
    max_concurrent = int(config.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))
    if max_concurrent < 1:
        raise ValidationError(
            f"workload_guard 'semaphore' 的 max_concurrent 须 >= 1，得到 {max_concurrent}。"
            "为 0 时所有昂贵安全操作（密码哈希、完整性验证）都会被拒绝。"
        )
    return SemaphoreWorkloadGuard(max_concurrent)
