# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SecurityRuntime — 一次装配得到的安全能力集合（F05 §SecurityRuntime）。

Runtime **只做三件事**：持有能力引用、执行启动期健康检查、暴露统一生命周期。
它不实现认证、限流、密码学或授权算法——所有判断都在各能力自己的实现里。图示::

    SecurityRuntime
    ├── authenticator
    ├── authorizer
    ├── cryptography_provider
    ├── audit_integrity_provider
    ├── rate_limiter
    ├── workload_guard
    └── binding_policy

``cryptography_provider`` 与 ``audit_integrity_provider`` 都可以是 ``None``：是否启用
由部署选型表达（F05 §明文策略 / 审计完整性期未启用即普通审计）。不预留恒为
``None`` 的占位字段——一个永远为空的字段会诱导消费方写 ``if runtime.x:`` 这类
fail-open 分支；要么装配真实实现，要么干脆没有这个字段，``None`` 只在「配置了但
当前部署不启用」时出现。

``authorizer`` 是**必填**的，PR1 也不例外：字段形态由上游接口固定，实装 PR 只填值、
不改形状。PR1 填的是 ``allow_all`` 占位——它 ``is_test_only()`` 为真（上游契约为这类
实现预留的 capability），但它不在任何判定路径上——PEP 在 PR2 之前仍走
``PermissionManager``。**注意：PR1 尚没有装配层据此拒绝启动的守卫**——「生产装配
据此拒绝启动」是契约声称的语义，代码尚未实现，本文档仅如实陈述占位的 capability，
不宣称守卫已存在。

**运行期共享状态**（撤销缓存、分布式限流连接、key 缓存）通过 Factory 的**具名实例**
显式共享，不靠模块级单例——谁与谁共享哪个后端，从配置里就能读出来。

不同接入形态（HTTP / MCP / CLI）消费**同一个** Runtime 实例。

``SecurityRuntimeProducer``（配置顶层段 ``security`` 的注册式工厂及其 ``standard``
装配逻辑）随本实装 PR 落地：过渡期旧加密模块 :mod:`common.security.security`
（``SecurityProducer``）已删除，Producer 无冲突接管 ``security`` 顶层段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityProvider
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.authorization.base import AuthorizationProducer, Authorizer
from jiuwen_memory.common.security.cryptography.base import (
    CryptographyProducer,
    CryptographyProvider,
)
from jiuwen_memory.common.security.protection.binding_policy import (
    BindingPolicy,
    BindingPolicyProducer,
)
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter, RateLimitProducer
from jiuwen_memory.common.security.protection.workload_guard import (
    WorkloadGuard,
    WorkloadGuardProducer,
)

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

    # 旧 ``SecurityProducer``（已删除）注册在同一顶层段 ``security`` 下的 target。
    # 沿用旧配置的部署会带着这些名字进到本 Producer，此时「未注册的实现 'local'」
    # 这条通用报错说不清该往哪改——段名没变、target 名却归属另一个工厂了。
    _REMOVED_CRYPTO_TARGETS = frozenset({"local"})

    @classmethod
    def build(cls, target: str, params, ctx, *, name: str = ""):
        """拦截旧加密配置，给出迁移指引而非通用的「未注册」报错。

        只在 target 落在旧加密注册名上时拦截：这些名字在本 Producer 下永远不会有
        合法含义，误伤不了将来新增的 runtime target。
        """
        if target in cls._REMOVED_CRYPTO_TARGETS:
            raise ValidationError(
                f"配置段 security.{name or 'default'}.target={target!r} 是已删除的旧加密"
                f" provider 配置；顶层段 security 现由 SecurityRuntime 接管。存储加密的"
                f" target 名未变，只需把该段整体挪到 cryptography 下（并配套 key_provider"
                f" 段），即 cryptography.{name or 'default'}.target={target!r}。"
            )
        return super().build(target, params, ctx, name=name)


@dataclass(frozen=True)
class SecurityRuntime:
    """已装配的安全能力集合。

    ``cryptography_provider`` 可以是 ``None``：是否加密持久化数据由存储适配器的选型
    表达（F05 §明文策略），未启用存储加密的部署本就不需要这项能力。其余五项**必须
    非 None**——它们是每个请求都要经过的路径，缺一项就意味着某条边界没人把守。

    ``audit_integrity_provider`` 同样可以是 ``None``：未启用审计完整性时，审计子系统
    退化为普通 AuditLogger（只 record/query，不带链式 proof）。一旦装配了 provider，
    Runtime 的健康检查就覆盖它——健康检查的 provider 必须是实际写审计链的那个。provider
    自身不持有需要 Runtime 关闭的资源：它持有的 ChainedAuditStore 与 AuditLogger 是
    同一具名实例，由审计日志的生命周期所有者统一关闭，Runtime 不重复 close。PR1 只
    固定该装配位，实现（``audit_integrity_impl``）随 PR3 合入。

    ``authorizer`` 在这里只是**装配与健康检查**的归口。真正调用它的是 ``MemoryAPI``
    这个唯一 PEP，且由内核装配注入（见 ``api.memory_api_impl.assembly``）——Runtime
    不代为转发，避免出现第二条能绕开 PEP 的授权入口。PR1 尚无 PEP 消费它（判定仍走
    ``PermissionManager``），装的是 ``allow_all`` 占位，实装随 PR2 合入。
    """

    authenticator: Authenticator
    authorizer: Authorizer
    rate_limiter: RateLimiter
    workload_guard: WorkloadGuard
    binding_policy: BindingPolicy
    cryptography_provider: CryptographyProvider | None = None
    audit_integrity_provider: AuditIntegrityProvider | None = None

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
            ("authorizer", self.authorizer),
            ("rate_limiter", self.rate_limiter),
            ("workload_guard", self.workload_guard),
            ("binding_policy", self.binding_policy),
        ]
        if self.cryptography_provider is not None:
            pairs.append(("cryptography", self.cryptography_provider))
        if self.audit_integrity_provider is not None:
            pairs.append(("audit_integrity", self.audit_integrity_provider))
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

    ``authorizer`` 默认 ``allow_all``：PR1 没有做判定的实现，而该字段是上游固定的必填
    项。这个默认不构成 fail-open——PEP 在 PR2 之前不消费它，业务边界仍由
    ``PermissionManager`` 把守；它 ``is_test_only()`` 为真，是上游契约为这类实现预留的
    capability。**PR1 尚无装配层据此拒绝启动的守卫**——该语义是契约声称、代码未实现，
    登记为 PR2 合入 ``StandardAuthorizer`` 时补充的必做项。PR2 合入后默认改为该实现。
    """
    authenticator = AuthProducer.dep(config, "authenticator")
    runtime_name = getattr(config, "name", "")
    if runtime_name:
        authenticator.bind_instance_name(f"runtime:{runtime_name}")
    rate_limiter_default = (
        "unlimited" if authenticator.requires_loopback_binding() else "token_bucket"
    )
    return SecurityRuntime(
        authenticator=authenticator,
        authorizer=AuthorizationProducer.dep(config, "authorizer", default="allow_all"),
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
