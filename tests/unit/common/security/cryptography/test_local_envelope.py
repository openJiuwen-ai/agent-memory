"""ENC1 本地信封：往返、AAD 绑定、无明文回退、KeyProvider 与 v1 只读兼容。"""

from __future__ import annotations

import os
import stat
import struct

import pytest

import common.security.cryptography.cryptography_impl
from common.errors import ValidationError
from common.security.cryptography import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    CryptographyProducer,
    InvalidMagicError,
    KeyMismatchError,
    KeyProviderProducer,
    KeyRef,
)
from common.security.cryptography.cryptography_impl.local_envelope import (
    ENVELOPE_MAGIC,
    ENVELOPE_VERSION,
    ENVELOPE_VERSION_V1,
    LOCAL_ALGORITHM_ID,
    NONCE_SIZE,
    LocalEnvelopeCryptographyProvider,
    LocalKeyProvider,
    _aes_encrypt,
    _key_aad_v1,
)
from common.security.types import CryptoContext
from common.type_def import Scope
from config import AssemblyContext

_KEY_HEX = "11" * 32

pytestmark = pytest.mark.unit


def _context(
    *,
    org: str = "acme",
    user: str = "alice",
    purpose: str = "memory_unit",
    object_id: str = "/memory/u1",
) -> CryptoContext:
    return CryptoContext(
        scope=Scope(org=org, user=user),
        purpose=purpose,
        object_id=object_id,
    )


def _provider_from_hex() -> LocalEnvelopeCryptographyProvider:
    return LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex=_KEY_HEX))


# -- 往返与信封格式 ---------------------------------------------------------- #


def test_encrypts_enc1_and_round_trips(tmp_path) -> None:
    key_file = tmp_path / "master.key"
    provider = LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_file=str(key_file)))
    context = _context()

    ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")
    second_ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")

    assert ciphertext.startswith(ENVELOPE_MAGIC)
    assert ciphertext != b"secret payload"
    assert ciphertext != second_ciphertext  # 每次新 data key + 新 nonce
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"secret payload"
    assert key_file.exists()
    if os.name != "nt":
        # NTFS 不映射 POSIX mode 位，os.open(..., 0o600) 在 Windows 上恒为 0o666。
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_writes_version_two_envelopes() -> None:
    """写出一律 v2：v1 只读兼容，不再产出（F05 §信封格式要求 key id 与 epoch）。"""
    ciphertext = _provider_from_hex().encrypt(b"payload", context=_context())
    assert ciphertext[len(ENVELOPE_MAGIC)] == ENVELOPE_VERSION


def test_envelope_carries_key_id_and_epoch() -> None:
    """信封须自带 key ref，否则轮换后无从判断该用哪代密钥解。"""
    key_provider = LocalKeyProvider(key_hex=_KEY_HEX, key_epoch=3)
    provider = LocalEnvelopeCryptographyProvider(key_provider)
    ciphertext = provider.encrypt(b"payload", context=_context())

    ref = key_provider.active_key()
    assert ref.epoch == 3
    assert ref.key_id.encode("utf-8") in ciphertext
    assert provider.decrypt(ciphertext, context=_context()) == b"payload"


# -- 无明文回退（F05 §明文策略）--------------------------------------------- #


def test_plaintext_is_always_rejected() -> None:
    """不是合法信封就拒绝读取——不存在 ``allow_plaintext`` 降级开关。

    有降级开关时，拥有底层存储写权限的攻击者可用任意明文替换密文，
    绕过 AES-GCM tag 与 AAD。
    """
    provider = _provider_from_hex()
    with pytest.raises(InvalidMagicError):
        provider.decrypt(b"legacy plaintext", context=_context())


def test_provider_takes_no_plaintext_switch() -> None:
    """构造签名里不留 ``allow_plaintext``：降级只能靠换存储适配器表达。"""
    with pytest.raises(TypeError):
        LocalEnvelopeCryptographyProvider(  # type: ignore[call-arg]
            LocalKeyProvider(key_hex=_KEY_HEX),
            allow_plaintext=True,
        )


# -- AAD 绑定 ---------------------------------------------------------------- #


def test_rejects_aad_or_actor_mismatch() -> None:
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(user="alice"), aad=b"kv:a")

    with pytest.raises(AuthenticationFailedError):
        provider.decrypt(ciphertext, context=_context(user="alice"), aad=b"kv:b")
    with pytest.raises(AuthenticationFailedError):
        provider.decrypt(ciphertext, context=_context(user="bob"), aad=b"kv:a")


