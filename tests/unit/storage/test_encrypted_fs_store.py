"""EncryptedFSStore：装饰器契约 + 它的装配。

与 ``test_encrypted_kv_store.py`` 同构（同一个假 provider 套路），断言的核心是
两句：**上层看不出区别，内层看到的全是密文**；以及**装饰器交给 provider 的
``SecurityContext`` / AAD 到底绑了什么**——后者是加密能否抵抗「密文搬家」的唯一
依据，只测 roundtrip 的话完全不加密也是绿的。

明文兼容（迁移期读加密上线前的老数据）由 provider 的 ``allow_plaintext`` 控制，
本装饰器不重复提供同语义开关，故这里只验「provider 允许则读得出」。
"""

from __future__ import annotations

import io
import json

import pytest

from common.errors import BackendError, NotFoundError, ValidationError
from common.factory.factory import Factory
from common.security import SecurityContext, SecurityProducer, SecurityProvider
from common.type_def import Scope
from config.context import AssemblyContext
from storage.fs import FsProducer
from storage.fs_impl.encrypted_fs_store import EncryptedFSStore
from storage.fs_impl.local_fs import LocalFSStore

pytestmark = pytest.mark.unit

_PREFIX = b"fake1:"
_ALICE = Scope(org="acme", space="product", user="alice")


class _FakeSecurity(SecurityProvider):
    """把 AAD 编进密文的假 provider：AAD 对不上就解不开，与真信封同性质。"""

    def __init__(self, *, allow_plaintext: bool = True) -> None:
        self.allow_plaintext = allow_plaintext
        self.fail_decrypt = False
        self.encrypt_calls: list[tuple[SecurityContext | None, bytes, bytes]] = []
        self.decrypt_calls: list[tuple[SecurityContext | None, bytes, bytes]] = []

    def encrypt(
        self,
        plaintext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        self.encrypt_calls.append((context, aad, plaintext))
        return _PREFIX + len(aad).to_bytes(4, "big") + aad + plaintext[::-1]

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        context: SecurityContext | None = None,
        aad: bytes = b"",
    ) -> bytes:
        self.decrypt_calls.append((context, aad, ciphertext))
        if self.fail_decrypt:
            raise RuntimeError("decrypt failed")
        if not ciphertext.startswith(_PREFIX):
            if self.allow_plaintext:
                return ciphertext
            raise RuntimeError("missing encrypted envelope")
        offset = len(_PREFIX)
        aad_len = int.from_bytes(ciphertext[offset : offset + 4], "big")
        offset += 4
        embedded_aad = ciphertext[offset : offset + aad_len]
        if embedded_aad != aad:
            raise RuntimeError("aad mismatch")
        return ciphertext[offset + aad_len :][::-1]


@SecurityProducer.register("fake_encrypted_fs")
def _build_fake_security(config):
    return _FakeSecurity(allow_plaintext=bool(config.get("allow_plaintext", True)))


def _fs(
    tmp_path, security: _FakeSecurity | None = None
) -> tuple[EncryptedFSStore, LocalFSStore, _FakeSecurity]:
    inner = LocalFSStore(root=str(tmp_path / "files"))
    fake = security or _FakeSecurity()
    return EncryptedFSStore(inner, fake, max_plaintext_bytes=64 * 1024 * 1024), inner, fake


def _aad_payload(aad: bytes) -> dict:
    return json.loads(aad.decode("utf-8"))


def test_encrypted_fs_store_encrypts_content_and_decrypts_get(tmp_path) -> None:
    fs, inner, security = _fs(tmp_path)

    ref = fs.insert(_ALICE, "a/b/x.bin", io.BytesIO(b"secret payload"))

    with inner.get(_ALICE, ref) as fh:
        stored = fh.read()
    assert stored.startswith(_PREFIX)
    assert b"secret payload" not in stored
    with fs.get(_ALICE, ref) as fh:
        assert fh.read() == b"secret payload"

    context, aad, plaintext = security.encrypt_calls[0]
    assert plaintext == b"secret payload"
    assert context is not None
    assert context.scope == _ALICE
    assert context.purpose == "fs_object"
    assert context.metadata["ref"] == "a/b/x.bin"


