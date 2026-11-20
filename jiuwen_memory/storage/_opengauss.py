# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""openGauss SQL backend primitives; no PostgreSQL extension or asyncpg dependency."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    Scope,
    normalize,
)

from ._support import as_bool, sanitize_resource_id, scope_dims, wrap_backend

_SCOPE_COLUMNS = ("scope_org", "scope_space", "scope_user", "scope_agent", "scope_session")
_SCOPE_PREFIX = "#"
_RANGE_OPS = {FilterOp.GT: ">", FilterOp.GTE: ">=", FilterOp.LT: "<", FilterOp.LTE: "<="}


def quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value.encode()) > 63:
        raise ValidationError("openGauss identifiers must contain 1..63 UTF-8 bytes without NUL")
    return '"' + value.replace('"', '""') + '"'


def json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("openGauss metadata must be finite JSON data") from exc


def positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be a positive integer")
    try:
        result = int(value)
        valid = float(value) == result and result > 0
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid or (maximum is not None and result > maximum):
        raise ValidationError(
            f"{name} must be a positive integer" + (f" <= {maximum}" if maximum is not None else "")
        )
    return result


def scope_value(value: str | None) -> str:
    # openGauss A compatibility (the default) stores '' as NULL; never persist an empty dimension.
    return _SCOPE_PREFIX + (value or "")


def scope_clause(scope: Scope, *, exact: bool) -> tuple[str, list[str]]:
    dims = (
        [(dim, getattr(scope, dim)) for dim in ("org", "space", "user", "agent", "session")]
        if exact
        else scope_dims(scope)
    )
    return " AND ".join(f"scope_{dim} = %s" for dim, _ in dims), [scope_value(v) for _, v in dims]


def compile_filter(filters: FilterExpr | None) -> tuple[str, list[Any]]:
    """Use JSONB literals, not jsonb_build_object/to_jsonb unavailable on older OG builds."""
    expr = normalize(filters)
    if expr is None:
        return "", []
    if isinstance(expr, FilterClause):
        key, op, value = expr.field, expr.op, expr.value
        if op in (FilterOp.EQ, FilterOp.NE, FilterOp.IN, FilterOp.NOT_IN):
            values = value if op in (FilterOp.IN, FilterOp.NOT_IN) else [value]
            parts, params = [], []
            for item in values:
                parts.append(
                    "COALESCE((jsonb_typeof(metadata -> %s) IN ('string', 'number', 'boolean') "
                    "AND metadata @> %s::jsonb), FALSE)"
                )
                params.extend([key, json_text({key: item})])
            sql = "(" + " OR ".join(parts) + ")"
            return (f"(NOT {sql})" if op in (FilterOp.NE, FilterOp.NOT_IN) else sql), params
        if op is FilterOp.CONTAINS:
            return (
                "COALESCE((jsonb_typeof(metadata -> %s) = 'array' "
                "AND metadata @> %s::jsonb), FALSE)",
                [key, json_text({key: [value]})],
            )
        if op in _RANGE_OPS:
            return (
                "(CASE WHEN jsonb_typeof(metadata -> %s) = 'number' "
                f"THEN (metadata ->> %s)::numeric {_RANGE_OPS[op]} %s::numeric ELSE FALSE END)",
                [key, key, value],
            )
        raise ValidationError(f"unsupported openGauss filter operator: {op}")
    if not isinstance(expr, FilterGroup):
        raise ValidationError("invalid openGauss filter expression")
    children = [compile_filter(child) for child in expr.children]
    params = [v for _, values in children for v in values]
    if expr.logic is FilterLogic.NOT:
        return f"(NOT ({children[0][0]}))", params
    separator = " AND " if expr.logic is FilterLogic.AND else " OR "
    return "(" + separator.join(sql for sql, _ in children) + ")", params


@dataclass
class _PoolLease:
    pool: Any
    dsn: str
    active: int = 0
    retired: bool = False


