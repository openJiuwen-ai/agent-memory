"""openGauss vector adapter: SQL, isolation, transactions and registration contracts."""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import Scope, normalize
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage._opengauss import compile_filter, quote_identifier, scope_clause
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
from jiuwen_memory.storage.vector import VectorProducer
from jiuwen_memory.storage.vector_impl.opengauss_vector import OpenGaussVectorStore
from tests.unit.storage.opengauss_fixtures import (
    RecordingCursor,
    RecordingPool,
    bind_cursor,
)

pytestmark = pytest.mark.unit
SCOPE = Scope(org="org", space="space", user="user", agent="agent", session="session")
DSN = "postgresql://user:secret@example.invalid:5432/memory"


def make_store(**kwargs):
    return OpenGaussVectorStore(dsn=DSN, dim=3, **kwargs)


def test_registered_factory_is_lazy_and_validates_required_parameters():
    register_backends()
    assert "opengauss" in VectorProducer.known()
    with pytest.raises(ValidationError, match="dsn"):
        VectorProducer.build("opengauss", {"dim": 3}, AssemblyContext())
    store = VectorProducer.build(
        "opengauss",
        {"dsn": DSN, "dim": 3, "auto_create_schema": "false"},
        AssemblyContext(),
    )
    assert isinstance(store, OpenGaussVectorStore)
    assert store.connection_budget() == 8
    assert store.resource_descriptor() == "example.invalid:5432/memory/public/agent_memory_vectors"
    assert "secret" not in store.resource_descriptor()
    assert store._lease is None
    assert store._auto_create_schema is False
    assert StoreCapability.UNIT_RECORD_LOOKUP in store.capabilities()
    assert store.score_higher_is_better()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric_type": "wrong"},
        {"index_type": "diskann"},
        {"hnsw_m": 1},
        {"hnsw_ef_construction": 1},
        {"ef_search": 0},
        {"max_scan_tuples": 0},
        {"pool_min_size": 10, "pool_max_size": 2},
        {"connect_timeout": float("inf")},
        {"ssl_verify": True},
        {"table": "a" * 64},
    ],
)
def test_invalid_configuration_fails_before_connection(kwargs):
    with pytest.raises(ValidationError):
        make_store(**kwargs)


@pytest.mark.parametrize("dim", [0, -1, True, 1.5, 2001, 16001])
def test_hnsw_dimension_validation(dim):
    with pytest.raises(ValidationError):
        OpenGaussVectorStore(dsn=DSN, dim=dim)


def test_exact_mode_allows_dimensions_above_hnsw_limit():
    assert OpenGaussVectorStore(dsn=DSN, dim=3072, index_type="none")


@pytest.mark.parametrize("vector", [[1], [0, 0, 0], [1, float("nan"), 0], [1e100, 0, 0]])
def test_bad_vectors_fail_before_connection(vector):
    store = make_store()
    with pytest.raises(ValidationError):
        store.insert(SCOPE, [VectorRecord(id="bad", vector=vector)])
    with pytest.raises(ValidationError):
        store.search(SCOPE, VectorQuery(vector=vector))
    assert store._lease is None


def test_scope_filter_is_exact_for_crud_and_hierarchical_for_search():
    exact_sql, exact_params = scope_clause(Scope(org="o", user="u"), exact=True)
    search_sql, search_params = scope_clause(Scope(org="o", user="u"), exact=False)
    assert exact_sql.count("= %s") == 5 and exact_params == ["#o", "#", "#u", "#", "#"]
    assert search_sql == "scope_org = %s AND scope_space = %s AND scope_user = %s"
    assert search_params == ["#o", "#", "#u"]
    assert all(exact_params), "empty strings become NULL in A compatibility"


def test_filter_compiles_complete_tree_without_newer_postgres_json_builders():
    filters = normalize(
        {
            "AND": [
                {"color": {"in": ["red", "blue"]}},
                {"OR": [{"priority": {"gte": 5}}, {"tags": {"contains": "work"}}]},
                {"NOT": {"state": "archived"}},
            ]
        }
    )
    sql, params = compile_filter(filters)
    assert " AND " in sql and " OR " in sql and "NOT" in sql
    assert "CASE WHEN jsonb_typeof" in sql and "= 'array'" in sql
    assert "('string', 'number', 'boolean')" in sql
    assert "jsonb_build" not in sql and "to_jsonb" not in sql
    assert json.dumps({"user_metadata.color": "red"}, separators=(",", ":")) in params
    assert "red" not in sql and "archived" not in sql


@pytest.mark.parametrize("op", ["ne", "not_in"])
def test_negative_filters_negate_total_boolean_predicates(op):
    sql, _ = compile_filter(normalize({"color": {op: ["red"] if op == "not_in" else "red"}}))
    assert "NOT" in sql and "COALESCE" in sql


def test_identifiers_and_values_are_separately_escaped():
    assert quote_identifier('a"b') == '"a""b"'
    sql, params = compile_filter(normalize({"color": "'); DROP TABLE memories; --"}))
    assert "DROP" not in sql and "DROP" in params[1]


