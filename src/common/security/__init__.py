"""安全域：认证、密码学、资源保护与请求安全上下文（F05 Common Security）。

本包是安全能力的**唯一归属地**。消费方（Bootstrap/Surface、MemoryAPI、Storage
适配器、Audit）只 import 本包的契约与值对象，不反向被 import。

各能力子包按 F05 目录组织；``audit_integrity/`` 由 PR3 补齐。本模块只再导出跨能力
共享的公共类型、``RequestSecurityContext`` 的受控构造入口与 Runtime——各能力的契约从
其子包取（``common.security.authentication`` 等），避免顶层 ``__init__`` 变成什么都有
的入口而在装配前意外触发全部 import。
"""

from .request_context import internal_context, new_request_context
from .runtime import SecurityRuntime, SecurityRuntimeProducer
from .types import (
    AuthContext,
    Credentials,
    CryptoContext,
    RequestSecurityContext,
    Role,
    Surface,
)

__all__ = [
    "AuthContext",
    "Credentials",
    "CryptoContext",
    "RequestSecurityContext",
    "Role",
    "SecurityRuntime",
    "SecurityRuntimeProducer",
    "Surface",
    "internal_context",
    "new_request_context",
]
