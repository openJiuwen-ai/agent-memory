"""昂贵安全操作的全局并发预算契约（F05 §Protection §WorkloadGuard）。

入口限流限的是「单地址的请求速率」，限不住「同时在跑的昂贵校验数」——后者才是
CPU/内存耗尽攻击的真正向量：单 IP 30 个并发错误 key = 30 × 128 MiB Argon2 同时
驻留。本契约是在昂贵操作**之前** acquire、耗尽即快速拒绝的全局预算，是入口限流
之上的第一层。

**预算是能力级的，不是 Argon2 专用的**：密码哈希、密钥派生、全量完整性验证共用
同一份预算。原 ``Argon2Guard`` 只覆盖密码哈希一项，PR3 的审计全量验证同样昂贵，
届时无处挂靠。

**耗尽时快速拒绝，绝不排队**：排队会让线程无界堆积，把资源耗尽从 CPU/内存转移
到线程和请求队列——攻击者用慢请求占满队列就能把正常请求一并堵死。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory


class WorkloadGuardProducer(Factory):
    """WorkloadGuard 的注册式工厂（与契约同处接口层）。

    各实现在 ``protection_impl`` 下以 ``@WorkloadGuardProducer.register("<后端>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。

    进程级共享通过**具名实例**表达（``build_named`` 的实例缓存），不用模块级单例：
    单例把「谁持有预算」藏进模块状态，同进程多 Server / 热重载时只能靠比对参数
    抛冲突错来兜底；具名实例让共享成为一条可在配置里读出来的显式引用。
    """

    TOP_NAME = "workload_guard"


class WorkloadGuard(ABC):
    """昂贵安全操作的并发预算：一次操作占用一个槽位。"""

    @abstractmethod
    def acquire(self) -> bool:
        """有空槽则占用并返回 ``True``，否则立即返回 ``False``。

        **非阻塞**：实现不得在此排队等待（见模块 docstring）。

        **返回 bool 而非抛异常**：预算耗尽是「事实陈述」，翻译成 429 是调用方的事。
        不构成 fail-open——调用方拿到 ``False`` 唯一能做的就是拒绝。

        成功 acquire 后必须在 ``finally`` 中 :meth:`release`，否则槽位永久泄漏，
        预算耗尽后服务再也不接受任何昂贵操作。
        """

    @abstractmethod
    def release(self) -> None:
        """归还一个槽位。只有 :meth:`acquire` 返回 ``True`` 后才可调用。"""

    @property
    @abstractmethod
    def max_concurrent(self) -> int:
        """预算上限，供诊断与启动期日志展示。"""

    def supports_distributed_budget(self) -> bool:
        """本实现的预算是否**跨副本共享**（F05 §Protection §分布式部署）。

        默认 ``False``：进程内信号量只约束本副本，N 个副本 = N 倍实际并发。
        与 :meth:`~common.security.protection.rate_limit.RateLimiter.
        supports_distributed_quota` 同理，默认取「不宣称」，由共享后端实现显式覆写。
        """
        return False

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全组件同构。"""
