"""授权管理公共契约：安全域 Grant/Action 与 grant/revoke 公开签名（SEC-API-01）。

只固定接口形状：公共授权类型是安全域契约、`grant()` 产出安全域 Grant、
`revoke()` 接受带 `grant_id` 的安全域 Grant。`GrantStore` 未实装前运行语义
仍是旧 PermissionManager 的条件撤销，本文件把该过渡行为一并钉住，避免误读为
「精确 ID 撤销已生效」（见 F05-security-api-contracts §5.4）。
"""

from __future__ import annotations

import pytest

from jiuwen_memory import api as api_module
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.security import types as security_types
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import Action as ControlAction
from jiuwen_memory.control import Grant as ControlGrant
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (
    AllowAllPermissionManager,
)

pytestmark = pytest.mark.unit

_GRANTOR = Scope(org="acme", user="owner")
_GRANTEE = Scope(org="acme", user="reader")
_ACTIONS = frozenset({security_types.Action.READ})
_CAPTURED_PERMISSION_MANAGERS: list[AllowAllPermissionManager] = []


@PermissionProducer.register("grant_contract_recording")
def _build_recording_permission_manager(config) -> AllowAllPermissionManager:
    del config
    manager = AllowAllPermissionManager()
    _CAPTURED_PERMISSION_MANAGERS.append(manager)
    return manager


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
    created = api.grant(_grant(), security=legacy_request_context(_GRANTOR))
    assert isinstance(created, security_types.Grant)
    assert created.grantor == _GRANTOR
    assert created.grantee == _GRANTEE
    assert created.actions == _ACTIONS


def test_grant_reaches_legacy_permission_manager_without_lossy_conversion() -> None:
    _CAPTURED_PERMISSION_MANAGERS.clear()
    cfg = Config.from_dict({"permission": {"default": "grant_contract_recording"}})
    api = build_kernel(config=cfg).api
    grant = _grant("preserved-id")

    api.grant(grant, security=legacy_request_context(_GRANTOR))

    assert _CAPTURED_PERMISSION_MANAGERS[-1].grants[-1] is grant


def test_revoke_accepts_grant_id_bearing_grant() -> None:
    api = _api()
    api.grant(_grant(), security=legacy_request_context(_GRANTOR))
    api.revoke(_grant("some-id"), security=legacy_request_context(_GRANTOR))
    api.revoke(_grant("some-id"), security=legacy_request_context(_GRANTOR))  # 幂等，不报错


# -- 过渡行为：grant_id 尚未参与定位 --------------------------------------------- #


def test_grant_id_is_not_yet_server_generated() -> None:
    """GrantStore 未实装：服务端不生成 ID，返回值原样回传，不冒充精确撤销。"""
    api = _api()
    created = api.grant(_grant(), security=legacy_request_context(_GRANTOR))
    assert created.grant_id == ""


def test_revoke_still_accepts_empty_grant_id() -> None:
    """旧 /v1/revoke 请求（无 grant_id）必须保持可用，不因接口固定而破坏。"""
    api = _api()
    api.grant(_grant(), security=legacy_request_context(_GRANTOR))
    api.revoke(_grant(), security=legacy_request_context(_GRANTOR))


# -- 过渡适配：安全域独有动作 fail-closed ---------------------------------------- #


def test_security_only_action_fails_closed_on_grant() -> None:
    """安全域独有动作在旧 PermissionManager 无对应成员时必须显式失败。"""
    api = _api()
    with pytest.raises(ValueError):
        api.grant(
            security_types.Grant(
                grant_id="",
                grantor=_GRANTOR,
                grantee=_GRANTEE,
                actions=frozenset({security_types.Action.MANAGE_SPACE}),
            ),
            security=legacy_request_context(_GRANTOR),
        )
