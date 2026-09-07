# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""旧安全加密 API 的一个发布周期兼容导出。

新代码应从 :mod:`jiuwen_memory.common.security.cryptography` 导入。这里不再注册第二个
``security`` Producer，避免与 SecurityRuntime 的固定命名空间冲突；旧 Producer 名直接
指向新的 ``CryptographyProducer``。
"""

from __future__ import annotations

from dataclasses import dataclass

from jiuwen_memory.common.security.cryptography import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    CryptographyError,
    CryptographyProducer,
    CryptographyProvider,
    InvalidMagicError,
    KeyMismatchError,
)
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope

SecurityError = CryptographyError
EncryptionError = CryptographyError
SecurityProducer = CryptographyProducer
SecurityProvider = CryptographyProvider


@dataclass(frozen=True, init=False)
class SecurityContext(CryptoContext):
    """保留旧 ``(scope, purpose, metadata)`` 构造顺序的 ``CryptoContext``。"""

    def __init__(
        self,
        scope: Scope | None = None,
        purpose: str = "",
        metadata: dict[str, str] | None = None,
    ) -> None:
        object.__setattr__(self, "scope", scope if scope is not None else Scope())
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "object_id", "")
        object.__setattr__(self, "format_version", 1)
        object.__setattr__(self, "metadata", dict(metadata or {}))


__all__ = [
    "AuthenticationFailedError",
    "CorruptedCiphertextError",
    "EncryptionError",
    "InvalidMagicError",
    "KeyMismatchError",
    "SecurityContext",
    "SecurityError",
    "SecurityProducer",
    "SecurityProvider",
]
