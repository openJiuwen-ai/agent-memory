"""Memory index queries score stable snapshots while writers continue."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.storage.fulltext_impl import in_memory_fulltext_store as text_module
from jiuwen_memory.storage.types import Document, TextQuery, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl import in_memory_vector_store as vector_module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("method", ["search", "recall"])
def test_vector_scoring_does_not_block_writes_or_observe_index_changes(monkeypatch, method):
    scope = Scope(org="demo", space="project")
    store = vector_module.InMemoryVectorStore()
    store.insert(scope, [VectorRecord(key, [1.0, 0.0]) for key in ("a", "c")])
    entered, resume = threading.Event(), threading.Event()
    evaluate = vector_module.evaluate

    def paused_evaluate(*args):
        entered.set()
        assert resume.wait(5), "query was not released"
        return evaluate(*args)

    def write():
        store.insert(scope, [VectorRecord("b", [1.0, 0.0])])
        store.update(scope, [VectorRecord("a", [0.0, 1.0])])
        store.delete(scope, ["c"])

    monkeypatch.setattr(vector_module, "evaluate", paused_evaluate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(getattr(store, method), scope, VectorQuery([1.0, 0.0]))
        try:
            assert entered.wait(3), "query did not reach scoring"
            pool.submit(write).result(timeout=3)
        finally:
            resume.set()
        assert {hit.id for hit in query.result(timeout=3)} == {"a", "c"}
    assert {hit.id for hit in store.search(scope, VectorQuery([1.0, 0.0]))} == {"b"}


def test_fulltext_scoring_uses_matching_document_and_token_snapshot(monkeypatch):
    scope = Scope(org="demo", space="project")
    store = text_module.InMemoryFulltextStore(WhitespaceTokenizer())
    store.insert(scope, [Document(key, "memory progress") for key in ("a", "c")])
    entered, resume = threading.Event(), threading.Event()
    evaluate = text_module.evaluate

    def paused_evaluate(*args):
        entered.set()
        assert resume.wait(5), "query was not released"
        return evaluate(*args)

    def write():
        store.insert(scope, [Document("b", "memory progress")])
        store.update(scope, [Document("a", "unrelated")])
        store.delete(scope, ["c"])

    monkeypatch.setattr(text_module, "evaluate", paused_evaluate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(store.search, scope, TextQuery("memory"))
        try:
            assert entered.wait(3), "query did not reach scoring"
            pool.submit(write).result(timeout=3)
        finally:
            resume.set()
        assert {hit.id for hit in query.result(timeout=3)} == {"a", "c"}
    assert {hit.id for hit in store.search(scope, TextQuery("memory"))} == {"b"}


@pytest.mark.parametrize("kind", ["vector", "fulltext"])
def test_index_records_do_not_alias_callers_or_get_results(kind):
    scope = Scope(org="demo", space="project")
    if kind == "vector":
        store = vector_module.InMemoryVectorStore()
        record = VectorRecord("a", [1.0], {"labels": ["original"]})
    else:
        store = text_module.InMemoryFulltextStore(WhitespaceTokenizer())
        record = Document("a", "memory", {"labels": ["original"]})
    store.insert(scope, [record])
    record.metadata["labels"].append("input mutation")
    retrieved = store.get(scope, ["a"])[0]
    assert retrieved.metadata["labels"] == ["original"]
    retrieved.metadata["labels"].append("output mutation")
    assert store.get(scope, ["a"])[0].metadata["labels"] == ["original"]