@pytest.mark.parametrize(
    "metric,operator,score",
    [
        ("COSINE", "<=>", "1.0 - (embedding <=> %s::vector)"),
        ("L2", "<->", "1.0 / (1.0 + (embedding <-> %s::vector))"),
        ("IP", "<#>", "-(embedding <#> %s::vector)"),
    ],
)
def test_search_sql_score_order_scope_and_filters(monkeypatch, metric, operator, score):
    store = make_store(metric_type=metric)
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[("a", 0.9)]]))
    hits = store.search(SCOPE, VectorQuery(vector=[1, 0, 0], top_k=5, filters={"color": "red"}))
    sql, params = cursor.calls[-1]
    assert hits[0].id == "a" and hits[0].score == 0.9
    assert score in sql and f"ORDER BY embedding {operator} %s::vector ASC LIMIT %s" in sql
    assert "scope_org = %s" in sql and "scope_session = %s" in sql and "metadata @>" in sql
    assert params[1:6] == ("#org", "#space", "#user", "#agent", "#session")
    assert params[-1] == 5
    assert ("SET LOCAL hnsw_iterative_scan = strict_order", ()) in cursor.calls
    assert all("hnsw." not in statement for statement, _ in cursor.calls)


def test_exact_mode_cannot_use_an_ann_ordering_path(monkeypatch):
    store = make_store(index_type="none")
    cursor = bind_cursor(monkeypatch, store, RecordingCursor())
    assert store.search(SCOPE, VectorQuery(vector=[1, 0, 0])) == []
    assert len(cursor.calls) == 1
    assert "ORDER BY (embedding <=> %s::vector) + 0.0 ASC" in cursor.calls[0][0]


def test_recall_fetches_metadata_in_same_query(monkeypatch):
    store = make_store()
    cursor = bind_cursor(
        monkeypatch,
        store,
        RecordingCursor(
            batches=[
                [
                    ("a", 0.9, '{"unit_id":"u1"}'),
                ]
            ]
        ),
    )
    hits = store.recall(SCOPE, VectorQuery(vector=[1, 0, 0]), ["metadata", "unsupported"])
    assert hits[0].metadata == {"unit_id": "u1"}
    assert "metadata::text" in cursor.calls[-1][0]
    assert len([sql for sql, _ in cursor.calls if sql.startswith("SELECT")]) == 1


def test_empty_operations_never_connect():
    store = make_store()
    store.insert(SCOPE, [])
    store.update(SCOPE, [])
    store.delete(SCOPE, [])
    assert store.get(SCOPE, []) == []
    assert store.record_ids_for_units(SCOPE, []) == {}
    assert store._lease is None


def test_duplicate_ids_fail_before_connection():
    store = make_store()
    records = [VectorRecord(id="x", vector=[1, 0, 0])] * 2
    with pytest.raises(ConflictError):
        store.insert(SCOPE, records)
    with pytest.raises(ValidationError):
        store.update(SCOPE, records)
    assert store._lease is None


def test_insert_preflight_scope_and_single_batch_statement(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor())
    store.insert(SCOPE, [VectorRecord(id="x", vector=[1, 0, 0], metadata={"unit_id": "u"})])
    assert "FOR UPDATE" in cursor.calls[1][0]
    sql, params = cursor.calls[-1]
    assert sql.startswith("INSERT") and "::vector" in sql and "::jsonb" in sql
    assert params[:6] == ("x", "#org", "#space", "#user", "#agent", "#session")
    assert params[-1] == '{"unit_id":"u"}'


def test_missing_update_preflight_leaves_whole_batch_untouched(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[("a",)]]))
    with pytest.raises(NotFoundError):
        store.update(
            SCOPE,
            [
                VectorRecord(id="a", vector=[1, 0, 0]),
                VectorRecord(id="missing", vector=[0, 1, 0]),
            ],
        )
    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.calls)


def test_update_uses_exact_scope_and_deterministic_id_order(monkeypatch):
    store = make_store()
    cursor = bind_cursor(monkeypatch, store, RecordingCursor(batches=[[("a",), ("b",)]]))
    store.update(
        SCOPE, [VectorRecord(id="b", vector=[1, 0, 0]), VectorRecord(id="a", vector=[0, 1, 0])]
    )
    updates = [(sql, params) for sql, params in cursor.calls if sql.startswith("UPDATE")]
    assert [params[-1] for _, params in updates] == ["a", "b"]
    assert all("scope_session = %s" in sql for sql, _ in updates)


def test_get_delete_and_unit_lookup_keep_exact_scope(monkeypatch):
    store = make_store()
    cursor = bind_cursor(
        monkeypatch,
        store,
        RecordingCursor(
            batches=[
                [
                    ("a", "[1,0,0]", '{"unit_id":"u1"}'),
                ],
                [("a", "u1"), ("a-1", "u1")],
            ]
        ),
    )
    assert store.get(SCOPE, ["a"])[0].metadata == {"unit_id": "u1"}
    assert store.record_ids_for_units(SCOPE, ["u1"]) == {"u1": ["a", "a-1"]}
    store.delete(SCOPE, ["a"])
    for sql, params in cursor.calls:
        if "WHERE" in sql:
            assert "scope_session = %s" in sql and params[:5] == (
                "#org",
                "#space",
                "#user",
                "#agent",
                "#session",
            )


