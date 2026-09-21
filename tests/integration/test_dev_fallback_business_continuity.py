"""显式 DEV 业务流的回归防线：PR2 接管后跨组织业务不断流。

第三次验收报告的合入阻断项之一：默认（未配置 security）服务的跨组织 ``add`` 在
PR1 后变成了 403，破坏了既有业务流程。同事的改法是删除原来期望 200 的测试、把
403 改写为「预期行为」——那是在改验收目标以适应实现，不是修复。

**PR2 已把这段旁路删除**：判定接通 PDP 后，DEV 回落产出的 ``role=ROOT`` 经
``StandardAuthorizer`` 的 ROOT 档正常放行，跨组织业务由**判定**保住。于是同一条业务
保障换了担保人——从「装配层有一条绕开判定的恒放行分支」变成「判定读到了服务端认过
的档位」。本文件随之改测后者：DEV 的 200 必须是判定给的，且判定用的就是 Runtime 持有
的那一个实例。

已核实的装配事实（与第四次验收报告 SDK-SCOPE-01 的修复一致）：

- 公共 :func:`api.assemble` **不做**此覆写，默认权限仍是
  内置的 sqlite/:memory:（``defaults.py`` 的 ``permission.default``）；
- ``Server.build`` **不再**改写 ``permission`` 段：默认权限实现回到内置 sqlite，与公共
  入口一致；
- 入口显式配置 DEV 认证，产出 ``system/dev``
  主体、``role=ROOT``；
- ``PermissionManager`` 不收 ``role``（接口只有 ``(actor, target, action, context)``），
  但内容读写的判定 PR2 起由 ``Authorizer`` 终局，它收整份 ``AuthContext``——``role``
  在那里有执行点。跨组织放行走的是 ROOT 档，不是 Scope 形状。

于是本文件按用户拆解的 5 点断言钉死业务保障：
1. 显式 loopback DEV 跨组织 add/get 成功；
2. 显式 API Key 跨主体访问仍为 403；
3. 显式 SQLite permission 不被任何默认覆盖；
4. 业务路径的判定就是 Runtime 持有的那个 Authorizer（PR1 是「不得调用」，PR2 是
   「必须是同一个」——两版都在钉死「判定点只有一个」）；
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

from jiuwen_memory.api import assemble  # noqa: E402
from jiuwen_memory.common.errors import (  # noqa: E402
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.security import internal_context  # noqa: E402
from jiuwen_memory.common.security.authorization.authorization_impl.allow_all_authorizer import (  # noqa: E402
    AllowAllAuthorizer,
)
from jiuwen_memory.common.security.types import Credentials, Role  # noqa: E402
from jiuwen_memory.common.type_def.scope import Scope  # noqa: E402
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (  # noqa: E402
    AllowAllPermissionManager,
)
from jiuwen_memory.control.permission_impl.sqlite_permission_manager import (  # noqa: E402
    SQLitePermissionManager,
)
from tests.support.scoped_authenticator import ScopedAuthenticator  # noqa: E402

pytestmark = pytest.mark.integration

_DEFAULT_ORG = "acme"
_DEFAULT_USER = "alice"
_ALICE = Scope(org="acme", user="alice")
_MALLORY = Scope(org="acme", user="mallory")


def _dispatch(srv, verb, payload, *, security):
    return srv.dispatch(verb, payload, security=security)


def _dev_context():
    """返回一个可用的 :class:`DevAuthenticator`（导入即触发注册，幂等）。"""
    from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
        DevAuthenticator,
    )

    return DevAuthenticator()


def _dev_config():
    """显式 DEV 配置；由 ``Server.build`` 在内核装配后构建 security runtime。"""
    return {
        "memory_api": {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {"authenticator": {"target": "dev"}},
                }
            }
        }
    }


@pytest.fixture(scope="module")
def dev_srv():
    """入口显式选择 DEV、未配置 permission 的进程内 Server（OFFLINE 档）。

    PR2 由真实 Authorizer 消费 ROOT 角色；不再注入 ``allow_all`` PermissionManager。
    公共 ``assemble`` 与 Server 的旧 permission 兼容对象都保持默认 sqlite，但它不进入
    生产判定路径。
    """
    import server
    from profiles import OFFLINE, load_config

    return server.Server.build(load_config([OFFLINE, _dev_config()]))


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


# -- 第 1 点：显式 loopback DEV 跨组织 add/get 成功 ---------------------------- #


def test_dev_fallback_cross_org_add_get_succeeds(dev_srv) -> None:
    """默认（未配置 security）服务的跨组织 add/get 必须仍是 200。

    这是「不影响既有业务流程」的直接落点：DEV 主体是具名 ``local/developer``，
    只经 ``role=ROOT`` 表达服务端特权，而 PR1 的权限门不消费 role——跨组织放行
    由恒放行的 ``AllowAllPermissionManager`` 保住，不是靠提前接通 role。
    """
    from auth_middleware import authenticated

    auth = _dev_context()
    payload = {"tenant_id": "acme", "scope": "alice", "content": "dev cross-org note"}

    with authenticated(auth, Credentials()) as security:
        status, body = _dispatch(dev_srv, "add", payload, security=security)
        assert status == 200, body
        item_id = body["item_id"]

    get_payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}
    with authenticated(auth, Credentials()) as security:
        assert _dispatch(dev_srv, "get", get_payload, security=security)[0] == 200


def test_dev_fallback_still_enforces_loopback_binding(dev_srv) -> None:
    """第 5 点：显式 DEV 不等于放开绑定，非 loopback 在绑定前仍被拒绝。"""
    runtime = dev_srv.security_runtime
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
    """显式装配 API Key（**内联形态**）时，隔离语义不因 DEV 回退而放宽。

    Registry 真源闭环（P1-1 整改）：``Server.build`` 在 runtime 装配完成后，把
    Authenticator 实际持有的 KeyStore 与已绑定 issuer 注册进 PEP 的
    ``CredentialStatusRegistry``——注册表不再从 root 的 key_store 命名空间猜测。
    于是本条恢复为隔离断言：alice 的 key 写入 200；mallory 跨主体读 403；撤销后
    撤销前认证的旧上下文立即拒绝（在线复核读的就是签发 KeyStore 的同一份事实）。
    """
    import server
    from auth_middleware import authenticated
    from profiles import OFFLINE, load_config

    from jiuwen_memory.common.security.authentication.key_store import fingerprint

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

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        status, body = srv.dispatch(
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "alice secret"},
            security=security,
        )

    assert status == 200, body
    payload = {"tenant_id": "acme", "scope": "alice", "item_id": body["item_id"]}

    # 跨主体：mallory 的 key 读 alice 的条目 → 403。
    with authenticated(auth, Credentials(api_key=mallory_key)) as security:
        status, body = srv.dispatch("get", payload, security=security)
    assert status == 403, body
    assert body["error"] == "PermissionDeniedError"

    # 撤销生效：撤销前认证的旧上下文立即被拒（在线复核）。
    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        store.revoke(fingerprint(alice_key))
        status, body = srv.dispatch("get", payload, security=security)
    assert status == 403, body

    # Root Key 不声明在线复核——同一装配下它照常放行，证明上面的拒绝钉在
    # 「凭据撤销复核」上，而不是「api_key 装配整体坏了」。
    with authenticated(auth, Credentials(api_key="root-key-for-tests")) as security:
        status, body = srv.dispatch(
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "root note"},
            security=security,
        )
    assert status == 200, body


# -- 第 3 点：显式 SQLite permission 不被 DEV 默认覆盖 ------------------------ #


def test_explicit_sqlite_not_overridden_by_dev_default(sqlite_srv) -> None:
    """显式装了 SQLite 的服务，权限实现不得被 DEV 兼容回退偷偷替换成 allow_all。"""
    assert isinstance(sqlite_srv.api._perm, SQLitePermissionManager)
    assert not isinstance(sqlite_srv.api._perm, AllowAllPermissionManager)


# -- 第 4 点：业务路径的判定就是 Runtime 持有的那个 Authorizer ------------------ #


def test_dev_default_has_no_allow_all_bypass(dev_srv) -> None:
    """DEV 默认装配里不得再有任何恒放行件——判定与权限两侧都不许。

    PR1 靠 ``AllowAllPermissionManager`` 保住 DEV 业务，代价是装配层多了一条绕开判定
    的分支；``AllowAllAuthorizer`` 则是判定实装前的必填字段占位。PR2 两者都不需要了，
    所以两者都必须不在场：只要还有一个恒放行件默认装上，「本地开发方便」就会变成
    「默认部署有一条放行旁路」。断言取 ``is_test_only()`` 这个 capability，不看 target
    名（F05 §授权不变量 8）。
    """
    assert not isinstance(dev_srv.api._perm, AllowAllPermissionManager)
    assert not isinstance(dev_srv.security_runtime.authorizer, AllowAllAuthorizer)
    assert dev_srv.security_runtime.authorizer.is_test_only() is False


def test_dev_business_path_goes_through_the_runtime_authorizer(dev_srv, monkeypatch) -> None:
    """炸弹探针反向：把 Runtime 的 Authorizer 换成「一调用就炸」，DEV add 必须炸。

    PR1 用同一个探针钉「业务路径**不**调用 Authorizer」；PR2 判定接通后，同一个探针
    钉的是它的对偶——业务路径**必须**调用，且调用的就是 ``srv.security.authorizer``
    这一个对象。monkeypatch 打在 Runtime 持有的实例上而炸弹响了，说明 PEP 与 Runtime
    拿的是同一个实例；若不响，只能是两边各持一份（各自的 Grant/Delegation 视图会分叉，
    同一次授权在两处得到不同结论），或判定压根没接上。

    这条断言比「存在性 + 类型」强：类型相同的两个实例照样是两个判定点。
    """
    from auth_middleware import authenticated

    def _bomb(*args, **kwargs):
        raise AssertionError("bomb: runtime authorizer reached")

    monkeypatch.setattr(dev_srv.security_runtime.authorizer, "authorize", _bomb)

    auth = _dev_context()
    payload = {"tenant_id": "acme", "scope": "alice", "content": "bomb probe note"}
    with authenticated(auth, Credentials()) as security:
        status, body = _dispatch(dev_srv, "add", payload, security=security)
    assert status == 500 and "bomb: runtime authorizer reached" in body["message"], body


def test_dev_cross_org_is_allowed_by_the_root_role_not_by_a_bypass(dev_srv, monkeypatch) -> None:
    """DEV 的 200 必须是判定给的：把 role 降成 USER，同一个请求立刻 403。

    上一条证明「判定被调用了」，不足以证明「200 是判定给的」——一个恒放行判定同样
    会被调用。这里改的是**唯一的输入差异**：DEV 认证产出的 ``role``。ROOT 时 200、
    USER 时 403，说明放行确实由 ROOT 档做出，而不是别处还留着一条旁路。

    同时反证 handler 没有把认证结果里的 role 丢掉：丢掉就恒为 USER，ROOT 那一问
    也会是 403。
    """
    import dataclasses

    from auth_middleware import authenticated

    auth = _dev_context()
    original = auth.authenticate

    def _as_user(credentials):
        return dataclasses.replace(original(credentials), role=Role.USER)

    monkeypatch.setattr(auth, "authenticate", _as_user)

    payload = {"tenant_id": "acme", "scope": "alice", "content": "downgraded role note"}
    with authenticated(auth, Credentials()) as security:
        status, _ = _dispatch(dev_srv, "add", payload, security=security)
    assert status == 403


# -- 第四次验收 SDK-SCOPE-01：DEV 兼容覆写只在 Server.build 注入 ----------------- #


def test_public_assemble_default_is_sqlite() -> None:
    """公共 :func:`assemble` 默认权限仍是 sqlite，不做 DEV 覆写。"""
    api = assemble()
    assert isinstance(api._perm, SQLitePermissionManager)
    assert not isinstance(api._perm, AllowAllPermissionManager)


def test_dev_adapter_does_not_rewrite_the_permission_section() -> None:
    """DEV 配置适配器不再改写 ``permission`` 段。

    PR1 在这里注入 ``permission.default=allow_all``（注入副本、不改原字典），本用例
    当时钉的是「注入了、且没污染用户配置」。旁路删除后钉的是它的终点：bootstrap 路径
    与公共 ``assemble`` 装出的东西必须一样——只要还存在「只有服务端这条路会变」的装配
    分支，本地跑通就不再证明部署跑得通。
    """
    import server
    from dev_security import with_local_dev_security
    from profiles import OFFLINE, load_config

    raw = {"memory_api": {}}
    cfg = load_config([OFFLINE, raw])
    adapted = with_local_dev_security(cfg)
    srv = server.build(adapted)
    assert isinstance(srv.api._perm, SQLitePermissionManager)
    assert not isinstance(srv.api._perm, AllowAllPermissionManager)
    assert "permission" not in cfg.settings["memory_api"]
    assert "permission" not in adapted.settings["memory_api"]
    assert "security" in adapted.settings["memory_api"]
    assert isinstance(assemble()._perm, SQLitePermissionManager)


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

    with authenticated(srv.authenticator, _trusted("alice")) as security:
        status, body = srv.dispatch(
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "alice secret"},
            security=security,
        )
        assert status == 200, body
        item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    with authenticated(srv.authenticator, _trusted("alice")) as security:
        assert srv.dispatch("get", payload, security=security)[0] == 200

    with authenticated(srv.authenticator, _trusted("mallory")) as security:
        assert srv.dispatch("get", payload, security=security)[0] == 403


def test_sdk_cross_principal_via_sqlite_is_permission_denied() -> None:
    """SDK 直连路径：Alice 同一身份 add/get 成功，Mallory 读 Alice 的同一条 → 拒绝。

    绕过 surface dispatch，直接以 :class:`PermissionDeniedError` 断言（不是 HTTP 403，
    是 PEP 层抛的异常）。这证明公共内核默认的 sqlite 真的在做跨主体隔离，而不是
    DEV 的恒放行把门打开——这是验收方「显式 SQLite permission 不被 DEV 默认覆盖」
    在 SDK 直连路径上的直接落点。
    """
    api = assemble()
    items = api.add("alice secret", _ALICE, security=internal_context(ScopedAuthenticator(_ALICE)))
    assert items
    item_id = items[0].id

    got = api.get(item_id, _ALICE, security=internal_context(ScopedAuthenticator(_ALICE)))
    assert got.id == item_id

    with pytest.raises(PermissionDeniedError):
        api.get(item_id, _ALICE, security=internal_context(ScopedAuthenticator(_MALLORY)))
