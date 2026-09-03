"""PostgreSQL 存储后端共享基础：asyncpg 惰性连接池、schema 工具与过滤编译。

驱动为 asyncpg（Apache-2.0，规避 psycopg 的 LGPL 分发义务）。asyncpg 是纯异步
客户端，而 Store 接口是同步的：经模块级专职事件循环线程（``_LOOP``）桥接——同步
调用方把协程经 ``run_coroutine_threadsafe`` 提交到常驻 loop 并阻塞等待结果；
多个调用线程可并发提交，连接池并发能力不受影响（spike 实测 4 写 + 4 读线程并发
40/40 成功，见 docs/features/storage/F06-asyncpg-driver.md）。

驱动适配三铁律（spike 实测）：
1. jsonb 走 type codec（json.dumps / json.loads），参数类型为 jsonb 时 dict 直传
   直取；但 FROM 子查询的 VALUES 列表无赋值上下文、裸参数会被推断为 text，
   该场景（如 update 的 incoming VALUES）须显式 $N::jsonb 转型；
2. vector 走 text 参数 + $N::vector 服务端转型，无需 pgvector pip 包与 codec；
3. SET LOCAL / pg_advisory_xact_lock 依赖显式事务（asyncpg 默认 autocommit 下
   二者静默失效），必须包 async with conn.transaction()。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import threading
from typing import Any, Coroutine

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    BackendError,
    HealthCheckError,
    ValidationError,
)
from jiuwen_memory.common.log import install_privacy_filter
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    Scope,
    filter_field_metadata_key,
)

from ._support import scope_dims

logger = logging.getLogger(__name__)
install_privacy_filter(logger)

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


# -- asyncpg 适配层 -------------------------------------------------------------- #


def _quote_ident(name: str) -> str:
    """SQL 标识符安全引用（``psycopg.sql.Identifier`` 的等价替代）。"""
    return '"' + name.replace('"', '""') + '"'


def _convert_placeholders(sql_text: str) -> str:
    """把 psycopg 风格 ``%s`` 按出现顺序改写为 asyncpg 的 ``$N``。

    本模块的 SQL 文本不含字面 ``%``（前缀匹配用 ``starts_with`` 函数而非
    ``LIKE``），可安全按出现顺序编号。过滤片段（``compile_pg_filter`` /
    ``pg_scope_clause``）继续产出 ``%s`` 片段，拼装完成后在执行边界统一转换。
    """
    counter = iter(range(1, sql_text.count("%s") + 1))
    return re.sub(r"%s", lambda _m: f"${next(counter)}", sql_text)


class _LoopRunner:
    """进程级专职事件循环线程。

    ``run`` 可从任意线程调用：协程经 ``run_coroutine_threadsafe`` 提交到常驻
    loop，调用线程阻塞等待结果。多个调用线程可并发提交，保住连接池并发。
    线程为 daemon，随进程退出；``close()`` 只关池不停 loop。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._loop is None:
                    raise RuntimeError("pg loop thread is alive but event loop is missing")
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="agent-memory-pg-loop", daemon=True
            )
            self._thread.start()
            return self._loop

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return future.result(timeout)


_LOOP = _LoopRunner()


