"""角色感知授权与 agent 代操作委托（security.md §3.2 / §3.5 / §4.3）。

PR① 造出了 ``AuthContext``，但 ``role`` 与 ``acting_user`` 两个字段至今没有消费方：
认证层算出来、进审计 detail、然后在授权边界被丢掉。本文件覆盖把它们接进 PDP 之后
的行为，以及「没有认证上下文时行为逐字不变」这条向后兼容线。
"""

from __future__ import annotations

import pytest

from common.type_def import Scope
from common.type_def.auth import AuthContext, Role
from control.permission_impl.allow_all_permission_manager import AllowAllPermissionManager
from control.permission_impl.sqlite_permission_manager import SQLitePermissionManager
from control.types import Action, PermissionContext

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", space="product", user="alice")
_BOB = Scope(org="acme", space="product", user="bob")
_AGENT = Scope(org="acme", space="product", agent="assistant")


@pytest.fixture()
def mgr(tmp_path) -> SQLitePermissionManager:
    return SQLitePermissionManager(str(tmp_path / "permission.db"))


# -- 角色闸门 ------------------------------------------------------------- #


def test_promoted_root_is_equivalent_to_declared_root(mgr) -> None:
    """§3.5：「提升式 ROOT」与「声明式 ROOT」在运行时权限检查中等价。

    今天 PDP 唯一能识别的特权是 ``actor == Scope()``（声明式 ROOT 的 actor 形态），
    一个绑了具体 org/user 的 ROOT 在它眼里就是普通用户——两者并不等价。
    """
    promoted = AuthContext(actor=_ALICE, role=Role.ROOT)

    assert mgr.check(_ALICE, _BOB, Action.READ, auth=promoted) is True
    assert mgr.check(_ALICE, Scope(org="other", user="carol"), Action.WRITE, auth=promoted) is True


def test_empty_actor_without_root_role_is_denied(mgr) -> None:
    """空 actor 不再单凭形状拿到全局放行——纵深防御。

    今天 ``PrincipalKeyStore.issue`` 会拒绝签发 actor 为空的 key，但那道闸在
    ``security/`` 层。PDP 自己必须也守住：换一个 authenticator 实现、或将来加
    OAuth 通道时，没人保证那个前置假设还在。
    """
    impostor = AuthContext(actor=Scope(), role=Role.USER)

    assert mgr.check(Scope(), _ALICE, Action.READ, auth=impostor) is False


def test_empty_actor_is_still_root_without_auth_context(mgr) -> None:
    """无认证上下文时保留旧规则——测试、后台 job 与直连 build_kernel 的路径不受影响。"""
    assert mgr.check(Scope(), _ALICE, Action.READ) is True


def test_admin_role_gets_no_extra_power(mgr) -> None:
    """ADMIN 在本期**刻意**没有额外权限：§3.2 里属于 ADMIN 的那行（管理本租户
    user/agent）在本仓一个接口都没有，凭空造闸门守一扇不存在的门就是 dead flexibility。
    """
    admin = AuthContext(actor=_ALICE, role=Role.ADMIN)

    assert mgr.check(_ALICE, _BOB, Action.READ, auth=admin) is False


def test_auth_actor_mismatch_is_denied(mgr) -> None:
    """``auth`` 与 ``actor`` 指向不同主体时 fail-closed。

    两个身份来源不一致，要么是接线错误要么是攻击，两种都该拒。与 F01 决策 1
    （``AuthContext.actor`` 不给默认值）是同一思路的两面：不让装配错误变成静默的权限。
    """
    alice_ctx = AuthContext(actor=_ALICE, role=Role.ROOT)

    # 拿着 alice 的 ROOT 上下文去问「bob 能不能读」——不认。
    assert mgr.check(_BOB, _ALICE, Action.READ, auth=alice_ctx) is False


# -- agent 代 user 操作（§4.3 路径 1） ------------------------------------- #


def test_agent_acting_for_user_may_access_that_user(mgr) -> None:
    """认证层产出 ``actor=agent`` + ``acting_user=alice`` 时，目标 alice 的 scope 放行。

    今天 ``_owner_scope_covers(Scope(agent=...), Scope(user=...))`` 恒 False
    （primary 维不等），grants 表里也没有这条——代操作必然 403。
    """
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)

    assert mgr.check(_AGENT, _ALICE, Action.READ, auth=delegated) is True
    assert mgr.check(_AGENT, _ALICE, Action.WRITE, auth=delegated) is True


