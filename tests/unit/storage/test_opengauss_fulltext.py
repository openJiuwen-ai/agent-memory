"""Native BM25 store contracts, including unsafe execution-plan rejection."""

from __future__ import annotations

# pylint: disable=protected-access  # White-box assertions verify schema and BM25 cache safety.
import json

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import ParsedQuery, Scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.domain_store_impl.vector_recaller import VectorRecaller
from jiuwen_memory.storage.fulltext import FulltextProducer
from jiuwen_memory.storage.fulltext_impl.opengauss_fulltext import OpenGaussFulltextStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import Document, ScoredHit, ScoredID, TextQuery
from jiuwen_memory.storage.vector_impl.opengauss_vector import OpenGaussVectorStore
from tests.unit.storage.opengauss_fixtures import RecordingCursor, RecordingPool, bind_cursor

pytestmark = pytest.mark.unit
SCOPE = Scope(org="org", space="space", user="user", agent="agent", session="session")
DSN = "postgresql://user:secret@example.invalid:5432/memory"


def make_store(**kwargs):
    return OpenGaussFulltextStore(dsn=DSN, **kwargs)


def bm25_plan(index="bm25_index"):
    return [
        {
            "Plan": {
                "Node Type": "Limit",
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": index,
                        "Order By": "(content <&> 'query'::text)",
                    }
                ],
            }
        }
    ]


def search_cursor(monkeypatch, store, *, rows=(), plan=None, refreshed=None):
    """Store as after pool creation: the BM25 index name is already cached."""
    store._bm25_index_names = frozenset({"bm25_index"})
    batches = [list(rows)] if refreshed is None else [list(refreshed), list(rows)]
    return bind_cursor(
        monkeypatch,
        store,
        RecordingCursor(one=[(bm25_plan() if plan is None else plan,)], batches=batches),
    )


def settings_of(cursor) -> dict[str, str]:
    sql, args = next(c for c in cursor.calls if "set_config" in c[0])
    assert sql.count("set_config(%s, %s, true)") == len(args) // 2
    return dict(zip(args[::2], args[1::2]))


def test_factory_registration_and_lazy_parameters():
    register_backends()
    assert "opengauss" in FulltextProducer.known()
    with pytest.raises(ValidationError, match="dsn"):
        FulltextProducer.build("opengauss", {}, AssemblyContext())
    store = FulltextProducer.build("opengauss", {"dsn": DSN}, AssemblyContext())
    assert isinstance(store, OpenGaussFulltextStore)
    assert store._lease is None and store._config_namespace == "fulltext_store"
    assert "secret" not in store.resource_descriptor()


@pytest.mark.parametrize(
    "options",
    [
        {"bm25_k1": -1},
        {"bm25_k1": 3.1},
        {"bm25_b": 1.1},
        {"bm25_b": True},
        {"max_score_ratio": 0.49},
        {"max_score_ratio": 1.4},
        {"bm25_k1": "invalid"},
        {"bm25_b": float("nan")},
    ],
)
def test_bad_bm25_settings_fail_without_connection(options):
    with pytest.raises(ValidationError):
        make_store(**options)


def test_schema_creates_native_single_field_bm25_and_caches_index_name():
    store = make_store()
    cursor = RecordingCursor(one=[(1,), None], batches=[[("bm25_index",)]])
    store._ensure_schema(cursor)
    statements = "\n".join(sql for sql, _ in cursor.calls)
    assert "USING bm25 (content)" in statements
    assert "content text NOT NULL" in statements
    assert "indpred IS NULL" in statements and "indexprs IS NULL" in statements
    assert "indnatts = 1" in statements and "am.amname = 'bm25'" in statements
    assert "a.attname = 'content'" in statements and "%s::regclass" in statements
    assert "pg_extension" not in statements
    assert store._bm25_index_names == {"bm25_index"}


def test_precreated_table_must_have_bm25_index():
    store = make_store(auto_create_schema=False)
    cursor = RecordingCursor(one=[(1,)])
    with pytest.raises(ValidationError, match="BM25"):
        store._ensure_schema(cursor)
    assert not any(sql.startswith("CREATE") for sql, _ in cursor.calls)


