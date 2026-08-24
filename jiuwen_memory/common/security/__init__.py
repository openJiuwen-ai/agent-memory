"""安全域：认证、密码学、资源保护与请求安全上下文（F05 Common Security）。

本包是安全能力的**唯一归属地**。消费方（Bootstrap/Surface、MemoryAPI、Storage
适配器、Audit）只 import 本包的契约与值对象，不反向被 import。

各能力子包按 F05 目录组织；``authorization/`` 与 ``audit_integrity/`` 分别由 PR2、
PR3 补齐。本模块只再导出跨能力共享的公共类型与 Runtime--各能力的契约从其子包
取（``common.security.authentication`` 等），避免顶层 ``__init__`` 变成什么都有的
入口而在装配前意外触发全部 import。

**接口先行过渡期**：本仓库当前只合入 F05 契约层（types / 各能力 base / runtime），
``*_impl`` 实现包暂缓合入。旧加密模块 :mod:`common.security.security`
（``SecurityProvider`` 系，服务于存储加密装配）在实现 PR 落地前继续从本顶层
导出，避免破坏既有消费方；新契约的同名异常（如
:class:`~common.security.cryptography.base.AuthenticationFailedError`）从各自子包
取，不与本顶层旧导出冲突。
"""

from .key_source import KeySource
from .runtime import SecurityRuntime
from .security import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    EncryptionError,
    InvalidMagicError,
    KeyMismatchError,
    SecurityContext,
    SecurityError,
    SecurityProducer,
    SecurityProvider,
)
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
    "AuthenticationFailedError",
    "CorruptedCiphertextError",
    "Credentials",
    "CryptoContext",
    "EncryptionError",
    "InvalidMagicError",
    "KeyMismatchError",
    "KeySource",
    "RequestSecurityContext",
    "Role",
    "SecurityContext",
    "SecurityError",
    "SecurityProducer",
    "SecurityProvider",
    "SecurityRuntime",
    "Surface",
]
