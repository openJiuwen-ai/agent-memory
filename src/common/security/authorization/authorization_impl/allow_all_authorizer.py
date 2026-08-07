"""恒放行 Authorizer —— **仅供测试**（F05 §授权不变量 8）。

存在的理由只有一个：单测里那些不关心授权的用例需要一个不需要布置 Grant/Delegation
的 Authorizer。它通过 :meth:`is_test_only` 把「我是测试件」声明成 capability，装配层
据此在生产模式拒绝启动——而不是靠核心去认 ``target == "allow_all"`` 这个名字
（S08 不变量 7：禁止按 target 名推测安全保证）。
"""

from __future__ import annotations

from common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from common.security.types import AuthContext, AuthorizationEnvironment, ResourceDescriptor


class AllowAllAuthorizer(Authorizer):
    """恒放行。恒放行是本实现的**全部**语义——不看角色、不看 scope、不看时效。"""

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        return AuthorizationDecision.allow("allow_all")

    def is_test_only(self) -> bool:
        return True

    def health(self) -> None:
        return None


@AuthorizationProducer.register("allow_all")
def _build(config) -> AllowAllAuthorizer:
    return AllowAllAuthorizer()
