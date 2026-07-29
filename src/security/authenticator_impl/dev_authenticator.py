"""DEV 认证：无条件返回 ROOT（security.md §2.2.1）。

**只用于本地开发。** 配套的 localhost 强制绑定在
:func:`security.binding.check_dev_binding`，由 HTTP surface 在启动时调用——
本类不知道服务器绑了哪个地址，也不该在一个可被单测 import 的类里 ``sys.exit``。
"""

from __future__ import annotations

from common.type_def.auth import AuthContext, Role
from common.type_def.scope import Scope
from security.authenticator import Authenticator, AuthProducer
from security.types import AuthMode, Credentials


class DevAuthenticator(Authenticator):
    """恒 ROOT，不校验任何凭据。"""

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """无条件返回 ROOT 身份。

        ROOT 的 actor 是**空 Scope()**——与 ``LocalMemoryAPI._ROOT`` 及
        ``SQLitePermissionManager.check`` 的第一条规则（``actor == Scope()``
        全局通过）一致。security.md §2.2.1 示例写的 ``Scope(org="*")`` 是参考
        demo 的形态，在本主干会被 check 的「跨 org 拒绝」规则挡住，不可照搬。
        """
        return AuthContext(actor=Scope(), role=Role.ROOT)

    def mode(self) -> AuthMode:
        return AuthMode.DEV

    def health(self) -> None:
        return None


@AuthProducer.register("dev")
def _build(config):
    return DevAuthenticator()
