"""授权管理公共契约：安全域 Grant/Action 与 grant/revoke 公开签名（SEC-API-01）。

只固定接口形状：公共授权类型是安全域契约、`grant()` 产出安全域 Grant、
`revoke()` 接受带 `grant_id` 的安全域 Grant。`grant_id` 由服务端生成、撤销按 ID
唯一定位（PR2 / 审核 P1-4）：Store 侧幂等精确撤销，缺失 ID 一律拒绝，不回退到
旧 PermissionManager 的条件批量撤销——本文件把「ID 定位不得误伤其他 Grant」
一并钉住（见 F05-security-api-contracts §5.4）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory import api as api_module
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security import types as security_types
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import Action as ControlAction
from jiuwen_memory.control import Grant as ControlGrant
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

_GRANTOR = Scope(org="acme", user="owner")
_GRANTEE = Scope(org="acme", user="reader")
_ACTIONS = frozenset({security_types.Action.READ})


def _api():
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    return build_kernel(config=cfg).api


def _grant(grant_id: str = "") -> security_types.Grant:
    return security_types.Grant(
        grant_id=grant_id, grantor=_GRANTOR, grantee=_GRANTEE, actions=_ACTIONS
    )


# -- 公共导出：新旧授权域不得双入口 --------------------------------------------- #


def test_public_and_control_grant_types_share_one_security_domain_source() -> None:
    """api/control 只做兼容再导出，不得维护第二套授权值对象。"""
    assert api_module.Grant is security_types.Grant
    assert api_module.Action is security_types.Action
    assert ControlGrant is security_types.Grant
    assert ControlAction is security_types.Action


def test_public_grant_preserves_legacy_constructor_shape() -> None:
    """公共导出切到安全域后，旧调用方不必预先提供服务端生成的 grant_id。"""
    grant = api_module.Grant(
        grantor=_GRANTOR,
        grantee=_GRANTEE,
        actions=[api_module.Action.READ],
    )

    assert grant.grant_id == ""
    assert grant.actions == _ACTIONS
    assert isinstance(grant.actions, frozenset)


# -- grant() / revoke()：公开签名形状 -------------------------------------------- #


def test_grant_returns_security_domain_grant() -> None:
    api = _api()
    created = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    assert isinstance(created, security_types.Grant)
    assert created.grantor == _GRANTOR
    assert created.grantee == _GRANTEE
    assert created.actions == _ACTIONS


def test_revoke_accepts_grant_id_bearing_grant() -> None:
    api = _api()
    api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    api.revoke(_grant("some-id"), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    # 幂等，不报错
    api.revoke(_grant("some-id"), security=internal_context(ScopedAuthenticator(_GRANTOR)))


# -- grant_id：服务端生成与按 ID 精确撤销 ---------------------------------------- #


def test_grant_id_is_server_generated() -> None:
    """ID 是审计标识，必须由服务端生成——客户端自述 ID 等于让调用方选审计键。"""
    api = _api()
    created = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    assert created.grant_id
    other = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    assert other.grant_id != created.grant_id


def test_client_supplied_grant_id_is_overwritten() -> None:
    """入参自带 ID 不被采信：否则两次创建可以撞同一个 ID，撤销一条会连坐另一条。"""
    api = _api()
    created = api.grant(
        _grant("client-chosen"), security=internal_context(ScopedAuthenticator(_GRANTOR))
    )
    assert created.grant_id != "client-chosen"


def test_grant_is_written_to_the_authorizer_store() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """PEP 写的真源必须就是 PDP 判定时读的那个 Store，否则授权写了等于没写。"""
    kernel = build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}}))
    created = kernel.api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    store = kernel.api._authorizer.management_grant_stores()[0]
    active = store.find_active(
        grantee=_GRANTEE,
        grantor_org=_GRANTOR.org,
        action=security_types.Action.READ,
        now=datetime.now(timezone.utc),
    )
    assert [grant.grant_id for grant in active] == [created.grant_id]


def test_revoke_by_id_does_not_touch_other_grants() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """反向测试：按 ID 撤销只能命中那一条，未知 ID 不得撤销任何东西。"""
    kernel = build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}}))
    api = kernel.api
    keep = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))
    drop = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    api.revoke(drop, security=internal_context(ScopedAuthenticator(_GRANTOR)))

    store = api._authorizer.management_grant_stores()[0]
    remaining = store.find_active(
        grantee=_GRANTEE,
        grantor_org=_GRANTOR.org,
        action=security_types.Action.READ,
        now=datetime.now(timezone.utc),
    )
    assert [grant.grant_id for grant in remaining] == [keep.grant_id]


def test_revoke_with_unknown_id_revokes_nothing() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    kernel = build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}}))
    api = kernel.api
    kept = api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    api.revoke(_grant("no-such-id"), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    store = api._authorizer.management_grant_stores()[0]
    remaining = store.find_active(
        grantee=_GRANTEE,
        grantor_org=_GRANTOR.org,
        action=security_types.Action.READ,
        now=datetime.now(timezone.utc),
    )
    assert [grant.grant_id for grant in remaining] == [kept.grant_id]


def test_revoke_without_grant_id_is_rejected() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """缺失 grant_id 的撤销必须拒绝：不得回退为按条件批量撤销（审核 P1-4）。

    条件撤销（grantor+grantee+action）连坐所有匹配记录：调用方带一个伪造 ID 或干脆
    不带 ID，都能变成「把别人的授权一起撤掉」。Store.revoke 只认 ID、幂等静默，
    拒绝发生在读 Store 之前，既无副作用也不误伤。
    """
    api = _api()
    api.grant(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    with pytest.raises(ValidationError, match="grant_id"):
        api.revoke(_grant(), security=internal_context(ScopedAuthenticator(_GRANTOR)))

    # 未撤销：那条 grant 仍然生效。
    store = api._authorizer.management_grant_stores()[0]
    remaining = store.find_active(
        grantee=_GRANTEE,
        grantor_org=_GRANTOR.org,
        action=security_types.Action.READ,
        now=datetime.now(timezone.utc),
    )
    assert remaining
