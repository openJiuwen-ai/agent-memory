"""接口先行过渡期的调用方桥接（**实装 PR 合入时删除**）。

背景：``MemoryAPI`` 公开签名已从 ``identity: Scope`` 固化为
``security: RequestSecurityContext``（接口契约先行合入），但认证/授权**实现**
（Authenticator / Authorizer 及各 ``*_impl``）随版本发布安排暂缓合入。过渡期
内，所有旧调用点（handler / CLI / MCP / 测试 / 示例）用本函数把原来的
identity ``Scope`` 包装成 ``RequestSecurityContext`` 继续传入。

**这是假认证**：``AuthContext`` 的 role / credential 字段为占位值，接口 PR 中
没有任何代码消费这些字段--:class:`~api.memory_api_impl.local_memory_api.LocalMemoryAPI`
只取 ``security.auth.actor`` 用于原有的 PermissionManager 路径，鉴权行为与
identity 直传时代逐位等价。payload 仍携带 identity 属已知临时态（与接口文档
「payload 不得声明 actor」的评审点冲突），实装 PR 合入时随本模块一并删除。
"""

from __future__ import annotations

from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext, Surface
from jiuwen_memory.common.type_def.scope import Scope


def legacy_request_context(
    actor: Scope,
    *,
    surface: Surface = Surface.INTERNAL,
    peer: str = "",
) -> RequestSecurityContext:
    """把旧调用方的 identity ``Scope`` 包装成 ``RequestSecurityContext``。"""
    return new_request_context(
        AuthContext(
            actor=actor,
            credential_type="legacy",
            auth_method="legacy",
        ),
        surface=surface,
        peer=peer,
    )