def test_delegation_cannot_share_on_behalf_of_user(mgr) -> None:
    """审计 P1-1：委托只覆盖记忆 CRUD，不含 SHARE。

    否则 agent 拿到一次请求级委托后，可对 acting_user 的 scope 发 SHARE，给
    自己（或同伙）写长期 Grant，把临时委托升级成永久访问。
    """
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)
    assert mgr.check(_AGENT, _ALICE, Action.SHARE, auth=delegated) is False


def test_agent_without_acting_user_is_still_denied(mgr) -> None:
    """证明放行来自委托本身，而不是别的规则顺带放过的。"""
    bare = AuthContext(actor=_AGENT, role=Role.USER)

    assert mgr.check(_AGENT, _ALICE, Action.READ, auth=bare) is False


def test_delegation_does_not_reach_other_users(mgr) -> None:
    """委托目标只能是 ``acting_user`` 本人。"""
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)

    assert mgr.check(_AGENT, _BOB, Action.READ, auth=delegated) is False


def test_delegation_does_not_cross_org(mgr) -> None:
    """org 是硬边界（§4.2）：同名 user 在别的 org 不是同一个人。"""
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)
    other_org_alice = Scope(org="other", space="product", user="alice")

    assert mgr.check(_AGENT, other_org_alice, Action.READ, auth=delegated) is False


def test_delegation_does_not_cross_space(mgr) -> None:
    """space 同理：授权发生在某个 space 内，不外溢到同 org 的别的 space。"""
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)
    other_space_alice = Scope(org="acme", space="coding", user="alice")

    assert mgr.check(_AGENT, other_space_alice, Action.READ, auth=delegated) is False


def test_delegation_does_not_reach_another_agent_branch(mgr) -> None:
    """代 user 操作的目标是 user 分支，不是另一个 agent 的分支。"""
    delegated = AuthContext(actor=_AGENT, acting_user="alice", role=Role.USER)
    alice_other_agent = Scope(org="acme", space="product", user="alice", agent="other-bot")

    assert mgr.check(_AGENT, alice_other_agent, Action.READ, auth=delegated) is False


def test_user_cannot_claim_delegation_toward_an_agent(mgr) -> None:
    """§4.3 只定义了 agent 代 user 一个方向，反向不成立。"""
    reversed_ctx = AuthContext(actor=_ALICE, acting_user="alice", role=Role.USER)

    assert mgr.check(_ALICE, _AGENT, Action.READ, auth=reversed_ctx) is False


# -- 管理面闸门（§3.2 后四行里有接口的那三行） ------------------------------ #


@pytest.mark.parametrize("resource_type", ["admin", "audit"])
def test_management_resources_require_root(mgr, resource_type: str) -> None:
    """管理面靠 ``PermissionContext.resource_type`` 表达，不靠「target 恰好是空 scope」。

    靠形状表达语义正是角色缺口的同一个毛病。``resource_type`` 的注释里本来就列了
    ``admin``（``control/types.py``），只是从没有人填。
    """
    user_ctx = AuthContext(actor=_ALICE, role=Role.USER)
    root_ctx = AuthContext(actor=_ALICE, role=Role.ROOT)
    context = PermissionContext(resource_type=resource_type)

    assert mgr.check(_ALICE, Scope(), Action.READ, context, auth=user_ctx) is False
    assert mgr.check(_ALICE, Scope(), Action.READ, context, auth=root_ctx) is True


def test_sharing_own_scope_is_not_a_management_operation(mgr) -> None:
    """``grant`` **不**进管理面闸门。

    §3.2 那行说的是「**跨租户**修改权限」，而跨 org 的 grant 今天已被
    ``actor.org != target.org`` 挡住。对自己 scope 发 grant 是 Grant 模型的
    主用途——把它闸进 ROOT 会把正常共享一起废掉。
    """
    user_ctx = AuthContext(actor=_ALICE, role=Role.USER)

    assert mgr.check(_ALICE, _ALICE, Action.SHARE, auth=user_ctx) is True
    assert mgr.check(_ALICE, Scope(org="other"), Action.SHARE, auth=user_ctx) is False


