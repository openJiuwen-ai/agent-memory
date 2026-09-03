"""默认 DEV 业务流的回归防线：未接 PR2 前跨组织业务不断流。

第三次验收报告的合入阻断项之一：默认（未配置 security）服务的跨组织 ``add`` 在
PR1 后变成了 403，破坏了既有业务流程。同事的改法是删除原来期望 200 的测试、把
403 改写为「预期行为」——那是在改验收目标以适应实现，不是修复。

本文件的修复口径（与「不影响既有业务流程」的目标一致）：

- 不把 ``role`` 重新塞回 :class:`PermissionManager`（那会提前实现 PR2 的授权语义）；
- 不恢复空 ``Scope()`` 的 platform-admin 隐式放行；
- 不用临时 :class:`AllowAllAuthorizer` 接管判定（它当前不在任何判定路径上）；
- 而是只在 ``jiuwen_memory_entry.core.server.Server.build`` 这一条装配路径上，于「未配置 security、
  隐式 loopback DEV、未显式选择 permission」时把默认权限实现换成既有
  :class:`AllowAllPermissionManager`（恒放行、不消费 role、不调用 Authorizer）。

已核实的装配事实（与第四次验收报告 SDK-SCOPE-01 的修复一致）：

- 公共 :func:`api.build_kernel` / :func:`api.assemble` **不做**此覆写，默认权限仍是
  内置的 sqlite/:memory:（``defaults.py`` 的 ``permission.default``）；
- ``Server.build`` 经 :func:`~jiuwen_memory_entry.core.server._apply_dev_permission_fallback`
  把 ``permission.default`` 注入为 ``allow_all``——注入的是 ``config.settings["memory_api"]``
  的**副本**，不修改用户原字典；触发条件 = 该段无 ``security``（DEV 回落）且无
  ``permission``（用户未显式选），两者其一被显式配置都尊重用户选择、不注入；
- 同一时刻 ``build_security_runtime`` 因无 ``security`` 段回落 DEV 认证，产出
  ``system/dev`` 主体、``role=ROOT``；
- ``PermissionManager`` 不收 ``role``：接口只有 ``(actor, target, action, context)``，
  放行与否完全由 ``Scope`` 判定。DEV 回落主体（``system/dev``）持具名 Scope，恒被
  sqlite 的平台-admin 全局放行线之外的跨组织判定拒绝——这是改为显式 SQLite 后 403
  的根因，也是「为什么 DEV 兼容覆写必须只在 Server.build 注入」的原因。

于是本文件按用户拆解的 5 点断言钉死业务保障：
1. 隐式 loopback DEV 跨组织 add/get 成功；
2. 显式 API Key 跨主体访问仍为 403；
3. 显式 SQLite permission 不被 DEV 默认覆盖；
4. PR1 业务路径不调用临时 Authorizer；
5. DEV 远端绑定仍被拒绝。
"""

from __future__ import annotations

import os
import sys

import pytest

# 本文件是装配白盒验收，需要确认临时 DEV fallback 未扩散到其他入口。
# pylint: disable=protected-access

# jiuwen_memory_entry/core 是 flat import root（server.py / handler.py / profiles.py），
# 不是包；与 http_server/cli surface 用同样的方式接进来。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CORE_DIR = os.path.join(_ROOT, "jiuwen_memory_entry", "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

from jiuwen_memory.api import assemble, build_kernel  # noqa: E402
from jiuwen_memory.common.errors import (  # noqa: E402
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.security.authorization.authorization_impl.allow_all_authorizer import (  # noqa: E402
    AllowAllAuthorizer,
)
from jiuwen_memory.common.security.legacy import legacy_request_context  # noqa: E402
from jiuwen_memory.common.security.types import Credentials, Role  # noqa: E402
from jiuwen_memory.common.type_def.scope import Scope  # noqa: E402
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (  # noqa: E402
    AllowAllPermissionManager,
)
from jiuwen_memory.control.permission_impl.sqlite_permission_manager import (  # noqa: E402
    SQLitePermissionManager,
)

pytestmark = pytest.mark.integration

_DEFAULT_ORG = "acme"
_DEFAULT_USER = "alice"
_ALICE = Scope(org="acme", user="alice")
_MALLORY = Scope(org="acme", user="mallory")


def _dispatch(srv, verb, payload):
    from handler import dispatch

    return dispatch(srv, verb, payload)


def _dev_context():
    """返回一个可用的 :class:`DevAuthenticator`（导入即触发注册，幂等）。"""
    from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
        DevAuthenticator,
    )

    return DevAuthenticator()


