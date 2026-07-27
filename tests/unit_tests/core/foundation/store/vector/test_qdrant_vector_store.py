# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

qdrant_client = pytest.importorskip("qdrant_client")

from jiuwen_memory.common.exception.errors import BaseError  # noqa: E402
from jiuwen_memory.foundation.store.base_vector_store import (  # noqa: E402
    CollectionSchema,
    FieldSchema,
    VectorDataType,
)
from jiuwen_memory.foundation.store import create_vector_store  # noqa: E402
from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc  # noqa: E402
from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex  # noqa: E402
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore  # noqa: E402
from jiuwen_memory.foundation.store.vector.qdrant_vector_store import QdrantVectorStore  # noqa: E402
from jiuwen_memory.memory_core.migration.operation.base_operation import OperationMetadata  # noqa: E402
from jiuwen_memory.memory_core.migration.operation.operations import AddScalarFieldOperation  # noqa: E402


def _schema() -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True),
            FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
            FieldSchema(name="category", dtype=VectorDataType.VARCHAR),
            FieldSchema(name="metadata", dtype=VectorDataType.JSON),
        ]
    )


@pytest_asyncio.fixture(name="store")
async def _store():
    client = qdrant_client.AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(client=client, collection_prefix="pytest_agent_vector")
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_foundation_factory_resolves_qdrant():
    client = qdrant_client.AsyncQdrantClient(location=":memory:")
    store = create_vector_store("qdrant", client=client, collection_prefix="factory_test")
    try:
        assert isinstance(store, QdrantVectorStore)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_qdrant_collection_creation_is_concurrent_and_schema_safe(store):
    collection = "qdrant_create_race"
    await asyncio.gather(*(store.create_collection(collection, _schema()) for _ in range(10)))
    assert await store.collection_exists(collection)

    incompatible = _schema()
    incompatible.get_vector_fields()[0].dim = 8
    with pytest.raises(BaseError):
        await store.create_collection(collection, incompatible)


@pytest.mark.asyncio
async def test_qdrant_recovers_metadata_after_interrupted_creation():
    collection = "qdrant_metadata_recovery"
    prefix = "pytest_agent_vector"
    client = qdrant_client.AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(client=client, collection_prefix=prefix)
    try:
        await client.create_collection(
            f"{prefix}__{collection}",
            vectors_config={
                "embedding": qdrant_client.models.VectorParams(size=4, distance=qdrant_client.models.Distance.COSINE)
            },
        )
        assert await store.get_collection_metadata(collection) == {}

        recovery_schema = CollectionSchema(
            fields=[
                FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True),
                FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
            ]
        )
        await store.create_collection(collection, recovery_schema)
        assert (await store.get_schema(collection)).get_vector_fields()[0].dim == 4
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_qdrant_integrates_with_simple_memory_index(store):
    class Embedding:
        async def embed_documents(self, texts):
            return [[1.0, 0.0] if "apple" in text else [0.0, 1.0] for text in texts]

        async def embed_query(self, text):
            return [1.0, 0.0]

    index = SimpleMemoryIndex(InMemoryKVStore(), store, Embedding())
    memory = MemoryDoc(
        id="000000000000000000000001",
        text="apple preference",
        type="semantic_memory",
        timestamp=datetime.now(timezone.utc),
    )
    await index.add_memories("user", "scope", [memory])

    results = await index.search("user", "scope", "fruit", ["semantic_memory"])
    assert results[0][0].text == "apple preference"
    assert results[0][1] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_qdrant_vector_store_public_interfaces(store):
    collection = "qdrant_vectors"
    await store.create_collection(collection, _schema(), distance_metric="COSINE")

    assert await store.collection_exists(collection)
    assert collection in await store.list_collection_names()
    assert (await store.get_schema(collection)).get_vector_fields()[0].dim == 4

    metadata = await store.get_collection_metadata(collection)
    assert metadata["primary_key_field"] == "id"
    assert metadata["distance_metric"] == "COSINE"
    await store.update_collection_metadata(collection, {"schema_version": 1, "owner": "test"})
    assert (await store.get_collection_metadata(collection))["owner"] == "test"

    await store.add_docs(
        collection,
        [
            {
                "id": "arbitrary/string/id",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "category": "fruit",
                "metadata": {"source": "one"},
            },
            {
                "id": "doc-2",
                "embedding": [0.0, 1.0, 0.0, 0.0],
                "category": "vehicle",
                "metadata": {"source": "two"},
            },
        ],
    )

    results = await store.search(
        collection,
        [1.0, 0.0, 0.0, 0.0],
        "embedding",
        filters={"category": "fruit"},
    )
    assert len(results) == 1
    assert results[0].fields["id"] == "arbitrary/string/id"
    assert results[0].fields["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert results[0].score == pytest.approx(1.0)

    await store.delete_docs_by_ids(collection, ["arbitrary/string/id"])
    results = await store.search(collection, [1.0, 0.0, 0.0, 0.0], "embedding", top_k=5)
    assert {result.fields["id"] for result in results} == {"doc-2"}

    await store.delete_docs_by_filters(collection, {"category": "vehicle"})
    assert await store.search(collection, [0.0, 1.0, 0.0, 0.0], "embedding") == []

    await store.delete_collection(collection)
    assert not await store.collection_exists(collection)


@pytest.mark.asyncio
async def test_qdrant_schema_migration_preserves_documents(store):
    collection = "qdrant_migration"
    await store.create_collection(collection, _schema())
    await store.add_docs(
        collection,
        [
            {
                "id": "doc-1",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "category": "fruit",
                "metadata": {},
            }
        ],
    )

    await store.update_schema(
        collection,
        [
            AddScalarFieldOperation(
                metadata=OperationMetadata(schema_version=1),
                data_type="memory",
                field_name="status",
                field_type="string",
                default_value="active",
            )
        ],
    )

    schema = await store.get_schema(collection)
    assert schema.get_field("status").default_value == "active"
    results = await store.search(collection, [1.0, 0.0, 0.0, 0.0], "embedding")
    assert results[0].fields["id"] == "doc-1"
    assert results[0].fields["status"] == "active"


@pytest.mark.asyncio
async def test_qdrant_schema_migration_restores_original_on_rewrite_failure(store, monkeypatch):
    collection = "qdrant_migration_rollback"
    await store.create_collection(collection, _schema())
    await store.add_docs(
        collection,
        [
            {
                "id": "doc-1",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "category": "fruit",
                "metadata": {},
            }
        ],
    )
    original_add = store.add_docs

    async def fail_migrated_rewrite(target, docs, **kwargs):
        if target == collection and docs and "status" in docs[0]:
            monkeypatch.setattr(store, "add_docs", original_add)
            raise RuntimeError("simulated Qdrant rewrite failure")
        await original_add(target, docs, **kwargs)

    monkeypatch.setattr(store, "add_docs", fail_migrated_rewrite)
    operation = AddScalarFieldOperation(
        metadata=OperationMetadata(schema_version=1),
        data_type="memory",
        field_name="status",
        field_type="string",
        default_value="active",
    )

    with pytest.raises(RuntimeError, match="simulated Qdrant rewrite failure"):
        await store.update_schema(collection, [operation])

    assert (await store.get_schema(collection)).get_field("status") is None
    results = await store.search(collection, [1.0, 0.0, 0.0, 0.0], "embedding")
    assert results[0].fields["id"] == "doc-1"
    assert "status" not in results[0].fields
