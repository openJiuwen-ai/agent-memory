"""PostgreSQL-backed :class:`~storage.kv.KVStore` implementation."""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.kv import KvProducer

from .._pg import PgStoreBase, pg_scope_clause
from .._support import read_ssl_config, wrap_backend
from ..base import StoreType
from ..kv import KVStore
from ..types import KVMemoryListResult
from .memory_list import list_memory_entries


class PostgresKVStore(PgStoreBase, KVStore):
    """一张 PostgreSQL 表承载全部 scope 的 bytes 值与 TTL。"""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "public",
        table: str = "agent_memory_kv",
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        connect_timeout: float = 10.0,
        application_name: str = "agent_memory",
        auto_create_schema: bool = True,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source=None,
        config_namespace: str = "kv_store",
    ) -> None:
        """初始化 PostgresKVStore。

        Args:
            dsn: 参数 dsn（str）。
            schema: 参数 schema（str）。
            table: 参数 table（str）。
            pool_min_size: 参数 pool_min_size（int）。
            pool_max_size: 参数 pool_max_size（int）。
            connect_timeout: 参数 connect_timeout（float）。
            application_name: 参数 application_name（str）。
            auto_create_schema: 参数 auto_create_schema（bool）。
            ssl_verify: 参数 ssl_verify（bool）。
            ssl_ca_cert: 参数 ssl_ca_cert（str | None）。
            config_source: 参数 config_source。
            config_namespace: 参数 config_namespace（str）。
        """
        super().__init__(
            dsn=dsn,
            schema=schema,
            table=table,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
            connect_timeout=connect_timeout,
            application_name=application_name,
            auto_create_schema=auto_create_schema,
            ssl_verify=ssl_verify,
            ssl_ca_cert=ssl_ca_cert,
            config_source=config_source,
            config_namespace=config_namespace,
            config_dsn_field="dsn",
        )

    @staticmethod
    def _expiry_sql() -> str:
        """执行 `expiry_sql` 操作。

        Returns:
            返回 str。
        """
        return (
            "CASE WHEN %s > 0 THEN "
            "extract(epoch from clock_timestamp()) + %s ELSE NULL END"
        )

    @staticmethod
    def _scope_params(scope: Scope) -> tuple[str, str, str, str, str]:
        """执行 `scope_params` 操作。

        Args:
            scope: 参数 scope（Scope）。

        Returns:
            返回 tuple[str, str, str, str, str]。
        """
        return scope.org, scope.space, scope.user, scope.agent, scope.session

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.KV

    def health(self) -> None:
        """执行健康检查。"""
        self._health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            ConflictError: 执行失败时抛出。
        """
        statement = self.sql.SQL(
            f"""
            INSERT INTO {{}} AS current (
                scope_org, scope_space, scope_user, scope_agent, scope_session,
                key, value, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, {self._expiry_sql()})
            ON CONFLICT (
                scope_org, scope_space, scope_user, scope_agent, scope_session, key
            )
            DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at
            WHERE current.expires_at IS NOT NULL
              AND current.expires_at <= extract(epoch from clock_timestamp())
            RETURNING 1
            """
        ).format(self._qualified())
        params = (*self._scope_params(scope), key, value, ttl, ttl)
        with wrap_backend(f"postgres insert {key!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, params)
                if cursor.fetchone() is None:
                    raise ConflictError(entity="key", key=key)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        statement = self.sql.SQL(
            f"""
            UPDATE {{}} SET
                value = %s,
                expires_at = {self._expiry_sql()}
            WHERE scope_org = %s
              AND scope_space = %s
              AND scope_user = %s
              AND scope_agent = %s
              AND scope_session = %s
              AND key = %s
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
            RETURNING 1
            """
        ).format(self._qualified())
        params = (value, ttl, ttl, *self._scope_params(scope), key)
        with wrap_backend(f"postgres update {key!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, params)
                if cursor.fetchone() is None:
                    raise NotFoundError(entity="key", key=key)

    def delete(self, scope: Scope, key: str) -> None:
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
        """
        clause, params = pg_scope_clause(scope, exact=True)
        statement = self.sql.SQL(f"DELETE FROM {{}} WHERE {clause} AND key = %s").format(
            self._qualified()
        )
        with wrap_backend(f"postgres delete {key!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [*params, key])

    def get(self, scope: Scope, key: str) -> bytes:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bytes。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        clause, params = pg_scope_clause(scope, exact=True)
        statement = self.sql.SQL(
            f"""
            SELECT value FROM {{}}
            WHERE {clause}
              AND key = %s
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
            """
        ).format(self._qualified())
        with wrap_backend(f"postgres get {key!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [*params, key])
                row = cursor.fetchone()
        if row is None:
            raise NotFoundError(entity="key", key=key)
        return bytes(row[0])

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        """执行 `mget` 操作。

        Args:
            scope: 参数 scope（Scope）。
            keys: 参数 keys（list[str]）。

        Returns:
            返回 list[bytes]。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        if not keys:
            return []
        clause, params = pg_scope_clause(scope, exact=True)
        statement = self.sql.SQL(
            f"""
            SELECT key, value FROM {{}}
            WHERE {clause}
              AND key = ANY(%s)
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
            """
        ).format(self._qualified())
        with wrap_backend(f"postgres mget {len(keys)} keys"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [*params, keys])
                rows = cursor.fetchall()
        values = {str(key): bytes(value) for key, value in rows}
        for key in keys:
            if key not in values:
                raise NotFoundError(entity="key", key=key)
        return [values[key] for key in keys]

    def exists(self, scope: Scope, key: str) -> bool:
        """检查指定记录或资源是否存在。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bool。
        """
        clause, params = pg_scope_clause(scope, exact=True)
        statement = self.sql.SQL(
            f"""
            SELECT 1 FROM {{}}
            WHERE {clause}
              AND key = %s
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
            """
        ).format(self._qualified())
        with wrap_backend(f"postgres exists {key!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [*params, key])
                return cursor.fetchone() is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """扫描指定范围内的记录。

        Args:
            scope: 参数 scope（Scope）。
            prefix: 参数 prefix（str）。

        Returns:
            返回 list[tuple[str, bytes]]。
        """
        clause, params = pg_scope_clause(scope, exact=True)
        statement = self.sql.SQL(
            f"""
            SELECT key, value FROM {{}}
            WHERE {clause}
              AND starts_with(key, %s)
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
            """
        ).format(self._qualified())
        with wrap_backend(f"postgres scan {prefix!r}"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [*params, prefix])
                rows = cursor.fetchall()
        return [(str(key), bytes(value)) for key, value in rows]

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        """列出符合条件的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            offset: 参数 offset（int）。
            limit: 参数 limit（int）。
            memory_types: 参数 memory_types（list[str] | None）。
            filters: 参数 filters（FilterExpr | None）。
            extensions: 参数 extensions（dict[str, str] | None）。

        Returns:
            返回 KVMemoryListResult。
        """
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        """执行 `scopes` 操作。

        Returns:
            返回 list[Scope]。
        """
        statement = self.sql.SQL(
            """
            SELECT DISTINCT
                scope_org, scope_space, scope_user, scope_agent, scope_session
            FROM {}
            """
        ).format(self._qualified())
        with wrap_backend("postgres scopes"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement)
                rows = cursor.fetchall()
        return [
            Scope(org=org, space=space, user=user, agent=agent, session=session)
            for org, space, user, agent, session in rows
        ]

    def _ensure_schema(self, pool: Any) -> None:
        """确保所需资源或状态已就绪。

        Args:
            pool: 参数 pool（Any）。
        """
        with pool.connection() as conn, conn.cursor() as cursor:
            if not self._auto_create_schema:
                self._require_table(cursor)
                return
            self._lock_schema(cursor)
            self._create_schema(cursor)
            cursor.execute(
                self.sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        scope_org text NOT NULL,
                        scope_space text NOT NULL,
                        scope_user text NOT NULL,
                        scope_agent text NOT NULL,
                        scope_session text NOT NULL,
                        key text NOT NULL,
                        value bytea NOT NULL,
                        expires_at double precision,
                        PRIMARY KEY (
                            scope_org, scope_space, scope_user, scope_agent,
                            scope_session, key
                        )
                    )
                    """
                ).format(self._qualified())
            )
            cursor.execute(
                self.sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} (expires_at) "
                    "WHERE expires_at IS NOT NULL"
                ).format(
                    self.sql.Identifier(f"{self._table}_expires_idx"),
                    self._qualified(),
                )
            )


@KvProducer.register("postgres")
def _build(config):
    # sslmode 是参数形态的真开关，无须校验 dsn scheme（见 _pg.PgStoreBase.pool）。
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    ssl = read_ssl_config(config, backend="postgres KV")
    return PostgresKVStore(
        dsn=Factory.require_param(config, "dsn", backend="postgres KV"),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        schema=Factory.cfg_get(config, "schema", "public"),
        table=Factory.cfg_get(config, "table", "agent_memory_kv"),
        pool_min_size=Factory.cfg_get(config, "pool_min_size", 1),
        pool_max_size=Factory.cfg_get(config, "pool_max_size", 8),
        connect_timeout=Factory.cfg_get(config, "connect_timeout", 10.0),
        application_name=Factory.cfg_get(config, "application_name", "agent_memory"),
        auto_create_schema=Factory.cfg_get(config, "auto_create_schema", True),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