def test_recall_returns_complete_metadata_without_point_read(monkeypatch):
    store = make_store()
    metadata = {
        "unit_id": "u1",
        "entities": ["Alice", "openGauss"],
        "system_metadata.project": "p",
        "user_metadata.custom": {"nested": [1, None, True]},
        "other": "完整元数据",
    }
    cursor = search_cursor(
        monkeypatch, store, rows=[("u1-chunk0", 3.0, json.dumps(metadata))]
    )
    hits = store.recall(
        SCOPE,
        TextQuery(text="医疗 memory", top_k=7, filters={"priority": {"gte": 5}}),
        output_fields=["metadata"],
    )
    assert [(h.id, h.score, h.metadata) for h in hits] == [("u1-chunk0", 3.0, metadata)]
    assert len(cursor.calls) == 3, "settings + EXPLAIN + one payload SELECT; no get/catalog"
    explain_sql, explain_args = cursor.calls[1]
    sql, args = cursor.calls[2]
    assert sql == explain_sql.removeprefix("EXPLAIN (FORMAT JSON) ")
    assert args == explain_args
    assert "metadata::text" in sql and "SELECT id, content <&> %s AS score" in sql
    assert "scope_org = %s" in sql and "scope_session = %s" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert args[:6] == ("医疗 memory", "#org", "#space", "#user", "#agent", "#session")
    assert args[-2:] == ("医疗 memory", 7)


@pytest.mark.parametrize("fields", [None, [], ["content"], ["metadata", "content"], ["metadata"]])
def test_recall_output_fields_and_search_results_remain_compatible(monkeypatch, caplog, fields):
    store = make_store()
    payload = fields is not None and "metadata" in fields
    row = ("a", 2.0, '{"unit_id":"u1","entities":["Alice"]}') if payload else ("a", 2.0)
    cursor = search_cursor(monkeypatch, store, rows=[row])
    with caplog.at_level("INFO"):
        hits = store.recall(SCOPE, TextQuery(text="q"), fields)
    assert isinstance(hits[0], ScoredHit)
    assert hits[0].metadata == ({"unit_id": "u1", "entities": ["Alice"]} if payload else {})
    assert ("metadata::text" in cursor.calls[-1][0]) is payload
    if fields and "content" in fields:
        assert "supports only the metadata" in caplog.text
    search_cursor(monkeypatch, store, rows=[("a", 2.0)])
    search_hits = store.search(SCOPE, TextQuery(text="q"))
    assert isinstance(search_hits[0], ScoredID) and search_hits[0].metadata is None
    assert [(h.id, h.score) for h in search_hits] == [(h.id, h.score) for h in hits]


def test_recall_verifies_every_plan_and_refreshes_only_unknown_index_names(monkeypatch):
    store = make_store()
    cursor = search_cursor(monkeypatch, store, rows=[("a", 2.0, '{}')])
    assert store.recall(SCOPE, TextQuery(text="q"), ["metadata"])[0].id == "a"
    cursor.one.append((bm25_plan("rebuilt"),))
    cursor.batches.extend([[('rebuilt',)], [("b", 1.0, '{}')]])
    assert store.recall(SCOPE, TextQuery(text="q"), ["metadata"])[0].id == "b"
    assert sum(sql.startswith("EXPLAIN") for sql, _ in cursor.calls) == 2
    assert sum("pg_index" in sql for sql, _ in cursor.calls) == 1
    assert store._bm25_index_names == {"rebuilt"}


def test_recall_refuses_missing_plan_and_invalid_metadata(monkeypatch):
    store = make_store()
    cursor = search_cursor(monkeypatch, store)
    cursor.one.clear()
    with pytest.raises(BackendError, match="no plan"):
        store.recall(SCOPE, TextQuery(text="q"), ["metadata"])
    assert cursor.calls[-1][0].startswith("EXPLAIN")
    search_cursor(monkeypatch, store, rows=[("a", 1.0, "not json")])
    with pytest.raises(ValueError):
        store.recall(SCOPE, TextQuery(text="q"), ["metadata"])


def test_search_uses_native_score_and_filters_inside_limit(monkeypatch):
    store = make_store()
    cursor = search_cursor(monkeypatch, store, rows=[("a", 3.0), ("b", 1.5)])
    hits = store.search(
        SCOPE,
        TextQuery(
            text="医疗 memory",
            top_k=10,
            filters={
                "AND": [
                    {"priority": {"gte": 5}},
                    {"tags": {"contains": "work"}},
                ]
            },
        ),
    )
    assert [(h.id, h.score) for h in hits] == [("a", 3.0), ("b", 1.5)]
    explain_sql, explain_args = next(c for c in cursor.calls if c[0].startswith("EXPLAIN"))
    sql, args = cursor.calls[-1]
    assert sql == explain_sql.removeprefix("EXPLAIN (FORMAT JSON) ") and args == explain_args
    assert "WHERE scope_org = %s" in sql and "scope_session = %s" in sql
    assert "metadata @>" in sql and sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert "ORDER BY content <&> %s DESC LIMIT %s" in sql
    assert args[:6] == ("医疗 memory", "#org", "#space", "#user", "#agent", "#session")
    assert args[-2:] == ("医疗 memory", 10)
    assert "WITH" not in sql and "score >" not in sql
    assert settings_of(cursor) == {
        "enable_seqscan": "off",
        "enable_bitmapscan": "off",
        "enable_indexscan": "on",
        "enable_bm25_taat": "off",
        "bm25_topk": "10",
        "bm25_k1": "1.2",
        "bm25_b": "0.75",
        "max_score_ratio": "1.05",
    }
    assert not any(statement.startswith("SET ") for statement, _ in cursor.calls)


