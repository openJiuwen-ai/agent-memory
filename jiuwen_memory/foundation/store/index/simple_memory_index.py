# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Backward-compatible memory index for legacy KV + Vector store data.

.. deprecated::
    This module provides ``SimpleMemoryIndex`` for operating on data created by
    the old SemanticStore + UserMemStore architecture.  It may be removed in a
    future release once migration is complete.

Legacy data layout
------------------
- KV store key:    ``UMD/{user_id}/{scope_id}/{mem_id}``
- KV store value:  JSON dict with ``id``, ``mem``, ``mem_type``, ``timestamp``, …
- Vector collection: ``uid_{user_id}_gid_{scope_id}_mtype_{mem_type}``
- Vector schema:   ``id`` (VARCHAR) + ``embedding`` (FLOAT_VECTOR)
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.codec import StorageCodec
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from jiuwen_memory.foundation.store.base_vector_store import (
    BaseVectorStore,
    CollectionSchema,
    FieldSchema,
    VectorDataType,
)
from jiuwen_memory.foundation.store.filter_dsl import FilterGroup


class SimpleMemoryIndex(BaseMemoryIndex):
    """
    Backward-compatible memory index for legacy KV + Vector store data.

    WARNING: This class exists solely for backward compatibility with data written
    by the old SemanticStore + UserMemStore architecture.  It may be removed in a
    future version.  Do not build new features or long-lived components on top of
    this class.
    """

    _KV_PREFIX = "UMD"
    _KV_SEP = "/"
    _IDS_SUFFIX = "ids"

    _BYTE_NUM_PER_ID = 24

    def __init__(
        self,
        kv_store: BaseKVStore,
        vector_store: BaseVectorStore,
        embedding_model: Any = None,
    ):
        self._kv_store = kv_store
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._created_collections: set[str] = set()
        self._schema_version = 0
        self._backups: dict[str, dict[str, Any]] = {}
        self._codec: StorageCodec | None = None

    def set_embedding_model(self, embedding_model: Any) -> None:
        self._embedding_model = embedding_model

    def set_storage_codec(self, codec: StorageCodec) -> None:
        self._codec = codec

    def _read_kv_value(self, raw: bytes | str | None) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return raw

    def _write_kv_value(self, text: str) -> bytes:
        return text.encode("utf-8")

    # ------------------------------------------------------------------ #
    #  KV helpers                                                         #
    # ------------------------------------------------------------------ #

    def _kv_mem_key(self, user_id: str, scope_id: str, mem_id: str) -> str:
        return f"{self._KV_PREFIX}{self._KV_SEP}{user_id}{self._KV_SEP}{scope_id}{self._KV_SEP}{mem_id}"

    def _kv_ids_key(self, user_id: str, scope_id: str, mem_type: str | None = None) -> str:
        if mem_type is None:
            return (f"{self._KV_PREFIX}{self._KV_SEP}{user_id}{self._KV_SEP}"
                    f"{scope_id}{self._KV_SEP}{self._IDS_SUFFIX}")
        return (f"{self._KV_PREFIX}{self._KV_SEP}{user_id}{self._KV_SEP}"
                f"{scope_id}{self._KV_SEP}{mem_type}{self._KV_SEP}{self._IDS_SUFFIX}")

    @staticmethod
    def _parse_all_ids(raw: str) -> list[str]:
        n = len(raw) // SimpleMemoryIndex._BYTE_NUM_PER_ID
        return [raw[i * SimpleMemoryIndex._BYTE_NUM_PER_ID:(i + 1) * SimpleMemoryIndex._BYTE_NUM_PER_ID]
                for i in range(n)]

    @staticmethod
    def _append_id(raw: str, mem_id: str) -> str:
        return raw + mem_id

    @staticmethod
    def _remove_id(raw: str, mem_id: str) -> str:
        bpid = SimpleMemoryIndex._BYTE_NUM_PER_ID
        total = len(raw) // bpid
        for i in range(total):
            s, e = i * bpid, (i + 1) * bpid
            if raw[s:e] == mem_id:
                return raw[:s] + raw[e:]
        return raw

    async def _add_id_to_tracking(
        self, user_id: str, scope_id: str, mem_id: str, mem_type: str,
    ) -> None:
        # Global IDs
        key = self._kv_ids_key(user_id, scope_id)
        val = self._read_kv_value(await self._kv_store.get(key)) or ""
        if mem_id not in self._parse_all_ids(val):
            await self._kv_store.set(key, self._write_kv_value(self._append_id(val, mem_id)))
        # Type-specific IDs
        tkey = self._kv_ids_key(user_id, scope_id, mem_type)
        tval = self._read_kv_value(await self._kv_store.get(tkey)) or ""
        if mem_id not in self._parse_all_ids(tval):
            await self._kv_store.set(tkey, self._write_kv_value(self._append_id(tval, mem_id)))

    async def _remove_id_from_tracking(
        self, user_id: str, scope_id: str, mem_id: str, mem_type: str | None,
    ) -> None:
        # Global IDs
        key = self._kv_ids_key(user_id, scope_id)
        val = self._read_kv_value(await self._kv_store.get(key)) or ""
        new_val = self._remove_id(val, mem_id)
        if new_val:
            await self._kv_store.set(key, self._write_kv_value(new_val))
        else:
            await self._kv_store.delete(key)
        if not mem_type:
            return
        # Type-specific IDs
        tkey = self._kv_ids_key(user_id, scope_id, mem_type)
        tval = self._read_kv_value(await self._kv_store.get(tkey)) or ""
        new_tval = self._remove_id(tval, mem_id)
        if new_tval:
            await self._kv_store.set(tkey, self._write_kv_value(new_tval))
        else:
            await self._kv_store.delete(tkey)

    # ------------------------------------------------------------------ #
    #  Data conversion                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _kv_data_to_memory_doc(data: dict[str, Any], mem_id: str) -> MemoryDoc:
        skip = {
            "id", "mem", "mem_type", "timestamp", "user_id", "scope_id",
            # Top-level scalar fields on MemoryDoc — pulled out below.
            "blacklisted", "is_important",
        }
        extra = {k: v for k, v in data.items() if k not in skip}

        ts_raw = data.get("timestamp", "")
        if isinstance(ts_raw, datetime):
            timestamp = ts_raw
        elif isinstance(ts_raw, str) and ts_raw:
            timestamp = None
            for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    timestamp = datetime.strptime(ts_raw, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if timestamp is None:
                try:
                    timestamp = datetime.fromisoformat(ts_raw)
                except ValueError:
                    timestamp = datetime.now(timezone.utc).astimezone()
        elif isinstance(ts_raw, (int, float)):
            timestamp = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc).astimezone()

        blacklisted = bool(data.get("blacklisted", False))
        is_important = bool(data.get("is_important", False))

        return MemoryDoc(
            id=mem_id,
            text=data.get("mem", ""),
            type=data.get("mem_type", ""),
            timestamp=timestamp,
            fields=extra,
            is_important=is_important,
            blacklisted=blacklisted,
        )

    @staticmethod
    def _memory_doc_to_kv_data(doc: MemoryDoc, user_id: str, scope_id: str) -> dict[str, Any]:
        ts = doc.timestamp.strftime("%Y-%m-%d %H-%M-%S") if doc.timestamp else datetime.now(
            timezone.utc).astimezone().strftime("%Y-%m-%d %H-%M-%S")
        return {
            "id": doc.id,
            "user_id": user_id,
            "scope_id": scope_id,
            "mem": doc.text,
            "mem_type": doc.type,
            "timestamp": ts,
            "blacklisted": doc.blacklisted,
            "is_important": doc.is_important,
            **doc.fields,
        }

    # ------------------------------------------------------------------ #
    #  Vector helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_collection_name(user_id: str, scope_id: str, mem_type: str) -> str:
        return f"uid_{user_id}_gid_{scope_id}_mtype_{mem_type}"

    @staticmethod
    def _parse_mem_type_from_collection(name: str) -> str | None:
        if "_mtype_" in name:
            return name.rsplit("_mtype_", 1)[-1]
        return None

    async def _ensure_collection(self, name: str, dim: int) -> None:
        if name in self._created_collections:
            return
        if await self._vector_store.collection_exists(name):
            self._created_collections.add(name)
            return
        schema = CollectionSchema(description="Semantic memory collection", enable_dynamic_field=False)
        schema.add_field(FieldSchema(name="id", dtype=VectorDataType.VARCHAR, max_length=256, is_primary=True))
        schema.add_field(FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=dim))
        schema.add_field(FieldSchema(name="blacklisted", dtype=VectorDataType.BOOL, default_value=False))
        schema.add_field(FieldSchema(name="is_important", dtype=VectorDataType.BOOL, default_value=False))
        await self._vector_store.create_collection(name, schema)
        self._created_collections.add(name)

    async def _collections_for(self, user_id: str, scope_id: str) -> list[str]:
        prefix = f"uid_{user_id}_gid_{scope_id}_mtype_"
        names = await self._vector_store.list_collection_names()
        return [n for n in names if n.startswith(prefix)]

    # ------------------------------------------------------------------ #
    #  BaseMemoryIndex implementation                                     #
    # ------------------------------------------------------------------ #

    async def add_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]) -> None:
        """Add or update memory documents (writes to both KV and vector stores)."""
        if not memories:
            return

        by_type: dict[str, list[MemoryDoc]] = {}
        for m in memories:
            by_type.setdefault(m.type, []).append(m)

        for mem_type, docs in by_type.items():
            col = self._get_collection_name(user_id, scope_id, mem_type)
            texts = [d.text for d in docs]

            if self._embedding_model:
                embeddings = await self._embedding_model.embed_documents(texts)
            else:
                memory_logger.error(
                    "Embedding model not initialized.",
                    event_type=LogEventType.MEMORY_STORE,
                    scope_id=scope_id,
                    metadata={"collection": col},
                )
                raise build_error(
                    StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                    memory_type="vector store",
                    error_msg="vector store failed: embedding model not initialized",
                )

            if embeddings:
                await self._ensure_collection(col, len(embeddings[0]))

            await self._vector_store.add_docs(
                col,
                [
                    {
                        "id": d.id,
                        "embedding": e,
                        "blacklisted": d.blacklisted,
                        "is_important": d.is_important,
                    }
                    for d, e in zip(docs, embeddings)
                ],
            )

            for doc in docs:
                kv_key = self._kv_mem_key(user_id, scope_id, doc.id)
                kv_data = self._memory_doc_to_kv_data(doc, user_id, scope_id)
                if self._codec is not None:
                    kv_data["mem"] = self._codec.encode(kv_data.get("mem", ""))
                await self._kv_store.set(
                    kv_key,
                    self._write_kv_value(json.dumps(kv_data)),
                )
                await self._add_id_to_tracking(user_id, scope_id, doc.id, mem_type)

    async def search(
        self,
        user_id: str,
        scope_id: str,
        query: str,
        mem_types: list[str] | None = None,
        top_k: int = 10,
        *,
        filters: Optional[FilterGroup] = None,
    ) -> list[tuple[MemoryDoc, float]]:
        """Search memories via vector similarity, then fetch content from KV store."""
        if not self._embedding_model:
            memory_logger.error(
                "Embedding model not initialized.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                scope_id=scope_id,
            )
            return []

        query_vec = await self._embedding_model.embed_query(query)

        if mem_types:
            types = mem_types
        else:
            cols = await self._collections_for(user_id, scope_id)
            types = [t for t in (self._parse_mem_type_from_collection(c) for c in cols) if t]

        results: list[tuple[MemoryDoc, float]] = []
        for mt in types:
            col = self._get_collection_name(user_id, scope_id, mt)
            if not await self._vector_store.collection_exists(col):
                continue

            hits = await self._vector_store.search(
                collection_name=col,
                query_vector=query_vec,
                vector_field="embedding",
                top_k=top_k,
                filters=filters,
            )

            hit_ids: list[str] = []
            scores: dict[str, float] = {}
            for h in hits:
                mid = h.fields.get("id", "")
                if mid:
                    hit_ids.append(mid)
                    scores[mid] = h.score

            if not hit_ids:
                continue

            keys = [self._kv_mem_key(user_id, scope_id, mid) for mid in hit_ids]
            values = await self._kv_store.mget(keys)

            for mid, raw in zip(hit_ids, values):
                decoded = self._read_kv_value(raw)
                if decoded is None:
                    continue
                data = json.loads(decoded)
                if self._codec is not None and "mem" in data:
                    data["mem"] = self._codec.decode(data["mem"])
                results.append((self._kv_data_to_memory_doc(data, mid), scores.get(mid, 0.0)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    async def update_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]) -> None:
        """Update memories by deleting old ones then adding new ones."""
        if not memories:
            return
        ids = [m.id for m in memories]
        await self.delete_memories(user_id, scope_id, ids)
        await self.add_memories(user_id, scope_id, memories)

    async def update_mem_by_id(
        self,
        user_id: str,
        scope_id: str,
        mem_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Update scalar fields on a single memory document by id.

        Only the fields present in ``fields`` are modified. The text
        content and embedding are never touched — this path does not
        call ``embed_documents`` or rewrite the vector document. Used by
        the forgetting pipeline to flip ``blacklisted`` / ``is_important``
        without paying for a re-embedding roundtrip, and by update paths
        that need to preserve scalar flags while changing other fields.

        Fields are split by destination:
        * ``blacklisted`` / ``is_important`` (top-level MemoryDoc scalar
          fields that exist on the vector collection schema) → pushed to
          ``vector_store.update_doc_fields`` (in-place scalar update on
          the existing vector doc, no re-embedding).
        * Any other field (e.g. ``mem_type``, ``timestamp``, arbitrary
          user fields) → written into the KV JSON. Top-level keys on the
          KV dict (``mem_type`` / ``timestamp`` / ``mem``) are updated
          by name; everything else goes into the ``fields`` sub-dict
          of the KV JSON so ``_kv_data_to_memory_doc`` can round-trip it.

        Both stores are always updated together so the KV JSON and the
        vector scalar index stay consistent. If the document does not
        exist in KV, ``STORE_VECTOR_DOC_INVALID`` is raised (the legacy
        layout treats the KV entry as the source of truth for mem_type
        / collection routing — without it we cannot target the correct
        vector collection).
        """
        if not fields:
            return
        if not mem_id:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg="update_mem_by_id requires non-empty mem_id",
            )

        kv_key = self._kv_mem_key(user_id, scope_id, mem_id)
        raw = self._read_kv_value(await self._kv_store.get(kv_key))
        if raw is None:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg=f"memory doc not found: user_id={user_id} scope_id={scope_id} mem_id={mem_id}",
            )
        data: dict[str, Any] = json.loads(raw)
        if self._codec is not None and "mem" in data:
            data["mem"] = self._codec.decode(data["mem"])

        # Partition fields by destination.
        vector_scalar_updates: dict[str, Any] = {}
        for name, value in fields.items():
            if name in self._VECTOR_PUSHDOWN_FIELDS:
                vector_scalar_updates[name] = value
        # KV JSON is flat: every field — whether top-level KV key
        # (mem_type / timestamp / mem) or arbitrary user field — goes
        # directly onto the top-level dict. ``_memory_doc_to_kv_data``
        # flattens ``doc.fields`` this way on write; we mirror it here
        # so ``_kv_data_to_memory_doc`` can round-trip via its ``extra``
        # collector.
        for name, value in fields.items():
            data[name] = value

        # Write KV JSON back (re-encode mem if codec is in use).
        out_data = dict(data)
        if self._codec is not None and "mem" in out_data:
            out_data["mem"] = self._codec.encode(out_data["mem"])
        await self._kv_store.set(kv_key, self._write_kv_value(json.dumps(out_data)))

        # Vector scalar update — only if there's at least one vector-schema
        # field to flip. Non-vector fields are KV-only and require no
        # vector call.
        if vector_scalar_updates:
            mem_type = data.get("mem_type", "")
            if not mem_type:
                # Without mem_type we can't resolve the collection name.
                # This shouldn't happen for docs written via add_memories,
                # but guard anyway rather than silently skipping.
                raise build_error(
                    StatusCode.STORE_VECTOR_DOC_INVALID,
                    error_msg=(
                        f"memory doc {mem_id} has no mem_type; cannot resolve "
                        "vector collection for scalar update"
                    ),
                )
            col = self._get_collection_name(user_id, scope_id, mem_type)
            await self._vector_store.update_doc_fields(col, mem_id, vector_scalar_updates)

    async def delete_memories(self, user_id: str, scope_id: str, ids: list[str]) -> None:
        """Delete memory documents from both KV and vector stores."""
        if not ids:
            return

        for mid in ids:
            kv_key = self._kv_mem_key(user_id, scope_id, mid)
            raw = self._read_kv_value(await self._kv_store.get(kv_key))

            mem_type = None
            if raw:
                data = json.loads(raw)
                mem_type = data.get("mem_type")

            await self._kv_store.delete(kv_key)
            await self._remove_id_from_tracking(user_id, scope_id, mid, mem_type)

        cols = await self._collections_for(user_id, scope_id)
        for col in cols:
            await self._vector_store.delete_docs_by_ids(col, ids)

    async def delete_by_user(self, user_id: str) -> None:
        """Delete all memories for a user across all scopes."""
        kv_prefix = f"{self._KV_PREFIX}{self._KV_SEP}{user_id}{self._KV_SEP}"
        await self._kv_store.delete_by_prefix(kv_prefix)

        all_cols = await self._vector_store.list_collection_names()
        marker = f"uid_{user_id}_gid_"
        for col in all_cols:
            if col.startswith(marker):
                await self._vector_store.delete_collection(col)
                self._created_collections.discard(col)

    async def delete_by_scope(self, scope_id: str) -> None:
        """Delete all memories for a scope across all users."""
        kv_prefix = f"{self._KV_PREFIX}{self._KV_SEP}"
        all_kv = await self._kv_store.get_by_prefix(kv_prefix)
        to_delete = []
        for key in all_kv:
            parts = key.split(self._KV_SEP)
            if len(parts) >= 3 and parts[2] == scope_id:
                to_delete.append(key)
        if to_delete:
            await self._kv_store.batch_delete(to_delete)

        scope_marker = f"_gid_{scope_id}_mtype_"
        for col in await self._vector_store.list_collection_names():
            if col.startswith("uid_") and scope_marker in col:
                await self._vector_store.delete_collection(col)
                self._created_collections.discard(col)

    async def delete_by_user_and_scope(self, user_id: str, scope_id: str) -> None:
        """Delete all memories for a specific user and scope."""
        kv_prefix = f"{self._KV_PREFIX}{self._KV_SEP}{user_id}{self._KV_SEP}{scope_id}{self._KV_SEP}"
        await self._kv_store.delete_by_prefix(kv_prefix)

        for col in await self._collections_for(user_id, scope_id):
            await self._vector_store.delete_collection(col)
            self._created_collections.discard(col)

    async def get_by_id(self, user_id: str, scope_id: str, mem_id: str) -> MemoryDoc | None:
        """Retrieve a single memory document by ID from the KV store."""
        raw = self._read_kv_value(await self._kv_store.get(self._kv_mem_key(user_id, scope_id, mem_id)))
        if raw is None:
            return None
        data = json.loads(raw)
        if self._codec is not None and "mem" in data:
            data["mem"] = self._codec.decode(data["mem"])
        return self._kv_data_to_memory_doc(data, mem_id)

    # Vector schema fields that ``_ensure_collection`` declares on every
    # collection. These are the only scalar fields eligible for pushdown
    # to ``vector_store.list_docs`` — other conditions (e.g. on mem_type
    # / timestamp / custom fields dict entries) must still be evaluated
    # at the application layer because the legacy layout keeps those
    # values inside the KV JSON blob.
    _VECTOR_PUSHDOWN_FIELDS = frozenset({"blacklisted", "is_important"})

    async def list_memories(
        self,
        user_id: str,
        scope_id: str,
        offset: int = 0,
        limit: int = 100,
        mem_types: list[str] | None = None,
        *,
        filters: Optional[FilterGroup] = None,
    ) -> list[MemoryDoc]:
        """List memory documents with pagination, reading from the KV store.

        Filtering strategy (two paths):

        * If ``filters`` only references fields that exist on the vector
          collection's scalar schema (``blacklisted`` / ``is_important`` —
          injected by ``_ensure_collection`` when the collection is first
          created), the entire FilterGroup is **pushed down** to
          ``vector_store.list_docs`` so the backend's scalar index handles
          the predicate. The matching ids are then joined back to KV to
          materialise ``MemoryDoc`` bodies.
          This is the vector-filter path.

        * If ``filters`` also references fields that live only inside the KV
          JSON (mem_type / timestamp / arbitrary user fields), the whole
          FilterGroup falls back to application-layer evaluation after the
          KV scan. This is the application-filter path.

        * ``filters=None`` always takes the KV-scan path.
        """
        if filters is not None and _can_pushdown_filter(filters, self._VECTOR_PUSHDOWN_FIELDS):
            return await self._list_memories_via_vector_pushdown(
                user_id, scope_id, offset, limit, mem_types, filters=filters
            )
        return await self._list_memories_via_kv_scan(
            user_id, scope_id, offset, limit, mem_types, filters=filters
        )

    async def _list_memories_via_vector_pushdown(
        self,
        user_id: str,
        scope_id: str,
        offset: int,
        limit: int,
        mem_types: list[str] | None,
        *,
        filters: FilterGroup,
    ) -> list[MemoryDoc]:
        """Push the entire FilterGroup to ``vector_store.list_docs`` for each
        target collection, then materialise MemoryDoc bodies from KV.
        Only used when every referenced field is on the vector schema
        (see ``_can_pushdown_filter``).
        """
        # Determine target collections from mem_types (or all user+scope cols).
        if mem_types:
            types = list(mem_types)
        else:
            cols = await self._collections_for(user_id, scope_id)
            types = [t for t in (self._parse_mem_type_from_collection(c) for c in cols) if t]

        # The pushdown path needs ids only — ask list_docs to skip text fields.
        # Use a comfortable page size: caller's pagination is applied after the
        # merge, so we fetch enough to satisfy offset+limit across all types.
        wanted = offset + limit

        docs: list[MemoryDoc] = []
        for mt in types:
            col = self._get_collection_name(user_id, scope_id, mt)
            if not await self._vector_store.collection_exists(col):
                continue
            rows = await self._vector_store.list_docs(
                collection_name=col,
                filters=filters,
                limit=wanted,
                offset=0,
            )
            ids_in_col = [str(r.get("id")) for r in rows if r.get("id")]
            if not ids_in_col:
                continue
            keys = [self._kv_mem_key(user_id, scope_id, mid) for mid in ids_in_col]
            values = await self._kv_store.mget(keys)
            for mid, val in zip(ids_in_col, values):
                decoded = self._read_kv_value(val)
                if decoded is None:
                    continue
                data = json.loads(decoded)
                if self._codec is not None and "mem" in data:
                    data["mem"] = self._codec.decode(data["mem"])
                docs.append(self._kv_data_to_memory_doc(data, mid))

        if mem_types:
            type_order = {mt: i for i, mt in enumerate(mem_types)}
            docs.sort(key=lambda d: (type_order.get(d.type, len(type_order)), -d.timestamp.timestamp()))
        return docs[offset:offset + limit]

    async def _list_memories_via_kv_scan(
        self,
        user_id: str,
        scope_id: str,
        offset: int,
        limit: int,
        mem_types: list[str] | None,
        *,
        filters: Optional[FilterGroup],
    ) -> list[MemoryDoc]:
        """Original KV-prefix-scan path. Used when ``filters`` references
        non-vector-schema fields or when ``filters`` is None."""
        ids_key = self._kv_ids_key(user_id, scope_id)
        raw = self._read_kv_value(await self._kv_store.get(ids_key)) or ""
        if not raw:
            return []

        all_ids = self._parse_all_ids(raw)
        if not all_ids:
            return []

        keys = [self._kv_mem_key(user_id, scope_id, mid) for mid in all_ids]
        values = await self._kv_store.mget(keys)

        docs: list[MemoryDoc] = []
        for mid, val in zip(all_ids, values):
            decoded = self._read_kv_value(val)
            if decoded is None:
                continue
            data = json.loads(decoded)
            if self._codec is not None and "mem" in data:
                data["mem"] = self._codec.decode(data["mem"])
            doc = self._kv_data_to_memory_doc(data, mid)
            if not mem_types or doc.type in mem_types:
                docs.append(doc)
        if mem_types:
            type_order = {mt: i for i, mt in enumerate(mem_types)}
            docs.sort(key=lambda d: (type_order.get(d.type, len(type_order)), -d.timestamp.timestamp()))
        if filters is not None:
            docs = [d for d in docs if _apply_filter_group(d, filters)]
        return docs[offset:offset + limit]

    def get_schema_version(self) -> int:
        return self._schema_version

    def update_schema_version(self, version: int) -> None:
        self._schema_version = version

    async def create_backup(self) -> str:
        bid = str(uuid.uuid4())
        self._backups[bid] = {"schema_version": self._schema_version}
        return bid

    async def restore_backup(self, backup_id: str) -> None:
        if backup_id not in self._backups:
            raise ValueError(f"Backup {backup_id} not found")
        self._schema_version = self._backups[backup_id]["schema_version"]

    async def cleanup_backup(self, backup_id: str) -> None:
        self._backups.pop(backup_id, None)

    async def list_user_scopes(self) -> list[tuple[str, str]]:
        """List all (user_id, scope_id) pairs by scanning KV keys."""
        kv_prefix = f"{self._KV_PREFIX}{self._KV_SEP}"
        all_kv = await self._kv_store.get_by_prefix(kv_prefix)
        scopes: set[tuple[str, str]] = set()
        for key in all_kv:
            parts = key.split(self._KV_SEP)
            if len(parts) >= 3:
                scopes.add((parts[1], parts[2]))
        return list(scopes)


def _apply_filter_group(doc: MemoryDoc, group: FilterGroup) -> bool:
    """Application-layer FilterGroup evaluator used by SimpleMemoryIndex.

    The legacy layout stores scalar fields inside the KV JSON value; we read
    them off the MemoryDoc (top-level fields take priority over ``fields`` dict).
    """
    results = [_apply_filter_condition_or_group(doc, c) for c in group.conditions]
    if not results:
        return True
    if group.logic.value == "and":
        return all(results)
    return any(results)


def _apply_filter_condition_or_group(doc: MemoryDoc, cond_or_group: Any) -> bool:
    if isinstance(cond_or_group, FilterGroup):
        return _apply_filter_group(doc, cond_or_group)
    return _apply_filter_condition(doc, cond_or_group)


def _apply_filter_condition(doc: MemoryDoc, cond) -> bool:
    # Top-level MemoryDoc fields first, then fields dict.
    if hasattr(doc, cond.field):
        value = getattr(doc, cond.field, None)
    else:
        value = doc.fields.get(cond.field)
    op = cond.op.value
    target = cond.value
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    # Other operators are out of scope for the first version.
    return False


def _can_pushdown_filter(group: FilterGroup, pushdown_fields: frozenset[str]) -> bool:
    """Return True iff every FilterCondition in ``group`` references a field
    that exists on the vector collection's scalar schema.

    Used by ``SimpleMemoryIndex.list_memories`` to decide whether the
    FilterGroup can be pushed down to ``vector_store.list_docs`` (vector-filter path)
    or must fall back to application-layer evaluation (application-filter path). A mixed
    group (some pushable + some app-only fields) is NOT partially pushed —
    partial pushdown would require splitting the group, which the DSL's
    EQ/NE-only first version doesn't support cleanly; the safe fallback is
    to evaluate the whole group at the application layer.

    Traverses nested FilterGroup subtrees recursively; any subtree that
    references a non-pushable field disqualifies the entire filter.
    """
    for cond_or_group in group.conditions:
        if isinstance(cond_or_group, FilterGroup):
            if not _can_pushdown_filter(cond_or_group, pushdown_fields):
                return False
        else:
            if cond_or_group.field not in pushdown_fields:
                return False
    return True
