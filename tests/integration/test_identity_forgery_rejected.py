"""身份伪造必须被拒——第一期唯一改变系统安全性的回归防线。

改动前的行为（已复现）：``handler._actor_scope`` 从 payload 读
``actor_tenant_id`` / ``actor_scope``，任何调用方声明 ``actor_scope: "alice"``
即可读到 alice 的记忆；声明空值即可拿到空 ``Scope()``，命中
``SQLitePermissionManager.check`` 的 platform-admin 全局放行。

改动后：身份只来自认证层产出的 ``AuthContext``（security.md §9 铁律 #1），
payload 里出现身份声明字段一律 400。F05 迁移后它进一步显式化——身份由
``RequestSecurityContext`` 作为 ``dispatch`` 的参数传入，ContextVar 只剩
日志/trace 用途，故本文件的 ``set_current`` 布置一并换成显式传参。

本文件测的是**跨 bootstrap 与 src 的完整链路**（认证中间件 → dispatch →
Authorizer），故落 integration 而非 unit。
"""

from __future__ import annotations

import os
import sys

import pytest

# bootstrap/core 是 flat import root（server.py / handler.py / profiles.py），
# 不是包；与 http_server/cli surface 用同样的方式接进来。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CORE_DIR = os.path.join(_ROOT, "bootstrap", "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

from common.bootstrap import register_plugins  # noqa: E402
from common.errors import AuthenticationError  # noqa: E402
from common.security.authentication.key_store import KeyStoreProducer  # noqa: E402
from common.security.types import Role  # noqa: E402
from common.type_def.scope import Scope  # noqa: E402
from config.context import AssemblyContext  # noqa: E402
from tests.conftest import sec  # noqa: E402

pytestmark = pytest.mark.integration

_ALICE = Scope(org="acme", user="alice")
_MALLORY = Scope(org="acme", user="mallory")


@pytest.fixture(scope="module")
def srv():
    """一个装配好的进程内 Server（OFFLINE profile，纯内存栈）。"""
    import server
    from profiles import OFFLINE, load_config

    return server.build(load_config([OFFLINE]))


@pytest.fixture
def api_key_srv():
    """API Key Runtime 与内核撤销注册表共享同一装配图。"""
    import server
    from profiles import OFFLINE, load_config

    return server.build(
        load_config(
            [
                OFFLINE,
                {
                    "memory_api": {
                        "security": {
                            "default": {
                                "target": "standard",
                                "params": {
                                    "authenticator": {
                                        "target": "api_key",
                                        "params": {
                                            "root_api_key": "",
                                            "key_store": {"target": "memory"},
                                        },
                                    }
                                },
                            }
                        }
                    }
                },
            ]
        )
    )


def _dispatch(srv, verb, payload, security=None):
    from handler import dispatch

    return dispatch(srv, verb, payload, security)


# -- 核心：payload 不再能声明身份 ------------------------------------------- #


def test_claimed_identity_in_payload_is_rejected(srv) -> None:
    """曾经的越权路径：mallory 声明 ``actor_scope: alice`` 读到了 alice 的数据。

    现在这类字段一律 400——**静默忽略是不够的**：运维会以为
    「我传了 actor_scope」仍然生效，写出错误的安全认知。
    """
    for forged in (
        {"actor_scope": "alice"},
        {"actor_tenant_id": "acme", "actor_scope": "alice"},
        {"actor_tenant_id": " "},  # 曾经命中 platform-admin 全局放行
        {"actor_agent": "bot"},
        {"actor_session": "s1"},
    ):
        payload = {"tenant_id": "acme", "scope": "alice", "item_id": "x", **forged}
        status, body = _dispatch(srv, "get", payload, sec(_MALLORY))
        assert status == 400, f"{forged} → {status} {body}"
        assert body["error"] == "ValidationError"


