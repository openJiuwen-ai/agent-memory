# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from unittest.mock import Mock, patch
import pytest

from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.store.base_embedding import Embedding as BaseEmbedding
from jiuwen_memory.common.utils.singleton import Singleton


class TestLongTermMemoryRegisterPlugin:
    """Test the register_plugin functionality in LongTermMemory"""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset the LongTermMemory singleton before each test"""
        Singleton._instances.pop(LongTermMemory, None)

    @pytest.mark.asyncio
    async def test_register_plugin(self):
        """Test that LongTermMemory can register a custom BaseMemoryIndex plugin"""
        long_term_memory = LongTermMemory()

        mock_kv_store = Mock(spec=BaseKVStore)
        mock_vector_store = Mock(spec=BaseVectorStore)
        mock_embedding = Mock(spec=BaseEmbedding)
        mock_embedding.embed_documents.return_value = [[0.1, 0.2, 0.3]]

        await long_term_memory.register_plugin(
            name="test_simple_index",
            cls=SimpleMemoryIndex,
            params={"kv_store": mock_kv_store, "vector_store": mock_vector_store, "embedding_model": mock_embedding}
        )

        # First registered plugin becomes the default memory_index
        assert long_term_memory.memory_index is not None
        assert isinstance(long_term_memory.memory_index, SimpleMemoryIndex)

    @pytest.mark.asyncio
    async def test_register_multiple_plugins(self):
        """Test that only the first registered plugin becomes default"""
        long_term_memory = LongTermMemory()

        mock_kv_store1 = Mock(spec=BaseKVStore)
        mock_kv_store2 = Mock(spec=BaseKVStore)
        mock_vector_store1 = Mock(spec=BaseVectorStore)
        mock_vector_store2 = Mock(spec=BaseVectorStore)
        mock_embedding = Mock(spec=BaseEmbedding)
        mock_embedding.embed_documents.return_value = [[0.1, 0.2, 0.3]]

        # Register first plugin
        await long_term_memory.register_plugin(
            name="test_simple_index1",
            cls=SimpleMemoryIndex,
            params={"kv_store": mock_kv_store1, "vector_store": mock_vector_store1, "embedding_model": mock_embedding}
        )
        first_index = long_term_memory.memory_index

        # Register second plugin
        await long_term_memory.register_plugin(
            name="test_simple_index2",
            cls=SimpleMemoryIndex,
            params={"kv_store": mock_kv_store2, "vector_store": mock_vector_store2, "embedding_model": mock_embedding}
        )

        # Default remains the first registered plugin
        assert long_term_memory.memory_index is first_index
        assert isinstance(long_term_memory.memory_index, SimpleMemoryIndex)

    @pytest.mark.asyncio
    @patch('jiuwen_memory.memory_core.long_term_memory.create_tables', return_value=None)
    async def test_register_plugin_after_store_registration(self, mock_create_tables):
        """Test that manual plugin registration doesn't overwrite auto-registered default"""
        long_term_memory = LongTermMemory()

        mock_kv_store = Mock()
        mock_vector_store = Mock(spec=BaseVectorStore)
        from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
        mock_db_store = Mock(spec=BaseDbStore)
        mock_embedding = Mock(spec=BaseEmbedding)
        mock_embedding.embed_documents.return_value = [[0.1, 0.2, 0.3]]
        await long_term_memory.register_store(
            kv_store=mock_kv_store,
            vector_store=mock_vector_store,
            db_store=mock_db_store,
            embedding_model=mock_embedding
        )

        # Auto-registered by register_store
        assert long_term_memory.memory_index is not None
        auto_index = long_term_memory.memory_index

        # Register a custom plugin manually — should not overwrite default
        custom_kv_store = Mock(spec=BaseKVStore)
        custom_vector_store = Mock(spec=BaseVectorStore)
        await long_term_memory.register_plugin(
            name="custom_simple_index",
            cls=SimpleMemoryIndex,
            params={"kv_store": custom_kv_store, "vector_store": custom_vector_store, "embedding_model": mock_embedding}
        )

        assert long_term_memory.memory_index is auto_index

    @pytest.mark.asyncio
    @patch('jiuwen_memory.memory_core.long_term_memory.create_tables', return_value=None)
    async def test_register_store_file_backend_wires_start_watcher(self, mock_create_tables, tmp_path, mocker):
        """file 后端下 register_store 应自动接线 start_watcher。

        回归检视意见：watcher 启用入口全仓未被调用（仅 file_memory_server 调），
        memory_server + file 后端下 watchdog 永不启动，外部编辑 .md 不实时同步。
        修复后 register_store 的 file 分支在 register_plugin 后调
        memory_index.start_watcher()。
        """
        long_term_memory = LongTermMemory()

        mock_kv_store = Mock()
        from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
        mock_db_store = Mock(spec=BaseDbStore)
        mock_embedding = Mock(spec=BaseEmbedding)
        mock_embedding.embed_documents.return_value = [[0.1, 0.2, 0.3]]

        # spy FileMemoryIndex.start_watcher —— 实例化后会被 register_store 调用
        from jiuwen_memory.foundation.store.index.file_index import FileMemoryIndex
        spy_start = mocker.patch.object(FileMemoryIndex, "start_watcher", autospec=True)

        await long_term_memory.register_store(
            kv_store=mock_kv_store,
            vector_store=None,
            db_store=mock_db_store,
            embedding_model=mock_embedding,
            index_backend="file",
            file_root_dir=str(tmp_path / "file_mem"),
        )

        # ★ 核心断言：register_store 接线了 start_watcher
        assert spy_start.called, "register_store(file) did not wire start_watcher — watcher never starts"
        assert long_term_memory.memory_index is not None


if __name__ == "__main__":
    pytest.main([__file__])
