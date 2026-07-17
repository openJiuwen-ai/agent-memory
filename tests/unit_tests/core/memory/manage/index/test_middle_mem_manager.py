#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jiuwen_memory.memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MiddleTermUnit, MemoryType
from jiuwen_memory.common.exception.errors import BaseError


@pytest.mark.asyncio
async def test_add_middle_term_memory_success():
    memory_index = MagicMock()
    memory_index.write = AsyncMock()

    crypto_key = b"test-crypto-key-32-bytes"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )
    manager.mem_store = MagicMock()
    manager.mem_store.write = AsyncMock()

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=True)

    middle_term_unit = MiddleTermUnit(
        mem_id="middle-mem-001",
        content="用户在北京工作，从事软件开发",
        message_mem_id="msg-001",
        timestamp="2026-06-23 10:00:00"
    )

    memories = {
        MemoryType.MIDDLE_TERM_MEMORY.value: [middle_term_unit]
    }

    with patch.object(
            MiddleTermMemoryManager,
            "encrypt_memory_if_needed",
            return_value="encrypted-content"
    ):

        result = await manager.add_memories(
            user_id="user-123",
            scope_id="scope-456",
            memories=memories,
            semantic_store=semantic_store
        )

    assert result == [middle_term_unit]
    semantic_store.add_docs.assert_called_once()

    call_args = semantic_store.add_docs.call_args
    assert call_args.kwargs["is_middle"] == True

    docs = call_args.kwargs["docs"]
    assert len(docs) == 1
    assert docs[0][0] == "msg-001"  # message_mem_id
    assert docs[0][1] == "用户在北京工作，从事软件开发"  # content
    assert docs[0][2] == "2026-06-23 10:00:00"  # timestamp


@pytest.mark.asyncio
async def test_add_middle_term_memory_multiple_units():

    memory_index = MagicMock()
    memory_index.write = AsyncMock()
    crypto_key = b"test-crypto-key"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )
    manager.mem_store = MagicMock()
    manager.mem_store.write = AsyncMock()

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=True)

    unit1 = MiddleTermUnit(
        mem_id="middle-001",
        content="用户喜欢喝咖啡",
        message_mem_id="msg-001",
        timestamp="2026-06-23 10:00:00"
    )

    unit2 = MiddleTermUnit(
        mem_id="middle-002",
        content="用户周末去健身房",
        message_mem_id="msg-002",
        timestamp="2026-06-23 11:00:00"
    )

    memories = {
        MemoryType.MIDDLE_TERM_MEMORY.value: [unit1, unit2]
    }

    with patch.object(
            MiddleTermMemoryManager,
            "encrypt_memory_if_needed",
            return_value="encrypted-content"
    ):
        result = await manager.add_memories(
            user_id="user-123",
            scope_id="scope-456",
            memories=memories,
            semantic_store=semantic_store
        )

    assert result == [unit1, unit2]

    assert semantic_store.add_docs.call_count == 1
    call_args = semantic_store.add_docs.call_args
    assert call_args.kwargs["is_middle"] == True
    assert call_args.kwargs["docs"] == [
        ("msg-001", "用户喜欢喝咖啡", "2026-06-23 10:00:00"),
        ("msg-002", "用户周末去健身房", "2026-06-23 11:00:00"),
    ]


@pytest.mark.asyncio
async def test_add_middle_term_memory_wrong_type_filtered():

    memory_index = MagicMock()
    crypto_key = b"test-crypto-key"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=True)

    from jiuwen_memory.memory_core.manage.mem_model.memory_unit import SummaryUnit
    summary_unit = SummaryUnit(
        mem_id="summary-001",
        summary="这是一个总结",
        timestamp="2026-06-23 10:00:00"
    )

    memories = {
        MemoryType.SUMMARY.value: [summary_unit]
    }

    result = await manager.add_memories(
        user_id="user-123",
        scope_id="scope-456",
        memories=memories,
        semantic_store=semantic_store
    )

    assert result == []
    semantic_store.add_docs.assert_not_called()


