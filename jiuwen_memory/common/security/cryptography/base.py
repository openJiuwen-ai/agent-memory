"""CryptographyProvider — bytes 边界的加解密契约（F05 §Cryptography）。

安全能力不是无状态模型插件，不继承 :class:`common.base.Plugin`，但仍使用
``Factory`` 提供注册式装配。调用方以**字节**为边界调用本接口：写入持久化字节前
加密，读取持久化字节后解密。

契约边界（F05 §CryptographyProvider）：本接口不接收 MemoryUnit、KV key 或文件
路径等业务对象，也**不决定数据是否应该加密**——那是存储适配器的选择，由上层配
不同适配器表达，不是本接口内部的开关。

密钥一律经 :class:`~common.security.cryptography.key_provider.KeyProvider` 取得，
实现不得自己读环境变量或配置文件里的根密钥。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.errors import AgentMemoryError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import CryptoContext


class CryptographyProducer(Factory):
    """CryptographyProvider 的注册式工厂（与契约同处接口层）。

    各实现在 ``cryptography_impl`` 下以 ``@CryptographyProducer.register("<名>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "cryptography"


class CryptographyError(AgentMemoryError):
    """加密或解密处理失败。"""


class InvalidMagicError(CryptographyError):
    """密文字节不符合当前 provider 期望的信封魔数。"""


class CorruptedCiphertextError(CryptographyError):
    """密文信封结构损坏、版本不支持或长度不完整。"""


class AuthenticationFailedError(CryptographyError):
    """认证加密 tag 校验失败，通常表示 AAD 不匹配或内容被篡改。"""


class KeyMismatchError(CryptographyError):
    """包裹的数据密钥无法用对应的密钥材料解开。"""


class CryptographyProvider(ABC):
    """字节级数据保护能力。"""

    @abstractmethod
    def encrypt(self, plaintext: bytes, *, context: CryptoContext, aad: bytes = b"") -> bytes:
        """加密明文字节，返回自描述信封。

        ``context`` **必填**：AAD 必须绑定规范化 Scope、存储用途、对象标识和格式
        版本（F05 §信封格式），缺了它密文就能被复制到其他租户、对象或存储位置后
        照样解开。旧接口允许 ``context=None`` 并静默用空 Scope，是这条不变量的缺口。

        ``aad`` 是调用方追加的附加认证数据，参与完整性保护但不写入密文。
        """

    @abstractmethod
    def decrypt(self, ciphertext: bytes, *, context: CryptoContext, aad: bytes = b"") -> bytes:
        """解密密文字节并校验完整性。

        **不提供明文回退**（F05 §明文策略）：入参不是合法信封时抛
        :class:`InvalidMagicError`，解密失败时抛
        :class:`AuthenticationFailedError`，两种情况都不得返回原始 bytes。是否允许
        未加密存储由上层选用不同存储适配器表达。
        """

    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则由实现抛出异常。"""
        return None
