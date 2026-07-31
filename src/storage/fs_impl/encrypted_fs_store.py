"""EncryptedFSStore — FSStore 加密装饰器。

与 ``EncryptedKVStore`` 同构：不含任何加解密算法，只在 FS 边界统一构造
``SecurityContext`` / AAD，并委托注入的 ``SecurityProvider``。真实算法位于
``common.security.security_impl``。

文件内容整体加密成一个信封再落盘，``ref`` / ``scope`` 保持明文（路径要能寻址），
``ref`` 进 AAD。

**已知代价（两条，都是 AES-GCM 整块认证的直接后果）**：

1. ``get`` 必须**读全文件到内存**再整体解密——没有跨块认证绑定就不能流式部分
   解密。大文件（视频、模型权重）会吃内存。第一期不做 chunked encryption：
   chunk 间无绑定，可被重排/截断，不适合作默认方案。
2. :attr:`~storage.types.FileStat.size` 返回的是**密文长度**，比明文长（信封头
   + 包装后的数据密钥 + 两个 nonce + 两个 16B 的 GCM tag）。不修正——修正需要先
   解密才能知道明文长度，代价荒谬。调用方拿它去分配缓冲区只会偏大，不影响正确性。

迁移期兼容明文读由 ``security`` 组件的 ``allow_plaintext`` 参数控制（provider 层
统一开关，KV / FS 共用），本装饰器不再重复提供同语义的旋钮。
"""

from __future__ import annotations

import io
import json
from typing import Any, BinaryIO

from common.errors import BackendError, ValidationError
from common.security import SecurityContext, SecurityProducer, SecurityProvider
from common.type_def import Scope
from storage.base import StoreType
from storage.fs import FsProducer, FSStore
from storage.types import FileStat

_AAD_VERSION = 1
_PURPOSE_FS_OBJECT = "fs_object"

# 单文件明文大小硬上限。AES-GCM 整块认证要求把整个明文读入内存再加密（见模块
# docstring 的已知代价），无上限意味着一个超大输入能把进程内存吃满。64 MiB 覆盖
# 文本/图片/中等模型分片等记忆资产；视频/原始模型权重本就该走专用对象存储而非
# memory 系统。chunked 加密（第一期不做）落地后可放宽。
_DEFAULT_MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024

# 密文上限的默认安全余量（加在明文上限上）。SecurityProvider 的 ABC 不暴露密文
# overhead，故不硬编码某个 provider 的精确值--用宽松余量覆盖 ENC1 信封固定开销
# （header + 加密 data key + nonce + GCM tag ≈ 100），宁可拒偏大也不读入超大密文。
# 需要精确控制时显式配 max_ciphertext_bytes（验收复验 P2-FS）。
_DEFAULT_CIPHERTEXT_OVERHEAD = 4 * 1024  # 4 KiB，远大于 ~100 字节信封开销


def _read_bounded_stream(stream: BinaryIO, limit: int, *, ref: str) -> bytes:
    """循环有界读取：反复 ``read`` 直到 EOF 或累计达到 ``limit + 1``。

    BinaryIO.read(n) 允许短读（返回 < n 字节而未 EOF）。单次 read 会把第一段当完整
    内容，造成静默截断（验收复验 P2-FS 问题 1）。循环读取并在累计超过 limit 时
    拒绝，才真正守住边界。多读 1 字节用于判定超限。

    用 ``bytearray`` 累积而非 ``list[bytes]`` + ``join``（验收第三次 P2-2）：恶意
    1-byte 短读会让 list 长出百万级元素 + join 拼接元数据，8 MiB 内容能放大到 ~700 MiB。
    bytearray.extend 是单个连续缓冲区，内存与内容字节数成正比，不随分片数放大。
    """
    buffer = bytearray()
    while len(buffer) <= limit:
        chunk = stream.read(limit + 1 - len(buffer))
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) > limit:
        raise ValidationError(f"fs encrypted: 内容超过单文件上限 {limit}B（ref={ref!r}）")
    return bytes(buffer)