@pytest.mark.asyncio
async def test_add_middle_term_memory_vector_store_failure():

    memory_index = MagicMock()
    memory_index.write = AsyncMock()
    crypto_key = b"test-crypto-key"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )
    manager.mem_store = MagicMock()
    manager.mem_store.write = AsyncMock()

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=False)

    middle_term_unit = MiddleTermUnit(
        mem_id="middle-001",
        content="测试内容",
        message_mem_id="msg-001",
        timestamp="2026-06-23 10:00:00"
    )

    memories = {
        MemoryType.MIDDLE_TERM_MEMORY.value: [middle_term_unit]
    }

    with patch.object(
            MiddleTermMemoryManager,
            "encrypt_memory_if_needed",
            return_value="encrypted-content"
    ):
        with pytest.raises(BaseError) as exc_info:
            await manager.add_memories(
                user_id="user-123",
                scope_id="scope-456",
                memories=memories,
                semantic_store=semantic_store
            )

    error_msg = str(exc_info.value.message).lower()
    assert "vector store" in error_msg or "failed" in error_msg


@pytest.mark.asyncio
async def test_add_middle_term_memory_missing_semantic_store():

    memory_index = MagicMock()
    crypto_key = b"test-crypto-key"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )

    middle_term_unit = MiddleTermUnit(
        mem_id="middle-001",
        content="测试内容",
        message_mem_id="msg-001",
        timestamp="2026-06-23 10:00:00"
    )

    memories = {
        MemoryType.MIDDLE_TERM_MEMORY.value: [middle_term_unit]
    }

    with pytest.raises(BaseError) as exc_info:
        await manager.add_memories(
            user_id="user-123",
            scope_id="scope-456",
            memories=memories
        )

    assert "semantic_store is required" in str(exc_info.value.message)


@pytest.mark.asyncio
async def test_add_middle_term_memory_empty_memories():
    memory_index = MagicMock()
    crypto_key = b"test-crypto-key"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=True)

    result = await manager.add_memories(
        user_id="user-123",
        scope_id="scope-456",
        memories={},
        semantic_store=semantic_store
    )

    assert result == []
    semantic_store.add_docs.assert_not_called()


@pytest.mark.asyncio
async def test_add_middle_term_memory_with_real_data():
    memory_index = MagicMock()
    memory_index.write = AsyncMock()
    crypto_key = b"test-crypto-key-32-bytes"

    manager = MiddleTermMemoryManager(
        memory_index=memory_index,
        crypto_key=crypto_key
    )
    manager.mem_store = MagicMock()
    manager.mem_store.write = AsyncMock()

    semantic_store = MagicMock()
    semantic_store.add_docs = AsyncMock(return_value=True)

    middle_term_unit = MiddleTermUnit(
        mem_id="mid-20260623-001",
        content="用户提到他在互联网公司工作，主要负责云存储相关的业务",
        message_mem_id="msg-session-abc123",
        timestamp="2026-06-23 14:30:00"
    )

    memories = {
        MemoryType.MIDDLE_TERM_MEMORY.value: [middle_term_unit]
    }

    with patch.object(
            MiddleTermMemoryManager,
            "encrypt_memory_if_needed",
            return_value="encrypted-阿里云存储业务"
    ):
        result = await manager.add_memories(
            user_id="user-zhangsan",
            scope_id="scope-personal-assistant",
            memories=memories,
            semantic_store=semantic_store
        )

    assert result == [middle_term_unit]

    call_args = semantic_store.add_docs.call_args
    assert call_args.kwargs["is_middle"] == True

    docs = call_args.kwargs["docs"]
    assert docs[0][0] == "msg-session-abc123"  # message_mem_id
    assert docs[0][1] == "用户提到他在互联网公司工作，主要负责云存储相关的业务"
    assert docs[0][2] == "2026-06-23 14:30:00"