@pytest.fixture(scope="module")
def dev_srv():
    """未配置 security / permission 的默认进程内 Server（OFFLINE 档）。

    ``Server.build`` 经 ``_apply_dev_permission_fallback`` 把 ``permission.default``
    注入为 ``allow_all``（注入 ``config.settings["memory_api"]`` 的副本，不改原字典）；
    ``build_security_runtime`` 因无 ``security`` 段回落 DEV。公共 ``build_kernel`` /
    ``assemble`` 不做此覆写，默认权限仍是 sqlite。
    """
    import server
    from profiles import OFFLINE, load_config

    return server.build(load_config([OFFLINE]))


@pytest.fixture(scope="module")
def sqlite_srv():
    """显式装配 SQLite permission 的 Server（与第三份报告约束 5 同口径）。

    隔离性断言（跨主体 403）不能借用 DEV 的恒放行内核，必须显式装 SQLite。
    """
    import server
    from profiles import OFFLINE, load_config

    sqlite_permission = {
        "memory_api": {
            "permission": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}
        }
    }
    return server.build(load_config([OFFLINE, sqlite_permission]))


# -- 第 1 点：隐式 loopback DEV 跨组织 add/get 成功 ---------------------------- #


def test_dev_fallback_cross_org_add_get_succeeds(dev_srv) -> None:
    """默认（未配置 security）服务的跨组织 add/get 必须仍是 200。

    这是「不影响既有业务流程」的直接落点：DEV 回落主体是具名 ``system/dev``，
    只经 ``role=ROOT`` 表达服务端特权，而 PR1 的权限门不消费 role——跨组织放行
    由恒放行的 ``AllowAllPermissionManager`` 保住，不是靠提前接通 role。
    """
    from auth_middleware import authenticated

    auth = _dev_context()
    payload = {"tenant_id": "acme", "scope": "alice", "content": "dev cross-org note"}

    with authenticated(auth, Credentials()):
        status, body = _dispatch(dev_srv, "add", payload)
        assert status == 200, body
        item_id = body["item_id"]

    get_payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}
    with authenticated(auth, Credentials()):
        assert _dispatch(dev_srv, "get", get_payload)[0] == 200


def test_dev_fallback_still_enforces_loopback_binding(dev_srv) -> None:
    """第 5 点：回落 DEV 不等于放开绑定，非 loopback 在绑定前仍被拒绝。"""
    runtime = dev_srv.security
    runtime.binding_policy.check(
        "127.0.0.1",
        requires_loopback=runtime.authenticator.requires_loopback_binding(),
    )
    with pytest.raises(ValidationError):
        runtime.binding_policy.check(
            "0.0.0.0",
            requires_loopback=runtime.authenticator.requires_loopback_binding(),
        )


# -- 第 2 点：显式 API Key 跨主体访问仍为 403 --------------------------------- #


def test_api_key_cross_principal_still_403() -> None:
    """显式装配 API Key 时，隔离语义不因 DEV 回退而放宽。

    用真实 ``Server.build`` 配置（security 段显式 api_key + 未配 permission）：
    权限实现回落到 sqlite，经 ``srv.authenticator.key_store`` 给 A/B 签发不同 key，
    用 A 的 key 写、用 B 的 key 读同一数据 → 403（不是 200，也不是 401）。
    """
    import server
    from auth_middleware import authenticated
    from profiles import OFFLINE, load_config

    api_key_cfg = {
        "memory_api": {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {
                            "target": "api_key",
                            "params": {"root_api_key": "root-key-for-tests"},
                        }
                    },
                }
            }
        }
    }
    srv = server.build(load_config([OFFLINE, api_key_cfg]))
    assert isinstance(srv.api._perm, SQLitePermissionManager)

    auth = srv.authenticator
    store = auth.key_store
    alice_key = store.issue(_ALICE, Role.USER)
    mallory_key = store.issue(_MALLORY, Role.USER)

    with authenticated(auth, Credentials(api_key=alice_key)):
        status, body = srv.dispatch(
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "alice secret"},
        )
        assert status == 200, body
        item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    with authenticated(auth, Credentials(api_key=alice_key)):
        assert srv.dispatch("get", payload)[0] == 200

    with authenticated(auth, Credentials(api_key=mallory_key)):
        assert srv.dispatch("get", payload)[0] == 403


