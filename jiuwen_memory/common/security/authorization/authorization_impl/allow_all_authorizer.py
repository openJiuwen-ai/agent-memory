# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""恒放行 Authorizer：仅供隔离测试使用（F05 §授权不变量 8）。

**为什么 PR1 需要它**：上游已把 ``SecurityRuntime.authorizer`` 固定为必填字段，
但真正做判定的 ``StandardAuthorizer`` 归 PR2。必填字段总得有值可填，于是 PR1
提供这一个恒放行实现——它使 Runtime 能按固定接口装配起来，而不必去改那个已经
冻结的字段。

PR2 起 ``LocalMemoryAPI`` 已把 ``Authorizer`` 作为唯一生产 PDP，本实现不能再作为
Runtime 占位进入任何服务装配。需要绕开授权以隔离测试其他组件时，可在直接内核测试中
显式使用；``Server`` 与显式配置的生产装配都会按 ``is_test_only()`` 拒绝它。

``is_test_only()`` 返回 ``True``，是上游 :class:`~.base.Authorizer` 契约为这类
实现预留的 capability：装配层**可以**据此在生产模式拒绝启动，不必去看
``target == "allow_all"`` 这个名字（S08 不变量 7——第三方注册的恒放行实现同样要能被
拦住）。

当前守卫不按 target 名分支，第三方实现只要声明 ``is_test_only()`` 也会被同样拒绝。
"""

from __future__ import annotations

from jiuwen_memory.common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from jiuwen_memory.common.security.types import (
    AuthContext,
    AuthorizationEnvironment,
    ResourceDescriptor,
)


class AllowAllAuthorizer(Authorizer):
    """恒放行测试替身，不构成生产授权能力。"""

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        # rule 写明放行来自占位而非任何判据：审计里看到 allow 时，能一眼区分
        # "某条规则放行了" 和 "授权根本还没实装"。
        return AuthorizationDecision.allow("allow_all_placeholder")

    def is_test_only(self) -> bool:
        """恒放行实现只允许出现在测试/过渡装配中（F05 §授权不变量 8）。"""
        return True

    def health(self) -> None:
        return None


@AuthorizationProducer.register("allow_all")
def _build(config):
    return AllowAllAuthorizer()
