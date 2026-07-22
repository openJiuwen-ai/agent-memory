# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import os
import uuid

import pytest
import pytest_asyncio

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.foundation.store.base_vector_store import CollectionSchema, FieldSchema, VectorDataType
from jiuwen_memory.foundation.store.filter_dsl import FilterCondition, FilterGroup, FilterOperator
from jiuwen_memory.memory_core.migration.operation.base_operation import OperationMetadata
from jiuwen_memory.memory_core.migration.operation.operations import AddScalarFieldOperation
from jiuwen_memory.foundation.store.vector.es_vector_store import ElasticsearchVectorStore

pytest.importorskip("elasticsearch")

pytestmark = pytest.mark.skipif(
    not os.getenv("ES_HOSTS"),
    reason="ES_HOSTS is not set; skipping Elasticsearch integration test",
)


def _build_schema() -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
            FieldSchema(name="text", dtype=VectorDataType.VARCHAR, max_length=65535),
            FieldSchema(name="category", dtype=VectorDataType.VARCHAR, max_length=128),
            FieldSchema(name="score_value", dtype=VectorDataType.DOUBLE),
            FieldSchema(name="metadata", dtype=VectorDataType.JSON),
        ],
        description="Elasticsearch vector store integration test collection",
    )


@pytest_asyncio.fixture(name="es_vector_store")
async def es_vector_store_fixture():
    kwargs = {}
    username = os.getenv("ES_USERNAME")
    password = os.getenv("ES_PASSWORD")
    if username and password:
        kwargs["basic_auth"] = (username, password)

    if os.getenv("ES_VERIFY_CERTS") == "false":
        kwargs["verify_certs"] = False

    ca_certs = os.getenv("ES_CA_CERTS")
    if ca_certs:
        kwargs["ca_certs"] = ca_certs

    store = ElasticsearchVectorStore(
        hosts=os.environ["ES_HOSTS"],
        index_prefix="pytest_agent_vector",
        **kwargs,
    )
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_elasticsearch_vector_store_public_interfaces(es_vector_store):
    collection_name = f"es_vec_it_{uuid.uuid4().hex[:12]}"
    schema = _build_schema()

    try:
        assert await es_vector_store.collection_exists(collection_name) is False

        await es_vector_store.create_collection(collection_name, schema, distance_metric="COSINE")
        assert await es_vector_store.collection_exists(collection_name) is True

        names = await es_vector_store.list_collection_names()
        assert collection_name in names

        loaded_schema = await es_vector_store.get_schema(collection_name)
        assert loaded_schema.get_primary_key_field().name == "id"
        assert loaded_schema.get_vector_fields()[0].name == "embedding"
        assert loaded_schema.get_vector_fields()[0].dim == 4

        metadata = await es_vector_store.get_collection_metadata(collection_name)
        assert metadata["distance_metric"] == "COSINE"
        assert metadata["vector_field"] == "embedding"
        assert metadata["vector_dim"] == 4
        assert metadata["schema_version"] == 0

        await es_vector_store.update_collection_metadata(
            collection_name,
            {"schema_version": 1, "owner": "integration-test"},
        )
        updated_metadata = await es_vector_store.get_collection_metadata(collection_name)
        assert updated_metadata["schema_version"] == 1
        assert updated_metadata["owner"] == "integration-test"

        docs = [
            {
                "id": "doc-1",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "text": "apple banana fruit",
                "category": "fruit",
                "score_value": 0.95,
                "metadata": {},
            },
            {
                "id": "doc-2",
                "embedding": [0.9, 0.1, 0.0, 0.0],
                "text": "orange fruit",
                "category": "fruit",
                "score_value": 0.85,
                "metadata": {},
            },
            {
                "id": "doc-3",
                "embedding": [0.0, 1.0, 0.0, 0.0],
                "text": "car engine vehicle",
                "category": "vehicle",
                "score_value": 0.75,
                "metadata": {},
            },
        ]
        await es_vector_store.add_docs(collection_name, docs, batch_size=2)

        inserted_results = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=3,
            output_fields=["id"],
        )
        assert {result.fields["id"] for result in inserted_results} == {"doc-1", "doc-2", "doc-3"}

        with pytest.raises(BaseError) as exc_info:
            await es_vector_store.add_docs(
                collection_name,
                [{"id": "doc-invalid", "embedding": [1.0, 0.0, 0.0, 0.0], "score_value": "not-a-double"}],
            )
        assert exc_info.value.status == StatusCode.STORE_VECTOR_DOC_INVALID
        assert exc_info.value.details

        results = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=2,
            output_fields=["id", "text", "category", "score_value", "metadata"],
        )
        assert len(results) == 2, f"expected 2 search results, got {len(results)}: {results}"
        assert results[0].fields["id"] == "doc-1"
        assert results[0].fields["category"] == "fruit"
        assert results[0].score >= results[1].score

        filtered_results = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=5,
            filters=FilterGroup(conditions=[
                FilterCondition(field="category", op=FilterOperator.EQ, value="fruit"),
            ]),
            output_fields=["id", "category"],
        )
        assert filtered_results
        assert all(result.fields["category"] == "fruit" for result in filtered_results)

        await es_vector_store.delete_docs_by_ids(collection_name, ["doc-1"])
        after_id_delete = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=5,
            output_fields=["id", "category"],
        )
        assert "doc-1" not in {result.fields["id"] for result in after_id_delete}

        # delete_docs_by_filters is out-of-scope for Step 1 (it stays dict-typed in
        # ES backend until Step 8). Only the search() path was migrated to FilterGroup.
        await es_vector_store.delete_docs_by_filters(collection_name, {"category": "vehicle"})
        after_filter_delete = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[0.0, 1.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=5,
            output_fields=["id", "category"],
        )
        assert all(result.fields["category"] != "vehicle" for result in after_filter_delete)

    finally:
        if await es_vector_store.collection_exists(collection_name):
            await es_vector_store.delete_collection(collection_name)
        assert await es_vector_store.collection_exists(collection_name) is False


