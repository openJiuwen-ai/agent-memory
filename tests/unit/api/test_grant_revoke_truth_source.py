"""公共 grant/revoke 真源与上下文受控构造经 PEP 的端到端契约（F05 §Authorization）。

P1-1：``LocalMemoryAPI.grant`` / ``revoke`` 写入 Authorizer 实际查询的 ``GrantStore``
      （经 ``authorizer.management_grant_store()`` 共享），具名 YAML 令 Authorizer 引用
      别的 Store 时公共 grant 也写入同一实例，不双真源。
P1-2：绕过受控构造器、直接拼出的 ``RequestSecurityContext``（``_origin`` 未受控）不得
      进入授权--补齐 request_id/started_at 也不行。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError, ValidationError
from common.security.types import AuthContext, RequestSecurityContext
from common.type_def import Scope
from config import Config
from control.types import Action as ControlAction
from control.types import Grant as ControlGrant
from tests.conftest import sec

pytestmark = pytest.mark.unit

OWNER = Scope(org="acme", user="owner")
READER = Scope(org="acme", user="reader")


def test_public_grant_drives_the_authorizer_truth_source() -> None:
    """公开 grant 成功后，PDP 立即看到授权（P1-1）。"""
    api = build_kernel().api
    unit = api.write("shared", OWNER, security=sec(OWNER))[0]
    api.grant(
        ControlGrant(grantor=OWNER, grantee=READER, actions=[ControlAction.READ]),
        security=sec(OWNER),
    )
    assert api.get(unit.id, OWNER, security=sec(READER)).id == unit.id


def test_public_revoke_drives_the_authorizer_truth_source() -> None:
    """公开 revoke 撤掉 PDP 正在读的记录（P1-1）。"""
    api = build_kernel().api
    unit = api.write("shared", OWNER, security=sec(OWNER))[0]
    api.grant(
        ControlGrant(grantor=OWNER, grantee=READER, actions=[ControlAction.READ]),
        security=sec(OWNER),
    )
    api.revoke(
        ControlGrant(grantor=OWNER, grantee=READER, actions=[ControlAction.READ]),
        security=sec(OWNER),
    )
    with pytest.raises(PermissionDeniedError):
        api.get(unit.id, OWNER, security=sec(READER))


def test_yaml_named_authorizer_store_is_also_the_grant_truth_source() -> None:
    """Authorizer 改用具名 Store 后，公共 grant 仍写入同一实例（P1-1，非双真源）。"""
    config = Config.from_dict(
        {
            "grant_store": {"pdp_grants": {"target": "memory"}},
            "authorizer": {
                "default": {
                    "target": "standard",
                    "params": {
                        "grant_store": "pdp_grants",
                        "delegation_store": "default",
                    },
                }
            },
        }
    )
    api = build_kernel(config=config).api
    unit = api.write("named-store", OWNER, security=sec(OWNER))[0]
    api.grant(
        ControlGrant(grantor=OWNER, grantee=READER, actions=[ControlAction.READ]),
        security=sec(OWNER),
    )
    assert api.get(unit.id, OWNER, security=sec(READER)).id == unit.id


def test_structurally_complete_but_forged_context_is_rejected() -> None:
    """补齐 request_id/started_at 仍非受控来源，PEP 拒（P1-2）。"""
    api = build_kernel().api
    forged = RequestSecurityContext(
        auth=AuthContext(actor=OWNER),
        request_id="attacker-chosen-id",
        started_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError):
        api.write("forged-context", OWNER, security=forged)
