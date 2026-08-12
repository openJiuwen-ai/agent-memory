"""SecurityProvider — 数据保护横切接口。

安全能力不是无状态模型插件，不继承 :class:`common.base.Plugin`，但仍使用
``Factory`` 提供注册式装配。调用方以字节为边界调用本接口：
写入持久化字节前加密，读取持久化字节后解密；
是否启用加密由具体实现与配置决定。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..errors import AgentMemoryError
from ..factory.factory import Factory
from ..type_def import Scope


@dataclass(frozen=True)
class SecurityContext:
    """一次安全处理的上下文。

    ``scope`` 用于表达租户/主体隔离边界，``purpose`` 用于区分调用场景
    （如 ``"memory_unit_content"``），``metadata`` 透传实现所需的非敏感标签。
    """

    scope: Scope = field(default_factory=Scope)
    purpose: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


class SecurityProducer(Factory):
    """SecurityProvider 的注册式工厂。

    各实现在 ``security_impl`` 下以 ``@SecurityProducer.register("<名>")`` 自注册。
    当前 ``security_impl`` 注册 ``local`` ENC1 AES-GCM 实现。
    """

    TOP_NAME = "security"


class SecurityError(AgentMemoryError):
    """所有安全横切处理异常的基类。"""


class EncryptionError(SecurityError):
    """加密或解密处理失败。"""


class InvalidMagicError(EncryptionError):
    """密文字节不符合当前 provider 期望的信封魔数。"""


class CorruptedCiphertextError(EncryptionError):
    """密文信封结构损坏、版本不支持或长度不完整。"""


class AuthenticationFailedError(EncryptionError):
    """认证加密 tag 校验失败，通常表示 AAD 不匹配或内容被篡改。"""


class KeyMismatchError(EncryptionError):
    """包裹的数据密钥无法用当前租户密钥解开。"""


class SecurityProvider(ABC):
    """字节级数据保护能力。"""

    @abstractmethod
    def encrypt(
        self,
        plaintext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        """加密明文字节。

        ``aad`` 是附加认证数据，具体实现可用于完整性保护但不写入密文。
        """

    @abstractmethod
    def decrypt(
        self,
        ciphertext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        """解密密文字节并校验完整性。"""

    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则由实现抛出异常。"""
        return None