# -- 第 3 点：显式 SQLite permission 不被 DEV 默认覆盖 ------------------------ #


def test_explicit_sqlite_not_overridden_by_dev_default(sqlite_srv) -> None:
    """显式装了 SQLite 的服务，权限实现不得被 DEV 兼容回退偷偷替换成 allow_all。"""
    assert isinstance(sqlite_srv.api._perm, SQLitePermissionManager)
    assert not isinstance(sqlite_srv.api._perm, AllowAllPermissionManager)


# -- 第 4 点：PR1 业务路径不调用临时 Authorizer -------------------------------- #


def test_dev_fallback_perm_is_allow_all_authorizer_placeholder_only(dev_srv) -> None:
    """默认 DEV 服务用 ``AllowAllPermissionManager`` 放行业务，**不**接临时 Authorizer。

    这里的语义拆清：业务判定全部经 ``PermissionManager.decide``（PEP 的
    ``_authorize_with_context`` 只调 ``_perm``）。Runtime 持有的 ``AllowAllAuthorizer``
    只是 P2 实装前的必填字段占位，且它自身声称 ``is_test_only()``——绝不能再让一条
    具体业务断言借它放行，否则「恒放行 Authorizer 从占位变成生效规则」。
    """
    # 业务内核是恒放行的 PermissionManager（保住跨组织业务不断流）。
    assert isinstance(dev_srv.api._perm, AllowAllPermissionManager)
    # Runtime 里那个 Authorizer 只是占位：不在任何判定路径上，且声明 test-only。
    assert isinstance(dev_srv.security.authorizer, AllowAllAuthorizer)
    assert dev_srv.security.authorizer.is_test_only()


def test_dev_business_path_never_calls_authorizer(dev_srv, monkeypatch) -> None:
    """第 4 点炸弹探针：把 Authorizer 换成「一调用就炸」，DEV add 仍为 200。

    只用最小通过条件里的「存在性 + test-only」断言，不足以证明 PR1 业务路径
    **不调用**临时 Authorizer——万一哪条路径悄悄接上它，恒放行就从占位变成生效规则。
    所以这里做负向探针：把 ``authorizer.authorize`` 替换成 ``AssertionError`` 炸弹，
    再走一次真实 DEV add。若返回 200，说明判定全程未触摸 ``authorize``（业务只经
    ``PermissionManager.decide``）；若炸弹被触发，说明业务路径误接了 Authorizer，
    必须立刻修掉——这正是「占位绝不能再变成生效规则」的机器化守门。
    """
    from auth_middleware import authenticated

    def _bomb(*args, **kwargs):
        raise AssertionError("业务路径调用了 Authorizer.authorize()——PR1 判定必须只走 _perm")

    monkeypatch.setattr(dev_srv.security.authorizer, "authorize", _bomb)

    auth = _dev_context()
    payload = {"tenant_id": "acme", "scope": "alice", "content": "bomb probe note"}
    with authenticated(auth, Credentials()):
        status, body = _dispatch(dev_srv, "add", payload)
        assert status == 200, body


# -- 第四次验收 SDK-SCOPE-01：DEV 兼容覆写只在 Server.build 注入 ----------------- #


def test_public_build_kernel_default_is_sqlite() -> None:
    """公共 :func:`build_kernel` 默认权限仍是 sqlite，不做 DEV 覆写。"""
    kernel = build_kernel()
    assert isinstance(kernel.api._perm, SQLitePermissionManager)
    assert not isinstance(kernel.api._perm, AllowAllPermissionManager)


def test_public_assemble_default_is_sqlite() -> None:
    """公共 :func:`assemble` 默认权限仍是 sqlite，不做 DEV 覆写。"""
    api = assemble()
    assert isinstance(api._perm, SQLitePermissionManager)
    assert not isinstance(api._perm, AllowAllPermissionManager)


def test_dev_server_injects_allow_all_on_copy() -> None:
    """``Server.build`` 注入的是 ``config.settings["memory_api"]`` 的副本，不改原字典。

    触发条件 = 无 ``security``（DEV 回落）且无 ``permission``（用户未显式选）。
    """
    import server
    from profiles import OFFLINE, load_config

    raw = {"memory_api": {}}
    cfg = load_config([OFFLINE, raw])
    srv = server.build(cfg)
    assert isinstance(srv.api._perm, AllowAllPermissionManager)
    # 用户原字典不被改动：memory_api 里没有凭空多出 permission 段。
    assert "permission" not in cfg.settings["memory_api"]
    # 公共入口不受影响。
    assert isinstance(build_kernel().api._perm, SQLitePermissionManager)


