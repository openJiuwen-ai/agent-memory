"""身份伪造必须被拒——第一期唯一改变系统安全性的回归防线。

改动前的行为（已复现）：``handler._actor_scope`` 从 payload 读
``actor_tenant_id`` / ``actor_scope``，任何调用方声明 ``actor_scope: "alice"``
即可读到 alice 的记忆；声明空值即可拿到空 ``Scope()``，命中
``SQLitePermissionManager.check`` 的 platform-admin 全局放行。

改动后：网络身份只来自认证层产出的 ``RequestSecurityContext``。legacy 内部协议
仍兼容历史 actor 字段，但传输适配器提供的 security actor 始终覆盖这些声明。

本文件测的是**跨应用入口与内核的完整链路**（认证中间件 → dispatch →
PermissionManager），故落 integration 而非 unit。
"""

from __future__ import annotations

import os
import sys

import pytest

# jiuwen_memory_entry/core 是 flat import root（server.py / handler.py / profiles.py），
# 不是包；与 http_server/cli surface 用同样的方式接进来。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CORE_DIR = os.path.join(_ROOT, "jiuwen_memory_entry", "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

from jiuwen_memory.common.bootstrap import register_plugins  # noqa: E402
from jiuwen_memory.common.errors import AuthenticationError  # noqa: E402
from jiuwen_memory.common.security import internal_context  # noqa: E402
from jiuwen_memory.common.security.authentication.key_store import KeyStoreProducer  # noqa: E402
from jiuwen_memory.common.security.types import Role, get_current  # noqa: E402
from jiuwen_memory.common.type_def.scope import Scope  # noqa: E402
from jiuwen_memory.config.context import AssemblyContext  # noqa: E402
from tests.support.scoped_authenticator import ScopedAuthenticator  # noqa: E402

pytestmark = pytest.mark.integration

_ALICE = Scope(org="acme", user="alice")
_MALLORY = Scope(org="acme", user="mallory")


@pytest.fixture(scope="module")
def srv():
    """一个装配好的进程内 Server（OFFLINE profile，纯内存栈）。

    显式装配 ``memory_api.permission.default`` 为 SQLite：本文件测的是**用户隔离**（跨主体
    403）。显式 DEV 入口会临时选用 ``AllowAllPermissionManager`` 维持本地业务流，
    隔离断言不能借用该全放行内核，须显式装 SQLite。
    """
    import server
    from profiles import OFFLINE, load_config

    # 与 OFFLINE 同层合并：给 memory_api 段一个显式 sqlite permission，保留用户隔离。
    sqlite_permission = {
        "memory_api": {
            "permission": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}
        }
    }
    return server.build(load_config([OFFLINE, sqlite_permission]))


def _dispatch(srv, verb, payload, *, security=None):
    return srv.dispatch(verb, payload, security=security)


# -- 核心：payload 不再能声明身份 ------------------------------------------- #


def test_claimed_identity_in_payload_cannot_override_authenticated_actor(srv) -> None:
    """曾经的越权路径：mallory 声明 ``actor_scope: alice`` 读到了 alice 的数据。

    legacy 内部协议为兼容旧调用仍识别这些字段；一旦适配器提供已认证 security，
    其 actor 必须覆盖 payload 声明，不能借字段伪造 Alice。
    """
    security = internal_context(ScopedAuthenticator(_MALLORY))
    for forged in (
        {"actor_scope": "alice"},
        {"actor_tenant_id": "acme", "actor_scope": "alice"},
        {"actor_tenant_id": " "},
        {"actor_agent": "bot"},
        {"actor_session": "s1"},
    ):
        payload = {"tenant_id": "acme", "scope": "alice", "item_id": "x", **forged}
        status, body = _dispatch(srv, "get", payload, security=security)
        assert status == 403, f"{forged} → {status} {body}"
        assert body["error"] == "PermissionDeniedError"


def test_identity_comes_from_context_not_payload(srv) -> None:
    """同一个 payload，认证上下文不同 → 授权结果不同。

    这条直接钉死「身份来自上下文」：payload 一字未改，只换了 AuthContext，
    alice 能读、mallory 不能。
    """
    alice_security = internal_context(ScopedAuthenticator(_ALICE))
    status, body = _dispatch(
        srv,
        "add",
        {"tenant_id": "acme", "scope": "alice", "content": "alice salary 999"},
        security=alice_security,
    )
    assert status == 200, body
    item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    assert _dispatch(srv, "get", payload, security=alice_security)[0] == 200

    status, body = _dispatch(
        srv, "get", payload, security=internal_context(ScopedAuthenticator(_MALLORY))
    )
    assert status == 403, body


# -- 认证与授权确实串起来了 --------------------------------------------------- #


def _api_key_named_config() -> dict:
    """具名形态的 API Key 配置：key_store 与 authenticator 都走具名命名空间。"""
    return {
        "memory_api": {
            "key_store": {"primary": {"target": "memory"}},
            "authenticator": {
                "primary": {
                    "target": "api_key",
                    "params": {"key_store": "primary", "root_api_key": "root-key-for-tests"},
                }
            },
            "security": {"default": {"target": "standard", "params": {"authenticator": "primary"}}},
        }
    }


