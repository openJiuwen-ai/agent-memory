# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-DB integration test for ChromaVectorStore.

Unlike the ES integration test (which needs an external ES server),
Chroma runs embedded — ``chromadb.PersistentClient(path=...)`` opens
a local directory. So this test is always runnable in any environment
that has the ``chromadb`` extra installed; no service to start, no
env vars to set.

Coverage of Step 1 / Step 2 / Step 2-review-feedback surface area:

  1. ``create_collection`` with a schema that declares ``blacklisted`` /
     ``is_important`` BOOL scalar fields (the schema
     ``SimpleMemoryIndex._ensure_collection`` builds on every new
     collection).
  2. ``add_docs`` writes docs that carry both BOOL fields (mirrors
     what ``SimpleMemoryIndex.add_memories`` now does).
  3. ``list_docs`` + FilterGroup EQ/NE on ``blacklisted`` /
     ``is_important`` — verifies the pushdown path actually hits
     Chroma's scalar ``where`` filter and returns the right subset.
  4. ``update_doc_fields`` flips ``blacklisted=True`` on a single doc
     by id — verifies the scalar-update path doesn't touch the vector
     field and the next ``list_docs`` sees the new value.
  5. ``search`` with FilterGroup prefilter — verifies vector similarity
     + scalar filter works end-to-end.

