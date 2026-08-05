"""Authorizer 契约层（F05 §PEP 与 PDP / §授权不变量 8）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.security.authorization.authorization_impl.allow_all_authorizer import (
    AllowAllAuthorizer,
)
from common.security.authorization.base import AuthorizationDecision, Authorizer
from common.security.types import (
    Action,
    AuthContext,
    AuthorizationEnvironment,
    DenyReason,
    ResourceDescriptor,
)
from common.type_def import Scope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# ====================================================================== #
# AuthorizationDecision
# ====================================================================== #


def test_allow_decision_carries_the_rule_that_permitted_it() -> None:
    """allow 侧也必须记规则：只记 deny 的原因，审计里就看不出一次放行是
    「owner 访问自己的数据」还是「某条快过期的 Grant 兜住了」。"""
    decision = AuthorizationDecision.allow("owner_cover")
    assert decision.allowed
    assert decision.rule == "owner_cover"
    assert decision.reason is None


def test_deny_decision_requires_a_reason() -> None:
    with pytest.raises(ValueError):
        AuthorizationDecision(allowed=False, rule="whatever")


def test_allow_decision_rejects_a_deny_reason() -> None:
    """``allowed=True`` 却带着 ``CROSS_ORG`` 是矛盾状态，构造期就该拒。"""
    with pytest.raises(ValueError):
        AuthorizationDecision(allowed=True, rule="owner_cover", reason=DenyReason.CROSS_ORG)


def test_decision_requires_a_rule() -> None:
    with pytest.raises(ValueError):
        AuthorizationDecision.allow("")


def test_decision_is_frozen() -> None:
    decision = AuthorizationDecision.allow("owner_cover")
    with pytest.raises(Exception):
        decision.allowed = False  # type: ignore[misc]


# ====================================================================== #
# 测试专用能力
# ====================================================================== #


def test_allow_all_declares_itself_test_only() -> None:
    """allow-all 通过 **capability** 声明自己是测试件（F05 §授权不变量 8）。

    装配层据此在生产模式拒绝启动，而不是靠核心去认 ``target == "allow_all"``
    这个名字——第三方注册的恒放行实现同样要能被拦住，而它的 target 名核心不认识
    （S08 不变量 7）。
    """
    assert AllowAllAuthorizer().is_test_only()


def test_authorizer_default_is_not_test_only() -> None:
    """默认 ``False``：新实现不会因为忘了覆写而被误判成测试件放进生产。"""

    class Minimal(Authorizer):
        def authorize(self, *, auth, resource, environment):
            return AuthorizationDecision.deny(DenyReason.DEFAULT_DENY, "minimal")

        def health(self) -> None:
            return None

    assert not Minimal().is_test_only()


def test_allow_all_ignores_every_input() -> None:
    """恒放行是它的全部语义——过期上下文、空 actor、管理动作一律放行。"""
    decision = AllowAllAuthorizer().authorize(
        auth=AuthContext(actor=Scope()),
        resource=ResourceDescriptor(
            action=Action.ADMINISTER_SYSTEM, resource_type="admin", scope=Scope()
        ),
        environment=AuthorizationEnvironment(now=NOW),
    )
    assert decision.allowed