def test_encrypted_fs_store_aad_binds_all_five_scope_dimensions(tmp_path) -> None:
    """AAD 少绑一维，那一维就能搬密文。

    存储层的 scope 隔离是访问控制、可以被绕过（直接写底层、备份恢复串了）；
    AAD 是密码学的，绕不过——前提是它真的绑满了。``space`` 是 ``Scope`` 五维化时
    新加的维度，漏了它同 org 下的两个 space 就能互读。
    """
    fs, _, security = _fs(tmp_path)
    scope = Scope(org="acme", space="product", user="alice", agent="bot", session="s1")

    fs.insert(scope, "x.bin", io.BytesIO(b"v"))

    payload = _aad_payload(security.encrypt_calls[0][1])
    assert payload["scope"] == {
        "org": "acme",
        "space": "product",
        "user": "alice",
        "agent": "bot",
        "session": "s1",
    }
    assert payload["ref"] == "x.bin"
    assert payload["purpose"] == "fs_object"


def test_encrypted_fs_store_cross_scope_ciphertext_move_fails(tmp_path) -> None:
    """把 alice 的密文直接塞进 bob 的槽位——绕过存储层隔离后仍然读不出来。"""
    fs, inner, _ = _fs(tmp_path)
    bob = Scope(org="acme", space="product", user="bob")

    fs.insert(_ALICE, "x.bin", io.BytesIO(b"alice-data"))
    with inner.get(_ALICE, "x.bin") as fh:
        inner.insert(bob, "x.bin", io.BytesIO(fh.read()))

    with pytest.raises(BackendError):  # 不是 NotFoundError——是「解不开」
        fs.get(bob, "x.bin")


def test_encrypted_fs_store_update_also_encrypts(tmp_path) -> None:
    """update 是第二条写路径——只在 insert 上加密是个真实会犯的错。"""
    fs, inner, _ = _fs(tmp_path)

    fs.insert(_ALICE, "x.bin", io.BytesIO(b"old"))
    fs.update(_ALICE, "x.bin", io.BytesIO(b"newer-secret"))

    with inner.get(_ALICE, "x.bin") as fh:
        stored = fh.read()
    assert stored.startswith(_PREFIX)
    assert b"newer-secret" not in stored
    with fs.get(_ALICE, "x.bin") as fh:
        assert fh.read() == b"newer-secret"


def test_encrypted_fs_store_roundtrips_empty_file(tmp_path) -> None:
    fs, _, _ = _fs(tmp_path)

    fs.insert(_ALICE, "empty.bin", io.BytesIO(b""))

    with fs.get(_ALICE, "empty.bin") as fh:
        assert fh.read() == b""


def test_encrypted_fs_store_stat_reports_ciphertext_size(tmp_path) -> None:
    """已知代价，显式钉住：size 是密文长度，比明文长。改了要有人主动来改这条。"""
    fs, _, _ = _fs(tmp_path)

    fs.insert(_ALICE, "x.bin", io.BytesIO(b"12345"))

    assert fs.stat(_ALICE, "x.bin").size > 5


def test_encrypted_fs_store_passes_through_missing_and_delete(tmp_path) -> None:
    fs, _, security = _fs(tmp_path)

    with pytest.raises(NotFoundError):
        fs.get(_ALICE, "nope")
    fs.insert(_ALICE, "x.bin", io.BytesIO(b"a"))
    fs.delete(_ALICE, "x.bin")
    fs.delete(_ALICE, "x.bin")  # 幂等
    with pytest.raises(NotFoundError):
        fs.get(_ALICE, "x.bin")
    assert not security.decrypt_calls  # delete 不经加解密