def test_schema_uses_native_datavec_not_extension_catalog():
    store = make_store()
    cursor = RecordingCursor(one=[(1,), None, (3,)])
    store._ensure_schema(cursor)
    statements = "\n".join(sql for sql, _ in cursor.calls)
    assert "embedding vector(3)" in statements
    assert "USING hnsw (embedding vector_cosine_ops)" in statements
    assert "pg_extension" not in statements and "CREATE EXTENSION" not in statements
    assert (
        "PRIMARY KEY (scope_org, scope_space, scope_user, scope_agent, scope_session, id)"
        in statements
    )
    assert "scope_space text NOT NULL," in statements and "DEFAULT ''" not in statements, (
        "scope columns must not rely on empty-string defaults that A compatibility turns into NULL"
    )
    assert "USING gin (metadata)" in statements and "jsonb_path_ops" not in statements, (
        "openGauss GIN only ships the default jsonb_ops operator class"
    )


def test_precreated_schema_validates_dimension_without_ddl():
    store = make_store(auto_create_schema=False)
    cursor = RecordingCursor(one=[(1,), (8,)])
    with pytest.raises(ValidationError, match="dimension"):
        store._ensure_schema(cursor)
    assert not any(sql.startswith("CREATE") for sql, _ in cursor.calls)


def test_transaction_commits_and_rolls_back_and_releases_connections(monkeypatch):
    store = make_store()
    pool = RecordingPool()
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with store.transaction() as cursor:
        cursor.execute("SELECT 1")
    with pytest.raises(ConflictError):
        with store.transaction():
            raise ConflictError()
    assert pool.connection.commits == 1 and pool.connection.rollbacks == 1
    assert len(pool.returns) == 2 and store._lease.active == 0
    store.close()
    store.close()
    assert pool.closed == 1


def test_rotation_and_close_never_close_active_connections(monkeypatch):
    store = make_store()
    old, new = RecordingPool(), RecordingPool()
    pools = iter([old, new])
    dsn = [DSN]
    monkeypatch.setattr(store, "_new_pool", lambda value: next(pools))
    monkeypatch.setattr(store, "_resolved_dsn", lambda: dsn[0])
    with store.transaction():
        dsn[0] = "postgresql://new.invalid/memory"
        with store.transaction():
            assert old.closed == 0
        assert old.closed == 0
        store.close()
        assert new.closed == 1
    assert old.closed == 1


def test_io_failures_are_backend_errors_and_connections_released(monkeypatch):
    store = make_store()
    pool = RecordingPool()
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(BackendError):
        with store.transaction():
            raise RuntimeError("connection lost")
    assert pool.connection.rollbacks == 1 and len(pool.returns) == 1


def test_tls_kwargs_override_dsn_and_no_driver_import_on_module_load(monkeypatch):
    captured = {}
    pool = RecordingPool()
    driver = ModuleType("psycopg2.pool")

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return pool

    driver.ThreadedConnectionPool = factory
    monkeypatch.setitem(sys.modules, "psycopg2.pool", driver)
    store = make_store(ssl_verify=True, ssl_ca_cert="ca.pem")
    monkeypatch.setattr(store, "_ensure_schema", lambda cursor: None)
    store._new_pool(DSN + "?sslmode=disable")
    assert captured["sslmode"] == "verify-full" and captured["sslrootcert"] == "ca.pem"


def test_missing_external_driver_has_clear_backend_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2.pool", None)
    with pytest.raises(BackendError, match="externally installed psycopg2"):
        make_store()._new_pool(DSN)


def test_schema_failure_closes_pool_and_rolls_back(monkeypatch):
    pool = RecordingPool()
    driver = ModuleType("psycopg2.pool")
    driver.ThreadedConnectionPool = lambda *args, **kwargs: pool
    monkeypatch.setitem(sys.modules, "psycopg2.pool", driver)
    store = make_store()

    def fail_schema(cursor):
        raise ValidationError("wrong schema")

    monkeypatch.setattr(store, "_ensure_schema", fail_schema)
    with pytest.raises(ValidationError, match="wrong schema"):
        store._new_pool(DSN)
    assert pool.connection.rollbacks == 1 and pool.closed == 1


def test_mid_batch_update_error_rolls_back_whole_transaction(monkeypatch):
    cursor = RecordingCursor(batches=[[("a",), ("b",)]])
    cursor.failure = ("UPDATE", RuntimeError("write failed"))
    pool = RecordingPool(cursor)
    store = make_store()
    monkeypatch.setattr(store, "_new_pool", lambda dsn: pool)
    with pytest.raises(BackendError, match="write failed"):
        store.update(
            SCOPE,
            [
                VectorRecord(id="a", vector=[1, 0, 0]),
                VectorRecord(id="b", vector=[0, 1, 0]),
            ],
        )
    assert pool.connection.rollbacks == 1 and pool.connection.commits == 0
