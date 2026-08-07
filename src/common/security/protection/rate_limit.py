"""入口限流契约：认证前按调用方地址执行低成本准入（F05 §Protection §入口限流）。

限流挂在**认证之前**：认证本身就是要保护的资源。API_KEY 模式下每次
``authenticate`` 都跑一次 Argon2id verify（128 MiB × time_cost=4，约
50~200ms），无限制地触发它能把进程的 CPU 与内存同时打满。

**限流维度是调用方地址，不是 key 指纹。** 按 ``key_fp`` 分桶防的是「单个合法
key 打爆配额」（配额公平），不是「攻击者打爆 CPU」——攻击者每次换一把随机 key
就换一个新桶，对枚举与耗尽两种攻击都不生效。真正能收敛攻击的是来源地址。
按 key 的配额公平是独立需求，本期不做。

抽象与实现分离的理由：分布式部署下进程内桶各算各的（N 个副本 = N 倍额度），
真正的多副本限流要 Redis 之类的共享计数器。契约留在这里，届时新增一个实现
即可，无需改中间件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from common.factory.factory import Factory


class RateLimitProducer(Factory):
    """RateLimiter 的注册式工厂（与契约同处接口层）。

    各实现在 ``protection_impl`` 下以 ``@RateLimitProducer.register("<后端>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "rate_limiter"


class RateLimiter(ABC):
    """请求准入：一次调用消耗一个额度。"""

    @abstractmethod
    def allow(self, peer: str) -> bool:
        """``peer`` 还有额度则消耗一个并返回 ``True``，否则返回 ``False``。

        **返回 bool 而非抛异常**：限流是「事实陈述」，翻译成 HTTP 状态码是
        调用方（``auth_middleware``）的事。这与
        :meth:`~common.security.authentication.key_store.PrincipalKeyStore.resolve`
        返回 ``None`` 同理，且不构成 fail-open——调用方拿到 ``False`` 唯一能做的
        就是拒绝。

        实现必须是**并发安全**的：``ThreadingHTTPServer`` 每请求一线程，
        「读余量 → 减一 → 写回」在 GIL 下不是原子的，两个线程能同时看到
        最后一个令牌。

        ``peer`` 为空串（进程内直连 / MCP stdio，无网络对端）时应放行：
        没有远端就没有可收敛的攻击面，限流反而会把本地 CLI 卡住。
        """

    def supports_distributed_quota(self) -> bool:
        """本实现是否提供**跨副本共享**的配额（F05 §Protection §分布式部署）。

        默认 ``False``：进程内计数只是单实例保护，N 个副本 = N 倍实际额度。
        默认值取「不宣称」而非「宣称」，是为了让「进程内限流被当成集群配额」
        这类误判必须由实现显式承担——共享计数器后端（Redis 等）覆写返回
        ``True``，装配期据此校验 capability，而不是靠 target 名推断。
        """
        return False

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全组件同构。"""