def _scope_payload(scope: Scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": str(getattr(scope, "space", "")),
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _aad(scope: Scope, ref: str) -> bytes:
    payload = {
        "version": _AAD_VERSION,
        "scope": _scope_payload(scope),
        "ref": ref,
        "purpose": _PURPOSE_FS_OBJECT,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _security_context(scope: Scope, ref: str) -> SecurityContext:
    return SecurityContext(
        scope=scope,
        purpose=_PURPOSE_FS_OBJECT,
        metadata={
            "ref": ref,
            "aad_version": str(_AAD_VERSION),
        },
    )


class EncryptedFSStore(FSStore):
    """对任意 FSStore 做透明加解密的装饰器。"""

    def __init__(
        self,
        inner: FSStore,
        security: SecurityProvider,
        *,
        max_plaintext_bytes: int,
        max_ciphertext_bytes: int = 0,
    ) -> None:
        self._inner = inner
        self._security = security
        self._max_plaintext_bytes = max_plaintext_bytes
        # 密文上限：默认按明文上限 + 安全余量（覆盖 ENC1 信封固定开销 + 一点 buffer），
        # 可显式配置覆盖。不硬编码某个 provider 的精确开销--SecurityProvider 的 ABC
        # 不暴露 ciphertext bound，硬编码 128 会随 provider 实现变化失准（验收复验 P2-FS）。
        self._max_ciphertext_bytes = (
            max_ciphertext_bytes or max_plaintext_bytes + _DEFAULT_CIPHERTEXT_OVERHEAD
        )

    def store_type(self) -> StoreType:
        return StoreType.FS

    def health(self) -> None:
        self._inner.health()
        self._security.health()

    # -- 写：永远加密 ---------------------------------------------------- #

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        plaintext = self._read_bounded(data, ref=key)
        return self._inner.insert(scope, key, io.BytesIO(self._encrypt(scope, key, plaintext)))

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        plaintext = self._read_bounded(data, ref=ref)
        return self._inner.update(scope, ref, io.BytesIO(self._encrypt(scope, ref, plaintext)))

    # -- 读：解密 -------------------------------------------------------- #

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        # stat 只作快速早拒（避免无谓打开超大对象）；它不是唯一边界--stat 与随后 get
        # 之间内容可能变化（TOCTOU），故真正读取仍用有界循环（验收复验 P2-FS）。
        stat = self._inner.stat(scope, ref)
        if stat.size > self._max_ciphertext_bytes:
            raise ValidationError(
                f"fs encrypted: 密文 {stat.size}B 超过单文件上限 "
                f"{self._max_ciphertext_bytes}B（ref={ref!r}）"
            )
        with self._inner.get(scope, ref) as fh:
            stored = _read_bounded_stream(fh, self._max_ciphertext_bytes, ref=ref)
        plaintext = self._decrypt(scope, ref, stored)
        # 解密后复核明文上限：密文长度通过不代表明文通过（密文可被替换成另一个合法但
        # 解压后超大的信封，或 stat/get 不一致时绕过了上面的早拒）。
        if len(plaintext) > self._max_plaintext_bytes:
            raise ValidationError(
                f"fs encrypted: 解密后明文 {len(plaintext)}B 超过单文件上限 "
                f"{self._max_plaintext_bytes}B（ref={ref!r}）"
            )
        return io.BytesIO(plaintext)

    # -- 不涉加解密的纯转发 ---------------------------------------------- #

    def delete(self, scope: Scope, ref: str) -> None:
        self._inner.delete(scope, ref)

    def stat(self, scope: Scope, ref: str) -> FileStat:
        # size 是密文长度，见模块 docstring。
        return self._inner.stat(scope, ref)

    # -- 内部 ------------------------------------------------------------ #

    def _read_bounded(self, data: BinaryIO, *, ref: str) -> bytes:
        """有界读取明文：循环 read 直到 EOF 或累计达到 limit+1。

        验收复验 P2-FS：单次 ``read(limit+1)`` 不等于「读到 EOF 或上限」--BinaryIO
        允许短读（返回 < n 字节而未 EOF），单次调用会把第一段当完整文件，造成静默
        数据截断。循环读取并在超限时拒绝，才能真正守住边界。
        """
        return _read_bounded_stream(data, self._max_plaintext_bytes, ref=ref)

    def _encrypt(self, scope: Scope, ref: str, plaintext: bytes) -> bytes:
        try:
            return self._security.encrypt(
                plaintext,
                context=_security_context(scope, ref),
                aad=_aad(scope, ref),
            )
        except Exception as exc:
            raise BackendError(f"fs encryption failed: ref={ref!r}") from exc

    def _decrypt(self, scope: Scope, ref: str, ciphertext: bytes) -> bytes:
        try:
            return self._security.decrypt(
                ciphertext,
                context=_security_context(scope, ref),
                aad=_aad(scope, ref),
            )
        except Exception as exc:
            raise BackendError(f"fs decryption failed: ref={ref!r}") from exc


def _inner_store(config: Any) -> FSStore:
    """取被包住的 Store。无默认值——加密装饰器必须显式指明包住哪个 Store，
    猜一个默认后端只会把数据写到调用方没预期的地方（理由同 EncryptedKVStore）。
    """
    inner = config.params.get("inner")
    if inner is None:
        raise ValidationError("fs_store.encrypted params.inner 必须配置")
    if isinstance(inner, str) and inner == config.name:
        raise ValidationError("fs_store.encrypted params.inner 不能指向自身")
    return FsProducer.dep(config, "inner")


@FsProducer.register("encrypted")
def _build(config):
    max_plaintext_bytes = int(
        config.params.get("max_plaintext_bytes", _DEFAULT_MAX_PLAINTEXT_BYTES)
    )
    if max_plaintext_bytes < 1:
        raise ValidationError(
            f"fs_store.encrypted params.max_plaintext_bytes 须 >= 1，得到 {max_plaintext_bytes}"
        )
    max_ciphertext_bytes = int(config.params.get("max_ciphertext_bytes", 0))
    if max_ciphertext_bytes < 0:
        raise ValidationError(
            f"fs_store.encrypted params.max_ciphertext_bytes 须 >= 0，得到 {max_ciphertext_bytes}"
        )
    return EncryptedFSStore(
        inner=_inner_store(config),
        security=SecurityProducer.dep(config),
        max_plaintext_bytes=max_plaintext_bytes,
        max_ciphertext_bytes=max_ciphertext_bytes,
    )
