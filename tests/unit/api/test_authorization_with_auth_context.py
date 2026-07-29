"""认证上下文经 PEP 抵达 PDP（security.md §3.2 / §3.5 / §4.3）。

`tests/unit/control/test_permission_role_aware.py` 覆盖的是 PDP 自身的判定规则；
本文件覆盖**接线**：`LocalMemoryAPI._authorize` 是否真的把 ContextVar 里的
`AuthContext` 取出来交给 `PermissionManager.check`。两者缺一不可——PDP 判得再对，
PEP 不传就等于没做。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError
from common.type_def import Scope
from common.type_def.auth import AuthContext, Role, reset_current, set_current
from config import Config
from control.types import Action, Grant

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", user="alice")
_BOB = Scope(org="acme", user="bob")


@contextmanager
def _as(ctx: AuthContext):
    token = set_current(ctx)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture()
def api():
    return build_kernel(config=Config.from_dict({"permission": {"default": "sqlite"}})).api


def test_promoted_root_reaches_admin_plane(api) -> None:
    """一个绑了具体 org/user 的 ROOT 能用管理面。

    接线前它做不到：`_authorize` 只传 `identity`，PDP 看到的是个普通 alice，
    而管理面的鉴权 target 是空 `Scope()`——跨 org 直接拒。§3.5 明写两种 ROOT
    「在运行时权限检查中等价」，这条就是那句话的可执行形式。
    """
    with _as(AuthContext(actor=_ALICE, role=Role.ROOT)):
        api.admin_set("rerank.enabled", "false", identity=_ALICE)
        assert api.admin_get("rerank.enabled", identity=_ALICE) == "false"


def test_plain_user_cannot_reach_admin_plane(api) -> None:
    with _as(AuthContext(actor=_ALICE, role=Role.USER)):
        with pytest.raises(PermissionDeniedError):
            api.admin_set("rerank.enabled", "false", identity=_ALICE)


def test_admin_role_is_not_enough_for_admin_plane(api) -> None:
    """ADMIN 本期没有额外权限——§3.2 里属于它的那行在本仓没有对应接口。

    这条断言的是**当前**行为，不是终局设计。租户管理面落地时它应当改，
    改动会撞在这里，那正是它存在的意义。
    """
    with _as(AuthContext(actor=_ALICE, role=Role.ADMIN)):
        with pytest.raises(PermissionDeniedError):
            api.admin_set("rerank.enabled", "false", identity=_ALICE)


def test_agent_writes_and_reads_on_behalf_of_user(api) -> None:
    """§4.3 路径 1 的端到端形态：agent 拿着 `acting_user` 读写目标 user 的 scope。"""
    agent = Scope(org="acme", agent="assistant")
    delegated = AuthContext(actor=agent, acting_user="alice", role=Role.USER)

    with _as(delegated):
        units = api.write("代 alice 记下的内容", _ALICE, identity=agent)
        assert units
        assert api.get(units[0].id, _ALICE, identity=agent) is not None


def test_agent_cannot_reach_a_user_it_does_not_act_for(api) -> None:
    agent = Scope(org="acme", agent="assistant")
    delegated = AuthContext(actor=agent, acting_user="alice", role=Role.USER)

    with _as(delegated):
        with pytest.raises(PermissionDeniedError):
            api.write("越权写 bob", _BOB, identity=agent)


def test_identity_must_match_the_authenticated_actor(api) -> None:
    """`identity` 与认证上下文不一致时拒绝。

    今天 handler 传的就是 `get_current().actor`，两者恒等；但没有任何东西**保证**
    这一点。直接调 `LocalMemoryAPI` 的代码（另一个 surface、一段脚本）完全可以
    传一个不相干的 `identity`，而在接线前那会被当成真身份。
    """
    with _as(AuthContext(actor=_BOB, role=Role.ROOT)):
        with pytest.raises(PermissionDeniedError):
            api.write("借 bob 的 ROOT 冒充 alice", _ALICE, identity=_ALICE)


def test_delegated_agent_cannot_grant_on_behalf_of_user(api) -> None:
    """审计 P1-1：委托不可代发授权，否则临时委托升级成永久 Grant。

    agent 持 alice 的委托对 alice 的 scope 发 SHARE 应 403，且不产生任何
    授权记录--否则 eve 会凭空拿到 alice 的长期读权限。
    """
    agent = Scope(org="acme", agent="assistant")
    delegated = AuthContext(actor=agent, acting_user="alice", role=Role.USER)
    grant = Grant(
        grantor=_ALICE,
        grantee=Scope(org="acme", user="eve"),
        actions=[Action.READ],
    )
    with _as(delegated):
        with pytest.raises(PermissionDeniedError):
            api.grant(grant, identity=agent)
    # 委托被拒、grant 未执行：eve 拿不到 alice 的任何权限
    with _as(AuthContext(actor=Scope(org="acme", user="eve"), role=Role.USER)):
        with pytest.raises(PermissionDeniedError):
            api.get("anything", _ALICE, identity=Scope(org="acme", user="eve"))


def test_no_auth_context_keeps_legacy_root_shape(api) -> None:
    """无认证上下文时空 `Scope()` 仍是 platform admin。

    `examples/quickstart.py`、直接调 `build_kernel` 的测试、后台 job 都走这条路径——
    它们不是安全场景，不该被角色闸门打红。
    """
    assert api.admin_get("rerank.enabled", identity=Scope()) is not None
