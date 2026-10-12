"""InMemoryFulltextStore BM25 ranking tests."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery

pytestmark = pytest.mark.unit

SCOPE = Scope(org="test", user="u1")


def test_bm25_ranks_exact_identifier_above_shared_stem_short_doc() -> None:
    store = InMemoryFulltextStore(WhitespaceTokenizer())
    store.insert(
        SCOPE,
        [
            Document(
                id="target",
                text=(
                    "[S1-2 smoke] token=S1SMOKE-01 role=<agent> 冒烟标记（2026-09-27） "
                    "target A target B target C target D target E target F target G target H"
                ),
            ),
            Document(id="stale-short", text="旧版 s1smoke 记录"),
            Document(id="none", text="completely unrelated content"),
        ],
    )

    hits = store.search(SCOPE, TextQuery(text="S1SMOKE-01", top_k=5))

    assert hits[0].id == "target", f"expected target first, got {hits[0].id}"
    assert hits[0].score > next(h.score for h in hits if h.id == "stale-short")
    assert "none" not in {h.id for h in hits}
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
