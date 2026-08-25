"""pgvector-backed :class:`~storage.vector.VectorStore` implementation."""

from __future__ import annotations

import json
from typing import Any

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import FilterExpr, Scope
from jiuwen_memory.storage.vector import VectorProducer

from .._pg import PgStoreBase, compile_pg_filter, pg_scope_clause
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

    def __init__(  # pylint: disable=too-many-locals
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
        row = self.sql.SQL("(%s, %s::vector, %s, %s, %s, %s, %s, %s::jsonb)")
        values = self.sql.SQL(", ").join(row for _ in records)
        statement = self.sql.SQL(
            """
            INSERT INTO {} (
                id, embedding, scope_org, scope_space, scope_user, scope_agent,
                scope_session, metadata
            )
            VALUES {}
            ON CONFLICT (
                scope_org, scope_space, scope_user, scope_agent, scope_session, id
            ) DO NOTHING
            RETURNING id
            """
        ).format(self._qualified(), values)
        params = [value for record in records for value in self._record_params(scope, record)]
        with wrap_backend("pgvector insert"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, params)
                inserted = {str(row[0]) for row in cursor.fetchall()}
                if len(inserted) != len(records):
                    conflict = next(record.id for record in records if record.id not in inserted)
                    raise ConflictError(entity="vector", key=conflict)

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:  # pylint: disable=too-many-locals
        if not records:
            return
        duplicate = self._first_duplicate([record.id for record in records])
        if duplicate is not None:
            raise ValidationError(f"duplicate vector id in update batch: {duplicate!r}")
        for record in records:
            self._vector_text(record.vector)
        row = self.sql.SQL("(%s, %s::vector, %s::jsonb)")
        values = self.sql.SQL(", ").join(row for _ in records)
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "target.id = incoming.id"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = self.sql.SQL(
            f"""
            UPDATE {{}} AS target
            SET embedding = incoming.embedding, metadata = incoming.metadata
            FROM (VALUES {{}}) AS incoming(id, embedding, metadata)
            WHERE {where}
            RETURNING target.id
            """
        ).format(self._qualified(), values)
        params: list[Any] = []
        for record in records:
            params.extend(
                [record.id, self._vector_text(record.vector), self.jsonb(record.metadata)]
            )
        params.extend(scope_params)
        with wrap_backend("pgvector update"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, params)
                updated = {str(row[0]) for row in cursor.fetchall()}
                if len(updated) != len(records):
                    missing = next(record.id for record in records if record.id not in updated)
                    raise NotFoundError(entity="vector", key=missing)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "id = ANY(%s::text[])"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = self.sql.SQL(f"DELETE FROM {{}} WHERE {where}").format(self._qualified())
        with wrap_backend("pgvector delete"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [ids, *scope_params])

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if not ids:
            return []
        scope_clause, scope_params = pg_scope_clause(scope, exact=True)
        where = "id = ANY(%s::text[])"
        if scope_clause:
            where += f" AND {scope_clause}"
        statement = self.sql.SQL(
            f"SELECT id, embedding::text, metadata FROM {{}} WHERE {where}"
        ).format(self._qualified())
        with wrap_backend("pgvector get"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(statement, [ids, *scope_params])
                rows = cursor.fetchall()
        return [
            VectorRecord(id=str(id_), vector=json.loads(embedding), metadata=metadata or {})
            for id_, embedding, metadata in rows
        ]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        query_vector = self._vector_text(query.vector)
        where, where_params = self._search_where(scope, query.filters)
        statement = self._knn_statement(where)
        with wrap_backend("pgvector search"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                self._apply_knn_settings(cursor, query)
                cursor.execute(
                    statement,
                    [query_vector, *where_params, query_vector, query.top_k],
                )
                rows = cursor.fetchall()
        return [ScoredID(id=str(id_), score=float(score_value)) for id_, score_value in rows]

    def recall(  # pylint: disable=too-many-locals
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
        with wrap_backend("pgvector recall"):
            with self.pool.connection() as conn, conn.cursor() as cursor:
                self._apply_knn_settings(cursor, query)
                cursor.execute(
                    statement,
                    [query_vector, *where_params, query_vector, query.top_k],
                )
                rows = cursor.fetchall()
        out: list[ScoredHit] = []
        for row in rows:
            if fetch_meta:
                id_, metadata, score_value = row
                out.append(
                    ScoredHit(
                        id=str(id_),
                        score=float(score_value),
                        metadata=metadata or {},
                    )
                )
            else:
                id_, score_value = row
                out.append(ScoredHit(id=str(id_), score=float(score_value)))
        return out

    def _ensure_schema(self, pool: Any) -> None:
        with pool.connection() as conn, conn.cursor() as cursor:
            if not self._auto_create_schema:
                self._require_vector_extension(cursor)
                self._require_table(cursor)
                self._require_dimension(cursor)
                return

            self._lock_schema(cursor)
            if self._create_extension:
                try:
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except Exception as exc:
                    raise BackendError(
                        "创建 pgvector 扩展失败；请由 DBA/云平台预建 vector 扩展"
                    ) from exc
            self._require_vector_extension(cursor)
            self._create_schema(cursor)
            cursor.execute(
                self.sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        id text NOT NULL,
                        embedding vector({}) NOT NULL,
                        scope_org text NOT NULL DEFAULT '',
                        scope_space text NOT NULL DEFAULT '',
                        scope_user text NOT NULL DEFAULT '',
                        scope_agent text NOT NULL DEFAULT '',
                        scope_session text NOT NULL DEFAULT '',
                        metadata jsonb NOT NULL DEFAULT {}::jsonb,
                        PRIMARY KEY (
                            scope_org, scope_space, scope_user, scope_agent,
                            scope_session, id
                        )
                    )
                    """
                ).format(
                    self._qualified(),
                    self.sql.Literal(self._dim),
                    self.sql.Literal("{}"),
                )
            )
            cursor.execute(
                self.sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} "
                    "(scope_org, scope_space, scope_user, scope_agent, scope_session)"
                ).format(
                    self.sql.Identifier(f"{self._table}_scope_idx"),
                    self._qualified(),
                )
            )
            if self._create_metadata_index:
                cursor.execute(
                    self.sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} USING gin "
                        "(metadata jsonb_path_ops)"
                    ).format(
                        self.sql.Identifier(f"{self._table}_metadata_idx"),
                        self._qualified(),
                    )
                )
            if self._index_type == "hnsw":
                opclass = self.sql.SQL(_METRICS[self._metric]["opclass"])
                cursor.execute(
                    self.sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw "
                        "(embedding {}) WITH (m = {}, ef_construction = {})"
                    ).format(
                        self.sql.Identifier(f"{self._table}_embedding_hnsw_idx"),
                        self._qualified(),
                        opclass,
                        self.sql.Literal(self._hnsw_m),
                        self.sql.Literal(self._hnsw_ef_construction),
                    )
                )
            self._require_dimension(cursor)

    def _require_dimension(self, cursor: Any) -> None:
        cursor.execute(
            """
            SELECT a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relname = %s
              AND a.attname = 'embedding'
              AND NOT a.attisdropped
            """,
            (self._schema, self._table),
        )
        row = cursor.fetchone()
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
        return [
            record.id,
            self._vector_text(record.vector),
            scope.org,
            scope.space,
            scope.user,
            scope.agent,
            scope.session,
            self.jsonb(record.metadata),
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

    def _knn_statement(self, where: str, *, extra_columns: str = "") -> Any:
        """构造 ANN 检索语句；``extra_columns`` 追加在 SELECT 列（如 ``metadata``）。

        ``search`` 与 ``recall`` 共用同款 HNSW 调优（ef_search / iterative_scan /
        max_scan_tuples / none 精确模式）与 scope+filters 下推，``recall`` 仅在
        SELECT 列上追加 ``metadata``，把"召回 + 取 payload"合并为一次查询，省掉
        调用方再发一次 ``get`` 的往返（pgvector 下比远端后端更天然——本就一条 SELECT）。
        ``where`` 是已参数化的 SQL 片段字符串（``TRUE`` 或 scope/filter 子句拼接）。
        """
        score = _METRICS[self._metric]["score"]
        operator = _METRICS[self._metric]["operator"]
        columns = "id, " + (f"{extra_columns}, " if extra_columns else "") + f"{score} AS score"
        return self.sql.SQL(
            f"""
            SELECT {columns}
            FROM {{}}
            WHERE {where}
            ORDER BY embedding {operator} %s::vector
            LIMIT %s
            """
        ).format(self._qualified())

    def _apply_knn_settings(self, cursor: Any, query: VectorQuery) -> None:
        """在一次事务内设置 HNSW 检索参数（随连接归还即失效，不影响连接池其他使用者）。"""
        actual_ef = min(1000, max(self._ef_search, query.top_k * 4))
        cursor.execute(
            self.sql.SQL("SET LOCAL hnsw.ef_search = {}").format(self.sql.Literal(actual_ef))
        )
        cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        cursor.execute(
            self.sql.SQL("SET LOCAL hnsw.max_scan_tuples = {}").format(
                self.sql.Literal(self._max_scan_tuples)
            )
        )
        if self._index_type == "none":
            cursor.execute("SET LOCAL enable_indexscan = off")


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
