"""Population counts use index metadata, distinct scopes and SEARCH authorization."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    ParsedQuery,
    RetrievalPipeline,
    Scope,
)
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.security import StorageAction
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import Document
from tests.unit.storage.test_graph_relation_reads import RecordingSecurity

pytestmark = pytest.mark.unit


class Tokens:
    def tokenize(self, text):
        return text.split()


def test_count_is_metadata_only_distinct_and_honors_scope_and_filters(monkeypatch):
    store = InMemoryFulltextStore(Tokens())
    home = Scope(org="org", space="research", user="alice", agent="agent")
    a, b = replace(home, session="a"), replace(home, session="b")
    def doc(uid, unit, kind):
        return Document(uid, "private body", {"unit_id": unit, "user_metadata.kind": kind})
    store.insert(a, [doc("c1", "turn1", "raw"), doc("c2", "turn1", "raw"),
                     doc("s", "summary", "summary")])
    store.insert(b, [doc("c1", "turn1", "raw")])
    store.insert(replace(a, user="bob"), [doc("c1", "turn1", "raw")])
    store.insert(replace(a, space=""), [doc("c1", "turn1", "raw")])

    def forbidden(*args, **kwargs):
        raise AssertionError("Counting must not load or search text")

    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr(store, "search", forbidden)
    monkeypatch.setattr(store._tokenizer, "tokenize", forbidden)
    filters = FilterClause("user_metadata.kind", FilterOp.EQ, "raw")
    assert store.count_units(home, filters, descendants=True) == 2
    assert store.count_units(home, filters) == 0
    assert store.count_units(a, filters, descendants=True) == 1


def test_domain_count_authorizes_search_and_uses_configured_l2_port():
    home = Scope(org="org", user="alice")
    selected, other = InMemoryFulltextStore(Tokens()), InMemoryFulltextStore(Tokens())
    selected.insert(replace(home, session="s"), [Document("u", "body", {"unit_id": "u"})])
    security = RecordingSecurity()
    manager = CompositeStoreManager(fulltext={"selected": selected, "default": other},
                                    security=security)
    domain = CompositeDomainStore(manager=manager,
                                  preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK)
    domain.bind_recallers([KeywordRecaller(manager, fulltext_name="selected")])
    assert domain.indexed_population(home, ParsedQuery(raw="ignored question")) == 1
    assert security.calls == [(home, StorageAction.SEARCH, "memory_unit"),
                              (home, StorageAction.SEARCH, "fulltext")]


def test_elasticsearch_count_pages_exact_ids_without_document_sources(monkeypatch):
    from jiuwen_memory.storage.fulltext_impl.elasticsearch_fulltext import (
        ElasticsearchFulltextStore,
    )

    calls = []

    class Client:
        def search(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["size"] == 0 and kwargs["source"] is False
            if len(calls) == 1:
                return {"aggregations": {"units": {"buckets": [{}, {}],
                        "after_key": {"unit": "u2"}}}}
            return {"aggregations": {"units": {"buckets": [{}]}}}

    monkeypatch.setattr(ElasticsearchFulltextStore, "client", property(lambda _: Client()))
    store = ElasticsearchFulltextStore()
    scope = Scope(org="org", user="alice")
    assert store.count_units(scope, descendants=True) == 3
    filters = calls[0]["query"]["bool"]["filter"]
    assert {"term": {"scope.org": "org"}} in filters
    assert {"term": {"scope.space": ""}} in filters
    assert {"term": {"scope.user": "alice"}} in filters
    assert not any("scope.session" in clause.get("term", {}) for clause in filters)
    assert calls[1]["aggregations"]["units"]["composite"]["after"] == {"unit": "u2"}