def test_encrypted_fs_store_supports_plaintext_compatibility_via_provider(tmp_path) -> None:
    """迁移期：加密层上线前写进去的老数据必须还能读，否则上线即全量不可用。"""
    fs, inner, _ = _fs(tmp_path, _FakeSecurity(allow_plaintext=True))

    inner.insert(_ALICE, "legacy.bin", io.BytesIO(b"legacy plaintext"))

    with fs.get(_ALICE, "legacy.bin") as fh:
        assert fh.read() == b"legacy plaintext"


def test_encrypted_fs_store_write_always_encrypts_even_when_plaintext_allowed(tmp_path) -> None:
    """兼容开关只影响**读**。若它顺带放松了写，迁移期写进去的数据就永远是明文。"""
    fs, inner, _ = _fs(tmp_path, _FakeSecurity(allow_plaintext=True))

    fs.insert(_ALICE, "x.bin", io.BytesIO(b"secret"))

    with inner.get(_ALICE, "x.bin") as fh:
        assert fh.read().startswith(_PREFIX)


def test_encrypted_fs_store_decryption_failure_is_fail_closed(tmp_path) -> None:
    fs, _, security = _fs(tmp_path)
    fs.insert(_ALICE, "x.bin", io.BytesIO(b"v"))
    security.fail_decrypt = True

    with pytest.raises(BackendError):
        fs.get(_ALICE, "x.bin")


def test_encrypted_fs_store_factory_builds_wrapper_from_named_dependencies(tmp_path) -> None:
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "security": {"default": "fake_encrypted_fs"},
            "fs_store": {
                "raw": {"target": "local", "params": {"root": str(tmp_path / "files")}},
                "default": {
                    "target": "encrypted",
                    "params": {"inner": "raw", "security": "default"},
                },
            },
        }
    )

    fs = FsProducer.build_named("default", ctx)

    assert isinstance(fs, EncryptedFSStore)
    ref = fs.insert(_ALICE, "x.bin", io.BytesIO(b"value"))
    with fs.get(_ALICE, ref) as fh:
        assert fh.read() == b"value"


def test_encrypted_fs_store_factory_requires_inner_dependency() -> None:
    """没配 inner 时必须报错。给个默认会让「配错了」静默变成「加密了一个内存
    store」——数据写得进去，重启后全没了。
    """
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "security": {"default": "fake_encrypted_fs"},
            "fs_store": {"default": {"target": "encrypted", "params": {"security": "default"}}},
        }
    )

    with pytest.raises(ValidationError):
        FsProducer.build_named("default", ctx)


def test_encrypted_fs_is_registered_by_storage_bootstrap() -> None:
    """``api.build_kernel`` 只调 ``register_backends()``，从不调 ``register_security()``。

    装饰器住在 storage 下就是为了这个：注册若挂在别处，不经该装配路径会得到
    「未注册的实现 'encrypted'」——一个只在部分入口出现的故障。
    """
    from storage.bootstrap import register_backends

    register_backends()

    assert "encrypted" in FsProducer.known()


def test_encrypted_fs_store_rejects_oversized_plaintext(tmp_path) -> None:
    """审计验收 P2-FS：写入用有界 read，超限即拒，不先整块读入内存。"""
    fs, inner, _ = _fs(tmp_path)
    fs._max_plaintext_bytes = 4
    with pytest.raises(ValidationError):
        fs.insert(_ALICE, "big.bin", io.BytesIO(b"abcdef"))
    ref = fs.insert(_ALICE, "ok.bin", io.BytesIO(b"ok"))
    fs._max_plaintext_bytes = 4
    with pytest.raises(ValidationError):
        fs.update(_ALICE, ref, io.BytesIO(b"oversized"))


