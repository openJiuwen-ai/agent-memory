"""真实 PostgreSQL/pgvector 后端的核心 CRUD、scope、过滤与排序回归。"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from common.errors import ConflictError, NotFoundError
from common.type_def import FilterClause, FilterGroup, FilterLogic, FilterOp, Scope
from storage.kv_impl.postgres_kv import PostgresKVStore
from storage.types import VectorQuery, VectorRecord
from storage.vector_impl.pgvector_vector import PgVectorStore

pytestmark = pytest.mark.integration

PG_DSN = os.getenv("AGENT_MEMORY_TEST_PG_DSN", "")
DIM = 8


def _require_postgres() -> None:
    psycopg = pytest.importorskip("psycopg")
    if not PG_DSN:
        pytest.skip("AGENT_MEMORY_TEST_PG_DSN is not configured")
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable for integration test: {exc}")


def _vector(index: int, value: float = 1.0) -> list[float]:
    result = [0.0] * DIM
    result[index] = value
    return result


def _drop_schema(store, schema: str) -> None:
    try:
        with store.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                store.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    store.sql.Identifier(schema)
                )
            )
    finally:
        store.close()


@pytest.fixture
def pg_kv():
    _require_postgres()
    schema = f"itest_kv_{uuid.uuid4().hex[:12]}"
    store = PostgresKVStore(dsn=PG_DSN, schema=schema)
    store.health()
    scope = Scope(org="itest", space=uuid.uuid4().hex, user=uuid.uuid4().hex)
    yield store, scope
    _drop_schema(store, schema)


@pytest.fixture
def pg_vector():
    _require_postgres()
    schema = f"itest_vec_{uuid.uuid4().hex[:12]}"
    store = PgVectorStore(dsn=PG_DSN, schema=schema, dim=DIM)
    store.health()
    scope = Scope(org="itest", space=uuid.uuid4().hex, user=uuid.uuid4().hex)
    yield store, scope, schema
    _drop_schema(store, schema)


def test_postgres_kv_core_semantics(pg_kv) -> None:
    store, scope = pg_kv

    store.insert(scope, "k", b"v1")
    assert store.get(scope, "k") == b"v1"
    assert store.exists(scope, "k")
    with pytest.raises(ConflictError):
        store.insert(scope, "k", b"other")

    store.update(scope, "k", b"v2")
    assert store.get(scope, "k") == b"v2"
    store.delete(scope, "k")
    store.delete(scope, "k")
    with pytest.raises(NotFoundError):
        store.get(scope, "k")
    with pytest.raises(NotFoundError):
        store.update(scope, "k", b"missing")


def test_postgres_kv_scope_ttl_and_literal_prefix(pg_kv) -> None:
    store, scope = pg_kv
    other = Scope(org=scope.org, space=f"{scope.space}-other", user=scope.user)

    store.insert(scope, "a_b", b"literal")
    store.insert(scope, "axb", b"wildcard")
    store.insert(other, "a_b", b"other")
    store.insert(scope, "expires", b"soon", ttl=0.05)

    assert store.scan(scope, "a_") == [("a_b", b"literal")]
    assert {item.space for item in store.scopes()} >= {scope.space, other.space}
    time.sleep(0.1)
    assert not store.exists(scope, "expires")
    assert store.get(other, "a_b") == b"other"


def test_pgvector_crud_scope_and_atomic_conflict(pg_vector) -> None:
    store, scope, _ = pg_vector
    other = Scope(org=scope.org, space=f"{scope.space}-other", user=scope.user)
    original = VectorRecord(id="a", vector=_vector(0), metadata={"color": "red"})
    store.insert(scope, [original])

    with pytest.raises(ConflictError):
        store.insert(
            scope,
            [
                VectorRecord(id="new", vector=_vector(1)),
                VectorRecord(id="a", vector=_vector(2)),
            ],
        )
    assert store.get(scope, ["new"]) == []
    assert store.get(other, ["a"]) == []

    with pytest.raises(NotFoundError):
        store.update(other, [VectorRecord(id="a", vector=_vector(1))])
    assert store.get(scope, ["a"])[0].metadata == {"color": "red"}

    store.insert(
        other,
        [VectorRecord(id="a", vector=_vector(2), metadata={"color": "other"})],
    )
    store.update(
        other,
        [VectorRecord(id="a", vector=_vector(3), metadata={"color": "other-updated"})],
    )
    assert store.get(other, ["a"])[0].metadata == {"color": "other-updated"}
    assert store.get(scope, ["a"])[0].metadata == {"color": "red"}

    store.update(
        scope,
        [VectorRecord(id="a", vector=_vector(1), metadata={"color": "blue"})],
    )
    assert store.get(scope, ["a"])[0].metadata == {"color": "blue"}
    store.delete(other, ["a"])
    assert store.get(scope, ["a"])
    store.delete(scope, ["a"])
    store.delete(scope, ["a"])
    assert store.get(scope, ["a"]) == []


def test_pgvector_search_order_scope_and_filters(pg_vector) -> None:
    store, scope, _ = pg_vector
    other = Scope(org=scope.org, space=f"{scope.space}-other", user=scope.user)
    store.insert(
        scope,
        [
            VectorRecord(
                id="x",
                vector=_vector(0),
                metadata={"color": "red", "priority": 9, "tags": ["work"]},
            ),
            VectorRecord(
                id="y",
                vector=[0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                metadata={"color": "blue", "priority": 7, "tags": ["work"]},
            ),
            VectorRecord(
                id="z",
                vector=_vector(1),
                metadata={"color": "red", "priority": 5, "tags": ["home"]},
            ),
        ],
    )
    store.insert(other, [VectorRecord(id="x", vector=_vector(0))])
    filters = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("tags", FilterOp.CONTAINS, "work"),
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("color", FilterOp.EQ, "red"),
                    FilterClause("priority", FilterOp.GTE, 7),
                ],
            ),
            FilterGroup(
                FilterLogic.NOT,
                [FilterClause("color", FilterOp.EQ, "green")],
            ),
        ],
    )

    hits = store.search(
        scope,
        VectorQuery(vector=_vector(0), top_k=10, filters=filters),
    )

    assert [hit.id for hit in hits] == ["x", "y"]
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits), reverse=True
    )


def test_pgvector_distinguishes_scalar_equality_from_array_membership(pg_vector) -> None:
    store, scope, _ = pg_vector
    store.insert(
        scope,
        [
            VectorRecord(id="scalar", vector=_vector(0), metadata={"kind": "work"}),
            VectorRecord(id="array", vector=_vector(0), metadata={"kind": ["work"]}),
        ],
    )

    def query_ids(op):
        query = VectorQuery(
            vector=_vector(0),
            top_k=10,
            filters=FilterClause("kind", op, "work"),
        )
        return {hit.id for hit in store.search(scope, query)}

    assert query_ids(FilterOp.EQ) == {"scalar"}
    assert query_ids(FilterOp.CONTAINS) == {"array"}


def test_pgvector_none_mode_runs_against_preexisting_hnsw(pg_vector) -> None:
    store, scope, schema = pg_vector
    store.insert(
        scope,
        [
            VectorRecord(id="x", vector=_vector(0)),
            VectorRecord(id="y", vector=_vector(1)),
        ],
    )
    exact = PgVectorStore(
        dsn=PG_DSN,
        schema=schema,
        table="agent_memory_vectors",
        dim=DIM,
        index_type="none",
        auto_create_schema=False,
        create_extension=False,
    )
    try:
        hits = exact.search(scope, VectorQuery(vector=_vector(0), top_k=2))
    finally:
        exact.close()

    assert [hit.id for hit in hits] == ["x", "y"]
