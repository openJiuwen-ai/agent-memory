"""request_context: RequestSecurityContext 的受控构造入口契约。

构造点收拢在两个入口函数：``request_id`` 服务端生成、``surface`` 必须显式、
进程内直连必须显式传入 authenticator。这些不变量只在接口层验证，不含任何实现。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.request_context import internal_context, new_request_context
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role, Surface
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit


class _DevAuthenticator(Authenticator):
    """进程内直连的 stub：身份由 authenticator 产出，不由调用方声明。"""

    def authenticate(self, credentials: Credentials) -> AuthContext:
        return AuthContext(actor=Scope(org="acme", user="system"), role=Role.ROOT)

    def mode(self) -> str:
        return "stub-dev"

    def health(self) -> None:
        return None


def test_request_id_is_generated_not_supplied() -> None:
    """request_id 服务端生成：同一身份两次请求各得各的 id。"""
    auth = AuthContext(actor=Scope(org="acme", user="alice"))
    first = new_request_context(auth, surface=Surface.HTTP)
    second = new_request_context(auth, surface=Surface.HTTP)
    assert first.request_id != second.request_id
    assert first.request_id  # 非空


def test_surface_has_no_default() -> None:
    """漏写 surface 必须在构造点失败：缺省会把漏传的网络请求伪装成进程内调用。"""
    with pytest.raises(TypeError):
        new_request_context(AuthContext(actor=Scope(org="acme", user="alice")))  # type: ignore[call-arg]


def test_attributes_default_to_empty_and_not_none() -> None:
    auth = AuthContext(actor=Scope(org="acme", user="alice"))
    ctx = new_request_context(auth, surface=Surface.MCP)
    assert ctx.attributes == {}


def test_internal_context_requires_explicit_authenticator() -> None:
    """无参领取超管上下文已不允许：拿到 ROOT 必须是一次显式传入。"""
    with pytest.raises(TypeError):
        internal_context()  # type: ignore[call-arg]


def test_internal_context_identity_comes_from_authenticator() -> None:
    """调用方要操作哪个 Scope 走业务参数；自己是谁由 authenticator 决定。"""
    ctx = internal_context(_DevAuthenticator())
    assert ctx.actor == Scope(org="acme", user="system")
    assert ctx.surface is Surface.INTERNAL
    assert ctx.has_valid_origin()
