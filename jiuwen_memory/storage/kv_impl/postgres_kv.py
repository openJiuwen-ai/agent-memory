"""PostgreSQL-backed :class:`~storage.kv.KVStore` implementation."""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.kv import KvProducer

from .._pg import PgStoreBase, _quote_ident, pg_scope_clause
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
        # 守卫参数须显式 ::numeric：裸参数与整数 0 比较时 PG 推断为 int4，
        # asyncpg 会把 float ttl 截断成 0，CASE 恒走 ELSE 使 TTL 失效（psycopg
        # 客户端声明 float8 类型故无此问题）。
        return (
            "CASE WHEN %s::numeric > 0 THEN "
            "extract(epoch from clock_timestamp()) + %s ELSE NULL END"
        )

    @staticmethod
    def _scope_params(scope: Scope) -> tuple[str, str, str, str, str]:
        return scope.org, scope.space, scope.user, scope.agent, scope.session

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        self._health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        statement = f"""
            INSERT INTO {self._table_ref} AS current (
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
        params = (*self._scope_params(scope), key, value, ttl, ttl)
        with wrap_backend(f"postgres insert {key!r}"):
            if self._fetch_one(statement, params) is None:
                raise ConflictError(entity="key", key=key)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        statement = f"""
            UPDATE {self._table_ref} SET
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
        params = (value, ttl, ttl, *self._scope_params(scope), key)
        with wrap_backend(f"postgres update {key!r}"):
            if self._fetch_one(statement, params) is None:
                raise NotFoundError(entity="key", key=key)

    def delete(self, scope: Scope, key: str) -> None:
        clause, params = pg_scope_clause(scope, exact=True)
        statement = f"DELETE FROM {self._table_ref} WHERE {clause} AND key = %s"
        with wrap_backend(f"postgres delete {key!r}"):
            self._execute(statement, (*params, key))

    def get(self, scope: Scope, key: str) -> bytes:
        clause, params = pg_scope_clause(scope, exact=True)
        statement = f"""
            SELECT value FROM {self._table_ref}
            WHERE {clause}
              AND key = %s
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
        """
        with wrap_backend(f"postgres get {key!r}"):
            row = self._fetch_one(statement, (*params, key))
        if row is None:
            raise NotFoundError(entity="key", key=key)
        return bytes(row[0])

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        if not keys:
            return []
        clause, params = pg_scope_clause(scope, exact=True)
        statement = f"""
            SELECT key, value FROM {self._table_ref}
            WHERE {clause}
              AND key = ANY(%s)
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
        """
        with wrap_backend(f"postgres mget {len(keys)} keys"):
            rows = self._fetch_all(statement, (*params, keys))
        values = {str(row[0]): bytes(row[1]) for row in rows}
        for key in keys:
            if key not in values:
                raise NotFoundError(entity="key", key=key)
        return [values[key] for key in keys]

    def exists(self, scope: Scope, key: str) -> bool:
        clause, params = pg_scope_clause(scope, exact=True)
        statement = f"""
            SELECT 1 FROM {self._table_ref}
            WHERE {clause}
              AND key = %s
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
        """
        with wrap_backend(f"postgres exists {key!r}"):
            return self._fetch_one(statement, (*params, key)) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        clause, params = pg_scope_clause(scope, exact=True)
        statement = f"""
            SELECT key, value FROM {self._table_ref}
            WHERE {clause}
              AND starts_with(key, %s)
              AND (expires_at IS NULL OR expires_at > extract(epoch from clock_timestamp()))
        """
        with wrap_backend(f"postgres scan {prefix!r}"):
            rows = self._fetch_all(statement, (*params, prefix))
        return [(str(row[0]), bytes(row[1])) for row in rows]

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
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        statement = (
            "SELECT DISTINCT scope_org, scope_space, scope_user, scope_agent, scope_session "
            f"FROM {self._table_ref}"
        )
        with wrap_backend("postgres scopes"):
            rows = self._fetch_all(statement, ())
        return [
            Scope(org=row[0], space=row[1], user=row[2], agent=row[3], session=row[4])
            for row in rows
        ]

    async def _ensure_schema(self, pool: Any) -> None:
        async with pool.acquire() as conn:
            if not self._auto_create_schema:
                await self._require_table(conn)
                return
            # advisory xact lock 依赖显式事务（spike 铁律 3），DDL 整体包一个事务。
            async with conn.transaction():
                await self._lock_schema(conn)
                await self._create_schema(conn)
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table_ref} (
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
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quote_ident(self._table + '_expires_idx')} "
                    f"ON {self._table_ref} (expires_at) "
                    "WHERE expires_at IS NOT NULL"
                )


@KvProducer.register("postgres")
def _build(config):
    # sslmode 是参数形态的真开关，无须校验 dsn scheme（见 _pg.PgStoreBase.pool）。
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