def test_search_skips_catalog_lookup_when_plan_uses_cached_index(monkeypatch):
    store = make_store()
    cursor = search_cursor(monkeypatch, store, rows=[("a", 1.0)])
    store.search(SCOPE, TextQuery(text="q"))
    statements = [sql for sql, _ in cursor.calls]
    assert len(statements) == 3, statements
    assert not any("pg_index" in sql or sql.startswith("LOCK") for sql in statements)


def test_search_refreshes_cache_when_index_was_recreated(monkeypatch):
    store = make_store()
    cursor = search_cursor(
        monkeypatch,
        store,
        plan=bm25_plan("bm25_rebuilt"),
        refreshed=[("bm25_rebuilt",)],
        rows=[("a", 2.0)],
    )
    assert store.search(SCOPE, TextQuery(text="q"))[0].id == "a"
    assert any("pg_index" in sql for sql, _ in cursor.calls)
    assert store._bm25_index_names == {"bm25_rebuilt"}


@pytest.mark.parametrize("method", ["search", "recall"])
def test_search_rejects_plan_index_that_is_not_a_valid_bm25_index(monkeypatch, method):
    store = make_store()
    cursor = search_cursor(monkeypatch, store, plan=bm25_plan("btree_scope"), refreshed=[])
    with pytest.raises(BackendError, match="ordered BM25 index scan"):
        kwargs = {"output_fields": ["metadata"]} if method == "recall" else {}
        getattr(store, method)(SCOPE, TextQuery(text="q"), **kwargs)
    assert not any(sql.startswith("SELECT id, content <&>") for sql, _ in cursor.calls)


@pytest.mark.parametrize("top_k,initial", [(1, 5), (10, 10), (500, 200)])
def test_bm25_candidate_guc_is_not_confused_with_final_limit(monkeypatch, top_k, initial):
    store = make_store()
    cursor = search_cursor(monkeypatch, store)
    assert store.search(SCOPE, TextQuery(text="query", top_k=top_k)) == []
    assert settings_of(cursor)["bm25_topk"] == str(initial)
    assert cursor.calls[-1][1][-1] == top_k


@pytest.mark.parametrize(
    "plan",
    [
        [{"Plan": {"Node Type": "Seq Scan"}}],
        [{"Plan": {"Node Type": "Sort", "Plans": [bm25_plan()[0]["Plan"]]}}],
        [{"Plan": {"Node Type": "Index Scan", "Index Name": "bm25_index"}}],
        [],
        "not json",
    ],
)
@pytest.mark.parametrize("method", ["search", "recall"])
def test_unsafe_plans_fail_before_scoring_select(monkeypatch, plan, method):
    store = make_store()
    cursor = search_cursor(monkeypatch, store, plan=plan)
    with pytest.raises(BackendError):
        kwargs = {"output_fields": ["metadata"]} if method == "recall" else {}
        getattr(store, method)(SCOPE, TextQuery(text="query"), **kwargs)
    assert cursor.calls[-1][0].startswith("EXPLAIN")
    assert not any(sql.startswith("SELECT id, content <&>") for sql, _ in cursor.calls)


def test_json_string_plan_is_accepted(monkeypatch):
    store = make_store()
    search_cursor(monkeypatch, store, plan=json.dumps(bm25_plan()), rows=[("a", 1.0)])
    assert store.search(SCOPE, TextQuery(text="q"))[0].id == "a"


def test_dropped_index_never_falls_back_to_seqscan(monkeypatch):
    store = make_store()
    plan = [{"Plan": {"Node Type": "Limit", "Plans": [{"Node Type": "Seq Scan"}]}}]
    cursor = search_cursor(monkeypatch, store, plan=plan)
    with pytest.raises(BackendError, match="ordered BM25 index scan"):
        store.search(SCOPE, TextQuery(text="query"))
    assert not any(sql.startswith("SELECT id, content <&>") for sql, _ in cursor.calls)


def test_empty_query_and_empty_crud_do_not_connect():
    store = make_store()
    store.insert(SCOPE, [])
    store.update(SCOPE, [])
    store.delete(SCOPE, [])
    assert store.get(SCOPE, []) == []
    assert store.search(SCOPE, TextQuery(text=" \n\t")) == []
    assert store.recall(SCOPE, TextQuery(text=" \n\t"), ["metadata"]) == []
    assert store._lease is None