class OpenGaussStoreBase:
    """Bounded psycopg2 pool with transaction-local state and safe DSN rotation."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str,
        table: str,
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        connect_timeout: float = 10,
        application_name: str = "agent_memory",
        auto_create_schema: bool = True,
        create_metadata_index: bool = True,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source=None,
        config_namespace: str = "vector_store",
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValidationError("openGauss requires a non-empty dsn")
        self._table_ref = f"{quote_identifier(schema)}.{quote_identifier(table)}"
        self._schema, self._table = schema, table
        self._dsn = dsn
        self._pool_min = positive_int(pool_min_size, "pool_min_size")
        self._pool_max = positive_int(pool_max_size, "pool_max_size")
        if self._pool_min > self._pool_max:
            raise ValidationError("pool_min_size must not exceed pool_max_size")
        try:
            timeout = float(connect_timeout)
        except (TypeError, ValueError) as exc:
            raise ValidationError("connect_timeout must be positive and finite") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValidationError("connect_timeout must be positive and finite")
        self._connect_timeout = math.ceil(timeout)
        self._application_name = application_name
        self._auto_create_schema = as_bool(auto_create_schema, default=True)
        self._create_metadata_index = as_bool(create_metadata_index, default=True)
        self._ssl_verify = as_bool(ssl_verify, default=False)
        self._ssl_ca_cert = ssl_ca_cert
        if self._ssl_verify and not ssl_ca_cert:
            raise ValidationError("openGauss ssl_verify requires ssl_ca_cert")
        self._config_source, self._config_namespace = config_source, config_namespace
        self._pool_lock = threading.RLock()
        self._lease: _PoolLease | None = None
        self._schema_lock = int.from_bytes(
            hashlib.sha256(f"{schema}.{table}".encode()).digest()[:8], "big", signed=True
        )

    def _resolved_dsn(self) -> str:
        from jiuwen_memory.config.binding import resolve_connection_url

        return (
            resolve_connection_url(
                self._config_source,
                namespace=self._config_namespace,
                field="dsn",
                fallback=self._dsn,
            )
            or self._dsn
        )

    def _new_pool(self, dsn: str):
        try:
            from psycopg2.pool import ThreadedConnectionPool
        except ImportError as exc:
            raise BackendError(
                "openGauss requires an externally installed psycopg2 connector and compatible "
                "libpq; use the official openGauss connector for the target database build"
            ) from exc
        options: dict[str, Any] = {
            "connect_timeout": self._connect_timeout,
            "application_name": self._application_name,
        }
        if self._ssl_verify:
            # libpq keyword arguments override both URL and key=value DSN TLS settings.
            options.update(sslmode="verify-full", sslrootcert=self._ssl_ca_cert)
        pool = ThreadedConnectionPool(self._pool_min, self._pool_max, dsn=dsn, **options)
        try:
            conn = pool.getconn()
            try:
                conn.autocommit = False
                with conn:
                    with conn.cursor() as cursor:
                        self._ensure_schema(cursor)
            finally:
                pool.putconn(conn)
        except Exception:
            pool.closeall()
            raise
        return pool

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with wrap_backend("openGauss transaction"):
            with self._pool_lock:
                dsn = self._resolved_dsn()
                if self._lease is None or self._lease.dsn != dsn:
                    fresh = _PoolLease(self._new_pool(dsn), dsn)
                    self._retire_pool()
                    self._lease = fresh
                lease = self._lease
                conn = lease.pool.getconn()
                lease.active += 1
            try:
                conn.autocommit = False
                with conn:
                    with conn.cursor() as cursor:
                        yield cursor
            finally:
                with self._pool_lock:
                    lease.pool.putconn(conn, close=bool(conn.closed))
                    lease.active -= 1
                    if lease.retired and lease.active == 0:
                        lease.pool.closeall()

    def _retire_pool(self) -> None:
        if self._lease is not None:
            self._lease.retired = True
            if self._lease.active == 0:
                self._lease.pool.closeall()
            self._lease = None

    def close(self) -> None:
        with self._pool_lock:
            self._retire_pool()

    def connection_budget(self) -> int:
        return self._pool_max

    def resource_descriptor(self) -> str:
        return f"{sanitize_resource_id(self._resolved_dsn())}/{self._schema}/{self._table}"

    def health(self) -> None:
        try:
            with self.transaction() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            raise HealthCheckError("openGauss health check failed") from exc

    def _index_name(self, suffix: str) -> str:
        digest = hashlib.sha256(self._table_ref.encode()).hexdigest()[:10]
        return quote_identifier(f"og_{digest}_{suffix}")

    def _ensure_schema(self, cursor) -> None:
        raise NotImplementedError

    def _ensure_table(self, cursor, payload_definition: str) -> None:
        if self._auto_create_schema:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (self._schema_lock,))
            cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self._schema,))
            if cursor.fetchone() is None:
                cursor.execute(f"CREATE SCHEMA {quote_identifier(self._schema)}")
        cursor.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
            (self._schema, self._table),
        )
        if cursor.fetchone() is None:
            if not self._auto_create_schema:
                raise ValidationError("openGauss table does not exist; pre-create it or enable DDL")
            scope_columns = ", ".join(f"{name} text NOT NULL" for name in _SCOPE_COLUMNS)
            cursor.execute(
                f"CREATE TABLE {self._table_ref} (id text NOT NULL, {payload_definition}, "
                f"{scope_columns}, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb, "
                f"PRIMARY KEY ({', '.join(_SCOPE_COLUMNS)}, id))"
            )
        if self._auto_create_schema:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {self._index_name('scope')} "
                f"ON {self._table_ref} ({', '.join(_SCOPE_COLUMNS)})"
            )
            if self._create_metadata_index:
                # openGauss has no jsonb_path_ops; the default jsonb_ops still serves @>.
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {self._index_name('metadata')} "
                    f"ON {self._table_ref} USING gin (metadata)"
                )

    def _search_where(self, scope: Scope, filters: FilterExpr | None) -> tuple[str, list[Any]]:
        scope_sql, scope_params = scope_clause(scope, exact=False)
        filter_sql, filter_params = compile_filter(filters)
        return " AND ".join(p for p in (scope_sql, filter_sql) if p) or "TRUE", [
            *scope_params,
            *filter_params,
        ]

    def _write_rows(
        self,
        scope: Scope,
        ids: list[str],
        rows: list[Sequence[Any]],
        *,
        columns: tuple[str, ...],
        placeholders: tuple[str, ...],
        entity: str,
        update: bool,
    ) -> None:
        if not ids:
            return
        if len(ids) != len(set(ids)):
            if update:
                raise ValidationError(f"duplicate {entity} ids in update batch")
            raise ConflictError(entity=entity, message=f"duplicate {entity} ids in insert batch")
        scope_sql, scope_params = scope_clause(scope, exact=True)
        with self.transaction() as cursor:
            # Serialize adapter writes so preflight and the complete batch stay indivisible.
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (self._schema_lock,))
            cursor.execute(
                f"SELECT id FROM {self._table_ref} WHERE {scope_sql} "
                "AND id = ANY(%s::text[]) ORDER BY id FOR UPDATE",
                (*scope_params, ids),
            )
            existing = {str(row[0]) for row in cursor.fetchall()}
            missing = [id_ for id_ in ids if id_ not in existing]
            if update and missing:
                raise NotFoundError(entity=entity, key=missing[0])
            if not update and existing:
                raise ConflictError(entity=entity, key=next(id_ for id_ in ids if id_ in existing))
            if update:
                assignments = ", ".join(f"{c} = {p}" for c, p in zip(columns, placeholders))
                statement = (
                    f"UPDATE {self._table_ref} SET {assignments} WHERE {scope_sql} AND id = %s"
                )
                for id_, row in sorted(zip(ids, rows)):
                    cursor.execute(statement, (*row, *scope_params, id_))
            else:
                all_columns = ("id", *_SCOPE_COLUMNS, *columns)
                row_sql = (
                    "(" + ", ".join(("%s", *("%s" for _ in _SCOPE_COLUMNS), *placeholders)) + ")"
                )
                params = [v for id_, row in zip(ids, rows) for v in (id_, *scope_params, *row)]
                try:
                    cursor.execute(
                        f"INSERT INTO {self._table_ref} ({', '.join(all_columns)}) VALUES "
                        + ", ".join(row_sql for _ in ids),
                        params,
                    )
                except Exception as exc:
                    if getattr(exc, "pgcode", None) == "23505":
                        raise ConflictError(
                            entity=entity, message=f"{entity} id already exists"
                        ) from exc
                    raise

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        sql, params = scope_clause(scope, exact=True)
        with self.transaction() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (self._schema_lock,))
            cursor.execute(
                f"DELETE FROM {self._table_ref} WHERE {sql} AND id = ANY(%s::text[])",
                (*params, ids),
            )

    def record_ids_for_units(self, scope: Scope, unit_ids: list[str]) -> dict[str, list[str]]:
        if not unit_ids:
            return {}
        sql, params = scope_clause(scope, exact=True)
        with self.transaction() as cursor:
            cursor.execute(
                f"SELECT id, metadata ->> 'unit_id' FROM {self._table_ref} WHERE {sql} "
                "AND metadata ->> 'unit_id' = ANY(%s::text[])",
                (*params, unit_ids),
            )
            result: dict[str, list[str]] = {}
            for id_, unit_id in cursor.fetchall():
                result.setdefault(str(unit_id), []).append(str(id_))
            return result