def test_named_api_key_online_recheck_uses_authenticator_key_store() -> None:
    """具名 API Key 配置：在线复核必须用 Authenticator 的同一 KeyStore（P1-1）。

    Registry 由 composition root（``Server.build``）从认证器真源调和，不再从 root
    的 key_store 命名空间猜测 issuer。alice 的 key 写入 200；mallory 跨主体读 403；
    **撤销后撤销前认证的旧上下文立即拒绝**——在线复核读的就是签发的那份事实。
    """
    import server
    from auth_middleware import authenticated
    from profiles import OFFLINE, load_config

    from jiuwen_memory.common.security.authentication.key_store import fingerprint
    from jiuwen_memory.common.security.types import Credentials

    srv = server.build(load_config([OFFLINE, _api_key_named_config()]))
    assert srv.authenticator.mode() == "api_key"

    auth = srv.authenticator
    store = auth.key_store
    alice_key = store.issue(_ALICE, Role.USER)
    mallory_key = store.issue(_MALLORY, Role.USER)

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        status, body = _dispatch(
            srv,
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "alice secret"},
            security=security,
        )
    assert status == 200, body
    payload = {"tenant_id": "acme", "scope": "alice", "item_id": body["item_id"]}

    # 跨主体：mallory 的 key 读 alice 的条目 → 403（隔离语义由判定给出）。
    with authenticated(auth, Credentials(api_key=mallory_key)) as security:
        status, body = _dispatch(srv, "get", payload, security=security)
    assert status == 403, body
    assert body["error"] == "PermissionDeniedError"

    # 撤销生效：撤销前认证的旧上下文必须立即被拒（缓存的 AuthContext 在线复核）。
    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        store.revoke(fingerprint(alice_key))
        status, body = _dispatch(srv, "get", payload, security=security)
    assert status == 403, body

    # 撤销后的 key 也无法再通过认证。
    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key=alice_key)):
            pass  # pragma: no cover - authenticate 在进入 with 体之前就抛了


def test_unreconciled_authenticator_context_fails_closed(srv) -> None:
    """未与 composition root 调和的认证器上下文必须 fail-closed（400，不是放行）。

    Registry 恒为实例；手工构造、未经 ``Server.build`` 调和的 ApiKeyAuthenticator
    产出的上下文，其 issuer 未注册，在线复核按 fail-closed 拒绝。
    """
    from jiuwen_memory.common.security.authentication.authentication_impl.api_key_authenticator import (  # noqa: E501
        ApiKeyAuthenticator,
    )
    from jiuwen_memory.common.security.types import Credentials

    register_plugins()
    store = KeyStoreProducer.build("memory", {}, AssemblyContext())
    alice_key = store.issue(_ALICE, Role.USER)
    auth = ApiKeyAuthenticator(key_store=store, root_api_key="")

    from auth_middleware import authenticated

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        status, body = _dispatch(
            srv,
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "key-bound secret"},
            security=security,
        )
    assert status == 400, body
    assert body["error"] == "ValidationError"
    assert "CredentialStatusRegistry" in body["message"]


def test_context_is_reset_after_failed_authentication(srv) -> None:
    """认证失败后 ContextVar 必须干净——否则下一个请求会继承上一个的身份。

    `ThreadingHTTPServer` 每请求一线程但线程可能被复用，这是最严重的一类越权。
    """
    from auth_middleware import authenticated

    from jiuwen_memory.common.security.authentication.authentication_impl.api_key_authenticator import (  # noqa: E501
        ApiKeyAuthenticator,
    )
    from jiuwen_memory.common.security.types import Credentials

    register_plugins()
    store = KeyStoreProducer.build("memory", {}, AssemblyContext())
    alice_key = store.issue(_ALICE, Role.USER)
    auth = ApiKeyAuthenticator(key_store=store, root_api_key="")

    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="wrong")):
            pass  # pragma: no cover

    # 失败之后仍应是「无身份」，而不是残留上一次的。
    assert get_current() is None

    with authenticated(auth, Credentials(api_key=alice_key)) as ctx:
        assert ctx.actor == _ALICE

    assert get_current() is None


# -- 具名系统主体 + role=ROOT 的跨组织放行（PR2 验收项，PR1 不消费 role） -------- #

# ``test_dev_named_actor_passes_permission_gate_as_root`` 曾钉死「DEV 具名主体
# (org=local, user=developer) + role=ROOT 跨 org 放行」。其前提是 ``PermissionManager``
# 能拿到 ``role``——PR1 的权限门只收 ``Scope``（``_identity()`` 丢弃 role，接口签名
# 无 ``auth`` 参数），该语义在 PR1 不成立，恒 403。真实特权判定随 PR2 ``Authorizer``
# 实装（``authorize(auth=AuthContext, ...)`` 的角色闸门）才接通，故本链路在 PR2 提交，
# PR1 不在此验证跨组织放行。