@pytest.mark.parametrize("text", ["bad\0text", 123])
def test_invalid_text_rejected_before_connection(text):
    store = make_store()
    with pytest.raises(ValidationError):
        store.insert(SCOPE, [Document(id="bad", text=text)])
    with pytest.raises(ValidationError):
        store.search(SCOPE, TextQuery(text=text))
    with pytest.raises(ValidationError):
        store.recall(SCOPE, TextQuery(text=text), ["metadata"])


def test_document_insert_and_update_preserve_json_paths(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[], [("a",)]]))
    document = Document(id="a", text="原文", metadata={"system_metadata.project": "p"})
    store.insert(SCOPE, [document])
    store.update(SCOPE, [Document(id="a", text="新文", metadata=document.metadata)])
    writes = [(sql, args) for sql, args in cursor.calls if sql.startswith(("INSERT", "UPDATE"))]
    assert writes[0][1][:6] == ("a", "#org", "#space", "#user", "#agent", "#session")
    assert writes[0][1][-1] == '{"system_metadata.project":"p"}'
    assert writes[1][1][:2] == ("新文", '{"system_metadata.project":"p"}')
    assert writes[1][1][-6:] == ("#org", "#space", "#user", "#agent", "#session", "a")


def test_conflict_preflight_does_not_write_any_bm25_document(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[("old",)]]))
    with pytest.raises(ConflictError):
        store.insert(SCOPE, [Document(id="new", text="new"), Document(id="old", text="old")])
    assert not any(sql.startswith("INSERT") for sql, _ in cursor.calls)


def test_update_missing_preflight_does_not_change_any_document(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[("a",)]]))
    with pytest.raises(NotFoundError):
        store.update(SCOPE, [Document(id="a", text="new"), Document(id="missing", text="missing")])
    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.calls)


def test_duplicate_documents_rejected_before_connection():
    store = make_store()
    docs = [Document(id="a", text="text")] * 2
    with pytest.raises(ConflictError):
        store.insert(SCOPE, docs)
    with pytest.raises(ValidationError):
        store.update(SCOPE, docs)


def test_get_delete_scope_and_unit_lookup(monkeypatch):
    store = make_store()
    cursor = bind_cursor(
        monkeypatch,
        store,
        RecordingCursor(
            batches=[
                [
                    ("a", "正文", '{"unit_id":"a"}'),
                ],
                [("a", "a")],
            ]
        ),
    )
    assert store.get(SCOPE, ["a"]) == [Document(id="a", text="正文", metadata={"unit_id": "a"})]
    assert store.record_ids_for_units(SCOPE, ["a"]) == {"a": ["a"]}
    store.delete(SCOPE, ["a"])
    assert "scope_session = %s" in cursor.calls[-1][0]


def test_fulltext_failure_rolls_back_and_releases_pool(monkeypatch):
    cursor = RecordingCursor(batches=[[]])
    cursor.failure = ("INSERT", RuntimeError("bm25 write failed"))
    pool = RecordingPool(cursor)
    store = make_store()
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(BackendError, match="bm25 write failed"):
        store.insert(SCOPE, [Document(id="a", text="text")])
    assert pool.connection.rollbacks == 1 and pool.connection.commits == 0
    assert len(pool.returns) == 1


def test_both_adapters_work_through_unchanged_recallers_and_rrf(monkeypatch):
    fulltext = make_store()
    vector = OpenGaussVectorStore(dsn=DSN, dim=3)
    text_cursor = search_cursor(
        monkeypatch, fulltext,
        rows=[("u1", 4.0, '{"unit_id":"u1"}'), ("u2", 2.0, '{"unit_id":"u2"}')],
    )
    bind_cursor(
        monkeypatch,
        vector,
        RecordingCursor(
            batches=[
                [
                    ("u2-chunk0", 0.05, '{"unit_id":"u2"}'),
                    ("u2-chunk1", 0.15, '{"unit_id":"u2"}'),
                    ("u1-chunk0", 0.2, '{"unit_id":"u1"}'),
                ]
            ]
        ),
    )
    manager = CompositeStoreManager(vector=vector, fulltext=fulltext)
    parsed = ParsedQuery(raw="apple", vector=[1, 0, 0])
    keyword_hits = KeywordRecaller(manager).recall(SCOPE, parsed, 3)
    vector_hits = VectorRecaller(manager).recall(SCOPE, parsed, 3)
    assert [h.unit_id for h in keyword_hits] == ["u1", "u2"]
    assert len(text_cursor.calls) == 3, "keyword recall must not issue get"
    assert [h.unit_id for h in vector_hits] == ["u2", "u1"]
    assert len(RRFFuser().fuse(parsed, [keyword_hits, vector_hits])) == 2
