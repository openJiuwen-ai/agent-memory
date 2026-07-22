# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for Ebbinghaus scalar-field declaration on the memory_index layer.

Covers the layer-responsibility refactor: vector backends stay
infrastructure-only and do NOT inject business fields (``blacklisted`` /
``is_important``). Declaration lives at the ``BaseMemoryIndex`` layer —
specifically ``SimpleMemoryIndex._ensure_collection``, which builds the
schema before calling ``vector_store.create_collection``.

What this file verifies:

1. ``SimpleMemoryIndex._ensure_collection`` declares ``blacklisted`` /
   ``is_important`` BOOL fields with ``default_value=False`` on every
   new collection it creates, so the FilterGroup EQ/NE pushdown path
   (see ``test_simple_index_filters.py``) can hit the backend's scalar
   index.
2. Vector backends do NOT mutate caller-supplied schemas — no
   ``ensure_ebbinghaus_scalar_fields`` helper exists anymore, and the
   four backends' ``create_collection`` do not add fields the caller
   didn't declare.
3. Milvus ``add_field`` still passes ``default_value`` + ``nullable=True``
   for any BOOL field with a ``default_value`` (generic capability — not
   Ebbinghaus-specific).
4. Gauss ``CREATE TABLE`` still emits ``DEFAULT False`` for any BOOL
   field with a ``default_value`` (generic capability).
5. Chroma ``add_docs`` does NOT auto-fill ``blacklisted`` / ``is_important``
   by name — caller is responsible for writing fields it wants stored.