def test_explicit_api_key_without_permission_is_sqlite() -> None:
    """显式配了 ``security``（走向 api_key）时，未配 permission 不触发 DEV 覆盖。

    这是验收方「显式 API Key/Trusted 跨主体访问仍为 403」的最小通过条件：权限实现
    回到 sqlite，跨主体判定由 Scope 隔离，而不是被 DEV 默认偷偷换成恒放行。
    """
    import server
    from profiles import OFFLINE, load_config

    api_key_cfg = {
        "memory_api": {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {
                            "target": "api_key",
                            "params": {"root_api_key": "root-key-for-tests"},
                        }
                    },
                }
            }
        }
    }
    srv = server.build(load_config([OFFLINE, api_key_cfg]))
    assert isinstance(srv.api._perm, SQLitePermissionManager)
    assert not isinstance(srv.api._perm, AllowAllPermissionManager)
    assert srv.authenticator.mode() == "api_key"


def test_explicit_trusted_without_permission_is_sqlite() -> None:
    """显式配了 ``security``（走向 trusted）时，未配 permission 不触发 DEV 覆盖。

    这是验收方「显式 Trusted 跨主体访问仍为 403」的最小通过条件：权限实现回到
    sqlite，跨主体判定由 Scope 隔离。这里用真实 ``Server.build`` + Trusted 网关
    header 走一遍真实装配：alice 写/读同一条 200，mallory 读同一条 403——证明不是
    DEV 的恒放行把门打开，而是 sqlite 的 Scope 隔离在起作用（与 api_key 探针对称）。
    """
    import server
    from auth_middleware import authenticated
    from profiles import OFFLINE, load_config

    trusted_cfg = {
        "memory_api": {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {
                            "target": "trusted",
                            "params": {"gateway_key": "gw-key-for-tests"},
                        }
                    },
                }
            }
        }
    }
    srv = server.build(load_config([OFFLINE, trusted_cfg]))
    assert isinstance(srv.api._perm, SQLitePermissionManager)
    assert not isinstance(srv.api._perm, AllowAllPermissionManager)
    assert srv.authenticator.mode() == "trusted"

    # Trusted 主体必须先注册（未注册一律 fail-closed 拒绝），role 从注册表查、
    # 不从 header 读——header 只声明「你是谁」。gateway_key 配了就要对上。
    # ``TrustedAuthenticator`` 未暴露公开 ``key_store`` 属性（与 ApiKeyAuthenticator
    # 不对称），这里经私有 ``_key_store`` 访问；配合模式："trusted" 本身只声明注册表。
    store = srv.authenticator._key_store
    store.issue(_ALICE, Role.USER)
    store.issue(_MALLORY, Role.USER)

    def _trusted(principal_id: str) -> Credentials:
        return Credentials(
            api_key="gw-key-for-tests",
            headers={
                "x-org-id": "acme",
                "x-principal-type": "user",
                "x-principal-id": principal_id,
            },
        )

    with authenticated(srv.authenticator, _trusted("alice")):
        status, body = srv.dispatch(
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "alice secret"},
        )
        assert status == 200, body
        item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    with authenticated(srv.authenticator, _trusted("alice")):
        assert srv.dispatch("get", payload)[0] == 200

    with authenticated(srv.authenticator, _trusted("mallory")):
        assert srv.dispatch("get", payload)[0] == 403


def test_sdk_cross_principal_via_sqlite_is_permission_denied() -> None:
    """SDK 直连路径：Alice 同一身份 add/get 成功，Mallory 读 Alice 的同一条 → 拒绝。

    绕过 surface dispatch，直接以 :class:`PermissionDeniedError` 断言（不是 HTTP 403，
    是 PEP 层抛的异常）。这证明公共内核默认的 sqlite 真的在做跨主体隔离，而不是
    DEV 的恒放行把门打开——这是验收方「显式 SQLite permission 不被 DEV 默认覆盖」
    在 SDK 直连路径上的直接落点。
    """
    api = assemble()
    items = api.add("alice secret", _ALICE, security=legacy_request_context(_ALICE))
    assert items
    item_id = items[0].id

    got = api.get(item_id, _ALICE, security=legacy_request_context(_ALICE))
    assert got.id == item_id

    with pytest.raises(PermissionDeniedError):
        api.get(item_id, _ALICE, security=legacy_request_context(_MALLORY))
