"""Opt-in real openGauss tests; a configured DSN must fail loudly, never silently skip."""

from __future__ import annotations

import os
import uuid

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage._opengauss import quote_identifier, scope_clause
from jiuwen_memory.storage.fulltext_impl.opengauss_fulltext import OpenGaussFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.opengauss_vector import OpenGaussVectorStore

pytestmark = pytest.mark.integration
DSN = os.getenv("AGENT_MEMORY_TEST_OPENGAUSS_DSN", "")


@pytest.fixture
def og_vector(request):
    if not DSN:
        pytest.skip("AGENT_MEMORY_TEST_OPENGAUSS_DSN is not configured")
    schema = "itest_og_" + uuid.uuid4().hex[:12]
    store = OpenGaussVectorStore(
        dsn=DSN,
        schema=schema,
        dim=3,
        metric_type=getattr(request, "param", "COSINE"),
    )
    try:
        store.health()
        yield store, Scope(org="test", space="s", user="u", agent="a", session="t")
    finally:
        try:
            with store.transaction() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {quote_identifier(schema)} CASCADE")
        finally:
            store.close()


def test_opengauss_vector_crud_scope_and_batch_atomicity(og_vector):
    store, scope = og_vector
    other = Scope(org="test", space="other", user="u", agent="a", session="t")
    a = VectorRecord(id="a", vector=[1, 0, 0], metadata={"unit_id": "a"})
    store.insert(scope, [a])
    store.insert(other, [a])
    with pytest.raises(ConflictError):
        store.insert(scope, [VectorRecord(id="new", vector=[0, 1, 0]), a])
    assert store.get(scope, ["new"]) == []
    with pytest.raises(NotFoundError):
        store.update(
            scope,
            [
                VectorRecord(id="a", vector=[0, 1, 0]),
                VectorRecord(id="absent", vector=[1, 0, 0]),
            ],
        )
    assert store.get(scope, ["a"])[0].vector == [1, 0, 0]
    store.update(scope, [VectorRecord(id="a", vector=[0, 1, 0], metadata={"unit_id": "a"})])
    assert store.get(scope, ["a"])[0].vector == [0, 1, 0]
    assert store.get(other, ["a"])[0].vector == [1, 0, 0]
    assert store.record_ids_for_units(scope, ["a"]) == {"a": ["a"]}
    store.delete(scope, ["a"])
    store.delete(scope, ["a"])
    assert store.get(scope, ["a"]) == [] and store.get(other, ["a"])


def test_opengauss_empty_scope_dimensions_round_trip(og_vector):
    store, _ = og_vector
    sparse = Scope(org="test", user="u")
    deeper = Scope(org="test", user="u", session="t")
    store.insert(sparse, [VectorRecord(id="a", vector=[1, 0, 0])])
    store.insert(deeper, [VectorRecord(id="a", vector=[0, 1, 0])])
    assert store.get(sparse, ["a"])[0].vector == [1, 0, 0]
    assert {h.id for h in store.search(sparse, VectorQuery(vector=[1, 0, 0]))} == {"a"}
    store.delete(sparse, ["a"])
    assert store.get(sparse, ["a"]) == [] and store.get(deeper, ["a"])


@pytest.mark.parametrize("og_vector", ["COSINE", "L2", "IP"], indirect=True)
def test_opengauss_vector_filtered_search_and_metadata(og_vector):
    store, scope = og_vector
    store.insert(
        scope,
        [
            VectorRecord(
                id="a",
                vector=[1, 0, 0],
                metadata={
                    "unit_id": "a",
                    "user_metadata.priority": 9,
                    "tags": ["work"],
                },
            ),
            VectorRecord(
                id="b",
                vector=[0.7, 0.3, 0],
                metadata={
                    "unit_id": "b",
                    "user_metadata.priority": 8,
                    "tags": ["work"],
                },
            ),
            VectorRecord(
                id="c",
                vector=[0, 1, 0],
                metadata={
                    "unit_id": "c",
                    "user_metadata.priority": 1,
                    "tags": ["home"],
                },
            ),
        ],
    )
    hits = store.recall(
        scope,
        VectorQuery(
            vector=[1, 0, 0],
            top_k=2,
            filters={
                "AND": [
                    {"priority": {"gte": 8}},
                    {"tags": {"contains": "work"}},
                ]
            },
        ),
        ["metadata"],
    )
    assert [h.id for h in hits] == ["a", "b"]
    assert hits[0].score >= hits[1].score and hits[0].metadata["unit_id"] == "a"
    assert store.search(scope, VectorQuery(vector=[1, 0, 0], filters={"tags": "work"})) == []
    assert store.search(Scope(org="test", space="wrong"), VectorQuery(vector=[1, 0, 0])) == []


