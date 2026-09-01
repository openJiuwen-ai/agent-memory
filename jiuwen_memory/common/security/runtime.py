# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SecurityRuntime - 一次装配得到的安全能力集合（F05 §SecurityRuntime）。

Runtime **只做三件事**：持有能力引用、执行启动期健康检查、暴露统一生命周期。
它不实现认证、限流、密码学、授权或审计完整性算法--所有判断都在各能力自己的实现里。图示::

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
``None`` 的占位字段--一个永远为空的字段会诱导消费方写 ``if runtime.x:`` 这类
fail-open 分支；要么装配真实实现，要么干脆没有这个字段，``None`` 只在「配置了但
当前部署不启用」时出现。

**运行期共享状态**（撤销缓存、分布式限流连接、key 缓存）通过 Factory 的**具名实例**
显式共享，不靠模块级单例--谁与谁共享哪个后端，从配置里就能读出来。

不同接入形态（HTTP / MCP / CLI）消费**同一个** Runtime 实例。

**接口先行说明**：本文件当前只固定 :class:`SecurityRuntime` 契约。
``SecurityRuntimeProducer``（配置顶层段 ``security`` 的注册式工厂及其 ``standard``
装配逻辑）随实现 PR 落地：它的 TOP_NAME 与过渡期仍在线的旧加密模块
:mod:`common.security.security` 的 ``SecurityProducer`` 同为 ``"security"``，在
Factory 全局唯一约束下二者不能并存；装配逻辑也引用尚未合入的 ``*_impl``
注册名。旧模块删除后，Producer 随实现 PR 无冲突接管该顶层段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityProvider
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authorization.base import Authorizer
from jiuwen_memory.common.security.cryptography.base import CryptographyProvider
from jiuwen_memory.common.security.protection.binding_policy import BindingPolicy
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityRuntime:
    """已装配的安全能力集合。

    ``cryptography_provider`` 可以是 ``None``：是否加密持久化数据由存储适配器的选型
    表达（F05 §明文策略），未启用存储加密的部署本就不需要这项能力。其余五项**必须
    非 None**--它们是每个请求都要经过的路径，缺一项就意味着某条边界没人把守。

    ``audit_integrity_provider`` 同样可以是 ``None``：未启用审计完整性期时，审计子系统
    退化为普通 AuditLogger（只 record/query，不带链式 proof）。一旦装配了 provider，
    Runtime 的健康检查就覆盖它--健康检查的 provider 必须是实际写审计链的那个。provider
    自身不持有需要 Runtime 关闭的资源：它持有的 ChainedAuditStore 与 AuditLogger 是
    同一具名实例，由审计日志的生命周期所有者统一关闭，Runtime 不重复 close。

    ``authorizer`` 在这里只是**装配与健康检查**的归口。真正调用它的是 ``MemoryAPI``
    这个唯一 PEP，且由内核装配注入（见 ``api.memory_api_impl.assembly``）--Runtime
    不代为转发，避免出现第二条能绕开 PEP 的授权入口。
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

        异常消息只带**能力名**--能力名来自配置、不是秘密；具体原因由各实现自己
        决定暴露多少（F05 §装配不变量 8：不得泄露 key、token 或主体是否存在）。
        """
        for name, capability in self._capabilities():
            try:
                capability.health()
            except Exception as exc:
                raise ValidationError(f"security capability {name!r} is unhealthy") from exc

    def close(self) -> None:
        """关闭持有连接的能力。没有 ``close`` 的能力跳过。

        逐个捕获并记录而不是让第一个失败中断后续--关闭路径上放弃剩余能力会漏掉
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
