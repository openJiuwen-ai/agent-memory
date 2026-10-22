"""ENC1 本地信封：往返、AAD 绑定、无明文回退、KeyProvider 与 v1 只读兼容。"""

from __future__ import annotations

import os
import stat
import struct
import subprocess
from pathlib import Path

import pytest

import jiuwen_memory.common.security.cryptography.cryptography_impl
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.security.cryptography import (
    AuthenticationFailedError,
    CorruptedCiphertextError,
    CryptographyProducer,
    InvalidMagicError,
    KeyMismatchError,
    KeyProviderProducer,
    KeyRef,
)
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    ENVELOPE_MAGIC,
    ENVELOPE_VERSION,
    LocalEnvelopeCryptographyProvider,
    LocalKeyProvider,
)
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import AssemblyContext

# 白盒并发测试需要替换受保护 helper，以精确撑开轮换竞态窗口。
# pylint: disable=protected-access

_KEY_HEX = "11" * 32
_LEGACY_V1_ENVELOPE = bytes.fromhex(
    "454e433101010030000c000cb76369ad7e6142f97d74bae917899876cd25dcc762"
    "c74ecb09ed4f6f75418e2c7c997b7190a303fa251f0fdd4dcac238040404040404"
    "0404040404040505050505050505050505057a47b805d1a53731cb77a66cb2835c"
    "c4a1131152146fc6b52e2a3c95c6cb"
)

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
    provider = LocalEnvelopeCryptographyProvider(
        LocalKeyProvider(key_file=str(key_file), create_key_file=True)
    )
    context = _context()

    ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")
    second_ciphertext = provider.encrypt(b"secret payload", context=context, aad=b"kv:a")

    assert ciphertext.startswith(ENVELOPE_MAGIC)
    assert ciphertext != b"secret payload"
    assert ciphertext != second_ciphertext  # 每次新 data key + 新 nonce
    assert provider.decrypt(ciphertext, context=context, aad=b"kv:a") == b"secret payload"
    assert key_file.exists()
    if os.name == "nt":
        _assert_windows_private(key_file)
    else:
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def _assert_windows_private(key_file) -> None:
    """Windows 的 0600 语义是 DACL：继承关闭、仅剩当前账号一条显式授权（AUTH-ENC-04）。"""
    icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    result = subprocess.run(
        [str(icacls), str(key_file)],
        capture_output=True,
        text=True,
        encoding="locale",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    aces = [line.strip() for line in result.stdout.splitlines() if "(" in line]
    assert len(aces) == 1, result.stdout  # 只剩我们授的那一条，无其他主体可访问
    assert "(I)" not in aces[0], result.stdout  # 继承已禁用：系统默认 ACE 不再挂上
    assert "(F)" in aces[0], result.stdout  # 完整权限；若退化读权限说明收权失败


def _grant_everyone_read(key_file) -> None:
    """预置一条显式 ``Everyone:(R)`` 读权限（反向场景的种子）。

    用 SID ``*S-1-1-0`` 而非 ``Everyone`` 字样，避免本地化账号名差异。
    """
    icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    result = subprocess.run(
        [str(icacls), str(key_file), "/grant", "*S-1-1-0:(R)"],
        capture_output=True,
        text=True,
        encoding="locale",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL 收权仅在 NTFS 上有意义")
def test_windows_restrict_strips_preexisting_world_ace(tmp_path) -> None:
    """反向场景：文件已带显式 Everyone 读权限时，收权必须把这条清掉（PR1-SEC-03）。

    只跑 ``/inheritance:r /grant`` 会留下这条显式 ACE（reviewer 论断），因此实现
    必须用替换型 DACL 或显式清理。收权后应只剩当前账号一条 ``(F)``，即使文件在
    装配前被预置了 Everyone/Users 读权限。
    """
    key_file = tmp_path / "master.key"
    key_file.write_text(f"{_KEY_HEX}\n", encoding="ascii")
    _grant_everyone_read(key_file)

    provider = LocalKeyProvider(key_file=str(key_file), create_key_file=True)
    provider.health()  # 触发 _load_or_create_root_key -> 文件已存在 -> _restrict_file_mode

    # 预置的 Everyone 显式 ACE 必须被清除，只留当前账号一条 (F) 且继承关闭。
    _assert_windows_private(key_file)


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
    ciphertext[epoch_offset:epoch_offset + 4] = (9).to_bytes(4, "big")  # fmt: skip

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
    """未保留材料的 epoch 不拿活动密钥试解：试成功等于 epoch 绑定失效。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    wrapped = provider.wrap(b"\x02" * 32, purpose="memory_unit", org="acme")
    forged = type(wrapped)(
        ciphertext=wrapped.ciphertext,
        nonce=wrapped.nonce,
        ref=KeyRef(key_id=wrapped.ref.key_id, epoch=wrapped.ref.epoch + 1),
    )

    with pytest.raises(KeyMismatchError):
        provider.unwrap(forged, purpose="memory_unit", org="acme")


def test_rotate_advances_epoch_and_keeps_old_epoch_readable() -> None:
    """rotate 推进 epoch，且旧 epoch 信封仍可解（F05 §KeyProvider 轮换契约）。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    data_key = b"\x02" * 32
    wrapped = provider.wrap(data_key, purpose="memory_unit", org="acme")
    before = provider.active_key()

    after = provider.rotate()

    assert after.epoch > before.epoch
    # 旧 epoch 信封仍可解（rotate 保留了旧代根密钥）
    assert provider.unwrap(wrapped, purpose="memory_unit", org="acme") == data_key
    # 新 epoch 写入用新 ref，且可解
    wrapped_new = provider.wrap(data_key, purpose="memory_unit", org="acme")
    assert wrapped_new.ref.epoch == after.epoch
    assert provider.unwrap(wrapped_new, purpose="memory_unit", org="acme") == data_key


def test_rotate_changes_key_id() -> None:
    """新 epoch 用新随机根密钥，key_id 随之改变。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    before = provider.active_key()
    after = provider.rotate()
    assert after.key_id != before.key_id


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


def test_wrap_concurrent_with_rotate_stays_decryptable() -> None:
    """wrap 与 rotate 并发时，信封 epoch 必须与实际用的根密钥同代（AUTH-ENC-02）。

    无锁交错会写出「信封标 epoch N、密钥却是 epoch N+1」的信封--unwrap 按信封
    epoch 取旧代根密钥派生，AEAD 校验必然失败，已持久化的密文永久不可读。
    """
    import threading

    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    data_key = b"k" * 32
    wrapped: list = []
    errors: list = []

    def _wrap_many() -> None:
        try:
            for _ in range(200):
                wrapped.append(provider.wrap(data_key, purpose="memory_unit", org="acme"))
        except Exception as exc:  # pragma: no cover - 竞争失败即测试失败
            errors.append(exc)

    def _rotate_many() -> None:
        for _ in range(20):
            provider.rotate()

    threads = [threading.Thread(target=_wrap_many) for _ in range(4)]
    threads += [threading.Thread(target=_rotate_many) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # 每个信封都必须可解：按其声明的 epoch 找到同代根密钥材料。
    for w in wrapped:
        assert provider.unwrap(w, purpose="memory_unit", org="acme") == data_key


def test_double_rotate_retains_all_generations() -> None:
    """并发两次 rotate 不丢中间代根密钥材料（AUTH-ENC-02 第二场景）。"""
    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    w1 = provider.wrap(b"k" * 32, purpose="memory_unit", org="acme")
    provider.rotate()
    w2 = provider.wrap(b"k" * 32, purpose="memory_unit", org="acme")
    provider.rotate()
    w3 = provider.wrap(b"k" * 32, purpose="memory_unit", org="acme")

    for w, expected in ((w1, 1), (w2, 2), (w3, 3)):
        assert w.ref.epoch == expected
        assert provider.unwrap(w, purpose="memory_unit", org="acme") == b"k" * 32


def test_active_key_is_an_atomic_snapshot_across_rotation(monkeypatch) -> None:
    """公共 ``active_key()`` 返回的 key_id 与 epoch 必须同代（PR1-SEC-02）。

    ``wrap``/``rotate`` 各自持锁，但 ``active_key()`` 自身锁外读两个字段时，
    「取到旧 key_id 后、读 epoch 前」发生 rotate，会返回一个从未存在过的
    ``KeyRef``（旧 key_id + 新 epoch）。调用方拿它进 AAD 写出的信封永远解不开。

    用时序窗口把那个间隙撑开，而不是靠随机竞争：探针在 ``active_key()`` 取到
    key_id 之后放行 rotate 线程并停留一段时间。加了锁，rotate 阻塞在锁上，快照
    仍是第 1 代；没加锁，rotate 在窗口内跑完，返回值就是撕裂的组合。两种情形都
    只断言**结果自洽**，不断言具体是哪一代。
    """
    import threading
    import time

    provider = LocalKeyProvider(key_hex=_KEY_HEX)
    epoch_one_key_id = provider.active_key().key_id

    probe_entered = threading.Event()
    original = LocalKeyProvider._active_key_id
    fired = False

    def _slow_active_key_id(self):
        nonlocal fired
        key_id = original(self)
        if not fired:  # rotate() 内部也会走这里，只对被测的那次调用制造时序
            fired = True
            probe_entered.set()
            time.sleep(0.2)  # 撑开「已取 key_id、未读 epoch」的窗口
        return key_id

    monkeypatch.setattr(LocalKeyProvider, "_active_key_id", _slow_active_key_id)

    def _rotate_once() -> None:
        probe_entered.wait(timeout=5)
        provider.rotate()

    rotator = threading.Thread(target=_rotate_once)
    rotator.start()
    ref = provider.active_key()
    rotator.join(timeout=10)
    assert not rotator.is_alive()

    # epoch 与 key_id 必须出自同一代：第 1 代的 epoch 只能配第 1 代的 key_id。
    if ref.epoch == 1:
        assert ref.key_id == epoch_one_key_id
    else:
        assert ref.key_id != epoch_one_key_id, f"撕裂快照：epoch={ref.epoch} 却带着第 1 代的 key_id"


def test_default_is_fail_closed_without_explicit_key_source(tmp_path) -> None:
    """不传 create_key_file（默认）时，缺密钥源必须在装配期失败且**不创建文件**。

    F04 §create_key_file 默认值：默认静默生成等于让容器 HOME / 挂载卷漂移
    悄悄换掉根密钥，直到读历史密文才暴露不可恢复的 KeyMismatchError。
    """
    key_file = tmp_path / "absent.key"
    provider = LocalKeyProvider(key_file=str(key_file), key_env="")
    with pytest.raises(BackendError):
        provider.health()
    assert not key_file.exists()


def test_producer_default_is_fail_closed(tmp_path) -> None:
    """builder 层同口径：params 未显式 create_key_file 时不生成 key file。"""
    key_file = tmp_path / "absent.key"
    ctx = AssemblyContext.from_dict(
        {
            "key_provider": {
                "fail_closed_test": {
                    "target": "local",
                    "params": {"key_file": str(key_file)},
                }
            }
        }
    )
    key_provider = KeyProviderProducer.build_named("fail_closed_test", ctx)
    with pytest.raises(BackendError):
        key_provider.health()
    assert not key_file.exists()


# -- v1 只读兼容 ------------------------------------------------------------- #


def test_v1_envelope_still_readable() -> None:
    """迁移前落盘的密文不能因为格式升级就读不出来。"""
    key_provider = LocalKeyProvider(key_hex=_KEY_HEX)
    provider = LocalEnvelopeCryptographyProvider(key_provider)

    context = CryptoContext(scope=Scope(org="acme", user="alice"), purpose="memory_unit")
    assert provider.decrypt(_LEGACY_V1_ENVELOPE, context=context) == b"legacy payload"


def test_v1_envelope_still_enforces_tenant_isolation() -> None:
    """只读兼容不等于放宽校验：跨 org 读旧密文照样拒绝。"""
    key_provider = LocalKeyProvider(key_hex=_KEY_HEX)
    provider = LocalEnvelopeCryptographyProvider(key_provider)

    context = CryptoContext(scope=Scope(org="other", user="alice"), purpose="memory_unit")
    with pytest.raises(KeyMismatchError):
        provider.decrypt(_LEGACY_V1_ENVELOPE, context=context)


# -- 装配 -------------------------------------------------------------------- #


def test_producer_builds_local_provider_from_config(tmp_path) -> None:
    assert (
        jiuwen_memory.common.security.cryptography.cryptography_impl.CryptographyProducer
        is CryptographyProducer
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
                    "params": {"key_file": str(key_file), "create_key_file": True},
                }
            },
        }
    )

    provider = CryptographyProducer.build(
        "local",
        {
            "key_provider": {
                "target": "local",
                "params": {"key_file": str(key_file), "create_key_file": True},
            }
        },
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
                    "params": {"key_file": str(key_file), "create_key_file": True},
                }
            }
        }
    )

    key_provider = KeyProviderProducer.build_named("default", ctx)
    assert isinstance(key_provider, LocalKeyProvider)
    key_provider.health()
    assert key_file.exists()
