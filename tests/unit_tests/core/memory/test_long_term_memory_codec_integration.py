# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from unittest.mock import AsyncMock, Mock, patch
import pytest

from jiuwen_memory.common.security.crypt_utils import (
    AesGcmCrypt,
    CryptUtils,
)
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
from jiuwen_memory.memory_core.config.config import MemoryEngineConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory


_VALID_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_global_state():
    Singleton._instances.pop(AesGcmCrypt, None)
    Singleton._instances.pop(LongTermMemory, None)
    CryptUtils._CRYPT_REGISTRY.clear()
    yield
    Singleton._instances.pop(AesGcmCrypt, None)
    Singleton._instances.pop(LongTermMemory, None)
    CryptUtils._CRYPT_REGISTRY.clear()


def _setup_minimal_ltm(ltm, crypto_key):
    from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
    kv = InMemoryKVStore()
    vs = Mock()
    vs.collection_exists = AsyncMock(return_value=False)
    vs.add_docs = AsyncMock()
    vs.list_collection_names = AsyncMock(return_value=[])
    vs.create_collection = AsyncMock()
    vs.delete_collection = AsyncMock()
    vs.search = AsyncMock(return_value=[])
    vs.delete_docs_by_ids = AsyncMock()
    emb = Mock()
    emb.dimension = 8
    emb.limiter = AsyncMock()
    emb.embed_documents = AsyncMock(return_value=[[0.1] * 8])
    emb.embed_query = AsyncMock(return_value=[0.1] * 8)

    ltm.kv_store = kv
    ltm.vector_store = vs
    ltm.db_store = AsyncMock()
    ltm.memory_index = SimpleMemoryIndex(kv_store=kv, vector_store=vs, embedding_model=emb)
    ltm._sys_mem_config = MemoryEngineConfig(crypto_key=crypto_key)

    from jiuwen_memory.foundation.codec import AesStorageCodec
    codec = AesStorageCodec(crypto_key)
    if crypto_key:
        crypt = AesGcmCrypt()
        CryptUtils.register_crypt(CryptUtils.AES_GCM_CRYPT_NAME, crypt)
    ltm.memory_index.set_storage_codec(codec)

    return ltm


