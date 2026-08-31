# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""pgvector-backed :class:`~storage.vector.VectorStore` implementation."""

from __future__ import annotations

import json
from typing import Any

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import FilterExpr, Scope
from jiuwen_memory.storage.vector import VectorProducer

from .._pg import (
    PgStoreBase,
    _convert_placeholders,
    _quote_ident,
    compile_pg_filter,
    pg_scope_clause,
)
from .._support import read_ssl_config, wrap_backend
from ..base import StoreType
from ..types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from ..vector import VectorStore

logger = get_logger(__name__)

_METRICS = {
    "COSINE": {
        "operator": "<=>",
        "opclass": "vector_cosine_ops",
        "score": "1.0 - (embedding <=> %s::vector)",
    },
    "L2": {
        "operator": "<->",
        "opclass": "vector_l2_ops",
        "score": "1.0 / (1.0 + (embedding <-> %s::vector))",
    },
    "IP": {
        "operator": "<#>",
        "opclass": "vector_ip_ops",
        "score": "-(embedding <#> %s::vector)",
    },
}
_INDEX_TYPES = {"hnsw", "none"}


class PgVectorStore(PgStoreBase, VectorStore):
    """PostgreSQL + pgvector 的 CRUD、过滤下推与 HNSW 近邻搜索。"""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "public",
        table: str = "agent_memory_vectors",
        dim: int = 0,
        metric_type: str = "COSINE",
        index_type: str = "hnsw",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        ef_search: int = 40,
        max_scan_tuples: int = 20000,
        create_metadata_index: bool = True,
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        connect_timeout: float = 10.0,
        application_name: str = "agent_memory",
        auto_create_schema: bool = True,
        create_extension: bool = True,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source=None,
        config_namespace: str = "vector_store",
    ) -> None:
        if dim <= 0:
            raise ValidationError("pgvector store requires positive 'dim'")
        metric = str(metric_type).upper()
        if metric not in _METRICS:
            raise ValidationError(
                f"unsupported pgvector metric_type {metric_type!r}; "
                f"expected one of {sorted(_METRICS)}"
            )
        index = str(index_type).lower()
        if index not in _INDEX_TYPES:
            raise ValidationError(
                f"unsupported pgvector index_type {index_type!r}; "
                f"expected one of {sorted(_INDEX_TYPES)}"
            )
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
        self._dim = dim
        self._metric = metric
        self._index_type = index
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._ef_search = ef_search
        self._max_scan_tuples = max_scan_tuples
        self._create_metadata_index = create_metadata_index
        self._create_extension = create_extension

    @staticmethod
    def _first_duplicate(ids: list[str]) -> str | None:
        seen: set[str] = set()
        for id_ in ids:
            if id_ in seen:
                return id_
            seen.add(id_)
        return None

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        self._health()

    def score_higher_is_better(self) -> bool:
        return True

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        if not records:
            return
        duplicate = self._first_duplicate([record.id for record in records])
        if duplicate is not None:
            raise ConflictError(entity="vector", key=duplicate)
        for record in records:
            self._vector_text(record.vector)
        row = "(%s, %s::vector, %s, %s, %s, %s, %s, %s::jsonb)"
        values = ", ".join(row for _ in records)
        statement = f"""
            INSERT INTO {self._table_ref} (
                id, embedding, scope_org, scope_space, scope_user, scope_agent,
                scope_session, metadata
            )
            VALUES {values}
            ON CONFLICT (
                scope_org, scope_space, scope_user, scope_agent, scope_session, id
            ) DO NOTHING
            RETURNING id
        """
        params = [value for record in records for value in self._record_params(scope, record)]
        pool = self.pool

        async def go() -> None:
            # 批次原子性：部分插入（冲突行被 ON CONFLICT 跳过）须整体回滚——
            # 冲突在事务内抛出，事务上下文退出即 ROLLBACK（psycopg 时期由
            # pool.connection() 异常回滚隐式事务提供同一原子性，asyncpg 默认
            # autocommit 须显式补上）。
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(_convert_placeholders(statement), *params)
                    inserted = {str(row[0]) for row in rows}
                    if len(inserted) != len(records):
                        conflict = next(
                            record.id for record in records if record.id not in inserted
                        )
                        raise ConflictError(entity="vector", key=conflict)

        with wrap_backend("pgvector insert"):
            self._run(go())

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        if not records:
            return
        duplicate = self._first_duplicate([record.id for record in records])
        if duplicate is not None:
            raise ValidationError(f"duplicate vector id in update batch: {duplicate!r}")
        for record in records:
            self._vector_text(record.vector)
        row = "(%s, %s::vector, %s::jsonb)"
        values = ", ".join(row for _ in records)
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "target.id = incoming.id"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = f"""
            UPDATE {self._table_ref} AS target
            SET embedding = incoming.embedding, metadata = incoming.metadata
            FROM (VALUES {values}) AS incoming(id, embedding, metadata)
            WHERE {where}
            RETURNING target.id
        """
        params: list[Any] = []
        for record in records:
            params.extend([record.id, self._vector_text(record.vector), record.metadata])
        params.extend(scope_params)
        pool = self.pool

        async def go() -> None:
            # 与 insert 同理：部分更新（缺失 id 未命中）在事务内抛 NotFoundError，
            # 事务回滚保证批次要么全更新要么不动。
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(_convert_placeholders(statement), *params)
                    updated = {str(row[0]) for row in rows}
                    if len(updated) != len(records):
                        missing = next(
                            record.id for record in records if record.id not in updated
                        )
                        raise NotFoundError(entity="vector", key=missing)

        with wrap_backend("pgvector update"):
            self._run(go())

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "id = ANY(%s::text[])"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = f"DELETE FROM {self._table_ref} WHERE {where}"
        with wrap_backend("pgvector delete"):
            self._execute(statement, (ids, *scope_params))

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if not ids:
            return []
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "id = ANY(%s::text[])"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = (
            f"SELECT id, embedding::text, metadata FROM {self._table_ref} WHERE {where}"
        )
        with wrap_backend("pgvector get"):
            rows = self._fetch_all(statement, (ids, *scope_params))
        return [
            VectorRecord(id=str(row[0]), vector=json.loads(row[1]), metadata=row[2] or {})
            for row in rows
        ]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        query_vector = self._vector_text(query.vector)
        where, where_params = self._search_where(scope, query.filters)
        statement = self._knn_statement(where)
        rows = self._knn_fetch("pgvector search", statement, query, query_vector, where_params)
        return [ScoredID(id=str(row[0]), score=float(row[1])) for row in rows]

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        # 把"召回 + 取 metadata"合并为同一条 KNN SELECT——仅在 SELECT 列追加
        # metadata，省掉调用方再发一次 get 的往返。output_fields 仅认 "metadata"
        # （归并所需的 unit_id 即在其中），其余值忽略并记日志，与 milvus 后端对齐。
        fetch_meta = bool(output_fields) and "metadata" in output_fields
        if output_fields:
            unknown = [f for f in output_fields if f != "metadata"]
            if unknown:
                logger.info(
                    "PgVectorStore.recall: output_fields only supports 'metadata', ignoring %s",
                    unknown,
                )
        query_vector = self._vector_text(query.vector)
        where, where_params = self._search_where(scope, query.filters)
        statement = self._knn_statement(where, extra_columns="metadata" if fetch_meta else "")
        rows = self._knn_fetch("pgvector recall", statement, query, query_vector, where_params)
        out: list[ScoredHit] = []
        for row in rows:
            if fetch_meta:
                out.append(
                    ScoredHit(id=str(row[0]), score=float(row[2]), metadata=row[1] or {})
                )
            else:
                out.append(ScoredHit(id=str(row[0]), score=float(row[1])))
        return out

    def _knn_fetch(
        self,
        action: str,
        statement: str,
        query: VectorQuery,
        query_vector: str,
        where_params: list[Any],
    ) -> list[Any]:
        """KNN 检索共用路径：显式事务内 SET LOCAL + 单条 SELECT。

        ``SET LOCAL`` 依赖显式事务（spike 铁律 3），事务随连接归还即回退，
        不影响连接池其他使用者。
        """
        pool = self.pool

        async def go() -> list[Any]:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._apply_knn_settings(conn, query)
                    return await conn.fetch(
                        _convert_placeholders(statement),
                        query_vector,
                        *where_params,
                        query_vector,
                        query.top_k,
                    )

        with wrap_backend(action):
            return self._run(go())

    async def _ensure_schema(self, pool: Any) -> None:
        async with pool.acquire() as conn:
            if not self._auto_create_schema:
                await self._require_vector_extension(conn)
                await self._require_table(conn)
                await self._require_dimension(conn)
                return

            # advisory xact lock 依赖显式事务（spike 铁律 3），DDL 整体包一个事务。
            async with conn.transaction():
                await self._lock_schema(conn)
                if self._create_extension:
                    try:
                        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    except Exception as exc:
                        raise BackendError(
                            "创建 pgvector 扩展失败；请由 DBA/云平台预建 vector 扩展"
                        ) from exc
                await self._require_vector_extension(conn)
                await self._create_schema(conn)
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table_ref} (
                        id text NOT NULL,
                        embedding vector({int(self._dim)}) NOT NULL,
                        scope_org text NOT NULL DEFAULT '',
                        scope_space text NOT NULL DEFAULT '',
                        scope_user text NOT NULL DEFAULT '',
                        scope_agent text NOT NULL DEFAULT '',
                        scope_session text NOT NULL DEFAULT '',
                        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        PRIMARY KEY (
                            scope_org, scope_space, scope_user, scope_agent,
                            scope_session, id
                        )
                    )
                    """
                )
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quote_ident(self._table + '_scope_idx')} "
                    f"ON {self._table_ref} "
                    "(scope_org, scope_space, scope_user, scope_agent, scope_session)"
                )
                if self._create_metadata_index:
                    await conn.execute(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"{_quote_ident(self._table + '_metadata_idx')} "
                        f"ON {self._table_ref} USING gin (metadata jsonb_path_ops)"
                    )
                if self._index_type == "hnsw":
                    opclass = _METRICS[self._metric]["opclass"]
                    await conn.execute(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"{_quote_ident(self._table + '_embedding_hnsw_idx')} "
                        f"ON {self._table_ref} USING hnsw (embedding {opclass}) "
                        f"WITH (m = {int(self._hnsw_m)}, "
                        f"ef_construction = {int(self._hnsw_ef_construction)})"
                    )
                await self._require_dimension(conn)

    async def _require_dimension(self, conn: Any) -> None:
        row = await conn.fetchrow(
            """
            SELECT a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1
              AND c.relname = $2
              AND a.attname = 'embedding'
              AND NOT a.attisdropped
            """,
            self._schema,
            self._table,
        )
        if row is None:
            raise ValidationError(
                f"PostgreSQL 向量表缺少 embedding 列：{self._schema}.{self._table}"
            )
        actual = int(row[0])
        if actual != self._dim:
            raise ValidationError(
                f"pgvector 维度不匹配：表为 {actual}，配置 dim={self._dim}"
            )

    def _vector_text(self, vector: list[float]) -> str:
        if len(vector) != self._dim:
            raise ValidationError(
                f"vector dimension mismatch: expected {self._dim}, got {len(vector)}"
            )
        return json.dumps(vector, separators=(",", ":"))

    def _record_params(self, scope: Scope, record: VectorRecord) -> list[Any]:
        # metadata 走 jsonb type codec 直传 dict（spike 铁律 1），无需转型。
        return [
            record.id,
            self._vector_text(record.vector),
            scope.org,
            scope.space,
            scope.user,
            scope.agent,
            scope.session,
            record.metadata,
        ]

    def _search_where(
        self, scope: Scope, filters: FilterExpr | None
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        scope_clause, scope_params = pg_scope_clause(scope, exact=False)
        if scope_clause:
            parts.append(scope_clause)
            params.extend(scope_params)
        filter_clause, filter_params = compile_pg_filter(filters)
        if filter_clause:
            parts.append(filter_clause)
            params.extend(filter_params)
        return (" AND ".join(parts) if parts else "TRUE"), params

    def _knn_statement(self, where: str, *, extra_columns: str = "") -> str:
        """构造 ANN 检索语句；``extra_columns`` 追加在 SELECT 列（如 ``metadata``）。

        ``search`` 与 ``recall`` 共用同款 HNSW 调优（ef_search / iterative_scan /
        max_scan_tuples / none 精确模式）与 scope+filters 下推，``recall`` 仅在
        SELECT 列上追加 ``metadata``，把"召回 + 取 payload"合并为一次查询，省掉
        调用方再发一次 ``get`` 的往返。``where`` 是已参数化的 SQL 片段字符串
        （``TRUE`` 或 scope/filter 子句拼接）。
        """
        score = _METRICS[self._metric]["score"]
        operator = _METRICS[self._metric]["operator"]
        columns = "id, " + (f"{extra_columns}, " if extra_columns else "") + f"{score} AS score"
        return f"""
            SELECT {columns}
            FROM {self._table_ref}
            WHERE {where}
            ORDER BY embedding {operator} %s::vector
            LIMIT %s
        """

    async def _apply_knn_settings(self, conn: Any, query: VectorQuery) -> None:
        """在一次事务内设置 HNSW 检索参数（随事务结束即失效）。"""
        actual_ef = min(1000, max(self._ef_search, query.top_k * 4))
        await conn.execute(f"SET LOCAL hnsw.ef_search = {int(actual_ef)}")
        await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        await conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {int(self._max_scan_tuples)}")
        if self._index_type == "none":
            await conn.execute("SET LOCAL enable_indexscan = off")


@VectorProducer.register("pgvector")
def _build(config):
    # sslmode 是参数形态的真开关，无须校验 dsn scheme（见 _pg.PgStoreBase.pool）。
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    ssl = read_ssl_config(config, backend="pgvector")
    return PgVectorStore(
        dsn=Factory.require_param(config, "dsn", backend="pgvector"),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        schema=Factory.cfg_get(config, "schema", "public"),
        table=Factory.cfg_get(config, "table", "agent_memory_vectors"),
        dim=Factory.cfg_get(config, "dim", Factory.cfg_get(config, "embedder_dim", 0)),
        metric_type=Factory.cfg_get(config, "metric_type", "COSINE"),
        index_type=Factory.cfg_get(config, "index_type", "hnsw"),
        hnsw_m=Factory.cfg_get(config, "hnsw_m", 16),
        hnsw_ef_construction=Factory.cfg_get(config, "hnsw_ef_construction", 64),
        ef_search=Factory.cfg_get(config, "ef_search", 40),
        max_scan_tuples=Factory.cfg_get(config, "max_scan_tuples", 20000),
        create_metadata_index=Factory.cfg_get(config, "create_metadata_index", True),
        pool_min_size=Factory.cfg_get(config, "pool_min_size", 1),
        pool_max_size=Factory.cfg_get(config, "pool_max_size", 8),
        connect_timeout=Factory.cfg_get(config, "connect_timeout", 10.0),
        application_name=Factory.cfg_get(config, "application_name", "agent_memory"),
        auto_create_schema=Factory.cfg_get(config, "auto_create_schema", True),
        create_extension=Factory.cfg_get(config, "create_extension", True),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
