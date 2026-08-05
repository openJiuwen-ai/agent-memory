"""进程内令牌桶限流，按调用方地址分桶（security.md §8.1）。

两个参数分别管两件事：``capacity`` 是**突发**额度（桶满时能一口气放多少个），
``refill_per_sec`` 是**持续**速率（长期平均每秒放多少个）。交互式客户端天然
是「短突发 + 长空闲」，所以默认给一个偏大的桶配一个偏小的补充速率。

**桶表是 LRU 有界的**：桶按 peer 建，peer 由远端决定，无界字典会让这个
「防资源耗尽」的组件自己变成资源耗尽的入口。超出 ``max_tracked`` 时淘汰最久
未活跃的那个——它最可能已经补满，淘汰等于重建成满桶，不丢有效状态。

**已知限制**（两条，都不是本实现能解决的）：

1. **多副本各算各的**：进程内计数，N 个副本 = N 倍实际额度。真正的多副本
   限流要 Redis 之类的共享计数器，届时在 ``rate_limit_impl`` 下新增一个实现，
   中间件不用改。
2. **按地址分桶挡不住僵尸网络**：来源足够分散时每个 IP 都拿到一个新满桶。
   能收敛这种攻击的是**对 Argon2 verify 本身做并发上限**（一个信号量，把
   同时进行的 verify 数压到内存能承受的范围），那是与限流互补的另一个机制，
   本期不做。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from common.admission.base import RateLimiter, RateLimitProducer
from common.errors import ValidationError

# 默认值面向「交互式使用不该被限流，脚本化枚举必须被限流」这条线：
# 30 个突发够任何人工操作和常规客户端启动时的几次探测；持续 5 QPS 远低于
# Argon2 verify 打满一个核所需的速率。
_DEFAULT_CAPACITY = 30
_DEFAULT_REFILL_PER_SEC = 5.0
_DEFAULT_MAX_TRACKED = 10_000


@dataclass
class _Bucket:
    """一个 peer 的桶。``last`` 是 ``time.monotonic()`` 读数，不是墙上时间。"""

    tokens: float
    last: float


class TokenBucketLimiter(RateLimiter):
    """按 peer 分桶的令牌桶；LRU 有界，并发安全。"""

    def __init__(self, capacity: int, refill_per_sec: float, max_tracked: int) -> None:
        self._capacity = float(capacity)
        self._refill = refill_per_sec
        self._max_tracked = max_tracked
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        # 一把全局锁，不做分桶锁：临界区只有几次浮点运算，而其后紧跟的
        # Argon2 verify 是 50~200ms——锁竞争在这个量级下不值得优化。
        self._lock = threading.Lock()

    def allow(self, peer: str) -> bool:
        if not peer:
            # 无网络对端（进程内直连 / MCP stdio）。没有远端就没有可收敛的
            # 攻击面，限流只会把本地 CLI 卡住。
            return True

        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(peer)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last=now)
                self._buckets[peer] = bucket
                if len(self._buckets) > self._max_tracked:
                    # 只可能超出 1 个（每次调用最多插一个），故一次淘汰即可。
                    # 刚插入的在末尾，不会被 last=False 弹掉。
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(peer)  # 维护 LRU 次序
                refilled = bucket.tokens + (now - bucket.last) * self._refill
                bucket.tokens = min(self._capacity, refilled)
                bucket.last = now

            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True

    def health(self) -> None:
        return None


@RateLimitProducer.register("token_bucket")
def _build(config):
    """装配 TokenBucketLimiter；参数非法在**装配期**报错。

    参数错了要在启动时炸，不能等到运行期：``capacity=0`` 会让服务拒绝一切
    请求，``refill_per_sec=0`` 会让桶空了再也补不回来——两者都是「配置写错
    等于服务下线」，而运行期才暴露就是一次生产事故。要关闭限流请显式配
    ``target: unlimited``，不要靠把参数写成 0。
    """
    capacity = int(config.get("capacity", _DEFAULT_CAPACITY))
    refill_per_sec = float(config.get("refill_per_sec", _DEFAULT_REFILL_PER_SEC))
    max_tracked = int(config.get("max_tracked", _DEFAULT_MAX_TRACKED))

    if capacity < 1:
        raise ValidationError(
            f"rate_limiter 'token_bucket' 的 capacity 须 >= 1，得到 {capacity}。"
            "要关闭限流请配 target: unlimited。"
        )
    if refill_per_sec <= 0:
        raise ValidationError(
            f"rate_limiter 'token_bucket' 的 refill_per_sec 须 > 0，得到 {refill_per_sec}。"
            "为 0 时桶一旦耗尽就永不恢复，等于把调用方永久拉黑。"
        )
    if max_tracked < 1:
        raise ValidationError(
            f"rate_limiter 'token_bucket' 的 max_tracked 须 >= 1，得到 {max_tracked}"
        )

    return TokenBucketLimiter(
        capacity=capacity,
        refill_per_sec=refill_per_sec,
        max_tracked=max_tracked,
    )