class TestLongTermMemoryCodecInjection:
    def test_set_config_injects_codec(self):
        crypt = AesGcmCrypt()
        CryptUtils.register_crypt(CryptUtils.AES_GCM_CRYPT_NAME, crypt)
        ltm = LongTermMemory()
        _setup_minimal_ltm(ltm, _VALID_KEY)

        assert ltm.memory_index is not None
        assert ltm.memory_index._codec is not None

    def test_set_config_empty_key_codec_still_present(self):
        ltm = LongTermMemory()
        _setup_minimal_ltm(ltm, b"")

        assert ltm.memory_index is not None
        assert ltm.memory_index._codec is not None
        assert ltm.memory_index._codec.encode("hello") == "hello"
        assert ltm.memory_index._codec.decode("hello") == "hello"

    @pytest.mark.asyncio
    async def test_full_write_read_cycle(self):
        from datetime import datetime, timezone

        crypt = AesGcmCrypt()
        CryptUtils.register_crypt(CryptUtils.AES_GCM_CRYPT_NAME, crypt)
        ltm = LongTermMemory()
        ltm = _setup_minimal_ltm(ltm, _VALID_KEY)
        idx = ltm.memory_index

        assert idx._codec is not None

        doc = MemoryDoc(
            id="test_m1",
            text="encrypted memory content",
            type="user_profile",
            timestamp=datetime.now(timezone.utc).astimezone(),
        )
        await idx.add_memories("u1", "s1", [doc])

        result = await idx.get_by_id("u1", "s1", "test_m1")
        assert result is not None
        assert result.text == "encrypted memory content"

        raw_data = await ltm.kv_store.get_by_prefix("UMD")
        raw_val = list(raw_data.values())[0]
        decoded = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val
        import json
        kv_json = json.loads(decoded)
        assert kv_json["mem"] != "encrypted memory content"

    @staticmethod
    def test_file_backend_injects_custom_codec_without_crypto_key():
        """Regression: file 后端 + 自定义 codec（不依赖 crypto_key）+ 空 crypto_key
        时，codec 仍应被注入，而非静默落明文。

        触发条件（修复前）：register_storage_codec("sm4", SM4Codec(key=...)) +
        MemoryEngineConfig(codec="sm4", crypto_key=b"") + file 后端 → 旧门控
        ``if config.crypto_key`` 为 False → 不注入 → 明文落盘，加密意图被忽略。
        """
        from jiuwen_memory.foundation.codec import (
            StorageCodec,
            get_default_registry,
            register_storage_codec,
        )

        class _KeylessCodec:
            """第三方 codec：加解密不依赖 crypto_key（密钥已在实例里）。"""
            @staticmethod
            def encode(text: str) -> str:
                return text[::-1]

            @staticmethod
            def decode(data: str) -> str:
                return data[::-1]

        ltm = LongTermMemory()
        ltm.kv_store = Mock()
        ltm.vector_store = Mock()
        ltm.db_store = Mock()
        # mock memory_index 监听 set_storage_codec 是否被调用
        ltm.memory_index = Mock()
        ltm.configure_index_backend("file")

        custom_codec = _KeylessCodec()
        register_storage_codec("keyless", custom_codec)
        try:
            config = MemoryEngineConfig(codec="keyless", crypto_key=b"")
            ltm.set_config(config)
        finally:
            get_default_registry().unregister("keyless")

        # 修复后：config.codec 非空 → codec 必须被注入，不再因 crypto_key 空而跳过
        ltm.memory_index.set_storage_codec.assert_called_once()
        injected = ltm.memory_index.set_storage_codec.call_args[0][0]
        assert injected is custom_codec
        # 且 engine 的 storage_codec 也是注入的那个
        assert ltm.storage_codec is custom_codec

    @staticmethod
    def test_file_backend_plaintext_when_no_key_and_no_codec():
        """对照组：既无 crypto_key 也无 codec 时，file 后端保持明文（设计意图）。"""
        ltm = LongTermMemory()
        ltm.kv_store = Mock()
        ltm.vector_store = Mock()
        ltm.db_store = Mock()
        ltm.memory_index = Mock()
        ltm.configure_index_backend("file")

        config = MemoryEngineConfig(crypto_key=b"", codec="")
        ltm.set_config(config)

        ltm.memory_index.set_storage_codec.assert_not_called()

    def _make_stub_message_store(self, *, override_set_codec: bool):
        """最小桩 message store：实现 BaseMessageStore 全部 abstractmethod。

        override_set_codec=False → 沿用基类 set_codec（no-op + warning），
        模拟"不需要加密、未覆盖 set_codec"的自定义 store。
        override_set_codec=True → 覆盖 set_codec 记录被注入的 codec。
        """
        from jiuwen_memory.foundation.store.base_message_store import BaseMessageStore

        if override_set_codec:

            class _StubStoreOverriding(BaseMessageStore):
                def __init__(self):
                    self.injected_codec = None

                def set_codec(self, codec) -> None:
                    self.injected_codec = codec

                async def add_message(self, message_add):
                    return "m1"

                async def add_messages(self, message_adds):
                    return ["m1"]

                async def get_message_by_id(self, message_id):
                    return None

                async def get_messages(self, message_filter, limit=10,
                                       order_by="timestamp", order_direction="desc"):
                    return []

                async def update_message(self, message_id, content):
                    return True

                async def delete_message_by_id(self, message_id):
                    return True

                async def delete_messages(self, message_filter):
                    return 0

                async def count_messages(self, message_filter):
                    return 0

                async def get_schema_version(self):
                    return None

                async def set_schema_version(self, version):
                    return None

            return _StubStoreOverriding()

        class _StubStorePlain(BaseMessageStore):
            # 不覆盖 set_codec → 沿用基类 no-op + warning
            async def add_message(self, message_add):
                return "m1"

            async def add_messages(self, message_adds):
                return ["m1"]

            async def get_message_by_id(self, message_id):
                return None

            async def get_messages(self, message_filter, limit=10,
                                   order_by="timestamp", order_direction="desc"):
                return []

            async def update_message(self, message_id, content):
                return True

            async def delete_message_by_id(self, message_id):
                return True

            async def delete_messages(self, message_filter):
                return 0

            async def count_messages(self, message_filter):
                return 0

            async def get_schema_version(self):
                return None

            async def set_schema_version(self, version):
                return None

        return _StubStorePlain()

    def test_set_config_does_not_crash_on_codec_less_message_store(self):
        """Regression: set_config 无条件调用 message_store.set_codec(codec)。

        未覆盖 set_codec 的自定义 BaseMessageStore 子类（不需要加密）在
        基类 set_codec 仍 raise NotImplementedError 时会让 set_config 崩溃，
        致引擎启动失败。修复后基类 set_codec 为 no-op + warning，不再崩溃。
        """
        ltm = LongTermMemory()
        ltm.kv_store = Mock()
        ltm.vector_store = Mock()
        ltm.db_store = Mock()
        ltm.memory_index = Mock()
        ltm.configure_index_backend("simple")
        ltm.message_store = self._make_stub_message_store(
            override_set_codec=False)

        config = MemoryEngineConfig(crypto_key=_VALID_KEY)
        # 修复前：下一行抛 NotImplementedError；修复后：安全通过
        ltm.set_config(config)

        assert ltm.message_manager is not None
        assert ltm.storage_codec is not None

    def test_set_config_injects_codec_into_overriding_message_store(self):
        """覆盖了 set_codec 的 message store 仍能正确收到 engine codec。"""
        ltm = LongTermMemory()
        ltm.kv_store = Mock()
        ltm.vector_store = Mock()
        ltm.db_store = Mock()
        ltm.memory_index = Mock()
        ltm.configure_index_backend("simple")
        store = self._make_stub_message_store(override_set_codec=True)
        ltm.message_store = store

        config = MemoryEngineConfig(crypto_key=_VALID_KEY)
        ltm.set_config(config)

        assert store.injected_codec is ltm.storage_codec
