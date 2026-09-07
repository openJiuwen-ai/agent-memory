"""common.security.protection.rate_limit：令牌桶限流（§8.1）。

测的是行为而非内部状态：桶的 tokens 字段是实现细节，「第 N 个请求被拒、
等一会儿又能过」才是契约。时间相关的断言全部注入假时钟，不用 sleep——
sleep 会让测试又慢又 flaky。
"""

from __future__ import annotations

# The bounded-table assertions are intentional white-box checks of the LRU state.
# pylint: disable=protected-access
import threading

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.protection.protection_impl.token_bucket_limiter import (
    TokenBucketLimiter,
)
from jiuwen_memory.common.security.protection.rate_limit import RateLimitProducer
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit

_MONOTONIC = (
    "jiuwen_memory.common.security.protection.protection_impl.token_bucket_limiter.time.monotonic"
)


@pytest.fixture(autouse=True, scope="module")
def _registered():
    register_plugins()


def _limiter(capacity=3, refill_per_sec=1.0, max_tracked=100) -> TokenBucketLimiter:
    return TokenBucketLimiter(
        capacity=capacity, refill_per_sec=refill_per_sec, max_tracked=max_tracked
    )


# -- 准入 -------------------------------------------------------------------- #


def test_burst_up_to_capacity_then_denied() -> None:
    limiter = _limiter(capacity=3)
    assert [limiter.allow("10.0.0.1") for _ in range(3)] == [True, True, True]
    assert limiter.allow("10.0.0.1") is False


def test_peers_have_independent_buckets() -> None:
    """一个调用方打满不该影响别人——否则单个攻击者就能拒绝全部服务。"""
    limiter = _limiter(capacity=2)
    assert limiter.allow("10.0.0.1") and limiter.allow("10.0.0.1")
    assert limiter.allow("10.0.0.1") is False
    assert limiter.allow("10.0.0.2") is True


def test_empty_peer_is_never_limited() -> None:
    """进程内直连 / MCP stdio 没有网络对端：没有攻击面，限流只会卡住本地 CLI。"""
    limiter = _limiter(capacity=1)
    assert all(limiter.allow("") for _ in range(50))


# -- 补充 -------------------------------------------------------------------- #


def test_tokens_refill_over_time(monkeypatch) -> None:
    """桶空之后等够时间要能再放行——不然限流等于永久拉黑。"""
    now = [1000.0]
    monkeypatch.setattr(_MONOTONIC, lambda: now[0])

    limiter = _limiter(capacity=2, refill_per_sec=1.0)
    assert limiter.allow("10.0.0.1") and limiter.allow("10.0.0.1")
    assert limiter.allow("10.0.0.1") is False

    now[0] += 0.5  # 不足一个令牌
    assert limiter.allow("10.0.0.1") is False

    now[0] += 0.5  # 累计 1.0s → 恰好一个令牌
    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is False


def test_refill_is_capped_at_capacity(monkeypatch) -> None:
    """长时间空闲不该攒出无限额度，否则突发保护形同虚设。"""
    now = [1000.0]
    monkeypatch.setattr(_MONOTONIC, lambda: now[0])

    limiter = _limiter(capacity=3, refill_per_sec=1.0)
    assert limiter.allow("10.0.0.1")
    now[0] += 3600  # 空闲一小时

    assert [limiter.allow("10.0.0.1") for _ in range(3)] == [True, True, True]
    assert limiter.allow("10.0.0.1") is False


# -- 桶表有界 ---------------------------------------------------------------- #


def test_bucket_table_is_bounded() -> None:
    """桶按 peer 建、peer 由远端决定：无界字典会让防耗尽的组件自己成为耗尽入口。"""
    limiter = _limiter(capacity=1, max_tracked=10)
    for i in range(100):
        limiter.allow(f"10.0.0.{i}")
    assert len(limiter._buckets) == 10


def test_eviction_drops_least_recently_used() -> None:
    """淘汰最久未活跃的那个：活跃 peer 的限流状态不能被一串陌生 IP 冲掉。"""
    limiter = _limiter(capacity=1, max_tracked=3)
    assert limiter.allow("busy") is True  # busy 的桶已耗尽
    limiter.allow("a")
    limiter.allow("busy")  # 触碰一次，把 busy 移到 LRU 末尾
    limiter.allow("b")
    limiter.allow("c")  # 超出 3 个 → 淘汰最久未活跃的 "a"

    assert "busy" in limiter._buckets
    assert "a" not in limiter._buckets
    # busy 仍然被限流——它的状态没被冲掉。
    assert limiter.allow("busy") is False


# -- 并发 -------------------------------------------------------------------- #


def test_concurrent_requests_do_not_exceed_capacity() -> None:
    """「读余量 → 减一 → 写回」在 GIL 下不是原子的：两个线程能同时看到最后一个令牌。

    没有锁时本测试会看到 allowed > capacity。
    """
    limiter = _limiter(capacity=50, refill_per_sec=0.0001)
    allowed: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def hammer() -> None:
        barrier.wait()  # 尽量让 20 个线程同时进 allow
        results = [limiter.allow("10.0.0.1") for _ in range(20)]
        with lock:
            allowed.extend(results)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(allowed) == 50


# -- 装配 -------------------------------------------------------------------- #


def test_build_uses_defaults() -> None:
    limiter = RateLimitProducer.build("token_bucket", {}, AssemblyContext())
    assert isinstance(limiter, TokenBucketLimiter)
    limiter.health()


def test_build_honours_params() -> None:
    limiter = RateLimitProducer.build(
        "token_bucket", {"capacity": 2, "refill_per_sec": 7.5}, AssemblyContext()
    )
    assert limiter.allow("10.0.0.1") and limiter.allow("10.0.0.1")
    assert limiter.allow("10.0.0.1") is False


@pytest.mark.parametrize(
    "params",
    [
        {"capacity": 0},
        {"capacity": -1},
        {"refill_per_sec": 0},
        {"refill_per_sec": -1.0},
        {"max_tracked": 0},
    ],
)
def test_invalid_params_rejected_at_assembly(params) -> None:
    """配错了要在启动时炸：capacity=0 会拒绝一切请求，refill=0 会永久拉黑调用方。

    这两种「配置写错等于服务下线」的情况，运行期才暴露就是一次生产事故。
    """
    with pytest.raises(ValidationError):
        RateLimitProducer.build("token_bucket", params, AssemblyContext())


def test_disabling_is_explicit_not_a_magic_value() -> None:
    """关闭限流走 target: unlimited；capacity 不接受反着读的魔法值。"""
    limiter = RateLimitProducer.build("unlimited", {}, AssemblyContext())
    assert all(limiter.allow("10.0.0.1") for _ in range(1000))
    limiter.health()
