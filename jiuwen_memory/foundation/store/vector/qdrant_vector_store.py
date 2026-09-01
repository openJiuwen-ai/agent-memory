# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Qdrant vector-store adapter."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.foundation.store.base_vector_store import (
    BaseVectorStore,
    CollectionSchema,
    VectorSearchResult,
)
from jiuwen_memory.foundation.store.filter_dsl import FilterGroup, FilterLogic, FilterOperator
from jiuwen_memory.foundation.store.vector.utils import (
    build_transform_func_for_operations,
    compute_new_schema,
    convert_cosine_similarity,
    convert_ip_similarity,
)
from jiuwen_memory.memory_core.migration.operation.base_operation import BaseOperation


_META_KEY = "__jiuwen_collection_metadata__"
_META_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, _META_KEY))
_DISTANCES = {
    "COSINE": models.Distance.COSINE,
    "L2": models.Distance.EUCLID,
    "EUCLID": models.Distance.EUCLID,
    "EUCLIDEAN": models.Distance.EUCLID,
    "IP": models.Distance.DOT,
    "DOT": models.Distance.DOT,
}


class QdrantVectorStore(BaseVectorStore):
    """Qdrant backend with persistent JiuwenMemory schema metadata."""

    def __init__(
        self,
        client: AsyncQdrantClient | None = None,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection_prefix: str = "agent_vector",
        prefer_grpc: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ):
        self._client = client or AsyncQdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
            timeout=timeout,
            **kwargs,
        )
        self._prefix = collection_prefix.strip("_")
        self._metadata: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self._client.close()
        self._metadata.clear()

    def _name(self, collection: str) -> str:
        return f"{self._prefix}__{collection}" if self._prefix else collection

    @staticmethod
    def _point_id(collection: str, value: Any) -> str:
        # Qdrant only allows uint64/UUID IDs.
        # Ref: https://qdrant.tech/documentation/manage-data/points/#point-ids
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection}\0{value}"))

    @staticmethod
    def _filter(values: dict[str, Any] | FilterGroup | None = None) -> models.Filter:
        if isinstance(values, FilterGroup):
            conditions = [QdrantVectorStore._filter_group(values)]
        else:
            conditions = [QdrantVectorStore._field_condition(key, value) for key, value in (values or {}).items()]
        return models.Filter(
            must=conditions or None,
            must_not=[models.FieldCondition(key=_META_KEY, match=models.MatchValue(value=True))],
        )

    @staticmethod
    def _field_condition(key: str, value: Any) -> models.Condition:
        if value is None:
            return models.IsEmptyCondition(is_empty=models.PayloadField(key=key))
        match = (
            models.MatchAny(any=list(value))
            if isinstance(value, (list, tuple, set))
            else models.MatchValue(value=value)
        )
        return models.FieldCondition(key=key, match=match)

    @staticmethod
    def _filter_group(group: FilterGroup) -> models.Filter:
        if not group.conditions:
            raise build_error(
                StatusCode.MEMORY_FILTER_FORMAT_ERROR,
                error_msg="FilterGroup.conditions must be non-empty",
            )
        conditions = []
        for condition in group.conditions:
            if isinstance(condition, FilterGroup):
                rendered = QdrantVectorStore._filter_group(condition)
            else:
                field = QdrantVectorStore._field_condition(condition.field, condition.value)
                rendered = models.Filter(must_not=[field]) if condition.op == FilterOperator.NE else field
            conditions.append(rendered)
        if group.logic == FilterLogic.OR:
            return models.Filter(should=conditions)
        return models.Filter(must=conditions)

    @staticmethod
    def _schema(schema: CollectionSchema | dict[str, Any]) -> CollectionSchema:
        return CollectionSchema.from_dict(schema) if isinstance(schema, dict) else schema

    @staticmethod
    def _distance(metric: str) -> models.Distance:
        try:
            return _DISTANCES[metric.upper()]
        except KeyError as exc:
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg=f"Unsupported Qdrant distance metric: {metric}",
            ) from exc

    @staticmethod
    def _score(value: float, metric: str) -> float:
        metric = metric.upper()
        if metric == "COSINE":
            return convert_cosine_similarity(value)
        if metric in {"IP", "DOT"}:
            return convert_ip_similarity(value)
        return 1.0 / (1.0 + value)

    @staticmethod
    def _vector_fields(schema: CollectionSchema) -> dict[str, int]:
        return {field.name: field.dim for field in schema.get_vector_fields()}

    async def _assert_compatible(
        self,
        collection_name: str,
        schema: CollectionSchema,
        metric: str,
    ) -> bool:
        metadata = await self.get_collection_metadata(collection_name)
        if metadata.get("schema"):
            compatible = metadata["schema"] == schema.to_dict() and self._distance(
                metadata.get("distance_metric", "COSINE")
            ) == self._distance(metric)
        else:
            configured = (await self._client.get_collection(self._name(collection_name))).config.params.vectors
            expected = {
                name: (dimension, self._distance(metric)) for name, dimension in self._vector_fields(schema).items()
            }
            actual = (
                {name: (params.size, params.distance) for name, params in configured.items()}
                if isinstance(configured, dict)
                else {}
            )
            compatible = actual == expected
        if not compatible:
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg=f"Qdrant collection '{collection_name}' already exists with an incompatible schema",
            )
        return bool(metadata.get("schema"))

    async def create_collection(
        self,
        collection_name: str,
        schema: CollectionSchema | dict[str, Any],
        **kwargs: Any,
    ) -> None:
        schema = self._schema(schema)
        primary = schema.get_primary_key_field()
        vectors = self._vector_fields(schema)
        if primary is None:
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg="schema must contain a primary key field",
            )
        if not vectors:
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg="schema must contain at least one FLOAT_VECTOR field",
            )

        metric = kwargs.pop("distance_metric", "COSINE").upper()
        distance = self._distance(metric)
        metadata = {
            "schema": schema.to_dict(),
            "distance_metric": metric,
            "primary_key_field": primary.name,
            "vector_field": next(iter(vectors)),
            "vector_dim": next(iter(vectors.values())),
            "schema_version": 0,
            "collection_name": collection_name,
        }
        if not await self.collection_exists(collection_name):
            try:
                await self._client.create_collection(
                    collection_name=self._name(collection_name),
                    vectors_config={
                        name: models.VectorParams(size=dimension, distance=distance)
                        for name, dimension in vectors.items()
                    },
                    **kwargs,
                )
            except UnexpectedResponse as exc:
                if exc.status_code != 409:
                    raise
        if not await self._assert_compatible(collection_name, schema, metric):
            await self._write_metadata(collection_name, metadata)

    async def delete_collection(self, collection_name: str, **kwargs: Any) -> None:
        if await self.collection_exists(collection_name):
            await self._client.delete_collection(self._name(collection_name), **kwargs)
        self._metadata.pop(collection_name, None)

    async def collection_exists(self, collection_name: str, **kwargs: Any) -> bool:
        return await self._client.collection_exists(self._name(collection_name))

    async def get_schema(self, collection_name: str, **kwargs: Any) -> CollectionSchema:
        metadata = await self.get_collection_metadata(collection_name)
        return self._schema_from_metadata(collection_name, metadata)

    @staticmethod
    def _schema_from_metadata(collection_name: str, metadata: dict[str, Any]) -> CollectionSchema:
        schema = metadata.get("schema")
        if not schema:
            raise build_error(
                StatusCode.STORE_VECTOR_COLLECTION_NOT_FOUND,
                collection_name=collection_name,
                error_msg="Qdrant collection schema metadata is missing",
            )
        return CollectionSchema.from_dict(schema)

    async def add_docs(self, collection_name: str, docs: list[dict[str, Any]], **kwargs: Any) -> None:
        if not docs:
            return
        metadata = await self.get_collection_metadata(collection_name)
        schema = self._schema_from_metadata(collection_name, metadata)
        primary = schema.get_primary_key_field()
        vector_fields = set(self._vector_fields(schema))
        points = []
        for doc in docs:
            key = doc.get(primary.name)
            if key is None:
                if not primary.auto_id:
                    raise build_error(
                        StatusCode.STORE_VECTOR_DOC_INVALID,
                        collection_name=collection_name,
                        error_msg=f"document is missing primary key field '{primary.name}'",
                    )
                key = str(uuid.uuid4())
            missing = {name for name in vector_fields if doc.get(name) is None}
            if missing:
                raise build_error(
                    StatusCode.STORE_VECTOR_DOC_INVALID,
                    collection_name=collection_name,
                    error_msg=f"document is missing vector fields: {sorted(missing)}",
                )
            payload = {name: value for name, value in doc.items() if name not in vector_fields and value is not None}
            payload[primary.name] = key
            points.append(
                models.PointStruct(
                    id=self._point_id(collection_name, key),
                    vector={name: doc[name] for name in vector_fields},
                    payload=payload,
                )
            )
        await self._client.upsert(self._name(collection_name), points=points, wait=kwargs.get("wait", True))

    async def search(
        self,
        collection_name: str,
        query_vector: list[float],
        vector_field: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[VectorSearchResult]:
        metadata = await self.get_collection_metadata(collection_name)
        output_fields = kwargs.get("output_fields")
        vector_fields = set(self._vector_fields(self._schema_from_metadata(collection_name, metadata)))
        include_vectors = output_fields is None or bool(vector_fields.intersection(output_fields))
        response = await self._client.query_points(
            self._name(collection_name),
            query=query_vector,
            using=vector_field,
            query_filter=self._filter(filters),
            limit=top_k,
            with_payload=True,
            with_vectors=include_vectors,
        )
        results = []
        for point in response.points:
            fields = dict(point.payload or {})
            if include_vectors and isinstance(point.vector, dict):
                fields.update({key: value for key, value in point.vector.items() if key in vector_fields})
            if output_fields:
                fields = {key: fields[key] for key in output_fields if key in fields}
            results.append(
                VectorSearchResult(score=self._score(point.score, metadata["distance_metric"]), fields=fields)
            )
        return results

    async def list_docs(
        self,
        collection_name: str,
        filters: FilterGroup | None = None,
        limit: int = 100,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        output_fields = kwargs.get("output_fields")
        metadata = await self.get_collection_metadata(collection_name)
        vector_fields = set(self._vector_fields(self._schema_from_metadata(collection_name, metadata)))
        with_vectors = output_fields is None or bool(vector_fields.intersection(output_fields))
        records, _ = await self._client.scroll(
            self._name(collection_name),
            scroll_filter=self._filter(filters),
            limit=limit + offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        documents = []
        for record in records[offset:]:
            document = {**(record.payload or {}), **(record.vector or {})}
            if output_fields:
                document = {field: document.get(field) for field in output_fields if field in document}
            documents.append(document)
        return documents

    async def update_doc_fields(
        self,
        collection_name: str,
        doc_id: str,
        fields: dict[str, Any],
    ) -> None:
        if not doc_id:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg="doc_id is required for update_doc_fields",
            )
        if not fields:
            return
        point_id = self._point_id(collection_name, doc_id)
        records = await self._client.retrieve(
            self._name(collection_name), ids=[point_id], with_payload=False, with_vectors=False
        )
        if not records:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg=f"doc not found in collection, doc_id={doc_id}",
            )
        await self._client.set_payload(
            self._name(collection_name),
            payload=fields,
            points=[point_id],
            wait=True,
        )

    async def delete_docs_by_ids(self, collection_name: str, ids: list[str], **kwargs: Any) -> None:
        if ids:
            await self._client.delete(
                self._name(collection_name),
                points_selector=models.PointIdsList(points=[self._point_id(collection_name, value) for value in ids]),
                wait=kwargs.get("wait", True),
            )

    async def delete_docs_by_filters(self, collection_name: str, filters: dict[str, Any], **kwargs: Any) -> None:
        if filters:
            await self._client.delete(
                self._name(collection_name),
                points_selector=models.FilterSelector(filter=self._filter(filters)),
                wait=kwargs.get("wait", True),
            )

    async def list_collection_names(self) -> list[str]:
        prefix = f"{self._prefix}__" if self._prefix else ""
        collections = (await self._client.get_collections()).collections
        return [item.name.removeprefix(prefix) for item in collections if not prefix or item.name.startswith(prefix)]

    async def _write_metadata(self, collection_name: str, metadata: dict[str, Any]) -> None:
        vectors = self._vector_fields(CollectionSchema.from_dict(metadata["schema"]))
        await self._client.upsert(
            self._name(collection_name),
            points=[
                models.PointStruct(
                    id=_META_ID,
                    vector={name: [0.0] * dimension for name, dimension in vectors.items()},
                    payload={_META_KEY: True, "metadata": metadata},
                )
            ],
            wait=True,
        )
        self._metadata[collection_name] = dict(metadata)

    async def get_collection_metadata(self, collection_name: str) -> dict[str, Any]:
        if collection_name in self._metadata:
            return dict(self._metadata[collection_name])
        records = await self._client.retrieve(
            self._name(collection_name), ids=[_META_ID], with_payload=True, with_vectors=False
        )
        metadata = dict(records[0].payload.get("metadata", {})) if records else {}
        if metadata:
            self._metadata[collection_name] = metadata
        return dict(metadata)

    async def update_collection_metadata(self, collection_name: str, metadata: dict[str, Any]) -> None:
        if not metadata:
            return
        version = metadata.get("schema_version")
        if version is not None and (not isinstance(version, int) or version < 0):
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg=f"schema_version must be a non-negative integer, got {version}",
            )
        current = await self.get_collection_metadata(collection_name)
        current.update(metadata)
        await self._write_metadata(collection_name, current)

    async def _documents(self, collection_name: str, batch_size: int = 256) -> list[dict[str, Any]]:
        documents, offset = [], None
        while True:
            records, offset = await self._client.scroll(
                self._name(collection_name),
                scroll_filter=self._filter(),
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for record in records:
                documents.append({**(record.payload or {}), **(record.vector or {})})
            if offset is None:
                return documents

    async def _replace(
        self,
        collection_name: str,
        schema: CollectionSchema,
        documents: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        if await self.collection_exists(collection_name):
            await self.delete_collection(collection_name)
        await self.create_collection(collection_name, schema, distance_metric=metadata["distance_metric"])
        await self.add_docs(collection_name, documents)
        await self.update_collection_metadata(
            collection_name,
            {key: value for key, value in metadata.items() if key not in {"schema", "collection_name"}},
        )

    async def update_schema(self, collection_name: str, operations: list[BaseOperation]) -> None:
        if not operations:
            return
        old_schema = await self.get_schema(collection_name)
        old_metadata = await self.get_collection_metadata(collection_name)
        old_documents = await self._documents(collection_name)
        new_schema = compute_new_schema(old_schema, operations)
        transform = build_transform_func_for_operations(operations)
        new_documents = [transform(copy.deepcopy(document)) for document in old_documents]
        temp = f"{collection_name}_migration_{uuid.uuid4().hex}"
        replacement_started = False
        try:
            await self.create_collection(temp, new_schema, distance_metric=old_metadata["distance_metric"])
            await self.add_docs(temp, new_documents)
            migrated = await self._documents(temp)
            replacement_started = True
            await self._replace(collection_name, new_schema, migrated, old_metadata)
        except Exception as migration_error:
            if replacement_started:
                try:
                    await self._replace(collection_name, old_schema, old_documents, old_metadata)
                except Exception as rollback_error:
                    migration_error.add_note(f"Qdrant schema rollback failed: {rollback_error}")
            raise
        finally:
            if await self.collection_exists(temp):
                await self.delete_collection(temp)
