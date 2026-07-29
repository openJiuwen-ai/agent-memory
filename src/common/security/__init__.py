"""安全横切接口：加密/解密等数据保护能力。"""

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