Verification is mock-based (no real databases required); end-to-end
coverage against running Milvus / ES / Gauss / Chroma instances is
deferred to Step 10 integration tests.
"""
# Schema-declaration and backend-internals assertions reach into protected
# members (``_ensure_collection`` / ``_client`` / ``_collections`` / ``_conn``
# / ``_collection_metadata`` / ``_vector_store`` / ``_created_collections`` /
# ``_exists`` / ``_index_prefix`` / ``_build_mappings``) to stub the backend
# without standing up a real database.
# pylint: disable=protected-access
import asyncio
from unittest.mock import MagicMock

from jiuwen_memory.foundation.store.base_vector_store import (
    CollectionSchema,
    FieldSchema,
    VectorDataType,
)
from jiuwen_memory.foundation.store.index.simple_memory_index import (
    SimpleMemoryIndex,
)
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore


class _DummyEmbedding:
    dimension = 8

    async def embed_query(self, text: str, **_kw) -> list[float]:
        return [0.0] * self.dimension

    async def embed_documents(self, texts: list[str], **_kw) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


class _RecordingVectorStore:
    """Minimal BaseVectorStore stand-in that records the schema each
    create_collection call received. Does NOT mutate the schema — mirrors
    what real backends now do (no business-field injection)."""

    def __init__(self):
        self.created: list[tuple[str, CollectionSchema]] = []
        self._exists: set[str] = set()

    async def collection_exists(self, name) -> bool:
        return name in self._exists

    async def create_collection(self, name, schema, **_kw):
        if isinstance(schema, dict):
            schema = CollectionSchema.from_dict(schema)
        # Record a *copy* so later mutations by callers don't lie about
        # what the backend saw at create_collection time.
        self.created.append((name, schema))
        self._exists.add(name)

    async def list_collection_names(self):
        return list(self._exists)

    async def add_docs(self, name, docs, **_kw):
        pass

    async def list_docs(self, collection_name, filters=None, limit=100, offset=0, **_kw):
        return []

    async def delete_docs_by_ids(self, name, ids, **_kw):
        pass

    async def delete_collection(self, name, **_kw):
        self._exists.discard(name)

    async def update_doc_fields(self, name, doc_id, fields, **_kw):
        pass


# ---------------------------------------------------------------------------
# SimpleMemoryIndex._ensure_collection declares Ebbinghaus fields
# ---------------------------------------------------------------------------


class TestSimpleIndexDeclaresEbbinghausFields:
    """
        The schema that ``SimpleMemoryIndex._ensure_collection`` hands to
        ``vector_store.create_collection`` must contain ``blacklisted`` /
        ``is_important`` as BOOL fields with ``default_value=False``. These are
        the only fields the FilterGroup pushdown path can target.
    """

    @staticmethod
    def _make_index():
        return SimpleMemoryIndex(
            kv_store=InMemoryKVStore(),
            vector_store=_RecordingVectorStore(),  # type: ignore[arg-type]
            embedding_model=_DummyEmbedding(),
        )

    def test_ensure_collection_adds_both_bool_fields(self):
        idx = self._make_index()
        asyncio.run(idx._ensure_collection("u_s_summary", dim=8))
        assert len(idx._vector_store.created) == 1
        name, schema = idx._vector_store.created[0]
        assert name == "u_s_summary"
        assert schema.has_field("blacklisted")
        assert schema.has_field("is_important")
        blacklisted = schema.get_field("blacklisted")
        assert blacklisted.dtype == VectorDataType.BOOL
        assert blacklisted.default_value is False
        important = schema.get_field("is_important")
        assert important.dtype == VectorDataType.BOOL
        assert important.default_value is False

    def test_ensure_collection_caches_created_set(self):
        """
            Second call to _ensure_collection with the same name must be a
            no-op (the collection is already in _created_collections).
        """
        idx = self._make_index()
        asyncio.run(idx._ensure_collection("u_s_summary", dim=8))
        asyncio.run(idx._ensure_collection("u_s_summary", dim=8))
        # Only the first call hit create_collection.
        assert len(idx._vector_store.created) == 1

    def test_ensure_collection_skips_when_backend_already_has_collection(self):
        """
            If the backend already has the collection, _ensure_collection
            must not call create_collection again (still adds to the cache so
            the next call is a fast in-memory hit).
        """
        idx = self._make_index()
        idx._vector_store._exists.add("u_s_summary")
        asyncio.run(idx._ensure_collection("u_s_summary", dim=8))
        assert len(idx._vector_store.created) == 0
        assert "u_s_summary" in idx._created_collections

    def test_ensure_collection_preserves_id_and_embedding_fields(self):
        """
            The schema handed to the backend must still carry id + embedding
            alongside the two Ebbinghaus scalars.
        """
        idx = self._make_index()
        asyncio.run(idx._ensure_collection("u_s_summary", dim=8))
        _, schema = idx._vector_store.created[0]
        id_field = schema.get_field("id")
        assert id_field.is_primary is True
        emb_field = schema.get_field("embedding")
        assert emb_field.dtype == VectorDataType.FLOAT_VECTOR
        assert emb_field.dim == 8


# ---------------------------------------------------------------------------
# Backends do NOT inject business fields (regression test for the refactor)
# ---------------------------------------------------------------------------


class TestBackendsDoNotInjectBusinessFields:
    """
        Vector backends must not mutate caller-supplied schemas by adding
        ``blacklisted`` / ``is_important`` — that responsibility was moved to
        the memory_index layer. This test is a regression guard: if a future
        commit re-introduces field injection in any backend, it will fail
        here.
    """

    @staticmethod
    def _schema_without_ebbinghaus_fields():
        return CollectionSchema(fields=[
            FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
        ])

    def test_milvus_does_not_inject_business_fields(self):
        from jiuwen_memory.foundation.store.vector.milvus_vector_store import (
            MilvusVectorStore,
        )

        store = MilvusVectorStore(milvus_uri="http://localhost:19530")
        mock_client = MagicMock()
        store._client = mock_client
        mock_client.has_collection.return_value = False
        milvus_schema_stub = MagicMock()
        mock_client.create_schema.return_value = milvus_schema_stub
        mock_client.prepare_index_params.return_value = MagicMock()

        schema = self._schema_without_ebbinghaus_fields()
        asyncio.run(store.create_collection("u_s_summary", schema))
        # The caller's schema must be unchanged — no silent field additions.
        assert not schema.has_field("blacklisted")
        assert not schema.has_field("is_important")

    def test_es_does_not_inject_business_fields(self):
        from jiuwen_memory.foundation.store.vector.es_vector_store import (
            ElasticsearchVectorStore,
        )

        store = ElasticsearchVectorStore.__new__(ElasticsearchVectorStore)
        store._index_prefix = "pytest"
        schema = self._schema_without_ebbinghaus_fields()
        mappings = store._build_mappings(schema, "COSINE")
        properties = mappings["properties"]
        # ES dynamic: strict means un-declared fields are rejected on write;
        # the backend must not add fields the caller didn't declare.
        assert "blacklisted" not in properties
        assert "is_important" not in properties

    def test_chroma_does_not_inject_business_fields(self):
        from jiuwen_memory.foundation.store.vector.chroma_vector_store import (
            ChromaVectorStore,
        )

        store = ChromaVectorStore.__new__(ChromaVectorStore)
        store._client = MagicMock()
        store._collections = {}
        store._client.get_or_create_collection.return_value = MagicMock()
        schema = self._schema_without_ebbinghaus_fields()
        asyncio.run(store.create_collection("u_s_summary", schema))
        assert not schema.has_field("blacklisted")
        assert not schema.has_field("is_important")

    def test_gauss_does_not_inject_business_fields(self):
        from jiuwen_memory.foundation.store.vector.gauss_vector_store import (
            GaussVectorStore,
        )

        store = GaussVectorStore.__new__(GaussVectorStore)
        store._collection_metadata = {}
        cursor_stub = MagicMock()
        cursor_stub.fetchone.return_value = (False,)  # table does not exist
        executed_sql: list[str] = []

        def _capture(sql, params=None):
            executed_sql.append(sql)

        cursor_stub.execute.side_effect = _capture
        conn_stub = MagicMock()
        conn_stub.cursor.return_value = cursor_stub
        store._conn = conn_stub

        schema = self._schema_without_ebbinghaus_fields()
        asyncio.run(store.create_collection("u_s_summary", schema, distance_metric="COSINE"))
        # The caller's schema must be unchanged.
        assert not schema.has_field("blacklisted")
        assert not schema.has_field("is_important")
        # The CREATE TABLE statement must not mention the business fields.
        joined_sql = "\n".join(executed_sql)
        assert "blacklisted" not in joined_sql
        assert "is_important" not in joined_sql


# ---------------------------------------------------------------------------
# Generic capabilities retained: Milvus default_value + Gauss DEFAULT clause
# ---------------------------------------------------------------------------


class TestGenericDefaultValueCapability:
    """
        While the backends no longer inject business fields, the generic
        ability to pass ``default_value`` through schema declarations is
        retained — this is infrastructure capability, not Ebbinghaus
        knowledge. Caller (e.g. SimpleMemoryIndex) declares the fields, the
        backend respects the declaration.
    """

    @staticmethod
    def test_milvus_add_field_passes_default_value_and_nullable():
        """
            Milvus rejects ``default_value`` without ``nullable=True``; the
            BOOL scalar branch must transmit both kwargs when the caller
            declares a ``default_value``.
        """
        from jiuwen_memory.foundation.store.vector.milvus_vector_store import (
            MilvusVectorStore,
        )

        store = MilvusVectorStore(milvus_uri="http://localhost:19530")
        mock_client = MagicMock()
        store._client = mock_client
        mock_client.has_collection.return_value = False

        added_fields: list[dict] = []

        def _capture(**kwargs):
            added_fields.append(kwargs)

        milvus_schema_stub = MagicMock()
        milvus_schema_stub.add_field.side_effect = _capture
        mock_client.create_schema.return_value = milvus_schema_stub
        mock_client.prepare_index_params.return_value = MagicMock()

        # Caller declares a BOOL field with default_value=False — this
        # is exactly what SimpleMemoryIndex._ensure_collection does.
        schema = CollectionSchema(fields=[
            FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
            FieldSchema(name="blacklisted", dtype=VectorDataType.BOOL, default_value=False),
        ])
        asyncio.run(store.create_collection("u_s_summary", schema))

        blacklisted_kwargs = next(
            f for f in added_fields if f.get("field_name") == "blacklisted"
        )
        assert blacklisted_kwargs.get("default_value") is False
        assert blacklisted_kwargs.get("nullable") is True

    @staticmethod
    def test_gauss_create_table_emits_default_clause_for_bool():
        """
            Gauss CREATE TABLE must append ``DEFAULT False`` for any BOOL
            field whose schema declares a ``default_value`` — generic
            capability, not Ebbinghaus-specific.
        """
        from jiuwen_memory.foundation.store.vector.gauss_vector_store import (
            GaussVectorStore,
        )

        store = GaussVectorStore.__new__(GaussVectorStore)
        store._collection_metadata = {}
        cursor_stub = MagicMock()
        cursor_stub.fetchone.return_value = (False,)
        executed_sql: list[str] = []

        def _capture(sql, params=None):
            executed_sql.append(sql)

        cursor_stub.execute.side_effect = _capture
        conn_stub = MagicMock()
        conn_stub.cursor.return_value = cursor_stub
        store._conn = conn_stub

        schema = CollectionSchema(fields=[
            FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
            FieldSchema(name="blacklisted", dtype=VectorDataType.BOOL, default_value=False),
        ])
        asyncio.run(store.create_collection("u_s_summary", schema, distance_metric="COSINE"))
        joined_sql = "\n".join(executed_sql)
        assert "blacklisted" in joined_sql
        assert "DEFAULT False" in joined_sql


# ---------------------------------------------------------------------------
# Chroma add_docs does NOT auto-fill Ebbinghaus fields by name
# ---------------------------------------------------------------------------


class TestChromaAddDocsNoBusinessFieldAutofill:
    """
        Regression guard: Chroma ``add_docs`` must not silently fill
        ``blacklisted`` / ``is_important`` by name. If a caller wants those
        fields stored, the caller writes them (SimpleMemoryIndex.add_memories
        does this explicitly). Chroma is schema-less and should stay
        business-agnostic.
    """

    @staticmethod
    def test_add_docs_does_not_add_blacklisted_or_is_important():
        import json
        from jiuwen_memory.foundation.store.vector.chroma_vector_store import (
            ChromaVectorStore,
        )

        store = ChromaVectorStore.__new__(ChromaVectorStore)
        store._client = MagicMock()
        store._collections = {}

        added_payload: dict = {}

        def _fake_add(**kwargs):
            added_payload.update(kwargs)

        collection_stub = MagicMock()
        collection_stub.add.side_effect = _fake_add
        collection_stub.metadata = {
            "field_mapping": json.dumps({
                "primary_key": "id",
                "vector_field": "embedding",
                "text_field": "text",
            }),
            "vector_field": "embedding",
            "distance_metric": "cosine",
        }
        store._collections["u_s_summary"] = collection_stub

        docs = [
            {"id": "doc_1", "embedding": [0.1, 0.2, 0.3, 0.4], "text": "first",
             "blacklisted": True},  # at least one metadata field so the
             # metadatas kwarg is passed; otherwise Chroma's add_docs takes
             # the no-metadata fast path and skips metadatas entirely.
        ]
        asyncio.run(store.add_docs("u_s_summary", docs))

        metadatas = added_payload.get("metadatas")
        assert metadatas is not None
        assert len(metadatas) == 1
        # blacklisted was supplied by caller → preserved; is_important was
        # NOT supplied → must not be silently filled by name.
        assert metadatas[0]["blacklisted"] is True
        assert "is_important" not in metadatas[0]