def test_rejects_object_id_mismatch() -> None:
    """对象标识进 AAD：否则同租户同用途的密文可在两个 key 之间原样搬运。"""
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(object_id="/memory/u1"))

    with pytest.raises(AuthenticationFailedError):
        provider.decrypt(ciphertext, context=_context(object_id="/memory/u2"))


def test_rejects_purpose_mismatch() -> None:
    """用途隔离（F05 §密钥隔离）：包裹密钥按 purpose 派生，换用途就解不开。"""
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(purpose="memory_unit"))

    with pytest.raises(KeyMismatchError):
        provider.decrypt(ciphertext, context=_context(purpose="raw_message"))


def test_rejects_org_key_mismatch() -> None:
    provider = _provider_from_hex()
    ciphertext = provider.encrypt(b"secret payload", context=_context(org="acme"), aad=b"kv:a")

    with pytest.raises(KeyMismatchError):
        provider.decrypt(ciphertext, context=_context(org="other"), aad=b"kv:a")


def test_key_ref_in_header_cannot_be_swapped() -> None:
    """key id/epoch 也进 AAD：只写进头部而不参与认证的字段是可篡改的。"""
    provider = _provider_from_hex()
    ciphertext = bytearray(provider.encrypt(b"secret payload", context=_context()))

    # header 尾部 4 字节是 key_epoch（!4sBBHHHBI）。改掉它而不动其余任何字节。
    epoch_offset = struct.calcsize("!4sBBHHHB")
    ciphertext[epoch_offset : epoch_offset + 4] = (9).to_bytes(4, "big")

    with pytest.raises(KeyMismatchError):
        provider.decrypt(bytes(ciphertext), context=_context())


def test_rejects_corrupted_envelope() -> None:
    provider = _provider_from_hex()

    with pytest.raises(CorruptedCiphertextError):
        provider.decrypt(ENVELOPE_MAGIC, context=_context())


def test_rejects_unknown_envelope_version() -> None:
    """未来版本的信封不能被当前实现「尽力而为」地解——不认识就拒绝。"""
    provider = _provider_from_hex()
    ciphertext = bytearray(provider.encrypt(b"payload", context=_context()))
    ciphertext[len(ENVELOPE_MAGIC)] = 0x7F

    with pytest.raises(CorruptedCiphertextError):
        provider.decrypt(bytes(ciphertext), context=_context())


# -- KeyProvider 契约 -------------------------------------------------------- #


def test_wrap_unwrap_round_trip() -> None:
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    data_key = b"\x02" * 32

    wrapped = provider.wrap(data_key, purpose="memory_unit", org="acme")
    assert wrapped.ref == provider.active_key()
    assert provider.unwrap(wrapped, purpose="memory_unit", org="acme") == data_key


def test_unwrap_rejects_other_key_generation() -> None:
    """单代实现不拿活动密钥试解另一代：试成功等于 epoch 绑定失效。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    wrapped = provider.wrap(b"\x02" * 32, purpose="memory_unit", org="acme")
    forged = type(wrapped)(
        ciphertext=wrapped.ciphertext,
        nonce=wrapped.nonce,
        ref=KeyRef(key_id=wrapped.ref.key_id, epoch=wrapped.ref.epoch + 1),
    )

    with pytest.raises(KeyMismatchError):
        provider.unwrap(forged, purpose="memory_unit", org="acme")


def test_key_id_does_not_leak_root_key() -> None:
    """key id 明文落盘：必须是不可逆派生，不能是根密钥本身或其直接编码。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    key_id = provider.active_key().key_id
    assert key_id
    assert _KEY_HEX not in key_id
    assert bytes.fromhex(_KEY_HEX).hex() not in key_id


def test_key_id_is_stable_across_instances() -> None:
    """同一根密钥必须给出同一 key id，否则重启后旧密文全部无法匹配。"""
    first = LocalKeyProvider(key_hex=_KEY_HEX).active_key()
    second = LocalKeyProvider(key_hex=_KEY_HEX).active_key()
    assert first == second


def test_different_roots_give_different_key_ids() -> None:
    assert (
        LocalKeyProvider(key_hex=_KEY_HEX).active_key().key_id
        != LocalKeyProvider(key_hex="22" * 32).active_key().key_id
    )


def test_epoch_must_be_positive() -> None:
    """epoch 0 会与 v1 信封「未声明 epoch」的哨兵值撞上。"""
    with pytest.raises(ValidationError):
        LocalKeyProvider(key_hex=_KEY_HEX, key_epoch=0)


def test_wrap_rejects_wrong_data_key_length() -> None:
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    with pytest.raises(ValidationError):
        provider.wrap(b"short", purpose="memory_unit", org="acme")