def test_opengauss_vector_exact_mode_on_prebuilt_hnsw(og_vector):
    store, scope = og_vector
    store.insert(scope, [VectorRecord(id="a", vector=[1, 0, 0])])
    exact = OpenGaussVectorStore(
        dsn=DSN,
        schema=store._schema,
        dim=3,
        index_type="none",
        auto_create_schema=False,
    )
    try:
        assert exact.search(scope, VectorQuery(vector=[1, 0, 0], top_k=1))[0].id == "a"
    finally:
        exact.close()


@pytest.fixture
def og_fulltext():
    if not DSN:
        pytest.skip("AGENT_MEMORY_TEST_OPENGAUSS_DSN is not configured")
    schema = "itest_og_text_" + uuid.uuid4().hex[:12]
    store = OpenGaussFulltextStore(dsn=DSN, schema=schema)
    try:
        store.health()
        yield store, Scope(org="test", space="s", user="u", agent="a", session="t")
    finally:
        try:
            with store.transaction() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {quote_identifier(schema)} CASCADE")
        finally:
            store.close()


def test_opengauss_fulltext_crud_and_conflict_preflight(og_fulltext):
    store, scope = og_fulltext
    doc = Document(id="a", text="apple memory", metadata={"unit_id": "a"})
    store.insert(scope, [doc])
    with pytest.raises(ConflictError):
        store.insert(scope, [Document(id="new", text="apple"), doc])
    assert store.get(scope, ["new"]) == []
    with pytest.raises(NotFoundError):
        store.update(scope, [Document(id="a", text="changed"), Document(id="missing", text="x")])
    assert store.get(scope, ["a"])[0] == doc
    assert store.search(scope, TextQuery(text="apple"))[0].id == "a"
    store.update(scope, [Document(id="a", text="banana memory", metadata={"unit_id": "a"})])
    assert store.search(scope, TextQuery(text="banana"))[0].id == "a"
    assert store.record_ids_for_units(scope, ["a"]) == {"a": ["a"]}
    store.delete(scope, ["a"])
    store.delete(scope, ["a"])
    assert store.get(scope, ["a"]) == []
    assert store.search(scope, TextQuery(text="banana")) == []


def test_opengauss_fulltext_scope_filters_and_topk(og_fulltext):
    store, scope = og_fulltext
    other = Scope(org="test", space="other", user="u", agent="a", session="t")
    store.insert(
        scope,
        [
            Document(id="a", text="apple apple memory", metadata={"user_metadata.priority": 9}),
            Document(id="b", text="apple memory work", metadata={"user_metadata.priority": 7}),
            Document(id="c", text="banana memory", metadata={"user_metadata.priority": 1}),
        ],
    )
    store.insert(other, [Document(id="a", text="apple apple apple")])
    hits = store.search(scope, TextQuery(text="apple", top_k=2, filters={"priority": {"gte": 7}}))
    assert {h.id for h in hits} == {"a", "b"}
    assert hits[0].score >= hits[1].score > 0
    assert (
        store.search(scope, TextQuery(text="apple", filters={"priority": {"gte": 8}}))[0].id == "a"
    )
    assert store.get(scope, ["a"])[0].text != store.get(other, ["a"])[0].text
    assert store.search(scope, TextQuery(text="term_not_in_corpus_7395")) == []


def test_opengauss_fulltext_explicit_rollback_keeps_index_usable(og_fulltext):
    store, scope = og_fulltext
    store.insert(scope, [Document(id="kept", text="apple committed")])
    with pytest.raises(ValidationError, match="rollback probe"):
        with store.transaction() as cursor:
            cursor.execute(
                f"INSERT INTO {store._table_ref} "
                "(id, content, scope_org, scope_space, scope_user, scope_agent, scope_session) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ("rolled", "apple rollback", *scope_clause(scope, exact=True)[1]),
            )
            raise ValidationError("rollback probe")
    assert store.get(scope, ["rolled"]) == []
    assert [h.id for h in store.search(scope, TextQuery(text="apple"))] == ["kept"]


def test_opengauss_fulltext_savepoint_rollback_keeps_index_usable(og_fulltext):
    store, scope = og_fulltext
    store.insert(scope, [Document(id="kept", text="apple committed")])
    with store.transaction() as cursor:
        cursor.execute("SAVEPOINT bm25_probe")
        cursor.execute(
            f"INSERT INTO {store._table_ref} "
            "(id, content, scope_org, scope_space, scope_user, scope_agent, scope_session) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("rolled", "apple savepoint", *scope_clause(scope, exact=True)[1]),
        )
        cursor.execute("ROLLBACK TO SAVEPOINT bm25_probe")
    assert store.get(scope, ["rolled"]) == []
    assert [h.id for h in store.search(scope, TextQuery(text="apple"))] == ["kept"]
