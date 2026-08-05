"""SecurityRuntime — 一次装配得到的安全能力集合（F05 §SecurityRuntime）。

Runtime **只做三件事**：持有能力引用、执行启动期健康检查、暴露统一生命周期。
它不实现认证、限流、密码学或授权算法——所有判断都在各能力自己的实现里。图示::

    SecurityRuntime
    ├── authenticator
    ├── cryptography_provider
    ├── rate_limiter
    ├── workload_guard
    └── binding_policy

（``authorizer`` 与 ``audit_integrity_provider`` 是 F05 目标态成员，分别由 PR2 与
PR3 补齐。PR1 不预留占位字段：一个恒为 ``None`` 的字段会诱导消费方写
``if runtime.authorizer:`` 这类 fail-open 分支。）

**运行期共享状态**（撤销缓存、分布式限流连接、key 缓存）通过 Factory 的**具名实例**
显式共享，不靠模块级单例——谁与谁共享哪个后端，从配置里就能读出来。

不同接入形态（HTTP / MCP / CLI）消费**同一个** Runtime 实例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from common.errors import ValidationError
from common.factory.factory import Factory
from common.security.authentication.base import Authenticator, AuthProducer
from common.security.cryptography.base import CryptographyProducer, CryptographyProvider
from common.security.protection.binding_policy import BindingPolicy, BindingPolicyProducer
from common.security.protection.rate_limit import RateLimiter, RateLimitProducer
from common.security.protection.workload_guard import WorkloadGuard, WorkloadGuardProducer

_LOG = logging.getLogger(__name__)


class SecurityRuntimeProducer(Factory):
    """SecurityRuntime 的注册式工厂。

    配置顶层段为 ``security``，其具名实例只**组合**其他能力的具名实例::

        security:
          default:
            target: standard
            params:
              authenticator: primary_auth
              cryptography: primary_crypto
              rate_limiter: ingress_limit
              workload_guard: security_budget
              binding_policy: server_binding
    """

    TOP_NAME = "security"


@dataclass(frozen=True)
class SecurityRuntime:
    """已装配的安全能力集合。

    ``cryptography_provider`` 可以是 ``None``：是否加密持久化数据由存储适配器的选型
    表达（F05 §明文策略），未启用存储加密的部署本就不需要这项能力。其余四项**必须
    非 None**——它们是每个请求都要经过的路径，缺一项就意味着某条边界没人把守。
    """

    authenticator: Authenticator
    rate_limiter: RateLimiter
    workload_guard: WorkloadGuard
    binding_policy: BindingPolicy
    cryptography_provider: CryptographyProvider | None = None

    def health(self) -> None:
        """启动期健康检查：任一能力不健康即抛出，不返回 bool。

        返回 ``bool`` 会诱导调用方写 ``if not runtime.health(): log.warning(...)``
        然后继续启动。健康检查失败必须拒绝启动（F05 §默认拒绝）。

        异常消息只带**能力名**——能力名来自配置、不是秘密；具体原因由各实现自己
        决定暴露多少（F05 §装配不变量 8：不得泄露 key、token 或主体是否存在）。
        """
        for name, capability in self._capabilities():
            try:
                capability.health()
            except Exception as exc:
                raise ValidationError(f"security capability {name!r} is unhealthy") from exc

    def close(self) -> None:
        """关闭持有连接的能力。没有 ``close`` 的能力跳过。

        逐个捕获并记录而不是让第一个失败中断后续——关闭路径上放弃剩余能力会漏掉
        连接与文件句柄。
        """
        for name, capability in self._capabilities():
            closer = getattr(capability, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                _LOG.error("关闭安全能力 %r 失败", name, exc_info=True)

    def _capabilities(self) -> list[tuple[str, Any]]:
        pairs = [
            ("authenticator", self.authenticator),
            ("rate_limiter", self.rate_limiter),
            ("workload_guard", self.workload_guard),
            ("binding_policy", self.binding_policy),
        ]
        if self.cryptography_provider is not None:
            pairs.append(("cryptography", self.cryptography_provider))
        return pairs


@SecurityRuntimeProducer.register("standard")
def _build(config) -> SecurityRuntime:
    """组合具名安全能力。

    ``authenticator`` **必填且无默认**：没有默认值，缺失就抛 ValidationError。给它
    一个默认会让「忘了配认证」静默变成某种可用配置——F05 §装配不变量 6 拒绝的正是
    这种隐式选择。开发部署要 dev 认证，就在配置里写出来。

    其余四项的默认取**保守侧**：``token_bucket`` 而非 ``unlimited``、有限并发预算而非
    无限、强制 loopback 校验而非放行。没配等于没读过文档，此时给出的默认必须是拦住
    请求的那个，不是放行的那个。

    唯一的例外是 ``rate_limiter``：只监听 loopback 的部署没有远端攻击面，默认限流只会
    卡住本地压测与调试脚本。这个分岔由 ``Authenticator.requires_loopback_binding()``
    这个 **capability** 决定，不看 target 名（F05 §依据 capability 做安全决策）。
    """
    authenticator = AuthProducer.dep(config, "authenticator")
    rate_limiter_default = (
        "unlimited" if authenticator.requires_loopback_binding() else "token_bucket"
    )
    return SecurityRuntime(
        authenticator=authenticator,
        rate_limiter=RateLimitProducer.dep(config, "rate_limiter", default=rate_limiter_default),
        workload_guard=WorkloadGuardProducer.dep(config, "workload_guard", default="semaphore"),
        binding_policy=BindingPolicyProducer.dep(config, "binding_policy", default="loopback"),
        cryptography_provider=_optional_cryptography(config),
    )


def _optional_cryptography(config) -> CryptographyProvider | None:
    """只在显式配置了 ``cryptography`` 时装配——没有默认实现。

    默认装一个加密 provider 会凭空造出一把没人管理生命周期的根密钥；不配就是不用，
    要用就得把 KeyProvider 一起配出来。
    """
    if Factory.cfg_get(config, "cryptography") is None:
        return None
    return CryptographyProducer.dep(config, "cryptography")