def test_encrypted_fs_store_bounded_read_does_not_load_oversized(tmp_path) -> None:
    """审计验收 P2-FS：超大输入不先全读。用 TrackingReader 证明只读了 max+1。"""
    fs, inner, _ = _fs(tmp_path)
    fs._max_plaintext_bytes = 4

    class _TrackingReader(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.read_calls = []

        def read(self, n=-1):
            self.read_calls.append(n)
            return super().read(n)

    reader = _TrackingReader(b"x" * 1024)
    with pytest.raises(ValidationError):
        fs.insert(_ALICE, "big.bin", reader)
    # 只读了 max+1=5 字节就判定超限，没读完整 1024
    assert reader.read_calls == [5]


def test_encrypted_fs_store_rejects_oversized_ciphertext_on_read(tmp_path) -> None:
    """验收第三次 P3：stat 早拒超大密文，且 decrypt 不被调用。

    用显式小 max_ciphertext_bytes，使 1024 字节密文触发 stat 早拒（而非走到
    解密后被明文复核拒--那是另一条分支）。
    """
    inner = LocalFSStore(root=str(tmp_path / "files"))
    fake = _FakeSecurity()
    fs = EncryptedFSStore(inner, fake, max_plaintext_bytes=4, max_ciphertext_bytes=8)
    big_ciphertext = b"x" * 1024
    ref = inner.insert(_ALICE, "big.enc", io.BytesIO(big_ciphertext))
    with pytest.raises(ValidationError):
        fs.get(_ALICE, ref)
    # stat 早拒：decrypt 根本没被调用
    assert fake.decrypt_calls == []


def test_encrypted_fs_store_handles_short_reads_without_truncation(tmp_path) -> None:
    """验收复验 P2-FS 问题 1：短读流不能被当完整文件静默截断。

    BinaryIO.read(n) 允许返回 < n 字节而未 EOF。单次 read 会把第一段当完整内容。
    循环有界读取必须反复 read 到 EOF，否则 b'a' 会被当成整个文件存下。
    """

    class _ShortReader(io.BytesIO):
        """每次只返回 1 字节，模拟短读流。"""

        def read(self, n=-1):
            if n is None or n < 0:
                return super().read()
            return super().read(1)

    fs, inner, fake = _fs(tmp_path)
    reader = _ShortReader(b"abcdef")
    ref = fs.insert(_ALICE, "short.bin", reader)
    # 完整 6 字节都应被读取并加密，不是只存第一段 b'a'
    with fs.get(_ALICE, ref) as fh:
        assert fh.read() == b"abcdef"


def test_encrypted_fs_store_toctou_stat_get_mismatch_still_bounded(tmp_path) -> None:
    """验收第四次 P3-test：stat 与 get 不一致时，读取按显式密文上限有界（TOCTOU）。

    stat 报小、get 返回大，stat 早拒通过后，真正读取仍用循环有界，读到
    max_ciphertext_bytes+1 即止并拒。用 tracking reader 断言**实际读取量**有界--
    防回归成「全读后再检查长度」（那样 decrypt 也没被调，旧断言测不出退化）。
    """

    class _TrackingReader(io.BytesIO):
        """记录每次 read(n) 的请求大小，用于断言有界读取。"""

        def __init__(self, data):
            super().__init__(data)
            self.read_calls: list[int] = []

        def read(self, n=-1):
            self.read_calls.append(n)
            return super().read(n)

    class _LyingStat:
        """stat 永远报 1，get 返回 tracking reader（1024 bytes）--模拟 stat/get 不一致。"""

        def __init__(self, inner):
            self._inner = inner
            self.last_reader: _TrackingReader | None = None  # 供测试断言

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def stat(self, scope, ref):
            from storage.types import FileStat

            return FileStat(ref=ref, size=1)

        def get(self, scope, ref):
            self.last_reader = _TrackingReader(b"x" * 1024)
            return self.last_reader

    inner = LocalFSStore(root=str(tmp_path / "files"))
    fake = _FakeSecurity()
    fs = EncryptedFSStore(inner, fake, max_plaintext_bytes=4, max_ciphertext_bytes=8)
    lying = _LyingStat(inner)
    fs._inner = lying
    ref = "fake-ref"
    with pytest.raises(ValidationError):
        fs.get(_ALICE, ref)
    # 密文流超过 max_ciphertext_bytes 被拒，decrypt 未调用
    assert fake.decrypt_calls == []
    # 实际读取有界（防回归成 fh.read() 全读后检查）：
    reader = lying.last_reader
    assert reader is not None
    calls = reader.read_calls
    assert calls, "未发生任何 read"
    # - 没有 read(-1)（无界全读）
    assert -1 not in calls, f"退化成 read(-1) 无界全读：{calls}"
    # - 首次请求大小 = max_ciphertext_bytes + 1 = 9
    assert calls[0] == 9, f"首次应请求 max+1=9，得到 {calls[0]}"
    # - 循环有界：读到上限即拒，不会有第二次大请求（首次 read(9) 即读到 9 字节超限）
    assert len(calls) == 1, f"应在首次 read 即超限拒，不该多次 read：{calls}"


def test_encrypted_fs_store_rejects_oversized_plaintext_after_decrypt(tmp_path) -> None:
    """验收复验 P2-FS：解密后复核明文上限。

    stat/密文长度都通过，但解密出的明文超限（密文被替换成另一个合法但解出超大的
    信封）也要拒。用一个解密时返回超大明文的 fake 触发。
    """

    class _InflatingSecurity(_FakeSecurity):
        def decrypt(self, ciphertext, *, context=None, aad=b""):
            return b"y" * 100  # 远超 max_plaintext_bytes=4

    fs, inner, fake = _fs(tmp_path, security=_InflatingSecurity())
    fs._max_plaintext_bytes = 4
    # 先正常写入一个小文件
    ref = fs.insert(_ALICE, "ok.bin", io.BytesIO(b"ok"))
    # 读取时解密返回 100 字节，应被解密后复核拒
    with pytest.raises(ValidationError):
        fs.get(_ALICE, ref)


def test_encrypted_fs_store_byte_by_byte_stream_does_not_amplify_memory(tmp_path) -> None:
    """验收第三次 P2-2：1-byte 短读不按 chunk 数线性增长内存。

    此前 list[bytes] + join 会为百万级 1-byte 分片造出 ~700 MiB / 8MiB 内容。
    bytearray 累积使内存与字节数成正比。用小尺寸（8 KiB + 1-byte 读）验证不放大：
    断言峰值增量与内容字节数同量级，而非百倍。
    """
    import tracemalloc

    fs, inner, fake = _fs(tmp_path)
    fs._max_plaintext_bytes = 8 * 1024  # 8 KiB，足够看出放大比、不压 CI

    class _ByteByByte(io.BytesIO):
        def read(self, n=-1):
            if n is None or n < 0:
                return super().read()
            return super().read(1)

    data = b"x" * (8 * 1024)
    tracemalloc.start()
    ref = fs.insert(_ALICE, "frag.bin", _ByteByByte(data))
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # bytearray 累积：峰值与内容同量级（8 KiB），不应是百倍放大
    assert peak < len(data) * 20, f"内存放大 {peak / len(data):.1f}x，疑似 list 累积"
    with fs.get(_ALICE, ref) as fh:
        assert fh.read() == data


def test_encrypted_fs_store_factory_accepts_max_plaintext_bytes(tmp_path) -> None:
    """factory 读取 max_plaintext_bytes 配置；非法值在装配期炸。"""
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "security": {"default": "fake_encrypted_fs"},
            "fs_store": {
                "raw": {"target": "local", "params": {"root": str(tmp_path / "files")}},
                "default": {
                    "target": "encrypted",
                    "params": {"inner": "raw", "security": "default", "max_plaintext_bytes": 8},
                },
            },
        }
    )
    fs = FsProducer.build_named("default", ctx)
    assert isinstance(fs, EncryptedFSStore)
    assert fs._max_plaintext_bytes == 8

    # 非法值：装配期炸。reset_all 避开上一次 build 的实例缓存。
    Factory.reset_all()
    ctx_bad = AssemblyContext.from_dict(
        {
            "security": {"default": "fake_encrypted_fs"},
            "fs_store": {
                "default": {
                    "target": "encrypted",
                    "params": {"security": "default", "max_plaintext_bytes": 0},
                }
            },
        }
    )
    with pytest.raises(ValidationError):
        FsProducer.build_named("default", ctx_bad)