def test_management_resource_denied_even_within_own_scope(mgr) -> None:
    """管理面闸门优先于 owner-cover：否则把 target 填成自己的 scope 就绕过去了。"""
    user_ctx = AuthContext(actor=_ALICE, role=Role.USER)
    context = PermissionContext(resource_type="admin", scope=_ALICE)

    assert mgr.check(_ALICE, _ALICE, Action.WRITE, context, auth=user_ctx) is False


def test_space_lifecycle_requires_root(mgr) -> None:
    """§3.2「创建/删除租户」→ ROOT。``create_space`` / ``delete_space`` 已在传
    ``resource_type="space"``，只需在 check 里对写类动作要求 ROOT。"""
    user_ctx = AuthContext(actor=_ALICE, role=Role.USER)
    root_ctx = AuthContext(actor=_ALICE, role=Role.ROOT)
    context = PermissionContext(resource_type="space")
    target = Scope(org="acme", space="product")

    assert mgr.check(_ALICE, target, Action.WRITE, context, auth=user_ctx) is False
    assert mgr.check(_ALICE, target, Action.DELETE, context, auth=user_ctx) is False
    assert mgr.check(_ALICE, target, Action.WRITE, context, auth=root_ctx) is True


def test_space_read_is_not_gated_by_role(mgr) -> None:
    """读 space 元数据不是「创建/删除租户」，不该被 ROOT 闸挡住——
    否则普通用户连自己所在 space 的名字都拿不到。"""
    user_ctx = AuthContext(actor=_ALICE, role=Role.USER)
    context = PermissionContext(resource_type="space_list")

    assert mgr.check(_ALICE, Scope(org="acme"), Action.READ, context, auth=user_ctx) is False
    assert mgr.check(_ALICE, _ALICE, Action.READ, context, auth=user_ctx) is True


# -- 其它实现 -------------------------------------------------------------- #


def test_allow_all_stays_all_allow_with_auth() -> None:
    """测试用实现不该被安全逻辑污染：它的全部语义就是「恒 True」。"""
    mgr = AllowAllPermissionManager()
    denied_shape = AuthContext(actor=Scope(), role=Role.USER)

    assert mgr.check(_ALICE, _BOB, Action.WRITE, auth=denied_shape) is True


def test_routing_passes_auth_through_to_delegate(tmp_path) -> None:
    """S03「routing 不改变授权语义，只选择 delegate」——``auth`` 必须原样透传，
    否则路由型部署下角色闸门与委托会**静默失效**。"""
    from control.permission_impl.routing_permission_manager import RoutingPermissionManager

    seen: list[AuthContext | None] = []

    class _Spy(SQLitePermissionManager):
        def check(self, actor, target, action, context=None, *, auth=None):
            seen.append(auth)
            return super().check(actor, target, action, context, auth=auth)

    delegate = _Spy(str(tmp_path / "permission.db"))
    router = RoutingPermissionManager(policies={"strict": delegate}, routes={}, fallback="strict")
    ctx = AuthContext(actor=_ALICE, role=Role.ROOT)

    assert router.check(_ALICE, _BOB, Action.READ, auth=ctx) is True
    assert seen == [ctx]


# -- 向后兼容 -------------------------------------------------------------- #


def test_no_auth_context_preserves_every_legacy_rule(mgr) -> None:
    """``auth=None`` 时逐字回到今天的纯 ACL 行为。

    这条撑着三件事同时成立：33 处既有 ``_authorize`` 调用点不改也能跑、
    ``AllowAllPermissionManager`` 语义不动、直接调 ``api.write(identity=...)``
    的测试与 ``examples/quickstart.py`` 不受影响。
    """
    assert mgr.check(Scope(), _ALICE, Action.READ) is True  # platform admin
    assert mgr.check(_ALICE, _ALICE, Action.WRITE) is True  # owner covers
    assert mgr.check(_ALICE, _BOB, Action.READ) is False  # 同 org 不同主体
    assert mgr.check(_ALICE, Scope(org="other"), Action.READ) is False  # 跨 org