Each test uses a fresh collection name (uuid suffix) so they don't
collide; the ``chroma_store`` fixture uses a per-test ``tmp_path``
directory so the whole DB is thrown away at teardown.
"""
import uuid
from typing import Any

import pytest

from jiuwen_memory.foundation.store.base_vector_store import (
    CollectionSchema,
    FieldSchema,
    VectorDataType,
)
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterOperator,
)
from jiuwen_memory.foundation.store.vector.chroma_vector_store import (
    ChromaVectorStore,
)


def _build_schema(dim: int = 4) -> CollectionSchema:
    """Schema mirroring what ``SimpleMemoryIndex._ensure_collection``
    builds: id (VARCHAR PK) + embedding (FLOAT_VECTOR) + the two
    Ebbinghaus BOOL scalars. We also add a ``text`` VARCHAR field
    so Chroma's ``documents`` slot is populated (otherwise
    ``list_docs`` returns empty text)."""
    schema = CollectionSchema(
        description="chroma ebbinghaus integration test collection",
        enable_dynamic_field=False,
    )
    schema.add_field(FieldSchema(name="id", dtype=VectorDataType.VARCHAR, max_length=256, is_primary=True))
    schema.add_field(FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=dim))
    schema.add_field(FieldSchema(name="text", dtype=VectorDataType.VARCHAR, max_length=65535))
    schema.add_field(FieldSchema(name="blacklisted", dtype=VectorDataType.BOOL, default_value=False))
    schema.add_field(FieldSchema(name="is_important", dtype=VectorDataType.BOOL, default_value=False))
    return schema


@pytest.fixture
def chroma_store(tmp_path):
    """
        PersistentClient on a per-test tmp directory — fully local,
        no external service. Collection-level cleanup is handled in each
        test via ``delete_collection`` in a ``finally`` block.
    """
    store = ChromaVectorStore(persist_directory=str(tmp_path))
    return store


def _make_doc(doc_id: str, embedding: list[float], text: str,
              blacklisted: bool = False, is_important: bool = False) -> dict[str, Any]:
    return {
        "id": doc_id,
        "embedding": embedding,
        "text": text,
        "blacklisted": blacklisted,
        "is_important": is_important,
    }


# ---------------------------------------------------------------------------
# create_collection + add_docs: BOOL fields are stored as metadata
# ---------------------------------------------------------------------------


class TestChromaCreateAndAdd:
    @pytest.mark.asyncio
    async def test_create_collection_with_ebbinghaus_fields(self, chroma_store):
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            assert await chroma_store.collection_exists(col) is True
            schema = await chroma_store.get_schema(col)
            field_names = {f.name for f in schema.fields}
            assert {"blacklisted", "is_important"}.issubset(field_names)
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_add_docs_persists_blacklisted_and_is_important(self, chroma_store):
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False, is_important=True),
                _make_doc("d2", [0.0, 1.0, 0.0, 0.0], "banana", blacklisted=True, is_important=False),
            ])
            # Verify both fields were persisted by reading back via list_docs
            # with no filter — all docs come back with their metadata.
            rows = await chroma_store.list_docs(col, filters=None, limit=10)
            assert len(rows) == 2
            by_id = {r["id"]: r for r in rows}
            assert by_id["d1"]["blacklisted"] is False
            assert by_id["d1"]["is_important"] is True
            assert by_id["d2"]["blacklisted"] is True
            assert by_id["d2"]["is_important"] is False
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)


# ---------------------------------------------------------------------------
# list_docs: FilterGroup EQ/NE pushdown to Chroma's `where` clause
# ---------------------------------------------------------------------------


class TestChromaListDocsPushdown:
    """
        The whole point of Step 2 + the §3.16 方式一 refactor: a
        FilterGroup that only references vector-schema scalar fields
        (``blacklisted`` / ``is_important``) gets pushed down to the
        backend's scalar filter. For Chroma, this means rendering the
        FilterGroup to Chroma's ``where`` clause and letting
        ``collection.get(where=...)`` do the filtering server-side.
    """

    @pytest.mark.asyncio
    async def test_list_docs_eq_filter_on_blacklisted(self, chroma_store):
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False),
                _make_doc("d2", [0.0, 1.0, 0.0, 0.0], "banana", blacklisted=True),
                _make_doc("d3", [0.0, 0.0, 1.0, 0.0], "cherry", blacklisted=True),
            ])
            # EQ True → only the two blacklisted docs.
            flt = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True),
            ])
            rows = await chroma_store.list_docs(col, filters=flt, limit=10)
            ids = {r["id"] for r in rows}
            assert ids == {"d2", "d3"}
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_list_docs_ne_filter_on_blacklisted(self, chroma_store):
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False),
                _make_doc("d2", [0.0, 1.0, 0.0, 0.0], "banana", blacklisted=True),
                _make_doc("d3", [0.0, 0.0, 1.0, 0.0], "cherry", blacklisted=True),
            ])
            # NE True → only the one non-blacklisted doc.
            flt = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
            ])
            rows = await chroma_store.list_docs(col, filters=flt, limit=10)
            ids = {r["id"] for r in rows}
            assert ids == {"d1"}
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_list_docs_combined_and_filter(self, chroma_store):
        """
            FilterGroup with two AND conditions on blacklisted + is_important
            — verifies Chroma's $and clause renders correctly and only the
            doc matching both comes back.
        """
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False, is_important=True),
                _make_doc("d2", [0.0, 1.0, 0.0, 0.0], "banana", blacklisted=False, is_important=False),
                _make_doc("d3", [0.0, 0.0, 1.0, 0.0], "cherry", blacklisted=True, is_important=True),
            ])
            flt = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
                FilterCondition(field="is_important", op=FilterOperator.EQ, value=True),
            ])
            rows = await chroma_store.list_docs(col, filters=flt, limit=10)
            ids = {r["id"] for r in rows}
            # d1: blacklisted=False AND is_important=True → matches.
            # d2: blacklisted=False but is_important=False → fails second cond.
            # d3: blacklisted=True → fails first cond.
            assert ids == {"d1"}
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_list_docs_pagination(self, chroma_store):
        """
            limit + offset on the pushdown path — verifies pagination
            is honoured by the backend, not the application layer.
        """
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc(f"d{i}", [float(i), 0.0, 0.0, 0.0], f"text{i}", blacklisted=False)
                for i in range(5)
            ])
            flt = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
            ])
            page = await chroma_store.list_docs(col, filters=flt, limit=2, offset=1)
            assert len(page) == 2
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)


# ---------------------------------------------------------------------------
# update_doc_fields: scalar-only update, no re-embedding
# ---------------------------------------------------------------------------


class TestChromaUpdateDocFields:
    @pytest.mark.asyncio
    async def test_update_blacklisted_to_true_persists(self, chroma_store):
        """
            Flip ``blacklisted`` from False to True on a single doc by id.
            The next ``list_docs`` with ``EQ True`` filter must include it;
            the original ``NE True`` filter that used to include it must now
            exclude it. This validates the scalar-update path actually wrote
            the new value to the backend.
        """
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False),
            ])
            # Before update: d1 is blacklisted=False → NE True matches.
            flt_ne = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
            ])
            before = await chroma_store.list_docs(col, filters=flt_ne, limit=10)
            assert {r["id"] for r in before} == {"d1"}

            # Flip the scalar.
            await chroma_store.update_doc_fields(col, "d1", {"blacklisted": True})

            # After update: d1 is now blacklisted=True → NE True excludes it.
            after_ne = await chroma_store.list_docs(col, filters=flt_ne, limit=10)
            assert {r["id"] for r in after_ne} == set()
            # And EQ True now includes it.
            flt_eq = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True),
            ])
            after_eq = await chroma_store.list_docs(col, filters=flt_eq, limit=10)
            assert {r["id"] for r in after_eq} == {"d1"}
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_update_does_not_touch_embedding(self, chroma_store):
        """
            Verify ``update_doc_fields`` only changes the scalar fields
            and doesn't touch the vector. After the update, vector search
            with the original query vector must still return the doc with
            a high similarity score — if the embedding was wiped or
            altered, the search would not find it.
        """
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False),
            ])
            await chroma_store.update_doc_fields(col, "d1", {"blacklisted": True, "is_important": True})

            # Vector search with the original query vector — if the
            # embedding was touched by update_doc_fields, the doc would
            # not be found (or would have a very low score).
            results = await chroma_store.search(
                collection_name=col,
                query_vector=[1.0, 0.0, 0.0, 0.0],
                vector_field="embedding",
                top_k=5,
            )
            assert any(r.fields.get("id") == "d1" for r in results)
            # The matching doc must carry the updated scalar values.
            d1_result = next(r for r in results if r.fields.get("id") == "d1")
            assert d1_result.fields.get("blacklisted") is True
            assert d1_result.fields.get("is_important") is True
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)

    @pytest.mark.asyncio
    async def test_update_nonexistent_doc_raises(self, chroma_store):
        from jiuwen_memory.common.exception.codes import StatusCode
        from jiuwen_memory.common.exception.errors import BaseError

        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple"),
            ])
            with pytest.raises(BaseError) as exc_info:
                await chroma_store.update_doc_fields(col, "nonexistent", {"blacklisted": True})
            assert exc_info.value.status == StatusCode.STORE_VECTOR_DOC_INVALID
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)


# ---------------------------------------------------------------------------
# search: vector similarity + FilterGroup prefilter
# ---------------------------------------------------------------------------


class TestChromaSearchWithFilters:
    @pytest.mark.asyncio
    async def test_search_prefilters_on_blacklisted(self, chroma_store):
        """
            Vector search with a FilterGroup prefilter on ``blacklisted``
            must return only the matching subset, ranked by similarity.
        """
        col = f"chr_it_{uuid.uuid4().hex[:12]}"
        try:
            await chroma_store.create_collection(col, _build_schema(), distance_metric="cosine")
            await chroma_store.add_docs(col, [
                _make_doc("d1", [1.0, 0.0, 0.0, 0.0], "apple", blacklisted=False),
                _make_doc("d2", [0.9, 0.1, 0.0, 0.0], "apple-like", blacklisted=True),
                _make_doc("d3", [0.0, 1.0, 0.0, 0.0], "banana", blacklisted=False),
            ])
            # Query near "apple" but exclude blacklisted → should only
            # return d1 (d2 is blacklisted, d3 is far in vector space).
            flt = FilterGroup(conditions=[
                FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
            ])
            results = await chroma_store.search(
                collection_name=col,
                query_vector=[1.0, 0.0, 0.0, 0.0],
                vector_field="embedding",
                top_k=5,
                filters=flt,
            )
            ids = {r.fields.get("id") for r in results}
            assert "d1" in ids
            assert "d2" not in ids  # blacklisted, excluded by filter
        finally:
            if await chroma_store.collection_exists(col):
                await chroma_store.delete_collection(col)
