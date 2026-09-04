"""jiuwen_memory.common.security.protection: 三个能力契约的形状与 fail-closed 默认值。

接口先行版：``protection_impl`` 未合入，只固定 RateLimiter / WorkloadGuard /
BindingPolicy 的抽象契约与 capability 默认值。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.protection.binding_policy import (
    BindingPolicy,
    BindingPolicyProducer,
)
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter, RateLimitProducer
from jiuwen_memory.common.security.protection.workload_guard import (
    WorkloadGuard,
    WorkloadGuardProducer,
)

pytestmark = pytest.mark.unit


def test_rate_limiter_contract() -> None:
    class Incomplete(RateLimiter):
        def allow(self, peer: str) -> bool:
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]

    class Minimal(RateLimiter):
        def allow(self, peer: str) -> bool:
            return True

        def health(self) -> None:
            return None

    # 默认不宣称跨副本共享：单实例计数被误当集群配额必须由实现显式承担。
    assert Minimal().supports_distributed_quota() is False


def test_workload_guard_contract() -> None:
    class Incomplete(WorkloadGuard):
        def acquire(self) -> bool:
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]

    class Minimal(WorkloadGuard):
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            return None

        @property
        def max_concurrent(self) -> int:
            return 1

        def health(self) -> None:
            return None

    assert Minimal().supports_distributed_budget() is False


def test_binding_policy_contract() -> None:
    class Incomplete(BindingPolicy):
        def check(self, hosts, *, requires_loopback: bool) -> None:
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_producers_declare_top_names() -> None:
    assert RateLimitProducer.TOP_NAME == "rate_limiter"
    assert WorkloadGuardProducer.TOP_NAME == "workload_guard"
    assert BindingPolicyProducer.TOP_NAME == "binding_policy"
