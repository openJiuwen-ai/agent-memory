"""jiuwen_memory.common.security.cryptography: 契约层--抽象方法、错误分类与密钥值对象。

接口先行版：``cryptography_impl`` 未合入，只固定
:class:`~common.security.cryptography.base.CryptographyProvider` /
:class:`~common.security.cryptography.key_provider.KeyProvider` 的契约形状。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import AgentMemoryError
from jiuwen_memory.common.security.cryptography.base import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    CryptographyError,
    CryptographyProducer,
    CryptographyProvider,
    InvalidMagicError,
    KeyMismatchError,
)
from jiuwen_memory.common.security.cryptography.key_provider import (
    KeyProvider,
    KeyProviderProducer,
    KeyRef,
    WrappedKey,
)
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit


def test_error_taxonomy_is_single_rooted() -> None:
    """五个密码学异常同根于 CryptographyError，并归属 AgentMemoryError 语义域。"""
    for exc in (
        InvalidMagicError,
        CorruptedCiphertextError,
        AuthenticationFailedError,
        KeyMismatchError,
    ):
        assert issubclass(exc, CryptographyError)
    assert issubclass(CryptographyError, AgentMemoryError)


def test_provider_cannot_be_partially_implemented() -> None:
    class Incomplete(CryptographyProvider):
        def encrypt(self, plaintext, *, context, aad=b""):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_key_provider_cannot_be_partially_implemented() -> None:
    class Incomplete(KeyProvider):
        def active_key(self):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_producers_declare_top_names() -> None:
    assert CryptographyProducer.TOP_NAME == "cryptography"
    assert KeyProviderProducer.TOP_NAME == "key_provider"


def test_keyref_is_frozen() -> None:
    """KeyRef 明文进信封头：轮换语义（id + epoch）不允许事后篡改。"""
    ref = KeyRef(key_id="kp-1", epoch=3)
    with pytest.raises(AttributeError):
        ref.epoch = 4  # type: ignore[misc]


def test_crypto_context_requires_scope_and_purpose() -> None:
    """AAD 的租户与用途绑定字段无默认：构造时必须显式给出。"""
    with pytest.raises(TypeError):
        CryptoContext()  # type: ignore[call-arg]
    ctx = CryptoContext(scope=Scope(org="acme"), purpose="kv_value")
    assert ctx.format_version == 1


def test_wrapped_key_is_plain_value_object() -> None:
    wrapped = WrappedKey(ciphertext=b"c", nonce=b"n", ref=KeyRef(key_id="kp-1"))
    assert wrapped.ref.epoch == 1
