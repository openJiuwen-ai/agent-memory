# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""安全域：认证、密码学、资源保护与请求安全上下文（F05 Common Security）。

本包是安全能力的**唯一归属地**。消费方（Bootstrap/Surface、MemoryAPI、Storage
适配器、Audit）只 import 本包的契约与值对象，不反向被 import。

各能力子包按 F05 目录组织。PR1 已实装 authentication / cryptography / protection；
authorization 与 audit_integrity 保持 PR2/PR3 固定契约，其中 authorization 仅有
PR1 过渡期 ``allow_all`` 占位。能力契约从各自子包获取，顶层只导出跨能力共享类型、
受控请求上下文入口与 Runtime。

存储加密统一使用 ``CryptographyProvider`` / ``KeyProvider``，顶层 ``security`` 配置由
``SecurityRuntimeProducer`` 接管。旧 ``SecurityProvider`` / ``SecurityProducer`` /
``SecurityContext`` / ``KeySource`` 保留一个发布周期的兼容导出，不再拥有独立实现或配置
命名空间。
"""

from .key_source import KeySource
from .request_context import internal_context, new_request_context
from .runtime import SecurityRuntime, SecurityRuntimeProducer
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
    "SecurityRuntimeProducer",
    "Surface",
    "internal_context",
    "new_request_context",
]