@pytest.mark.asyncio
async def test_update_schema_restores_collection_after_recreate_write_failure(es_vector_store, monkeypatch):
    collection_name = f"es_vec_migrate_it_{uuid.uuid4().hex[:12]}"
    schema = _build_schema()
    original_add_docs = es_vector_store.add_docs
    write_to_original_after_delete = False
    failed_original_rewrite = False

    async def fail_original_rewrite(target_collection_name, docs, **kwargs):
        nonlocal failed_original_rewrite
        if write_to_original_after_delete and target_collection_name == collection_name and not failed_original_rewrite:
            failed_original_rewrite = True
            raise RuntimeError("simulated rewrite failure")
        await original_add_docs(target_collection_name, docs, **kwargs)

    try:
        await es_vector_store.create_collection(collection_name, schema, distance_metric="COSINE")
        await es_vector_store.add_docs(
            collection_name,
            [{
                "id": "doc-restore",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "text": "restore target",
                "category": "migration",
                "score_value": 1.0,
                "metadata": {},
            }],
        )

        monkeypatch.setattr(es_vector_store, "add_docs", fail_original_rewrite)
        operation = AddScalarFieldOperation(
            metadata=OperationMetadata(schema_version=1, description="add restore flag"),
            data_type="test",
            field_name="restore_flag",
            field_type="string",
            default_value="restored",
        )
        original_delete_collection = es_vector_store.delete_collection

        async def track_original_delete(target_collection_name, **kwargs):
            nonlocal write_to_original_after_delete
            await original_delete_collection(target_collection_name, **kwargs)
            if target_collection_name == collection_name:
                write_to_original_after_delete = True

        monkeypatch.setattr(es_vector_store, "delete_collection", track_original_delete)

        with pytest.raises(RuntimeError, match="simulated rewrite failure"):
            await es_vector_store.update_schema(collection_name, [operation])

        assert await es_vector_store.collection_exists(collection_name)
        restored_results = await es_vector_store.search(
            collection_name=collection_name,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            vector_field="embedding",
            top_k=1,
            output_fields=["id", "restore_flag"],
        )
        assert len(restored_results) == 1
        assert restored_results[0].fields["id"] == "doc-restore"
        assert restored_results[0].fields["restore_flag"] == "restored"

    finally:
        if await es_vector_store.collection_exists(collection_name):
            await es_vector_store.delete_collection(collection_name)
