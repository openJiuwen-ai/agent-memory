# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""恒放行 Authorizer：三个安全 PR 全部实装前的装配占位（F05 §授权不变量 8）。

**为什么 PR1 需要它**：上游已把 ``SecurityRuntime.authorizer`` 固定为必填字段，
但真正做判定的 ``StandardAuthorizer`` 归 PR2。必填字段总得有值可填，于是 PR1
提供这一个恒放行实现——它使 Runtime 能按固定接口装配起来，而不必去改那个已经
冻结的字段。

**它当前不在任何判定路径上**：PEP（``LocalMemoryAPI``）在 PR2 之前仍走
``PermissionManager``，没有任何代码调用 ``AuthorizationProducer`` 装出来的
Authorizer。Runtime 持有它只为两件事：把必填字段填上，以及把它纳入启动期
``health()``。换句话说，本实现"恒放行"这件事在 PR1 不产生任何可观察的授权后果，
业务边界仍然由 ``PermissionManager`` 把守——这也是 PR2 接管判定时不会突然放开
权限的原因：那时被替换掉的是本占位，不是一条已经生效的放行规则。

``is_test_only()`` 返回 ``True``，是上游 :class:`~.base.Authorizer` 契约为这类
实现预留的 capability：装配层**可以**据此在生产模式拒绝启动，不必去看
``target == "allow_all"`` 这个名字（S08 不变量 7——第三方注册的恒放行实现同样要能被
拦住）。

**注意**：这是契约提供的 capability，不是 PR1 已实现的行为。PR1 的装配层**尚无任何
守卫调用 ``is_test_only()``**——生产模式不会因为它为真而拒绝启动。本文档如实陈述该
capability 的存在，不宣称守卫已实装；在合入 ``StandardAuthorizer`` 时补上「生产装配
拒绝 ``is_test_only()`` 为真的 Authorizer」守卫，是登记在案的 PR2 必做项。
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
    """恒放行。三个安全 PR 全部实装前的装配占位，不构成授权能力。"""

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
