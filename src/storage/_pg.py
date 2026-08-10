"""PostgreSQL 存储后端共享基础：惰性连接池、schema 工具与过滤编译。"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

from common.errors import AgentMemoryError, BackendError, HealthCheckError, ValidationError
from common.type_def import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    Scope,
    filter_field_metadata_key,
)

from ._support import scope_dims

_SCHEMA_LOCK_KEY = "agent-memory:postgres-storage:schema"
_CMP_OPS = {
    FilterOp.GT: ">",
    FilterOp.GTE: ">=",
    FilterOp.LT: "<",
    FilterOp.LTE: "<=",
}


def _typed_value(value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, str):
        return "text", value
    if isinstance(value, (int, float)):
        return "numeric", value
    raise ValidationError(f"unsupported PostgreSQL filter literal: {value!r}")


def _scalar_clause(key: str, value: Any) -> tuple[str, list[Any]]:
    type_name, parameter = _typed_value(value)
    return (
        f"metadata @> jsonb_build_object(%s::text, to_jsonb(%s::{type_name}))",
        [key, parameter],
    )


def _array_contains_clause(key: str, value: Any) -> tuple[str, list[Any]]:
    type_name, parameter = _typed_value(value)
    return (
        "(jsonb_typeof(metadata->%s) = 'array' AND "
        "metadata @> jsonb_build_object(%s::text, "
        f"jsonb_build_array(to_jsonb(%s::{type_name}))))",
        [key, key, parameter],
    )


def compile_pg_filter(expr: FilterExpr | None) -> tuple[str, list[Any]]:
    """把规范化的 ``FilterExpr`` 编译为参数化 jsonb SQL。"""
    if expr is None:
        return "", []
    if isinstance(expr, FilterClause):
        key = filter_field_metadata_key(expr.field)
        if expr.op in (FilterOp.EQ, FilterOp.NE):
            base, params = _scalar_clause(key, expr.value)
            if expr.op is FilterOp.NE:
                return f"(NOT COALESCE({base}, FALSE))", params
            return f"(COALESCE({base}, FALSE))", params
        if expr.op is FilterOp.CONTAINS:
            base, params = _array_contains_clause(key, expr.value)
            return f"(COALESCE({base}, FALSE))", params
        if expr.op in (FilterOp.IN, FilterOp.NOT_IN):
            parts: list[str] = []
            params: list[Any] = []
            for value in expr.value:
                part, part_params = _scalar_clause(key, value)
                parts.append(part)
                params.extend(part_params)
            base = f"({' OR '.join(parts)})"
            if expr.op is FilterOp.NOT_IN:
                return f"(NOT COALESCE({base}, FALSE))", params
            return f"(COALESCE({base}, FALSE))", params
        if expr.op in _CMP_OPS:
            op = _CMP_OPS[expr.op]
            return (
                "(CASE WHEN jsonb_typeof(metadata->%s) = 'number' "
                f"THEN (metadata->>%s)::numeric {op} %s::numeric ELSE FALSE END)",
                [key, key, expr.value],
            )
        raise ValidationError(f"unsupported filter op for pgvector: {expr.op}")
    if not isinstance(expr, FilterGroup):
        raise ValidationError(f"unsupported PostgreSQL filter node: {type(expr).__name__}")
    children = [compile_pg_filter(child) for child in expr.children]
    params = [value for _, child_params in children for value in child_params]
    if expr.logic is FilterLogic.AND:
        return f"({' AND '.join(fragment for fragment, _ in children)})", params
    if expr.logic is FilterLogic.OR:
        return f"({' OR '.join(fragment for fragment, _ in children)})", params
    if expr.logic is FilterLogic.NOT:
        return f"(NOT COALESCE({children[0][0]}, FALSE))", params
    raise ValidationError(f"unsupported PostgreSQL filter logic: {expr.logic}")


def pg_scope_clause(scope: Scope, *, exact: bool) -> tuple[str, list[str]]:
    """编译 scope 谓词；CRUD 用五维精确匹配，检索只约束有效维度。"""
    dims = (
        [
            ("org", scope.org),
            ("space", scope.space),
            ("user", scope.user),
            ("agent", scope.agent),
            ("session", scope.session),
        ]
        if exact
        else scope_dims(scope)
    )
    return (
        " AND ".join(f"scope_{dim} = %s" for dim, _ in dims),
        [value for _, value in dims],
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", version)
    if match is None:
        raise ValidationError(f"无法解析 pgvector 扩展版本：{version!r}")
    return tuple(int(part) for part in match.group(1).split("."))


class PgStoreBase:
    """每个 Store 实例自有的惰性 ``psycopg_pool.ConnectionPool``。

    ``dsn`` 可经 ConfigSource 晚绑定（如 ``kv_store.dsn`` / ``vector_store.dsn``）；
    DSN 变化时关闭旧池并按新值重建。旧库数据不自动迁移。
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str,
        table: str,
        pool_min_size: int,
        pool_max_size: int,
        connect_timeout: float,
        application_name: str,
        auto_create_schema: bool,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source: Any = None,
        config_namespace: str = "kv_store",
        config_dsn_field: str = "dsn",
    ) -> None:
        self._fallback_dsn = dsn
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._config_dsn_field = config_dsn_field
        self._schema = schema
        self._table = table
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = ssl_ca_cert
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._connect_timeout = connect_timeout
        self._application_name = application_name
        self._auto_create_schema = auto_create_schema
        self._pool: Any = None
        self._pool_dsn: str | None = None
        self._sql: Any = None
        self._jsonb: Any = None
        self._init_lock = threading.Lock()

    def _resolved_dsn(self) -> str:
        """当前应使用的 DSN（ConfigSource 优先，否则构造期回落）。"""
        from config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field=self._config_dsn_field,
            fallback=self._fallback_dsn,
        )
        return live or self._fallback_dsn

    def _close_pool_unlocked(self) -> None:
        if self._pool is None:
            return
        try:
            self._pool.close()
        except Exception as exc:
            # 重连路径上的尽力关闭；池可能已损坏，仍须丢弃句柄以便重建。
            logger.debug("postgres pool close during reconnect: %s", exc)
        self._pool = None
        self._pool_dsn = None

    @property
    def pool(self) -> Any:
        dsn = self._resolved_dsn()
        if self._pool is not None and self._pool_dsn == dsn:
            return self._pool
        with self._init_lock:
            dsn = self._resolved_dsn()
            if self._pool is not None and self._pool_dsn == dsn:
                return self._pool
            self._close_pool_unlocked()
            try:
                from psycopg import sql
                from psycopg.types.json import Jsonb
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise BackendError(
                    "psycopg client not installed (pip install 'psycopg[binary,pool]')"
                ) from exc

            connect_kwargs: dict[str, Any] = {
                "application_name": self._application_name,
                "connect_timeout": max(1, int(self._connect_timeout)),
            }
            if self._ssl_verify:
                # libpq 把 kwargs 合并进 conninfo 且优先级高于 dsn 中的同名项，
                # 故此处设定即为最终生效值。verify-full 同时校验证书链与主机名；
                # 若须用 IP 直连（证书 CN 为域名），改在 dsn 里写 sslmode=verify-ca。
                connect_kwargs["sslmode"] = "verify-full"
                connect_kwargs["sslrootcert"] = self._ssl_ca_cert
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                kwargs=connect_kwargs,
                open=False,
            )
            try:
                pool.open(wait=True, timeout=self._connect_timeout)
                self._pool = pool
                self._pool_dsn = dsn
                self._sql = sql
                self._jsonb = Jsonb
                self._ensure_schema(pool)
            except AgentMemoryError:
                pool.close()
                self._pool = None
                self._pool_dsn = None
                raise
            except Exception as exc:
                pool.close()
                self._pool = None
                self._pool_dsn = None
                raise BackendError(f"postgres connect: {exc}") from exc
        return self._pool

    def _ensure_schema(self, pool: Any) -> None:
        raise NotImplementedError

    @property
    def sql(self) -> Any:
        if self._sql is None:
            _ = self.pool
        return self._sql

    @property
    def jsonb(self) -> Any:
        if self._jsonb is None:
            _ = self.pool
        return self._jsonb

    def _qualified(self, name: str | None = None) -> Any:
        return self.sql.SQL(".").join(
            (self.sql.Identifier(self._schema), self.sql.Identifier(name or self._table))
        )

    @staticmethod
    def _lock_schema(cursor: Any) -> None:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_SCHEMA_LOCK_KEY,))

    def _create_schema(self, cursor: Any) -> None:
        cursor.execute(
            self._sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                self._sql.Identifier(self._schema)
            )
        )

    def _table_exists(self, cursor: Any) -> bool:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relname = %s
                  AND c.relkind IN ('r', 'p')
            )
            """,
            (self._schema, self._table),
        )
        row = cursor.fetchone()
        return bool(row and row[0])

    def _require_table(self, cursor: Any) -> None:
        if not self._table_exists(cursor):
            raise ValidationError(
                f"PostgreSQL 表不存在：{self._schema}.{self._table}；"
                "请预建或启用 auto_create_schema"
            )

    @staticmethod
    def _require_vector_extension(cursor: Any, minimum: str = "0.8.0") -> str:
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cursor.fetchone()
        if row is None:
            raise ValidationError("pgvector 扩展不存在；请执行 CREATE EXTENSION vector")
        actual = str(row[0])
        if _version_tuple(actual) < _version_tuple(minimum):
            raise ValidationError(f"pgvector 版本过低：实际 {actual}，要求 >= {minimum}")
        return actual

    def _health(self) -> None:
        try:
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            if isinstance(exc, HealthCheckError):
                raise
            raise HealthCheckError(f"postgres health failed: {exc}") from exc

    def close(self) -> None:
        with self._init_lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
