#!/usr/bin/env python
# coding: utf-8

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.memory_core.manage.mem_model.semantic_store import SemanticStore


@pytest.mark.asyncio
async def test_add_middle_docs_keeps_content_and_timestamp_per_doc():
    vector_store = MagicMock()
    vector_store.collection_exists = AsyncMock(return_value=True)
    vector_store.add_docs = AsyncMock(return_value=True)

    embedding_model = MagicMock()
    embedding_model.embed_documents = AsyncMock(return_value=[[0.1], [0.2]])

    semantic_store = SemanticStore(vector_store=vector_store, embedding_model=embedding_model)

    result = await semantic_store.add_docs(
        docs=[
            ("msg-001", "用户喜欢喝咖啡", "2026-06-23 10:00:00"),
            ("msg-002", "用户周末去健身房", "2026-06-23 11:00:00"),
        ],
        table_name="test_middle_term_memory",
        scope_id="scope-456",
        is_middle=True,
    )

    assert result is True
    vector_store.add_docs.assert_awaited_once_with(
        collection_name="test_middle_term_memory",
        docs=[
            {
                "id": "msg-001",
                "embedding": [0.1],
                "content": "用户喜欢喝咖啡",
                "timestamp": "2026-06-23 10:00:00",
            },
            {
                "id": "msg-002",
                "embedding": [0.2],
                "content": "用户周末去健身房",
                "timestamp": "2026-06-23 11:00:00",
            },
        ],
    )


@pytest.mark.asyncio
async def test_search_middle_docs_returns_string_fields():
    vector_store = MagicMock()
    vector_store.collection_exists = AsyncMock(return_value=True)
    vector_store.search = AsyncMock(return_value=[
        MagicMock(
            fields={
                "id": "msg-002",
                "content": "用户周末去健身房",
                "timestamp": "2026-06-23 11:00:00",
            },
            score=0.9,
        )
    ])

    embedding_model = MagicMock()
    embedding_model.embed_documents = AsyncMock(return_value=[[0.3]])

    semantic_store = SemanticStore(vector_store=vector_store, embedding_model=embedding_model)

    result = await semantic_store.search(
        query="健身",
        table_name="test_middle_term_memory",
        is_middle=True,
        top_k=1,
    )

    assert result == [("msg-002", 0.9, "用户周末去健身房", "2026-06-23 11:00:00")]
