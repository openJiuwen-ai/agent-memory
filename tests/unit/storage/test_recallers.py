"""Recall channel tests for keyword, vector, and graph recall."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.errors import BackendError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import RetrievalPipeline, normalize
from jiuwen_memory.common.type_def.entity import (
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.retriever_impl.unit_reader import UnitReader
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, RetrievalQuery
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.domain_store_impl.graph_recaller import GraphRecaller
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.domain_store_impl.vector_recaller import VectorRecaller
from jiuwen_memory.storage.entity_impl.in_memory_entity_store import InMemoryEntityStore
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.fulltext_impl.shard_routed_fulltext import ShardRoutedFulltextStore
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver
from jiuwen_memory.storage.shard_search import STORAGE_SHARD_FIELD, LazyShardLocator
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import Document, Edge, Node, ScoredHit, ScoredID, TextQuery

pytestmark = pytest.mark.unit


class _RecordingVectorStore:
    def __init__(self) -> None:
        self.query = None

    @staticmethod
    def score_higher_is_better() -> bool:
        return True

    def recall(self, scope, query, output_fields=None):
        self.query = query
        return []


class _RecordingFulltextStore(InMemoryFulltextStore):
    def __init__(self) -> None:
        super().__init__(WhitespaceTokenizer())
        self.query = None

    def search(self, scope, query):
        self.query = query
        return []

    @staticmethod
    def get(scope, ids):
        return []


class _PayloadFulltextStore(InMemoryFulltextStore):
    def __init__(self, hits=(), error=None) -> None:
        super().__init__(WhitespaceTokenizer())
        self.hits = list(hits)
        self.error = error
        self.calls = []

    def recall(self, scope, query, output_fields=None):
        self.calls.append((scope, query, output_fields))
        if self.error is not None:
            raise self.error
        return self.hits

    def search(self, scope, query):
        pytest.fail("payload recall must not call search")

    def get(self, scope, ids):
        pytest.fail("payload recall must not call get")


def test_keyword_payload_recall_preserves_maxp_and_query(scope) -> None:
    marker = object()
    store = _PayloadFulltextStore([
        ScoredHit(id="u1-low", score=2.0, metadata={"unit_id": "u1"}),
        ScoredHit(id="u1-high", score=5.0, metadata={"unit_id": "u1"}),
        ScoredHit(id="u2-l0", score=3.0, metadata={"unit_id": "u2"}),
        ScoredHit(id="plain", score=1.0),
    ])
    query = ParsedQuery(
        raw="raw", rewritten="rewritten", scalar_filters=normalize({"priority": {"gte": 2}}),
        extensions={"shards": ["main"], "runtime": marker},
    )
    recaller = KeywordRecaller(CompositeStoreManager(fulltext={"layers_l0": store}), layer="l0")
    result = recaller.recall(scope, query, 4)
    assert [(h.unit_id, h.score) for h in result] == [("u1", 5.0), ("u2", 3.0), ("plain", 1.0)]
    assert all(h.channel is RecallChannel.KEYWORD for h in result)
    seen_scope, tq, fields = store.calls[0]
    assert seen_scope is scope and fields == ["metadata"]
    assert tq.text == "rewritten" and tq.top_k == 4 and tq.filters == query.scalar_filters
    assert tq.extensions == query.extensions and tq.extensions is not query.extensions
    assert tq.extensions["runtime"] is marker


def test_keyword_payload_entities_still_expand_and_filter_true_source(scope, unit_factory) -> None:
    store = _PayloadFulltextStore([
        ScoredHit(id="seed-chunk", score=4.0, metadata={"unit_id": "seed", "entities": ["Alice"]})
    ])
    kv = InMemoryKVStore()
    for uid, project in [("related", "p"), ("filtered", "other")]:
        unit = unit_factory(uid, "unrelated words", scope=scope)
        unit.user_metadata["project"] = project
        kv.insert(scope, memory_key(uid), dumps(unit))
    entities = InMemoryEntityStore()
    record = EntityRecord(
        id="alice", space_id=space_id_from_scope(scope), entity_text="alice", entity_type="PERSON",
        linked_memory_ids=("seed", "related", "filtered"),
        filters=EntityStoreFilters.from_scope(scope), entity_text_hash=hash_entity_text("alice"),
    )
    entities.execute_operations(scope, [EntityOperation(type=EntityOpType.INSERT, record=record)])
    manager = CompositeStoreManager(kv=kv, fulltext=store, entity=entities)
    result = KeywordRecaller(manager, entity_store=manager.entity()).recall(
        scope, ParsedQuery(raw="text", scalar_filters=normalize({"project": "p"})), 3
    )
    assert [h.unit_id for h in result] == ["seed", "related"]
    assert result[0].score == 4.0 and 0 < result[1].score < result[0].score


def test_fulltext_default_recall_is_optional_and_keyword_falls_back(scope) -> None:
    class LegacyFulltextStore(InMemoryFulltextStore):
        def __init__(self):
            super().__init__(WhitespaceTokenizer())
            self.calls = []

        def search(self, scope, query):
            self.calls.append(("search", scope, query))
            return [ScoredID(id="u1-l1", score=4.0)]

        def get(self, scope, ids):
            self.calls.append(("get", scope, ids))
            return [Document(id="u1-l1", text="text", metadata={"unit_id": "u1"})]

    store = LegacyFulltextStore()
    with pytest.raises(NotImplementedError):
        store.recall(scope, TextQuery(text="text"), output_fields=["metadata"])
    assert store.calls == []
    result = KeywordRecaller(CompositeStoreManager(fulltext=store)).recall(
        scope, ParsedQuery(raw="text"), 2
    )
    assert [(h.unit_id, h.score) for h in result] == [("u1", 4.0)]
    assert [call[0] for call in store.calls] == ["search", "get"]
    assert all(call[1] is scope for call in store.calls)
    assert store.calls[1][2] == ["u1-l1"]


@pytest.mark.parametrize("error", [BackendError("backend down"), ValueError("bad payload")])
def test_keyword_payload_recall_propagates_backend_errors(scope, error) -> None:
    store = _PayloadFulltextStore(error=error)
    with pytest.raises(type(error)) as caught:
        KeywordRecaller(CompositeStoreManager(fulltext=store)).recall(
            scope, ParsedQuery(raw="text"), 2
        )
    assert caught.value is error


def test_keyword_payload_recall_empty_does_not_fallback(scope) -> None:
    store = _PayloadFulltextStore()
    assert KeywordRecaller(CompositeStoreManager(fulltext=store)).recall(
        scope, ParsedQuery(raw="text"), 2
    ) == []
    assert len(store.calls) == 1


class _SearchSecurity(StorageSecurity):
    def __init__(self, *, deny=False):
        self.deny = deny
        self.calls = []

    def authorize(self, access, scope, action, resource):
        self.calls.append((access, scope, action, resource))
        if self.deny:
            raise PermissionDeniedError("search denied")


def test_keyword_payload_recall_uses_search_authorization(scope) -> None:
    security = _SearchSecurity()
    store = _PayloadFulltextStore([ScoredHit(id="u1", score=1.0)])
    manager = CompositeStoreManager(fulltext=store, security=security)
    access = StorageAccessContext(actor=scope)
    query = TextQuery(text="text")
    assert manager.fulltext().recall(scope, query, ["metadata"], access=access) == store.hits
    assert security.calls == [(access, scope, StorageAction.SEARCH, "fulltext")]
    security.calls.clear()
    KeywordRecaller(manager).recall(scope, ParsedQuery(raw="text"), 1)
    assert security.calls == [(None, scope, StorageAction.SEARCH, "fulltext")]
    security.deny = True
    with pytest.raises(PermissionDeniedError, match="search denied"):
        KeywordRecaller(manager).recall(scope, ParsedQuery(raw="text"), 1)
    assert len(store.calls) == 2, "denied recall must not reach the backend or fallback"


@pytest.mark.parametrize("mode", ["shared", "split"])
def test_keyword_shard_routed_inherits_fallback_and_keeps_narrowing(scope, mode) -> None:
    resolver = RuleBasedShardResolver(shards=["main", "other"], default_shard="main", rules=[])
    main = InMemoryFulltextStore(WhitespaceTokenizer())
    other = InMemoryFulltextStore(WhitespaceTokenizer())
    main.insert(scope, [Document(
        id="u1-chunk", text="coffee", metadata={"unit_id": "u1", STORAGE_SHARD_FIELD: "main"}
    )])
    target = main if mode == "shared" else other
    target.insert(scope, [Document(
        id="u2-chunk", text="coffee", metadata={"unit_id": "u2", STORAGE_SHARD_FIELD: "other"}
    )])
    kwargs = {"inner": main} if mode == "shared" else {"stores": {"main": main, "other": other}}
    store = ShardRoutedFulltextStore(
        resolver=resolver, locator=LazyShardLocator("default"), mode=mode, **kwargs
    )
    with pytest.raises(NotImplementedError):
        store.recall(scope, TextQuery(text="coffee"), ["metadata"])
    result = KeywordRecaller(CompositeStoreManager(fulltext=store)).recall(
        scope, ParsedQuery(raw="coffee", extensions={"shards": ["main"]}), 1
    )
    assert [hit.unit_id for hit in result] == ["u1"]


class _RecordingGraphStore:
    def __init__(self) -> None:
        self.seed_terms = None
        self.query = None

    def seed_ids(self, scope, terms, *, extensions=None):
        self.seed_terms = terms
        return ["seed"]

    def search(self, scope, query):
        self.query = query
        return []


@pytest.fixture
def indexed_world(world, unit_factory, index_unit_fn):
    index_unit_fn(world, unit_factory("u1", "alice likes coffee"))
    index_unit_fn(world, unit_factory("u2", "bob likes tea"))
    return world


def test_keyword_recall_matches_token(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))

    ids = {result.unit_id for result in indexed_world.keyword.recall(scope, parsed, 10)}

    assert "u1" in ids
    assert "u2" not in ids


def test_vector_recall_ranks_relevant_first(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))

    results = indexed_world.vector_recaller.recall(scope, parsed, 10)

    assert results
    assert results[0].unit_id == "u1"


def test_vector_recall_empty_without_vector(world, scope) -> None:
    results = world.vector_recaller.recall(scope, ParsedQuery(raw="x"), 10)

    assert results == []


def test_vector_recaller_forwards_runtime_extension_identity(scope) -> None:
    """Unit boundary: VectorRecaller preserves a live extension object into VectorQuery."""
    marker = object()
    vector_store = _RecordingVectorStore()
    vector = VectorRecaller(CompositeStoreManager(vector=vector_store))
    vector.recall(
        scope,
        ParsedQuery(raw="x", vector=[0.1], extensions={"db_query_service": marker}),
        10,
    )
    assert vector_store.query.extensions["db_query_service"] is marker


def test_keyword_recaller_forwards_runtime_extension_identity(scope) -> None:
    """Unit boundary: KeywordRecaller preserves a live extension object into TextQuery."""
    marker = object()
    fulltext_store = _RecordingFulltextStore()
    keyword = KeywordRecaller(CompositeStoreManager(fulltext=fulltext_store))
    keyword.recall(
        scope,
        ParsedQuery(raw="x", extensions={"encryption_port": marker}),
        10,
    )
    assert fulltext_store.query.extensions["encryption_port"] is marker


def test_graph_recaller_forwards_runtime_extension_identity(scope) -> None:
    """Unit boundary: GraphRecaller preserves a live extension object into GraphQuery."""
    marker = object()
    graph_store = _RecordingGraphStore()
    recaller = GraphRecaller(CompositeStoreManager(graph=graph_store))

    recaller.recall(
        scope,
        ParsedQuery(raw="coffee", keywords=["coffee"], extensions={"graph_runtime": marker}),
        10,
    )

    assert graph_store.query.extensions["graph_runtime"] is marker


def test_pipeline_forwards_runtime_extension_identity_to_all_store_queries(scope) -> None:
    """Prove runtime-object identity from RetrievalQuery through each Store query.

    Marker values cannot cross JSON, so this covers in-process plugin objects only. HTTP tests cover
    JSON-compatible extension values separately.
    """
    vector_store = _RecordingVectorStore()
    fulltext_store = _RecordingFulltextStore()
    graph_store = _RecordingGraphStore()
    storage = CompositeStoreManager(
        kv=InMemoryKVStore(),
        vector=vector_store,
        fulltext=fulltext_store,
        graph=graph_store,
    )
    tokenizer = WhitespaceTokenizer()
    parser = SimpleQueryParser(tokenizer, HashingEmbedder(tokenizer))
    recallers = [
        VectorRecaller(storage),
        KeywordRecaller(storage),
        GraphRecaller(storage),
    ]
    domain_store = CompositeDomainStore(
        manager=storage, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK
    )
    domain_store.bind_recallers(recallers)
    storage.bind_domain_store(domain_store)
    retriever = PipelineRetriever(
        parser,
        RRFFuser(),
        TruncatingDiscloser(),
        UnitReader(storage.kv()),
        domain_store=domain_store,
    )
    vector_marker = object()
    text_marker = object()
    graph_marker = object()

    result = retriever.retrieve(
        scope,
        RetrievalQuery(
            text="coffee",
            top_k=1,
            channels=[RecallChannel.VECTOR, RecallChannel.KEYWORD, RecallChannel.GRAPH],
            extensions={
                "db_query_service": vector_marker,
                "encryption_port": text_marker,
                "graph_runtime": graph_marker,
            },
        ),
    )

    assert result.errors == []
    assert vector_store.query.extensions["db_query_service"] is vector_marker
    assert fulltext_store.query.extensions["encryption_port"] is text_marker
    assert graph_store.query.extensions["graph_runtime"] is graph_marker


def test_vector_recall_min_similarity_filters(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))
    baseline = indexed_world.vector_recaller.recall(scope, parsed, 10)
    assert baseline, "基线应有语义命中"
    top = baseline[0].score

    # 阈值高于最高分 → 全部砍掉（证明前置过滤生效）。
    storage = CompositeStoreManager(vector=indexed_world.vector)
    strict = VectorRecaller(storage, min_similarity=top + 0.01)
    assert strict.recall(scope, parsed, 10) == []

    # 阈值低于最高分 → 至少保留最相关的那条。
    loose = VectorRecaller(storage, min_similarity=top - 0.01)
    kept = loose.recall(scope, parsed, 10)
    assert kept and kept[0].unit_id == baseline[0].unit_id


class _LowerIsBetterStore:
    """契约桩：按 VectorStore 接口声明分数为距离型（越小越相关）。"""

    @staticmethod
    def score_higher_is_better() -> bool:
        return False


def test_vector_recaller_rejects_lower_is_better_metric() -> None:
    # MaxP 与融合统一要求高分优先；距离型度量无论是否开阈值都拒绝。
    with pytest.raises(ValidationError):
        VectorRecaller(CompositeStoreManager(vector=_LowerIsBetterStore()), min_similarity=0.5)

    with pytest.raises(ValidationError):
        VectorRecaller(CompositeStoreManager(vector=_LowerIsBetterStore()), min_similarity=0.0)


def test_graph_recaller_returns_seed_neighbor(scope) -> None:
    graph = InMemoryGraphStore()
    graph.insert(
        scope,
        nodes=[
            Node(id="A", properties={"content": "coffee origin"}),
            Node(id="B", properties={"content": "latte recipe"}),
        ],
        edges=[Edge(id="e", source="A", target="B", relation="related")],
    )
    recaller = GraphRecaller(CompositeStoreManager(graph=graph))

    results = recaller.recall(scope, ParsedQuery(raw="coffee", keywords=["coffee"]), 10)

    assert "B" in {result.unit_id for result in results}


# ---------------------------------------------------------------------------
# L0/L1 分层召回（store 为 None 时跳过；layer 参数正确）
# ---------------------------------------------------------------------------


def test_vector_recaller_layer_none_store_returns_empty(scope) -> None:
    """L0/L1 recaller store 未注入（None）→ recall 返空，不报错。"""
    storage = CompositeStoreManager()
    recaller = VectorRecaller(storage, layer="l0")
    parsed = ParsedQuery(raw="x", vector=[0.1, 0.2, 0.3])
    assert recaller.recall(scope, parsed, 10) == []

    recaller_l1 = VectorRecaller(storage, layer="l1")
    assert recaller_l1.recall(scope, parsed, 10) == []


def test_keyword_recaller_layer_none_store_returns_empty(scope) -> None:
    """L0/L1 keyword recaller store 未注入 → recall 返空。"""
    recaller = KeywordRecaller(CompositeStoreManager(), layer="l0")
    parsed = ParsedQuery(raw="x", keywords=["x"])
    assert recaller.recall(scope, parsed, 10) == []


def test_vector_recaller_layer_param_set() -> None:
    """layer 参数正确传入（l2/l0/l1）。"""
    storage = CompositeStoreManager()
    r_l2 = VectorRecaller(storage, layer="l2")
    r_l0 = VectorRecaller(storage, layer="l0")
    r_l1 = VectorRecaller(storage, layer="l1")
    assert r_l2.layer == "l2"
    assert r_l0.layer == "l0"
    assert r_l1.layer == "l1"


def test_keyword_recaller_layer_param_set() -> None:
    """layer 参数正确传入。"""
    storage = CompositeStoreManager()
    assert KeywordRecaller(storage, layer="l2").layer == "l2"
    assert KeywordRecaller(storage, layer="l0").layer == "l0"
    assert KeywordRecaller(storage, layer="l1").layer == "l1"
