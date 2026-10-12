"""KeywordDedup similarity contract tests."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.dedup_impl.keyword_dedup import KeywordDedup
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import Document, ScoredID, TextQuery
from tests.conftest import make_storage

pytestmark = pytest.mark.unit

SCOPE = Scope(org="test", user="u1")


class _FixedScoreFulltextStore(FulltextStore):
    """Return fixed backend scores to prove Dedup does not consume them."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, ...], dict[str, Document]] = {}

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def health(self) -> None:
        return None

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        bucket = self._docs.setdefault(
            (scope.org, scope.space, scope.user, scope.agent, scope.session), {}
        )
        for doc in docs:
            bucket[doc.id] = doc

    def update(self, scope: Scope, docs: list[Document]) -> None:
        self.insert(scope, docs)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        key = (scope.org, scope.space, scope.user, scope.agent, scope.session)
        for doc_id in ids:
            self._docs.get(key, {}).pop(doc_id, None)

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        bucket = self._docs.get(
            (scope.org, scope.space, scope.user, scope.agent, scope.session), {}
        )
        return [bucket[doc_id] for doc_id in ids if doc_id in bucket]

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        hits = [
            ScoredID(id="exact", score=0.01),
            ScoredID(id="partial", score=99.0),
        ]
        return hits[: query.top_k]


def _unit(unit_id: str, content: str) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=SCOPE,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=LifecycleState.ACTIVE,
        temporal=Temporal(),
    )


def _index(kv: InMemoryKVStore, fulltext: FulltextStore, unit: MemoryUnit) -> None:
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    fulltext.insert(unit.scope, [Document(id=unit.id, text=unit.content)])


def _dedup(
    kv: InMemoryKVStore,
    fulltext: FulltextStore,
    **kwargs,
) -> KeywordDedup:
    return KeywordDedup(
        storage=make_storage(kv=kv, fulltext=fulltext),
        tokenizer=WhitespaceTokenizer(),
        top_k=10,
        tier_filter=False,
        scope_filter=False,
        **kwargs,
    )


def test_exact_duplicate_returns_jaccard_one_despite_low_bm25_score() -> None:
    kv = InMemoryKVStore()
    fulltext = InMemoryFulltextStore(WhitespaceTokenizer())
    existing = _unit("target", "alpha")
    _index(kv, fulltext, existing)

    raw_hits = fulltext.search(SCOPE, TextQuery(text="alpha", top_k=5))
    assert raw_hits[0].score < 0.5

    hits = _dedup(kv, fulltext).recall(_unit("candidate", "alpha"))
    assert [(unit.id, score) for unit, score in hits] == [("target", 1.0)]


def test_partial_overlap_is_filtered_despite_high_bm25_score() -> None:
    kv = InMemoryKVStore()
    fulltext = InMemoryFulltextStore(WhitespaceTokenizer())
    for i in range(9):
        _index(kv, fulltext, _unit(f"noise-{i}", f"beta delta {i}"))
    _index(kv, fulltext, _unit("partial", "alpha gamma"))

    raw_hits = fulltext.search(SCOPE, TextQuery(text="alpha beta", top_k=10))
    raw_score = next(hit.score for hit in raw_hits if hit.id == "partial")
    assert raw_score > 0.9

    hits = _dedup(kv, fulltext).recall(_unit("candidate", "alpha beta"))
    assert hits == []


def test_backend_scores_do_not_determine_keyword_dedup_similarity() -> None:
    kv = InMemoryKVStore()
    fulltext = _FixedScoreFulltextStore()
    _index(kv, fulltext, _unit("exact", "alpha beta"))
    _index(kv, fulltext, _unit("partial", "alpha gamma"))

    candidate = _unit("candidate", "alpha beta")
    default_hits = _dedup(kv, fulltext).recall(candidate)
    assert [(unit.id, score) for unit, score in default_hits] == [("exact", 1.0)]

    all_hits = _dedup(kv, fulltext, min_similarity=0.0).recall(candidate)
    assert [(unit.id, score) for unit, score in all_hits] == [
        ("exact", 1.0),
        ("partial", 1 / 3),
    ]
    assert [score for _, score in all_hits] == sorted(
        (score for _, score in all_hits), reverse=True
    )


def test_no_lexical_hit_returns_empty() -> None:
    kv = InMemoryKVStore()
    fulltext = InMemoryFulltextStore(WhitespaceTokenizer())
    _index(kv, fulltext, _unit("unrelated", "gamma delta"))

    assert fulltext.search(SCOPE, TextQuery(text="alpha beta", top_k=5)) == []
    assert _dedup(kv, fulltext).recall(_unit("candidate", "alpha beta")) == []


def test_empty_token_union_scores_zero() -> None:
    kv = InMemoryKVStore()
    fulltext = _FixedScoreFulltextStore()
    _index(kv, fulltext, _unit("exact", "---"))

    hits = _dedup(kv, fulltext, min_similarity=0.0).recall(_unit("candidate", "---"))
    assert [(unit.id, score) for unit, score in hits] == [("exact", 0.0)]
