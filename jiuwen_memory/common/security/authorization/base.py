"""授权能力契约：Authorizer 与注册式 Producer（F05 §Authorization）。

Authorizer 是 **PDP**（决策点）：根据可信身份、动作和真实资源属性回答「这次操作
是否允许」。它不知道 HTTP、不知道 MemoryUnit、不读存储真源——那些是 **PEP**
（``MemoryAPI``）的职责，PEP 把结论整理成 :class:`ResourceDescriptor` 交进来。

与被它取代的 ``control.permission.PermissionManager`` 的三点差别：

1. **输入封闭**。固定为 ``AuthContext + ResourceDescriptor + AuthorizationEnvironment``
   （F05 §Authorization），不再有 ``auth=None`` 这条「没有认证上下文时退回纯 ACL」
   的兼容线——那条线的实际效果是「谁都不传 auth 就都按无角色处理」。
2. **不读 ContextVar**（F05 §授权不变量 7）。全部判定依据显式入参：单测不必先布置
   环境态，判定依据在调用点就能读全。
3. **输出带原因**。返回 :class:`AuthorizationDecision` 而非 ``bool``：审计要记稳定
   reason code（F05 §可观测性），布尔值到了审计那里只剩「拒了」。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import (
    AuthContext,
    AuthorizationEnvironment,
    DenyReason,
    ResourceDescriptor,
)

if TYPE_CHECKING:
    from jiuwen_memory.common.security.authorization.store import GrantStore


class AuthorizationProducer(Factory):
    """Authorizer 的注册式工厂（与契约同处接口层）。

    ``target`` 即授权实现名。各实现在 ``authorization_impl`` 下以
    ``@AuthorizationProducer.register("<名>")`` 自注册，由
    :func:`common.security.bootstrap.register_security` 统一触发 import。
    """

    TOP_NAME = "authorizer"


@dataclass(frozen=True)
class AuthorizationDecision:
    """一次授权判定的结果。

    ``rule`` 在 allow 与 deny 两侧都必填：只记 deny 的原因，审计里就看不出一次放行
    是「owner 访问自己的数据」还是「某条快过期的 Grant 兜住了」。两者的运维含义
    完全不同。

    ``reason`` 与 ``allowed`` 的一致性在构造期校验：一个 ``allowed=True`` 却带着
    ``CROSS_ORG`` 的决策是矛盾状态，让它构造成功只会把 bug 推迟到读审计的时候。
    """

    allowed: bool
    rule: str  # 做出判定的规则标识（owner_cover / grant / role_gate / ...）
    reason: DenyReason | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("allow 决策不得携带 DenyReason")
        if not self.allowed and self.reason is None:
            raise ValueError("deny 决策必须给出 DenyReason")
        if not self.rule:
            raise ValueError("授权决策必须标明做出判定的规则")

    @classmethod
    def allow(cls, rule: str) -> AuthorizationDecision:
        return cls(allowed=True, rule=rule)

    @classmethod
    def deny(cls, reason: DenyReason, rule: str) -> AuthorizationDecision:
        return cls(allowed=False, rule=rule, reason=reason)


class RoutingFieldsProvider:
    """声明授权策略选择所依赖的资源字段，供 PEP 绑定授权依据与数据范围。

    接口先行过渡期内，PEP 仍注入旧 ``PermissionManager``；后续实装改为注入
    ``Authorizer``。两者必须共享同一个 capability 契约，不能各自复制一份同名
    默认实现，否则接口迁移期间极易只更新其中一侧。
    """

    @staticmethod
    def routing_fields() -> tuple[str, ...]:
        """本实现据以选择策略的资源属性名；默认不路由。

        路由型实现按调用方可影响的资源属性挑选 delegate 时，PEP 必须把该属性
        回注为系统谓词，避免“用 A 策略授权、读取 B 类型数据”。字段如何从具体
        权限上下文取值由 PEP 负责，本 capability 只声明稳定字段名。
        """
        return ()


class Authorizer(RoutingFieldsProvider, ABC):
    """可信身份 + 资源 + 环境 → 允许/拒绝。"""

    @abstractmethod
    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        """作出授权判定。

        三个参数都是 **keyword-only**：它们类型不同但都是「一坨上下文」，位置传参
        写反了不会报错，只会得到一个语义颠倒却能跑的判定。

        **不抛异常表达拒绝**：拒绝是正常判定结果，抛异常会让调用方用 try/except 表达
        控制流，也让「拒绝」和「授权组件坏了」在调用点长得一样。存储不可用等真实故障
        仍然抛——那时不能把故障静默成 deny，PEP 需要区分 403 与 503。
        """

    def is_test_only(self) -> bool:
        """本实现是否只允许出现在测试装配中（F05 §授权不变量 8）。

        默认 ``False``。allow-all 这类恒放行实现返回 ``True``，装配层据此在生产模式
        拒绝启动。用 capability 而非 ``target == "allow_all"`` 判断，是 S08 不变量 7
        的要求：第三方注册的恒放行实现同样要能被拦住，而它的 target 名核心不认识。
        """
        return False

    def management_grant_store(self) -> GrantStore | None:
        """本 Authorizer 判定时实际查询的 GrantStore（供 PEP 的 grant/revoke 共享真源）。

        默认 ``None``：不基于 GrantStore 的实现（如 allow_all）没有管理写真源。基于
        GrantStore 的实现（StandardAuthorizer）覆盖之，返回其 ``find_active`` 所读的
        Store。PEP 的公共 grant/revoke 写这里，与 PDP 读取的同一实例--具名 YAML 令
        Authorizer 引用别的 Store 时，公共 grant 也写入同一 Store，不再双真源。

        **路由场景已弃用本方法**：RoutingAuthorizer 覆盖 :meth:`management_grant_stores`
        返回全部 policy Store，PEP 写入所有 Store 以统一真源（P1-4）。非路由实现仍可
        只覆盖本方法，PEP 向下兼容。
        """
        return None

    def management_grant_stores(self) -> list[GrantStore]:
        """本 Authorizer 判定时可能查询的全部 GrantStore（路由场景真源统一，P1-4）。

        默认实现：单 Store 情况返回 ``[management_grant_store()]``（向下兼容），无 Store
        返回空列表。RoutingAuthorizer 覆盖之，返回全部 delegate policy 的 Store 去重后
        列表。PEP 的公共 grant/revoke 写入返回的**所有** Store，确保路由命中任一 policy
        时都能读到授权——单 Store 时行为不变，路由时统一真源。
        """
        store = self.management_grant_store()
        return [store] if store is not None else []

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全能力同构。"""
