"""安全上下文经 PEP 抵达 PDP（F05 §Authorization / §PEP-PDP 分离）。

`tests/unit/common/security/authorization/` 覆盖的是 PDP 自身的判定规则；本文件覆盖
**接线**：`LocalMemoryAPI._authorize` 是否真的把调用方传进来的 `RequestSecurityContext`
里的 `AuthContext` 交给 `Authorizer.authorize`。两者缺一不可——PDP 判得再对，PEP 不传
就等于没做。

身份不经 ContextVar 注入，`security` 是 API 的显式参数，所以判定依据在调用点就读得全：
角色写在 `internal_context(ScopedAuthenticator(..., role=...))` 里。

这里没有 `test_identity_must_match_the_authenticated_actor` 那类用例——它钉的是
「`identity` 参数与 ContextVar 里的 `auth` 不一致要拒」，而现在只剩 `security` 一个身份
入口，「两者不一致」这个状态构造不出来，规则也就无从违反。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Action, Grant, Role
from jiuwen_memory.common.type_def import Scope
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", user="alice")
_BOB = Scope(org="acme", user="bob")
_AGENT = Scope(org="acme", agent="assistant")

# 平台运维主体：不隶属某个 org，管理面入口的目标是系统级资源。
_PLATFORM_OPS = Scope(org="system", user="platform-ops")


@pytest.fixture()
def api():
    return build_kernel().api


def test_promoted_root_reaches_admin_plane(api) -> None:
    """一个绑了具体 org/user 的 ROOT 能用管理面。

    接线前它做不到：`_authorize` 只传 `identity`，PDP 看到的是个普通 alice，
    而管理面的鉴权 target 是空 `Scope()`——跨 org 直接拒。F05 明写两种 ROOT
    「在运行时权限检查中等价」，这条就是那句话的可执行形式。
    """
    root_alice = internal_context(ScopedAuthenticator(_ALICE, role=Role.ROOT))
    api.admin_set("rerank.enabled", "false", security=root_alice)
    assert api.admin_get("rerank.enabled", security=root_alice) == "false"


def test_plain_user_cannot_reach_admin_plane(api) -> None:
    with pytest.raises(PermissionDeniedError):
        api.admin_set(
            "rerank.enabled", "false", security=internal_context(ScopedAuthenticator(_ALICE))
        )


def test_admin_role_is_not_enough_for_admin_plane(api) -> None:
    """ADMIN 够不到 `ADMINISTER_SYSTEM`——那条动作的最小角色是 ROOT。

    这条断言的是**当前**的最小角色表，不是终局设计。若哪天把系统级管理面下放给
    ADMIN，改动会撞在这里，那正是它存在的意义。
    """
    with pytest.raises(PermissionDeniedError):
        api.admin_set(
            "rerank.enabled",
            "false",
            security=internal_context(ScopedAuthenticator(_ALICE, role=Role.ADMIN)),
        )


def test_agent_cannot_reach_a_user_scope(api) -> None:
    """agent 够不到 user 的 scope——代操作不再由认证产物直接表达。

    这里原有三条用例，钉的是 ``AuthContext.acting_user`` 触发的端到端代操作：agent
    带着一个 user 名就能读写那个 user 的 scope。该字段与判定路径已删除，因为 header
    里的一个 user 名证明不了那个 user 真的授权过（F05 §从 header 直接产生 Delegation）。

    委托的端到端形态由 ``DelegationStore`` 复核 ``delegation_id`` 重建，认证产物里
    的一个名字不再是依据。此刻的正确行为就是拒。
    """
    with pytest.raises(PermissionDeniedError):
        api.add(
            "代 alice 记下的内容", _ALICE, security=internal_context(ScopedAuthenticator(_AGENT))
        )
    with pytest.raises(PermissionDeniedError):
        api.add("越权写 bob", _BOB, security=internal_context(ScopedAuthenticator(_AGENT)))


def test_agent_cannot_grant_on_another_principals_behalf(api) -> None:
    """agent 对 alice 的 scope 发 SHARE 应 403，且不产生任何授权记录。

    否则 eve 会凭空拿到 alice 的长期读权限。原用例走的是「持 alice 委托的 agent」，
    委托来源换成 DelegationStore 之后这条断言仍然成立，且理由更简单：agent 根本够不到
    alice 的 scope。
    """
    eve = Scope(org="acme", user="eve")
    grant = Grant(grantor=_ALICE, grantee=eve, actions=[Action.READ])

    with pytest.raises(PermissionDeniedError):
        api.grant(grant, security=internal_context(ScopedAuthenticator(_AGENT)))

    # grant 未执行：eve 拿不到 alice 的任何权限
    with pytest.raises(PermissionDeniedError):
        api.get("anything", _ALICE, security=internal_context(ScopedAuthenticator(eve)))


def test_named_root_reaches_admin_plane_without_a_surface(api) -> None:
    """直接调 `build_kernel` 的路径（脚本、后台 job、examples）照样走同一套判定。

    它们不经过任何 surface 的认证中间件，但仍要自己给出 `RequestSecurityContext`：
    ROOT 由 `role` 表达，不由空 `Scope()` 这个形状表达——空 actor 现在是
    「上下文不完整」的信号，PDP 对它直接拒。
    """
    ops = internal_context(ScopedAuthenticator(_PLATFORM_OPS, role=Role.ROOT))
    assert api.admin_get("rerank.enabled", security=ops) is not None
