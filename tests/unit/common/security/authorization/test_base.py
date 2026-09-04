"""Authorizer 契约层（F05 §PEP 与 PDP / §授权不变量 8）。

接口先行版：``authorization_impl`` 未合入，test-only capability 用测试内
stub 表达；``AuthorizationDecision`` 的构造期不变量照测。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.authorization.base import (
    AuthorizationDecision,
    Authorizer,
    RoutingFieldsProvider,
)
from jiuwen_memory.common.security.types import DenyReason

# ====================================================================== #
# AuthorizationDecision
# ====================================================================== #


def test_allow_decision_carries_the_rule_that_permitted_it() -> None:
    """allow 侧必须记录放行规则，以便审计区分 owner 访问和 Grant 放行。"""
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
# capability 默认值
# ====================================================================== #


class _TestOnlyAuthorizer(Authorizer):
    """恒放行 stub：以 capability 声明自己是测试件（接口先行版替身）。"""

    def authorize(self, *, auth, resource, environment):
        return AuthorizationDecision.allow("stub_allow_all")

    def health(self) -> None:
        return None

    def is_test_only(self) -> bool:
        return True


def test_test_only_capability_is_a_declaration_not_a_name() -> None:
    """test-only 必须通过 capability 声明，装配层据此拒绝生产启动。"""
    assert _TestOnlyAuthorizer().is_test_only()


def test_authorizer_default_is_not_test_only() -> None:
    """默认 ``False``：新实现不会因为忘了覆写而被误判成测试件放进生产。"""

    class Minimal(Authorizer):
        def authorize(self, *, auth, resource, environment):
            return AuthorizationDecision.deny(DenyReason.DEFAULT_DENY, "minimal")

        def health(self) -> None:
            return None

    assert not Minimal().is_test_only()


def test_routing_fields_default_empty() -> None:
    class Minimal(Authorizer):
        def authorize(self, *, auth, resource, environment):
            return AuthorizationDecision.deny(DenyReason.DEFAULT_DENY, "minimal")

        def health(self) -> None:
            return None

    assert Minimal().routing_fields() == ()
    assert Authorizer.routing_fields is RoutingFieldsProvider.routing_fields


def test_management_grant_stores_default_is_empty() -> None:
    """不基于 GrantStore 的实现没有管理写真源：默认空列表，不抛错。"""

    class Minimal(Authorizer):
        def authorize(self, *, auth, resource, environment):
            return AuthorizationDecision.deny(DenyReason.DEFAULT_DENY, "minimal")

        def health(self) -> None:
            return None

    assert Minimal().management_grant_store() is None
    assert Minimal().management_grant_stores() == []


def test_authorizer_cannot_be_partially_implemented() -> None:
    class Incomplete(Authorizer):
        def health(self) -> None:
            return None

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