class PgStoreBase:
    """每个 Store 实例自有的惰性 ``asyncpg.Pool``（经专职事件循环线程驱动）。

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
        self._init_lock = threading.Lock()

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
            self._pool = _LOOP.run(self._create_pool(dsn))
            self._pool_dsn = dsn
            return self._pool

    async def _create_pool(self, dsn: str) -> Any:
        try:
            import asyncpg
        except ImportError as exc:
            raise BackendError(
                "asyncpg client not installed (pip install asyncpg)"
            ) from exc

        async def _init(conn: Any) -> None:
            # jsonb type codec：dict 直传直取（spike 铁律 1）。
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

        connect_kwargs: dict[str, Any] = {
            "timeout": self._connect_timeout,
            "server_settings": {"application_name": self._application_name},
        }
        if self._ssl_verify:
            # verify-full 等价：CERT_REQUIRED 校验证书链，check_hostname 校验主机名。
            # 逃生舱与 psycopg 时期一致：ssl_verify=false 时连接串自带 TLS 参数自理。
            connect_kwargs["ssl"] = self._ssl_context()
        pool: Any = None
        try:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                init=_init,
                **connect_kwargs,
            )
            await self._ensure_schema(pool)
            return pool
        except AgentMemoryError:
            if pool is not None:
                await pool.close()
            raise
        except Exception as exc:
            if pool is not None:
                await pool.close()
            raise BackendError(f"postgres connect: {exc}") from exc

    def _ssl_context(self) -> ssl.SSLContext:
        """verify-full 等价的 SSLContext；缺 CA 在建池阶段即报错（铁律 10）。"""
        if not self._ssl_ca_cert:
            raise ValidationError(
                "ssl_verify=true 需要 ssl_ca_cert（verify-full 等价校验）"
            )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(self._ssl_ca_cert)
        return context

    def close(self) -> None:
        with self._init_lock:
            self._close_pool_unlocked()

    def _resolved_dsn(self) -> str:
        """当前应使用的 DSN（ConfigSource 优先，否则构造期回落）。"""
        from jiuwen_memory.config.binding import resolve_connection_url

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
        pool, self._pool = self._pool, None
        self._pool_dsn = None
        try:
            self._run(pool.close())
        except Exception as exc:
            # 重连路径上的尽力关闭；池可能已损坏，仍须丢弃句柄以便重建。
            logger.debug("postgres pool close during reconnect: %s", exc)

    # -- 桥接执行入口 ------------------------------------------------------------ #

    @staticmethod
    def _run(coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        return _LOOP.run(coro, timeout)

    def _fetch_all(self, statement: str, params: list[Any] | tuple[Any, ...]) -> list[Any]:
        """单语句取回全部行（autocommit，无显式事务）。"""
        pool = self.pool

        async def go() -> list[Any]:
            async with pool.acquire() as conn:
                return await conn.fetch(_convert_placeholders(statement), *params)

        return self._run(go())

    def _fetch_one(self, statement: str, params: list[Any] | tuple[Any, ...]) -> Any:
        """单语句取回首行（autocommit）。"""
        pool = self.pool

        async def go() -> Any:
            async with pool.acquire() as conn:
                return await conn.fetchrow(_convert_placeholders(statement), *params)

        return self._run(go())

    def _execute(
        self, statement: str, params: list[Any] | tuple[Any, ...] = ()
    ) -> str:
        """单语句执行，返回 asyncpg 状态串（autocommit）。"""
        pool = self.pool

        async def go() -> str:
            async with pool.acquire() as conn:
                return await conn.execute(_convert_placeholders(statement), *params)

        return self._run(go())

    @property
    def _table_ref(self) -> str:
        return f"{_quote_ident(self._schema)}.{_quote_ident(self._table)}"

    # -- schema 工具（conn 为 asyncpg 连接，均需显式事务由调用方包好） ------------- #

    async def _ensure_schema(self, pool: Any) -> None:
        raise NotImplementedError

    @staticmethod
    async def _lock_schema(conn: Any) -> None:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _SCHEMA_LOCK_KEY)

    @staticmethod
    async def _require_vector_extension(conn: Any, minimum: str = "0.8.0") -> str:
        row = await conn.fetchrow("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        if row is None:
            raise ValidationError("pgvector 扩展不存在；请执行 CREATE EXTENSION vector")
        actual = str(row[0])
        if _version_tuple(actual) < _version_tuple(minimum):
            raise ValidationError(f"pgvector 版本过低：实际 {actual}，要求 >= {minimum}")
        return actual

    async def _create_schema(self, conn: Any) -> None:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(self._schema)}")

    async def _table_exists(self, conn: Any) -> bool:
        row = await conn.fetchrow(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1
                  AND c.relname = $2
                  AND c.relkind IN ('r', 'p')
            )
            """,
            self._schema,
            self._table,
        )
        return bool(row and row[0])

    async def _require_table(self, conn: Any) -> None:
        if not await self._table_exists(conn):
            raise ValidationError(
                f"PostgreSQL 表不存在：{self._schema}.{self._table}；"
                "请预建或启用 auto_create_schema"
            )

    def _health(self) -> None:
        pool = self.pool

        async def go() -> None:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")

        try:
            self._run(go())
        except Exception as exc:
            if isinstance(exc, HealthCheckError):
                raise
            raise HealthCheckError(f"postgres health failed: {exc}") from exc
