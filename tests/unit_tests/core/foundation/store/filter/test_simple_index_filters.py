# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for SimpleMemoryIndex filters support and update_mem_by_id preservation.

Covers ``BaseMemoryIndex.search`` /
``list_memories`` filters passthrough and ``update_mem_by_id`` field retention.
"""
# Tests assert behaviour through the index's protected collaborators
# (``_vector_store`` / ``_embedding_model``) to verify routing without spinning
# up a real backend.
# pylint: disable=protected-access
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterOperator,
)
from jiuwen_memory.foundation.store.index.simple_memory_index import (
    SimpleMemoryIndex,
    _apply_filter_group,
)
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore


class _DummyEmbedding:
    dimension = 8

    def __init__(self):
        self.embed_documents_calls: list[list[str]] = []

    async def embed_query(self, text: str, **_kw) -> list[float]:
        return [0.0] * self.dimension

    async def embed_documents(self, texts: list[str], **_kw) -> list[list[float]]:
        self.embed_documents_calls.append(list(texts))
        return [[0.0] * self.dimension for _ in texts]


class _StubVectorStore:
    """Minimal in-memory vector store sufficient for SimpleMemoryIndex tests.

    The docs bucket is keyed by id; each entry is a flat dict that may carry
    blacklisted / is_important as top-level keys (mirroring what
    ensure_ebbinghaus_scalar_fields forces onto real backends' schemas).
    ``list_docs`` evaluates the FilterGroup via the same application-layer
    helper SimpleMemoryIndex uses, so pushdown-vs-fallback paths converge.
    """

    def __init__(self):
        self._collections: set[str] = set()
        self._docs: dict[str, dict[str, dict]] = {}
        # Track call counts so tests can assert which path was taken.
        self.list_docs_calls: list[tuple[str, Optional[FilterGroup]]] = []
        self.update_doc_fields_calls: list[tuple[str, str, dict[str, Any]]] = []
        # Track whether add_docs / embed_documents were called so tests can
        # assert the scalar-update path avoided re-embedding + re-writing
        # the vector doc.
        self.add_docs_calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def collection_exists(self, name) -> bool:
        return name in self._collections

    async def create_collection(self, name, schema, **_kw):
        self._collections.add(name)
        self._docs.setdefault(name, {})

    async def add_docs(self, name, docs, **_kw):
        self.add_docs_calls.append((name, list(docs)))
        bucket = self._docs.setdefault(name, {})
        for d in docs:
            bucket[d["id"]] = d

    async def list_collection_names(self):
        return list(self._collections)

    async def list_docs(
        self,
        collection_name: str,
        filters: Optional[FilterGroup] = None,
        limit: int = 100,
        offset: int = 0,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.list_docs_calls.append((collection_name, filters))
        bucket = self._docs.get(collection_name, {})
        # Reuse SimpleMemoryIndex's _apply_filter_group via a stand-in object
        # that exposes the same blacklisted / is_important attributes the
        # evaluator reads with getattr(). This keeps the stub honest about
        # which docs the backend would return to SimpleMemoryIndex.
        rows = []
        for doc_id, doc in bucket.items():
            row = _RowDoc(doc)
            if filters is None or _apply_filter_group(row, filters):
                rows.append({**doc, "id": doc_id})
        return rows[offset:offset + limit]

    async def update_doc_fields(
        self,
        collection_name: str,
        doc_id: str,
        fields: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        self.update_doc_fields_calls.append((collection_name, doc_id, dict(fields)))
        bucket = self._docs.get(collection_name, {})
        if doc_id not in bucket:
            return
        bucket[doc_id].update(fields)

    async def delete_docs_by_ids(self, collection_name, ids, **_kw):
        bucket = self._docs.get(collection_name, {})
        for i in ids:
            bucket.pop(i, None)

    async def delete_collection(self, name, **_kw):
        self._collections.discard(name)
        self._docs.pop(name, None)


class _RowDoc:
    """Lightweight stand-in for MemoryDoc that _apply_filter_group can read
    via getattr / .fields. Mirrors only the attributes the evaluator touches
    (top-level blacklisted / is_important, plus a fields dict for everything
    else)."""

    __slots__ = ("blacklisted", "is_important", "fields", "id", "type", "timestamp", "text")

    def __init__(self, d: dict[str, Any]):
        self.id = d.get("id")
        self.blacklisted = d.get("blacklisted", False)
        self.is_important = d.get("is_important", False)
        self.type = d.get("type", "")
        self.timestamp = d.get("timestamp")
        self.text = d.get("text", "")
        # Everything else goes into the fields dict, matching how MemoryDoc
        # stores non-top-level scalars.
        skip = {"id", "blacklisted", "is_important", "type", "timestamp", "text"}
        self.fields = {k: v for k, v in d.items() if k not in skip}


@pytest.fixture
def index():
    idx = SimpleMemoryIndex(
        kv_store=InMemoryKVStore(),
        vector_store=_StubVectorStore(),  # type: ignore[arg-type]
        embedding_model=_DummyEmbedding(),
    )
    return idx


# SimpleMemoryIndex tracks IDs in a fixed-width 24-byte layout (UUID hex width).
# Use deterministic 24-byte IDs so ``_parse_all_ids`` can split the ids blob.
_M1 = "m10000000000000000000001"  # 24 bytes
_M2 = "m20000000000000000000002"  # 24 bytes
_M3 = "m30000000000000000000003"  # 24 bytes


def _doc(mid: str, mem_type: str = "user_profile",
         blacklisted: bool = False, is_important: bool = False,
         text: str = "hello") -> MemoryDoc:
    return MemoryDoc(
        id=mid, text=text, type=mem_type,
        timestamp=datetime.now(timezone.utc).astimezone(),
        blacklisted=blacklisted, is_important=is_important,
    )


class TestListMemoriesFilters:
    @pytest.mark.asyncio
    async def test_filter_excludes_blacklisted(self, index):
        await index.add_memories("u", "s", [
            _doc(_M1, blacklisted=False),
            _doc(_M2, blacklisted=True),
        ])
        flt = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        docs = await index.list_memories("u", "s", 0, 100, filters=flt)
        ids = {d.id for d in docs}
        assert _M1 in ids
        assert _M2 not in ids

    @pytest.mark.asyncio
    async def test_filter_selects_important(self, index):
        await index.add_memories("u", "s", [
            _doc(_M1, is_important=False),
            _doc(_M2, is_important=True),
        ])
        flt = FilterGroup(conditions=[
            FilterCondition(field="is_important", op=FilterOperator.EQ, value=True),
        ])
        docs = await index.list_memories("u", "s", 0, 100, filters=flt)
        ids = {d.id for d in docs}
        assert ids == {_M2}

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self, index):
        await index.add_memories("u", "s", [
            _doc(_M1, blacklisted=True),
            _doc(_M2, blacklisted=False),
        ])
        docs = await index.list_memories("u", "s", 0, 100)
        ids = {d.id for d in docs}
        assert ids == {_M1, _M2}


class TestListMemoriesPushdownPath:
    """
        Verify that blacklisted / is_important filters go through
        vector_store.list_docs (vector-filter path), while non-vector-schema fields
        fall back to the KV-scan path (application-filter path).
    """

    @pytest.mark.asyncio
    async def test_blacklisted_filter_uses_list_docs_pushdown(self, index):
        """
            Filters that only reference vector-schema fields (blacklisted /
            is_important) must be pushed down to vector_store.list_docs.
        """
        await index.add_memories("u", "s", [
            _doc(_M1, blacklisted=False),
            _doc(_M2, blacklisted=True),
        ])
        flt = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        await index.list_memories("u", "s", 0, 100, filters=flt)
        # The stub recorded at least one list_docs call with the pushed filter.
        assert len(index._vector_store.list_docs_calls) >= 1
        for _col, passed_filter in index._vector_store.list_docs_calls:
            assert passed_filter is flt or (
                passed_filter is not None
                and passed_filter.conditions[0].field == "blacklisted"
            )

    @pytest.mark.asyncio
    async def test_mixed_filter_falls_back_to_kv_scan(self, index):
        """
            A filter referencing a non-vector-schema field (e.g. type, which
            lives on MemoryDoc but not on the vector collection's scalar schema)
            must NOT be pushed down — the whole filter is evaluated at the
            application layer after the KV scan.

            Setup: _M1 is user_profile+blacklisted=False (passes blacklisted NE
            True but fails type EQ summary). _M2 is summary+blacklisted=False
            (passes both conditions with default AND logic).
        """
        await index.add_memories("u", "s", [
            _doc(_M1, mem_type="user_profile", blacklisted=False),
            _doc(_M2, mem_type="summary", blacklisted=False),
        ])
        flt = FilterGroup(conditions=[
            FilterCondition(field="type", op=FilterOperator.EQ, value="summary"),
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        docs = await index.list_memories("u", "s", 0, 100, filters=flt)
        # list_docs should NOT have been called (mixed filter → fall back)
        assert len(index._vector_store.list_docs_calls) == 0
        # And the application-layer filter still gives correct result:
        # only _M2 matches (summary + not blacklisted)
        ids = {d.id for d in docs}
        assert _M2 in ids
        assert _M1 not in ids

    @pytest.mark.asyncio
    async def test_pushdown_pagination(self, index):
        """
            Pagination across the pushdown path: request 1 item from a 2-item
            result set that the pushdown filter would return.
        """
        await index.add_memories("u", "s", [
            _doc(_M1, blacklisted=False),
            _doc(_M2, blacklisted=False),
            _doc(_M3, blacklisted=True),
        ])
        flt = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        page = await index.list_memories("u", "s", offset=1, limit=1, filters=flt)
        # 2 docs match NE blacklisted; offset=1 limit=1 → 1 doc
        assert len(page) == 1
        assert page[0].blacklisted is False


class TestCanPushdownFilterHelper:
    """Direct unit tests on the _can_pushdown_filter decision helper."""

    @staticmethod
    def test_single_blacklisted_condition_pushdownable():
        from jiuwen_memory.foundation.store.index.simple_memory_index import (
            _can_pushdown_filter,
        )
        g = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        assert _can_pushdown_filter(g, frozenset({"blacklisted", "is_important"})) is True

    @staticmethod
    def test_mixed_field_not_pushdownable():
        from jiuwen_memory.foundation.store.index.simple_memory_index import (
            _can_pushdown_filter,
        )
        g = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
            FilterCondition(field="type", op=FilterOperator.EQ, value="summary"),
        ])
        assert _can_pushdown_filter(g, frozenset({"blacklisted", "is_important"})) is False

    @staticmethod
    def test_nested_subtree_with_non_pushable_field_disqualifies():
        from jiuwen_memory.foundation.store.index.simple_memory_index import (
            _can_pushdown_filter,
        )
        inner = FilterGroup(logic="or", conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True),
            FilterCondition(field="custom_field", op=FilterOperator.EQ, value="x"),
        ])
        outer = FilterGroup(conditions=[
            FilterCondition(field="is_important", op=FilterOperator.NE, value=True),
            inner,
        ])
        assert _can_pushdown_filter(outer, frozenset({"blacklisted", "is_important"})) is False

    @staticmethod
    def test_nested_subtree_all_pushable_passes():
        from jiuwen_memory.foundation.store.index.simple_memory_index import (
            _can_pushdown_filter,
        )
        inner = FilterGroup(logic="or", conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True),
            FilterCondition(field="is_important", op=FilterOperator.EQ, value=True),
        ])
        outer = FilterGroup(conditions=[
            FilterCondition(field="is_important", op=FilterOperator.NE, value=True),
            inner,
        ])
        assert _can_pushdown_filter(outer, frozenset({"blacklisted", "is_important"})) is True


class TestUpdateMemByIdScalarPath:
    """Cover the vector-filter path for scalar-only updates: ``update_mem_by_id``
    must route ``blacklisted`` / ``is_important`` to
    ``vector_store.update_doc_fields`` and never call ``embed_documents``
    or ``add_docs``.

    Other fields (arbitrary user fields stored inside the KV JSON
    ``fields`` dict) must be written to KV only — no vector call.
    Mixed updates (one vector-schema + one KV-only) must do both stores
    in one call.
    """

    @pytest.mark.asyncio
    async def test_blacklisted_only_uses_update_doc_fields(self, index):
        """Flipping ``blacklisted`` to True must hit
        ``vector_store.update_doc_fields`` with exactly
        ``{"blacklisted": True}`` and never call ``add_docs`` /
        ``embed_documents`` (the whole point of the scalar-update path).
        """
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary", blacklisted=False)])
        index._vector_store.update_doc_fields_calls.clear()
        index._vector_store.add_docs_calls.clear()
        index._embedding_model.embed_documents_calls.clear()

        await index.update_mem_by_id("u", "s", _M1, {"blacklisted": True})

        # Exactly one update_doc_fields call with the right payload.
        assert len(index._vector_store.update_doc_fields_calls) == 1
        col, doc_id, fields = index._vector_store.update_doc_fields_calls[0]
        assert doc_id == _M1
        assert fields == {"blacklisted": True}
        # No re-embedding, no vector rewrite.
        assert index._vector_store.add_docs_calls == []
        assert index._embedding_model.embed_documents_calls == []

    @pytest.mark.asyncio
    async def test_kv_json_synced_after_scalar_update(self, index):
        """After ``update_mem_by_id`` flips ``blacklisted``, a subsequent
        ``get_by_id`` must see the new value — the KV JSON is the source
        of truth for the application layer, so it must be updated too.
        """
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary", blacklisted=False)])
        await index.update_mem_by_id("u", "s", _M1, {"blacklisted": True, "is_important": True})
        doc = await index.get_by_id("u", "s", _M1)
        assert doc is not None
        assert doc.blacklisted is True
        assert doc.is_important is True

    @pytest.mark.asyncio
    async def test_arbitrary_field_written_to_kv_only(self, index):
        """A non-vector-schema field (e.g. ``custom_tag``) must be written
        into the KV JSON ``fields`` dict and must NOT trigger a vector
        ``update_doc_fields`` call.
        """
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary")])
        index._vector_store.update_doc_fields_calls.clear()
        await index.update_mem_by_id("u", "s", _M1, {"custom_tag": "fav"})
        # No vector call for KV-only field.
        assert index._vector_store.update_doc_fields_calls == []
        # But KV round-trips the value through fields dict.
        doc = await index.get_by_id("u", "s", _M1)
        assert doc is not None
        assert doc.fields.get("custom_tag") == "fav"

    @pytest.mark.asyncio
    async def test_mixed_vector_and_kv_fields_updates_both_stores(self, index):
        """A single call with ``blacklisted=True`` (vector-schema) and
        ``custom_tag="x"`` (KV-only) must:
        * call ``update_doc_fields`` with ONLY the vector-schema subset
          (``{"blacklisted": True}``)
        * write ``custom_tag`` into the KV JSON ``fields`` dict
        * not call ``add_docs`` / ``embed_documents``
        """
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary")])
        index._vector_store.update_doc_fields_calls.clear()
        index._vector_store.add_docs_calls.clear()
        index._embedding_model.embed_documents_calls.clear()
        await index.update_mem_by_id("u", "s", _M1, {
            "blacklisted": True,
            "custom_tag": "x",
        })
        assert len(index._vector_store.update_doc_fields_calls) == 1
        _, _, fields = index._vector_store.update_doc_fields_calls[0]
        assert fields == {"blacklisted": True}
        assert index._vector_store.add_docs_calls == []
        assert index._embedding_model.embed_documents_calls == []
        doc = await index.get_by_id("u", "s", _M1)
        assert doc is not None
        assert doc.blacklisted is True
        assert doc.fields.get("custom_tag") == "x"

    @pytest.mark.asyncio
    async def test_missing_doc_raises(self, index):
        """Updating a non-existent ``mem_id`` must raise ``BaseError``
        with ``STORE_VECTOR_DOC_INVALID`` — the legacy layout uses the KV
        entry as the source of truth for mem_type / collection routing,
        so without it we can't target a vector collection safely.
        """
        from jiuwen_memory.common.exception.codes import StatusCode
        from jiuwen_memory.common.exception.errors import BaseError

        with pytest.raises(BaseError) as exc_info:
            await index.update_mem_by_id("u", "s", "nonexistent_id", {"blacklisted": True})
        assert exc_info.value.status == StatusCode.STORE_VECTOR_DOC_INVALID

    @pytest.mark.asyncio
    async def test_empty_fields_is_noop(self, index):
        """``fields={}`` is a no-op — no KV read, no vector call."""
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary")])
        index._vector_store.update_doc_fields_calls.clear()
        await index.update_mem_by_id("u", "s", _M1, {})
        assert index._vector_store.update_doc_fields_calls == []

    @pytest.mark.asyncio
    async def test_update_memories_still_re_embeds_for_text_change(self, index):
        """``update_memories`` (the batch API) still re-embeds when text
        changes — the scalar-update shortcut doesn't apply to the text
        / embedding path.
        """
        await index.add_memories("u", "s", [_doc(_M1, mem_type="summary", text="hello")])
        index._embedding_model.embed_documents_calls.clear()
        index._vector_store.add_docs_calls.clear()
        await index.update_memories("u", "s", [
            _doc(_M1, mem_type="summary", text="updated content")
        ])
        assert len(index._embedding_model.embed_documents_calls) == 1
        assert len(index._vector_store.add_docs_calls) == 1