def test_missing_key_file_is_not_silently_created() -> None:
    """``create_key_file=False`` 时缺密钥必须拒绝，不能凭空造一把新的。

    静默新建等于把「密钥丢了」变成「旧数据全部解不开且无人察觉」。
    """
    provider = LocalKeyProvider(
        key_file="/nonexistent/agent-memory/master.key",
        key_env="",
        create_key_file=False,
    )
    with pytest.raises(Exception) as exc:
        provider.health()
    assert not isinstance(exc.value, KeyMismatchError)


# -- v1 只读兼容 ------------------------------------------------------------- #


def _build_v1_envelope(key_provider: LocalKeyProvider, plaintext: bytes, *, org: str) -> bytes:
    """按 v1 布局手工构造一个信封：模拟迁移前已落盘的密文。"""
    data_key = b"\x03" * 32
    key_nonce = b"\x04" * NONCE_SIZE
    data_nonce = b"\x05" * NONCE_SIZE
    wrapping_key = key_provider._derive_org_key_v1(org)  # noqa: SLF001 - 复刻旧派生
    wrapped = _aes_encrypt(wrapping_key, key_nonce, data_key, _key_aad_v1(org))
    context = CryptoContext(scope=Scope(org=org, user="alice"), purpose="memory_unit")
    from common.security.cryptography.cryptography_impl.local_envelope import _content_aad_v1

    content = _aes_encrypt(data_key, data_nonce, plaintext, _content_aad_v1(context, b""))
    header = struct.pack(
        "!4sBBHHH",
        ENVELOPE_MAGIC,
        ENVELOPE_VERSION_V1,
        LOCAL_ALGORITHM_ID,
        len(wrapped),
        len(key_nonce),
        len(data_nonce),
    )
    return header + wrapped + key_nonce + data_nonce + content


def test_v1_envelope_still_readable() -> None:
    """迁移前落盘的密文不能因为格式升级就读不出来。"""
    key_provider = LocalKeyProvider(key_hex=_KEY_HEX)
    provider = LocalEnvelopeCryptographyProvider(key_provider)
    legacy = _build_v1_envelope(key_provider, b"legacy payload", org="acme")

    context = CryptoContext(scope=Scope(org="acme", user="alice"), purpose="memory_unit")
    assert provider.decrypt(legacy, context=context) == b"legacy payload"


def test_v1_envelope_still_enforces_tenant_isolation() -> None:
    """只读兼容不等于放宽校验：跨 org 读旧密文照样拒绝。"""
    key_provider = LocalKeyProvider(key_hex=_KEY_HEX)
    provider = LocalEnvelopeCryptographyProvider(key_provider)
    legacy = _build_v1_envelope(key_provider, b"legacy payload", org="acme")

    context = CryptoContext(scope=Scope(org="other", user="alice"), purpose="memory_unit")
    with pytest.raises(KeyMismatchError):
        provider.decrypt(legacy, context=context)


# -- 装配 -------------------------------------------------------------------- #


def test_producer_builds_local_provider_from_config(tmp_path) -> None:
    assert (
        common.security.cryptography.cryptography_impl.CryptographyProducer is CryptographyProducer
    )
    key_file = tmp_path / "configured.key"
    ctx = AssemblyContext.from_dict(
        {
            "cryptography": {
                "default": {
                    "target": "local",
                    "params": {"key_provider": {"target": "local"}},
                }
            },
            "key_provider": {
                "default": {
                    "target": "local",
                    "params": {"key_file": str(key_file)},
                }
            },
        }
    )

    provider = CryptographyProducer.build(
        "local",
        {"key_provider": {"target": "local", "params": {"key_file": str(key_file)}}},
        ctx,
    )
    context = _context()
    ciphertext = provider.encrypt(b"value", context=context, aad=b"kv:a")

    assert isinstance(provider, LocalEnvelopeCryptographyProvider)
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"value"
    assert key_file.exists()


def test_key_provider_producer_is_separately_addressable(tmp_path) -> None:
    """KeyProvider 是独立 Producer：换 KMS/Vault 不必改加密实现（F05 §Producer 清单）。"""
    key_file = tmp_path / "named.key"
    ctx = AssemblyContext.from_dict(
        {
            "key_provider": {
                "default": {
                    "target": "local",
                    "params": {"key_file": str(key_file)},
                }
            }
        }
    )

    key_provider = KeyProviderProducer.build_named("default", ctx)
    assert isinstance(key_provider, LocalKeyProvider)
    key_provider.health()
    assert key_file.exists()
