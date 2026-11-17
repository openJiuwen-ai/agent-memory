"""认证能力：契约、主体凭据注册表与内置实现（安全能力 F04 §Authentication）。"""

from .base import Authenticator, AuthProducer
from .key_store import (
    KeyStoreProducer,
    PrincipalKeyStore,
    PrincipalRegistrationCapability,
    fingerprint,
    generate_api_key,
    key_prefix,
)

__all__ = [
    "AuthProducer",
    "Authenticator",
    "KeyStoreProducer",
    "PrincipalKeyStore",
    "PrincipalRegistrationCapability",
    "fingerprint",
    "generate_api_key",
    "key_prefix",
]
