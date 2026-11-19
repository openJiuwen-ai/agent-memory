import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.retrieval.fuser_impl.bm25_scored_fuser import _bm25_scores
from jiuwen_memory.storage.fulltext import FulltextProducer
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery

SCOPE = Scope(org="o", user="a")
OTHER = Scope(org="o", user="b")


def doc(uid, text):
    return Document(uid, text, {"unit_id": uid})


def test_term_frequency_length_repeated_queries_and_scope_match_batch_reference():
    tok = WhitespaceTokenizer()
    store = InMemoryFulltextStore(tok, scoring="bm25")
    docs = [
        doc("a", "the weekend"),
        doc("b", "dresser dresser bedroom the"),
        doc("c", "the time"),
        doc("d", ""),
    ]
    store.insert(SCOPE, docs)
    query = TextQuery("the dresser dresser")
    expected = _bm25_scores(
        [tok.tokenize(d.text) for d in docs], tok.tokenize(query.text), 1.2, 0.75
    )
    store.insert(OTHER, [doc("a", "dresser")])
    result = store.search(SCOPE, query)
    assert result[0].id == "b"
    assert {r.id: r.score for r in result} == pytest.approx(
        {d.id: v for d, v in zip(docs, expected) if v}
    )
    allowed = TextQuery(
        query.text, top_k=1, filters=[FilterClause("unit_id", FilterOp.IN, ["a", "c"])]
    )
    assert [r.id for r in store.search(SCOPE, allowed)] == ["a"]
    assert [r.id for r in store.search(OTHER, query)] == ["a"]


def test_local_updates_deletions_rebuild_and_failed_batch_are_consistent():
    store = InMemoryFulltextStore(WhitespaceTokenizer(), scoring="bm25")
    store.insert(SCOPE, [doc("a", "blue"), doc("b", "blue chair")])
    store.update(SCOPE, [doc("a", "map map")])
    assert [r.id for r in store.search(SCOPE, TextQuery("blue"))] == ["b"]
    before = store.search(SCOPE, TextQuery("map"))
    with pytest.raises(ConflictError):
        store.insert(SCOPE, [doc("c", "map"), doc("a", "other")])
    with pytest.raises(NotFoundError):
        store.update(SCOPE, [doc("a", "removed"), doc("absent", "new")])
    store.rebuild_keyword_statistics()
    assert store.search(SCOPE, TextQuery("map")) == before
    store.delete(SCOPE, ["b", "b"])
    assert store.search(SCOPE, TextQuery("blue")) == []
    assert [r.id for r in store.search(SCOPE, TextQuery("map"))] == ["a"]


def test_default_scoring_and_configuration_validation():
    store = InMemoryFulltextStore(WhitespaceTokenizer())
    store.insert(SCOPE, [doc("a", "red blue"), doc("b", "red")])
    assert [(r.id, r.score) for r in store.search(SCOPE, TextQuery("red"))] == [
        ("b", 1.0),
        ("a", 0.5),
    ]
    with pytest.raises(ValidationError):
        InMemoryFulltextStore(WhitespaceTokenizer(), scoring="other")


def test_example_default_and_per_store_override_use_native_backend_binding():
    ctx = default_context()
    ctx.globals["memory_fulltext_scoring"] = "bm25"
    docs = [doc("a", "the the"), doc("b", "dresser the"), doc("c", "the time"), doc("d", "the day")]
    for params, leader in [({}, "b"), ({"scoring": "overlap"}, "a")]:
        store = FulltextProducer.build("memory", {"tokenizer": "default", **params}, ctx)
        store.insert(SCOPE, docs)
        assert store.search(SCOPE, TextQuery("the dresser"))[0].id == leader


def test_recursive_summary_score_ties_use_source_position_before_top_k():
    def summary(uid, start):
        return Document(
            uid,
            "same term",
            {
                "unit_id": uid,
                "hierarchy_role": "time_span",
                "tree_span_start": start,
                "tree_span_end": start + 1,
                "system_metadata.graphmem_session_scope": "same-session",
            },
        )

    for earlier, later in (("z", "a"), ("b", "y")):
        stable = InMemoryFulltextStore(
            WhitespaceTokenizer(), scoring="bm25", stable_hierarchy_ties=True
        )
        baseline = InMemoryFulltextStore(WhitespaceTokenizer(), scoring="bm25")
        docs = [summary(later, 20), summary(earlier, 10)]
        for store in (stable, baseline):
            store.insert(SCOPE, docs)
        assert stable.search(SCOPE, TextQuery("same", top_k=1))[0].id == earlier
        assert {r.id: r.score for r in stable.search(SCOPE, TextQuery("same"))} == {
            r.id: r.score for r in baseline.search(SCOPE, TextQuery("same"))
        }