def test_identity_comes_from_context_not_payload(srv) -> None:
    """同一个 payload，安全上下文不同 → 授权结果不同。

    这条直接钉死「身份来自上下文」：payload 一字未改，只换了 security，
    alice 能读、mallory 不能。
    """
    status, body = _dispatch(
        srv,
        "add",
        {"tenant_id": "acme", "scope": "alice", "content": "alice salary 999"},
        sec(_ALICE),
    )
    assert status == 200, body
    item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    assert _dispatch(srv, "get", payload, sec(_ALICE))[0] == 200

    status, body = _dispatch(srv, "get", payload, sec(_MALLORY))
    assert status == 403, body


def test_no_context_fails_closed(srv) -> None:
    """中间件漏挂时必须 401，绝不回退到 payload 或默认身份。

    这是 fail-closed 的落点：一个装配错误应该让所有请求失败，
    而不是让所有请求以未知身份成功。
    """
    status, body = _dispatch(srv, "get", {"tenant_id": "acme", "scope": "alice", "item_id": "x"})
    assert status == 401, body
    assert body["error"] == "AuthenticationError"


# -- 认证与授权确实串起来了 --------------------------------------------------- #


def test_api_key_binds_identity_end_to_end(api_key_srv) -> None:
    """用 A 主体的 key 去读 B 主体的数据 → 403（不是 200，也不是 401）。

    401 说明认证没过（key 无效），403 说明认证过了但授权拒了。
    这条要的是后者——证明 key → AuthContext → RequestSecurityContext → Authorizer
    整条链通了。``authenticated`` yield 的正是要传给 dispatch 的那个上下文。
    """
    from common.security.types import Credentials

    auth = api_key_srv.security.authenticator
    store = auth.key_store
    alice_key = store.issue(_ALICE, Role.USER)
    mallory_key = store.issue(_MALLORY, Role.USER)

    from auth_middleware import authenticated

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        status, body = _dispatch(
            api_key_srv,
            "add",
            {"tenant_id": "acme", "scope": "alice", "content": "key-bound secret"},
            security,
        )
        assert status == 200, body
        item_id = body["item_id"]

    payload = {"tenant_id": "acme", "scope": "alice", "item_id": item_id}

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        assert _dispatch(api_key_srv, "get", payload, security)[0] == 200

    with authenticated(auth, Credentials(api_key=mallory_key)) as security:
        assert _dispatch(api_key_srv, "get", payload, security)[0] == 403

    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="not-a-real-key")):
            pass  # pragma: no cover - authenticate 在进入 with 体之前就抛了


def test_context_is_reset_after_failed_authentication(srv) -> None:
    """认证失败后不得留下任何可被下一个请求继承的身份。

    `ThreadingHTTPServer` 每请求一线程但线程可能被复用，这是最严重的一类越权。
    身份改为显式传参后这条更强了：没有 ``authenticated`` 就没有 security 可传，
    dispatch 只能 401——不存在「残留态」这个概念。ContextVar 的 reset 仍在
    ``authenticated`` 的 ``finally`` 里，只是不再是授权的依据。
    """
    from auth_middleware import authenticated

    from common.security.authentication.authentication_impl.api_key_authenticator import (
        ApiKeyAuthenticator,
    )
    from common.security.types import Credentials

    register_plugins()
    store = KeyStoreProducer.build("memory", {}, AssemblyContext())
    alice_key = store.issue(_ALICE, Role.USER)
    auth = ApiKeyAuthenticator(key_store=store, root_api_key="")

    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="wrong")):
            pass  # pragma: no cover

    # 失败之后仍应是「无身份」，而不是残留上一次的
    assert _dispatch(srv, "get", {"tenant_id": "acme", "scope": "alice", "item_id": "x"})[0] == 401

    with authenticated(auth, Credentials(api_key=alice_key)) as security:
        assert security.auth.actor == _ALICE

    assert _dispatch(srv, "get", {"tenant_id": "acme", "scope": "alice", "item_id": "x"})[0] == 401
