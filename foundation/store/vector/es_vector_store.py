# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import time
from typing import Any, Dict, List, Optional, Union

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from common.exception.codes import StatusCode
from common.exception.errors import build_error
from common.logging import LogEventType, store_logger
from foundation.store.base_vector_store import (
    BaseVectorStore,
    CollectionSchema,
    FieldSchema,
    VectorDataType,
    VectorSearchResult,
)
from foundation.store.vector.utils import compute_new_schema, build_transform_func_for_operations
from memory_core.migration.operation.base_operation import BaseOperation

_ES_SIMILARITY_MAP = {
    "COSINE": "cosine",
    "L2": "l2_norm",
    "IP": "dot_product",
}

_METADATA_DOC_ID = "__collection_metadata__"


class ElasticsearchVectorStore(BaseVectorStore):
    """Elasticsearch vector store implementation."""

    def __init__(
        self,
        es: Optional[AsyncElasticsearch] = None,
        hosts: Optional[Union[str, List[str]]] = None,
        index_prefix: str = "agent_vector",
        **kwargs: Any,
    ):
        self._es = es or AsyncElasticsearch(hosts=hosts or "http://localhost:9200", **kwargs)
        self._index_prefix = index_prefix
        self._metadata_cache: Dict[str, Dict[str, Any]] = {}

    async def close(self):
        await self._es.close()
        self._metadata_cache.clear()
        store_logger.info(
            "Elasticsearch connection closed",
            event_type=LogEventType.STORE_DELETE,
        )

    def _index_name(self, collection_name: str) -> str:
        return f"{self._index_prefix}__{collection_name}"

    @staticmethod
    def _map_es_type(field: FieldSchema) -> Dict[str, Any]:
        dtype = field.dtype
        if dtype == VectorDataType.FLOAT_VECTOR:
            return {"type": "dense_vector", "dims": field.dim}
        if dtype == VectorDataType.VARCHAR:
            return {"type": "keyword"}
        if dtype == VectorDataType.INT64:
            return {"type": "long"}
        if dtype in (VectorDataType.INT32, VectorDataType.INT16, VectorDataType.INT8):
            return {"type": "integer"}
        if dtype == VectorDataType.FLOAT:
            return {"type": "float"}
        if dtype == VectorDataType.DOUBLE:
            return {"type": "double"}
        if dtype == VectorDataType.BOOL:
            return {"type": "boolean"}
        if dtype in (VectorDataType.JSON, VectorDataType.ARRAY):
            return {"type": "object", "enabled": False}
        return {"type": "keyword"}

    @staticmethod
    def _map_es_type_to_our_type(es_type: str) -> VectorDataType:
        type_mapping = {
            "keyword": VectorDataType.VARCHAR,
            "text": VectorDataType.VARCHAR,
            "dense_vector": VectorDataType.FLOAT_VECTOR,
            "long": VectorDataType.INT64,
            "integer": VectorDataType.INT32,
            "short": VectorDataType.INT16,
            "byte": VectorDataType.INT8,
            "float": VectorDataType.FLOAT,
            "double": VectorDataType.DOUBLE,
            "boolean": VectorDataType.BOOL,
            "object": VectorDataType.JSON,
        }
        es_type_lower = es_type.lower()
        if es_type_lower in type_mapping:
            return type_mapping[es_type_lower]
        store_logger.warning(
            f"Unsupported ES data type: {es_type}, defaulting to VARCHAR",
            event_type=LogEventType.STORE_RETRIEVE,
        )
        return VectorDataType.VARCHAR

    def _build_mappings(self, schema: CollectionSchema, distance_metric: str) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        for field in schema.fields:
            if field.dtype == VectorDataType.FLOAT_VECTOR:
                similarity = _ES_SIMILARITY_MAP.get(distance_metric.upper(), "cosine")
                properties[field.name] = {
                    "type": "dense_vector",
                    "dims": field.dim,
                    "index": True,
                    "similarity": similarity,
                }
            else:
                properties[field.name] = self._map_es_type(field)
        properties["_meta"] = {"type": "object", "enabled": False}
        return {"dynamic": "strict", "properties": properties}

    async def _store_metadata(self, index_name: str, metadata: Dict[str, Any]) -> None:
        try:
            await self._es.index(index=index_name, id=_METADATA_DOC_ID, body={"_meta": metadata}, refresh=True)
        except Exception as e:
            store_logger.warning(
                "Failed to store collection metadata",
                event_type=LogEventType.STORE_UPDATE,
                table_name=index_name,
                exception=str(e),
            )

    async def _load_metadata(self, index_name: str) -> Dict[str, Any]:
        if index_name in self._metadata_cache:
            return self._metadata_cache[index_name]
        try:
            resp = await self._es.get(index=index_name, id=_METADATA_DOC_ID)
            if resp.get("found", False):
                meta = resp["_source"].get("_meta", {})
                self._metadata_cache[index_name] = meta
                return meta
        except Exception as e:
            store_logger.warning(
                "Failed to load collection metadata",
                event_type=LogEventType.STORE_RETRIEVE,
                table_name=index_name,
                exception=str(e),
            )
        return {}

    async def create_collection(
        self,
        collection_name: str,
        schema: Union[CollectionSchema, Dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        index_name = self._index_name(collection_name)
        distance_metric = kwargs.get("distance_metric", "COSINE").upper()

        if isinstance(schema, dict):
            schema = CollectionSchema.from_dict(schema)

        vector_fields = schema.get_vector_fields()
        if not vector_fields:
            raise build_error(
                StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                error_msg="schema must contain at least one FLOAT_VECTOR field",
            )

        primary_key_field = schema.get_primary_key_field()
        vector_field = vector_fields[0]
        metadata = {
            "schema": schema.to_dict(),
            "distance_metric": distance_metric,
            "vector_field": vector_field.name,
            "vector_dim": vector_field.dim,
            "schema_version": 0,
            "collection_name": collection_name,
        }
        if primary_key_field:
            metadata["primary_key_field"] = primary_key_field.name

        try:
            exists = await self._es.indices.exists(index=index_name)
            if exists.body if hasattr(exists, "body") else exists:
                current = await self._load_metadata(index_name)
                if not current:
                    await self._store_metadata(index_name, metadata)
                    self._metadata_cache[index_name] = metadata
                store_logger.info(
                    "Collection index already exists, skipping creation",
                    event_type=LogEventType.STORE_ADD,
                    table_name=collection_name,
                )
                return
        except Exception as e:
            store_logger.debug(
                "Failed to check if collection index exists",
                event_type=LogEventType.STORE_ADD,
                table_name=collection_name,
                exception=str(e),
            )

        try:
            await self._es.indices.create(index=index_name,
                                          body={"mappings": self._build_mappings(schema, distance_metric)})
        except Exception as e:
            err_str = str(e)
            if "resource_already_exists_exception" in err_str:
                await self._store_metadata(index_name, metadata)
                self._metadata_cache[index_name] = metadata
                store_logger.info(
                    "Collection index already exists (race)",
                    event_type=LogEventType.STORE_ADD,
                    table_name=collection_name,
                )
                return
            store_logger.error(
                "Failed to create collection index",
                event_type=LogEventType.STORE_ADD,
                table_name=collection_name,
                exception=str(e),
            )
            raise

        await self._store_metadata(index_name, metadata)
        self._metadata_cache[index_name] = metadata

        store_logger.info(
            f"Created collection with {len(schema.fields)} fields",
            event_type=LogEventType.STORE_ADD,
            table_name=collection_name,
        )

    async def delete_collection(self, collection_name: str, **kwargs: Any) -> None:
        index_name = self._index_name(collection_name)
        try:
            exists = await self._es.indices.exists(index=index_name)
            if not (exists.body if hasattr(exists, "body") else exists):
                store_logger.warning(
                    "Collection does not exist",
                    event_type=LogEventType.STORE_DELETE,
                    table_name=collection_name,
                )
                return
            await self._es.indices.delete(index=index_name)
            self._metadata_cache.pop(index_name, None)
            store_logger.info(
                "Deleted collection",
                event_type=LogEventType.STORE_DELETE,
                table_name=collection_name,
            )
        except Exception as e:
            store_logger.error(
                "Failed to delete collection",
                event_type=LogEventType.STORE_DELETE,
                table_name=collection_name,
                exception=str(e),
            )
            raise

    async def collection_exists(self, collection_name: str, **kwargs: Any) -> bool:
        try:
            resp = await self._es.indices.exists(index=self._index_name(collection_name))
            return bool(resp.body if hasattr(resp, "body") else resp)
        except Exception:
            return False

    async def get_schema(self, collection_name: str, **kwargs: Any) -> CollectionSchema:
        index_name = self._index_name(collection_name)
        meta = await self._load_metadata(index_name)
        schema_dict = meta.get("schema")
        if schema_dict:
            return CollectionSchema.from_dict(schema_dict)

        try:
            resp = await self._es.indices.get_mapping(index=index_name)
            mappings = resp[index_name]["mappings"]
            fields = []
            for fname, fdef in mappings.get("properties", {}).items():
                if fname == "_meta":
                    continue
                dtype = self._map_es_type_to_our_type(fdef.get("type", "keyword"))
                dim = fdef.get("dims") if dtype == VectorDataType.FLOAT_VECTOR else None
                fields.append(FieldSchema(name=fname, dtype=dtype, dim=dim))

            pk_field = kwargs.get("primary_key_field") or meta.get("primary_key_field") or "id"
            for field in fields:
                if field.name == pk_field:
                    field.is_primary = True
                    break

            return CollectionSchema(fields=fields, description=f"Collection '{collection_name}'")
        except Exception as e:
            raise build_error(
                StatusCode.STORE_VECTOR_COLLECTION_NOT_FOUND,
                collection_name=collection_name,
                error_msg=str(e),
            ) from e

    async def add_docs(self, collection_name: str, docs: List[Dict[str, Any]], **kwargs: Any) -> None:
        if not docs:
            return

        index_name = self._index_name(collection_name)
        meta = await self._load_metadata(index_name)
        schema_dict = meta.get("schema", {})
        primary_key_field = meta.get("primary_key_field") or _get_primary_key_field(schema_dict)
        batch_size = kwargs.get("batch_size", 500)
        if batch_size <= 0:
            batch_size = 500

        actions = []
        for doc in docs:
            doc_id = doc.get(primary_key_field) if primary_key_field else doc.get("id")
            action = {
                "_index": index_name,
                "_source": {key: value for key, value in doc.items() if value is not None},
            }
            if doc_id is not None:
                action["_id"] = str(doc_id)
            actions.append(action)

        for i in range(0, len(actions), batch_size):
            _, errors = await async_bulk(self._es, actions[i:i + batch_size], refresh=False, raise_on_error=False)
            if errors:
                store_logger.error(
                    f"Bulk insert had {len(errors)} errors",
                    event_type=LogEventType.STORE_ADD,
                    table_name=collection_name,
                )
                raise build_error(
                    StatusCode.STORE_VECTOR_DOC_INVALID,
                    error_msg=f"bulk insert failed with {len(errors)} errors",
                    details=errors,
                )

        try:
            await self._es.indices.refresh(index=index_name)
        except Exception as e:
            store_logger.debug(
                "Failed to refresh index after bulk insert",
                event_type=LogEventType.STORE_ADD,
                table_name=collection_name,
                exception=str(e),
            )

        store_logger.info(
            "Successfully added documents to collection",
            event_type=LogEventType.STORE_ADD,
            table_name=collection_name,
            data_num=len(docs),
        )

    async def search(
        self,
        collection_name: str,
        query_vector: List[float],
        vector_field: str,
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[VectorSearchResult]:
        index_name = self._index_name(collection_name)
        num_candidates = kwargs.get("num_candidates", max(top_k * 10, 100))
        output_fields = kwargs.get("output_fields")

        knn_clause: Dict[str, Any] = {
            "field": vector_field,
            "query_vector": query_vector,
            "k": top_k,
            "num_candidates": num_candidates,
        }

        if filters:
            filter_parts = []
            for key, value in filters.items():
                if isinstance(value, (list, tuple)):
                    filter_parts.append({"terms": {key: list(value)}})
                else:
                    filter_parts.append({"term": {key: value}})
            knn_clause["filter"] = {"bool": {"filter": filter_parts}}

        body: Dict[str, Any] = {"knn": knn_clause, "size": top_k, "_source": {"excludes": ["_meta"]}}
        if output_fields:
            body["_source"] = {"includes": output_fields, "excludes": ["_meta"]}

        try:
            resp = await self._es.search(index=index_name, body=body)
        except Exception as e:
            store_logger.error(
                "Vector search failed",
                event_type=LogEventType.STORE_RETRIEVE,
                table_name=collection_name,
                exception=str(e),
            )
            raise

        search_results: List[VectorSearchResult] = []
        for hit in resp.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            source.pop("_meta", None)
            if "id" not in source and "_id" in hit:
                source["id"] = hit["_id"]
            search_results.append(VectorSearchResult(score=float(hit.get("_score", 0.0)), fields=source))

        return search_results

    async def delete_docs_by_ids(self, collection_name: str, ids: List[str], **kwargs: Any) -> None:
        if not ids:
            return
        index_name = self._index_name(collection_name)
        batch_size = kwargs.get("batch_size", 500)
        if batch_size <= 0:
            batch_size = 500

        actions = [{"_op_type": "delete", "_index": index_name, "_id": doc_id} for doc_id in ids]
        for i in range(0, len(actions), batch_size):
            await async_bulk(self._es, actions[i:i + batch_size], refresh=True, raise_on_error=False)

        store_logger.info(
            "Deleted documents from collection",
            event_type=LogEventType.STORE_DELETE,
            table_name=collection_name,
            data_num=len(ids),
        )

    async def delete_docs_by_filters(self, collection_name: str, filters: Dict[str, Any], **kwargs: Any) -> None:
        if not filters:
            return

        filter_parts = []
        for key, value in filters.items():
            if isinstance(value, (list, tuple)):
                filter_parts.append({"terms": {key: list(value)}})
            else:
                filter_parts.append({"term": {key: value}})
        query = {"bool": {"filter": filter_parts}}

        try:
            resp = await self._es.delete_by_query(index=self._index_name(collection_name),
                                                  body={"query": query}, refresh=True)
            store_logger.info(
                "Deleted documents matching filters",
                event_type=LogEventType.STORE_DELETE,
                table_name=collection_name,
                data_num=resp.get("deleted", 0),
            )
        except Exception as e:
            store_logger.error(
                "Failed to delete docs by filters",
                event_type=LogEventType.STORE_DELETE,
                table_name=collection_name,
                exception=str(e),
            )
            raise

    async def list_collection_names(self) -> List[str]:
        prefix = f"{self._index_prefix}__"
        try:
            resp = await self._es.indices.get(index=f"{prefix}*")
            return [idx[len(prefix):] for idx in resp if idx.startswith(prefix)]
        except Exception:
            return []

    async def get_collection_metadata(self, collection_name: str) -> Dict[str, Any]:
        meta = await self._load_metadata(self._index_name(collection_name))
        meta.setdefault("distance_metric", "COSINE")
        meta.setdefault("schema_version", 0)
        return meta

    async def update_collection_metadata(self, collection_name: str, metadata: Dict[str, Any]) -> None:
        if not metadata:
            return

        if "schema_version" in metadata:
            version = metadata["schema_version"]
            if not isinstance(version, int) or version < 0:
                raise build_error(
                    StatusCode.STORE_VECTOR_SCHEMA_INVALID,
                    error_msg=f"schema_version must be a non-negative integer, got {version}",
                )

        index_name = self._index_name(collection_name)
        current = await self._load_metadata(index_name)
        current.update(metadata)
        await self._store_metadata(index_name, current)
        self._metadata_cache[index_name] = current

        store_logger.debug(
            f"Updated collection metadata for '{collection_name}'",
            event_type=LogEventType.STORE_UPDATE,
            table_name=collection_name,
        )

    async def update_schema(self, collection_name: str, operations: List[BaseOperation]) -> None:
        if not operations:
            return

        old_schema = await self.get_schema(collection_name)
        new_schema = compute_new_schema(old_schema, operations)
        transform_func = build_transform_func_for_operations(operations)
        metadata = await self.get_collection_metadata(collection_name)

        temp_collection_name = f"{collection_name}_migration_{int(time.time())}"
        store_logger.info(
            f"Starting migration for '{collection_name}'. Temp collection: '{temp_collection_name}'.",
            event_type=LogEventType.STORE_UPDATE,
            table_name=collection_name,
        )

        old_collection_deleted = False
        try:
            await self.create_collection(
                temp_collection_name,
                new_schema,
                distance_metric=metadata.get("distance_metric", "COSINE"),
            )

            old_docs = await self._get_all_documents(collection_name)
            if old_docs:
                await self.add_docs(temp_collection_name, [transform_func(doc) for doc in old_docs])
            temp_docs = await self._get_all_documents(temp_collection_name)

            await self.delete_collection(collection_name)
            old_collection_deleted = True
            await self.create_collection(
                collection_name,
                new_schema,
                distance_metric=metadata.get("distance_metric", "COSINE"),
            )
            if temp_docs:
                await self.add_docs(collection_name, temp_docs)
            await self.delete_collection(temp_collection_name)

            store_logger.info(
                f"Migration for '{collection_name}' completed successfully.",
                event_type=LogEventType.STORE_UPDATE,
                table_name=collection_name,
            )
        except Exception as e:
            store_logger.error(
                f"Migration for '{collection_name}' failed: {e}.",
                event_type=LogEventType.STORE_UPDATE,
                table_name=collection_name,
                exception=str(e),
            )
            if old_collection_deleted and await self.collection_exists(temp_collection_name):
                try:
                    await self._restore_collection_from_temp(collection_name,
                                                             temp_collection_name, new_schema, metadata)
                    await self.delete_collection(temp_collection_name)
                except Exception as restore_error:
                    store_logger.error(
                        f"Failed to restore collection '{collection_name}' from temp collection.",
                        event_type=LogEventType.STORE_UPDATE,
                        table_name=collection_name,
                        exception=str(restore_error),
                    )
            elif await self.collection_exists(temp_collection_name):
                await self.delete_collection(temp_collection_name)
            raise

    async def _restore_collection_from_temp(
        self,
        collection_name: str,
        temp_collection_name: str,
        schema: CollectionSchema,
        metadata: Dict[str, Any],
    ) -> None:
        if await self.collection_exists(collection_name):
            await self.delete_collection(collection_name)
        await self.create_collection(
            collection_name,
            schema,
            distance_metric=metadata.get("distance_metric", "COSINE"),
        )
        temp_docs = await self._get_all_documents(temp_collection_name)
        if temp_docs:
            await self.add_docs(collection_name, temp_docs)

    async def _get_all_documents(self, collection_name: str, batch_size: int = 1000) -> List[Dict[str, Any]]:
        index_name = self._index_name(collection_name)
        meta = await self._load_metadata(index_name)
        primary_key_field = meta.get("primary_key_field") or _get_primary_key_field(meta.get("schema", {})) or "id"
        if batch_size <= 0:
            batch_size = 1000
        body = {
            "query": {"bool": {"must_not": [{"term": {"_id": _METADATA_DOC_ID}}]}},
            "size": batch_size,
        }
        docs = []
        scroll_id = None
        try:
            resp = await self._es.search(index=index_name, body=body, scroll="2m")
            scroll_id = resp.get("_scroll_id")
            while True:
                hits = resp.get("hits", {}).get("hits", [])
                if not hits:
                    break
                for hit in hits:
                    source = dict(hit.get("_source", {}))
                    source.pop("_meta", None)
                    if primary_key_field not in source:
                        source[primary_key_field] = hit.get("_id")
                    docs.append(source)
                resp = await self._es.scroll(scroll_id=scroll_id, scroll="2m")
                scroll_id = resp.get("_scroll_id")
        finally:
            if scroll_id:
                await self._es.clear_scroll(scroll_id=scroll_id)
        return docs


def _get_primary_key_field(schema_dict: Dict[str, Any]) -> Optional[str]:
    for field in schema_dict.get("fields", []):
        if field.get("is_primary"):
            return field.get("name")
    return None
