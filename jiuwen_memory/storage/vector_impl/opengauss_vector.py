# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""openGauss DataVec VectorStore with native HNSW and exact-search modes."""

from __future__ import annotations

import json
import math
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage._opengauss import (
    OpenGaussStoreBase,
    json_text,
    positive_int,
    scope_clause,
)
from jiuwen_memory.storage._support import read_ssl_config
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)
_METRICS = {
    "COSINE": ("<=>", "vector_cosine_ops", "1.0 - (embedding <=> %s::vector)"),
    "L2": ("<->", "vector_l2_ops", "1.0 / (1.0 + (embedding <-> %s::vector))"),
    "IP": ("<#>", "vector_ip_ops", "-(embedding <#> %s::vector)"),
}


class OpenGaussVectorStore(OpenGaussStoreBase, VectorStore):
    def __init__(
        self,
        *,
        dsn: str,
        dim: int,
        schema: str = "public",
        table: str = "agent_memory_vectors",
        metric_type: str = "COSINE",
        index_type: str = "hnsw",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        ef_search: int = 40,
        max_scan_tuples: int = 20000,
        **options: Any,
    ) -> None:
        self._dim = positive_int(dim, "dim", maximum=16000)
        self._metric = str(metric_type).upper()
        if self._metric not in _METRICS:
            raise ValidationError("openGauss metric_type must be COSINE, L2 or IP")
        self._index_type = str(index_type).lower()
        if self._index_type not in {"hnsw", "none"}:
            raise ValidationError("openGauss index_type must be hnsw or none")
        if self._index_type == "hnsw" and self._dim > 2000:
            raise ValidationError(
                "openGauss float32 HNSW supports dim <= 2000; use index_type=none"
            )
        self._hnsw_m = positive_int(hnsw_m, "hnsw_m", maximum=100)
        if self._hnsw_m < 2:
            raise ValidationError("hnsw_m must be >= 2")
        self._hnsw_ef_construction = positive_int(
            hnsw_ef_construction,
            "hnsw_ef_construction",
            maximum=1000,
        )
        if self._hnsw_ef_construction < 2 * self._hnsw_m:
            raise ValidationError("hnsw_ef_construction must be >= 2 * hnsw_m")
        self._ef_search = positive_int(ef_search, "ef_search", maximum=1000)
        self._max_scan_tuples = positive_int(max_scan_tuples, "max_scan_tuples", maximum=2147483647)
        super().__init__(dsn=dsn, schema=schema, table=table, **options)

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset(
            {
                StoreCapability.RESOURCE_DESCRIPTOR,
                StoreCapability.SHARD_PREFILTER,
                StoreCapability.UNIT_RECORD_LOOKUP,
            }
        )

    def _ensure_schema(self, cursor) -> None:
        self._ensure_table(cursor, f"embedding vector({self._dim}) NOT NULL")
        cursor.execute(
            "SELECT a.atttypmod FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attname = 'embedding' "
            "AND NOT a.attisdropped AND t.typname = 'vector'",
            (self._schema, self._table),
        )
        row = cursor.fetchone()
        if row is None or int(row[0]) != self._dim:
            raise ValidationError("openGauss embedding type/dimension differs from configured dim")
        if self._auto_create_schema and self._index_type == "hnsw":
            opclass = _METRICS[self._metric][1]
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {self._index_name('hnsw')} "
                f"ON {self._table_ref} USING hnsw (embedding {opclass}) "
                f"WITH (m = {self._hnsw_m}, ef_construction = {self._hnsw_ef_construction})"
            )

    def _vector_text(self, vector: list[float]) -> str:
        if len(vector) != self._dim:
            raise ValidationError(
                f"vector dimension mismatch: expected {self._dim}, got {len(vector)}"
            )
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("vector values must be finite float32 numbers") from exc
        if any(not math.isfinite(value) or abs(value) > 3.4028234663852886e38 for value in values):
            raise ValidationError("vector values must be finite float32 numbers")
        if self._metric == "COSINE" and not any(values):
            raise ValidationError("cosine search cannot index or query a zero vector")
        return json_text(values)

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._write_vectors(scope, records, update=False)

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._write_vectors(scope, records, update=True)

    def _write_vectors(self, scope: Scope, records: list[VectorRecord], *, update: bool) -> None:
        rows = [(self._vector_text(r.vector), json_text(r.metadata)) for r in records]
        self._write_rows(
            scope,
            [r.id for r in records],
            rows,
            columns=("embedding", "metadata"),
            placeholders=("%s::vector", "%s::jsonb"),
            entity="vector",
            update=update,
        )

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if not ids:
            return []
        sql, params = scope_clause(scope, exact=True)
        with self.transaction() as cursor:
            cursor.execute(
                f"SELECT id, embedding::text, metadata::text FROM {self._table_ref} "
                f"WHERE {sql} AND id = ANY(%s::text[])",
                (*params, ids),
            )
            return [
                VectorRecord(id=str(id_), vector=json.loads(vector), metadata=json.loads(metadata))
                for id_, vector, metadata in cursor.fetchall()
            ]

    def _search(self, scope: Scope, query: VectorQuery, *, metadata: bool) -> list[ScoredHit]:
        top_k = positive_int(query.top_k, "top_k")
        vector = self._vector_text(query.vector)
        where, params = self._search_where(scope, query.filters)
        operator, _, score = _METRICS[self._metric]
        columns = f"id, {score} AS score" + (", metadata::text" if metadata else "")
        with self.transaction() as cursor:
            if self._index_type == "hnsw":
                actual_ef = min(1000, max(self._ef_search, top_k * 4))
                cursor.execute(f"SET LOCAL hnsw_ef_search = {actual_ef}")
                cursor.execute("SET LOCAL hnsw_iterative_scan = strict_order")
                cursor.execute(f"SET LOCAL hnsw_max_scan_tuples = {self._max_scan_tuples}")
                ordering = f"embedding {operator} %s::vector"
            else:
                # Arithmetic prevents an ANN order-by path even on prebuilt tables.
                ordering = f"(embedding {operator} %s::vector) + 0.0"
            cursor.execute(
                f"SELECT {columns} FROM {self._table_ref} WHERE {where} "
                f"ORDER BY {ordering} ASC LIMIT %s",
                (vector, *params, vector, top_k),
            )
            return [
                ScoredHit(
                    id=str(row[0]),
                    score=float(row[1]),
                    metadata=json.loads(row[2]) if metadata else {},
                )
                for row in cursor.fetchall()
            ]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        return [
            ScoredID(id=h.id, score=h.score) for h in self._search(scope, query, metadata=False)
        ]

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        if any(field != "metadata" for field in output_fields or []):
            logger.info("OpenGaussVectorStore.recall supports only the metadata output field")
        return self._search(scope, query, metadata="metadata" in (output_fields or []))


@VectorProducer.register(
    "opengauss",
    config_inspectors={"at_rest_locations": lambda config: {"dsn": config.get("dsn")}},
)
def _build(config):
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    ssl = read_ssl_config(config, backend="opengauss vector")
    fields = {
        "schema": "public",
        "table": "agent_memory_vectors",
        "metric_type": "COSINE",
        "index_type": "hnsw",
        "hnsw_m": 16,
        "hnsw_ef_construction": 64,
        "ef_search": 40,
        "max_scan_tuples": 20000,
        "pool_min_size": 1,
        "pool_max_size": 8,
        "connect_timeout": 10,
        "application_name": "agent_memory",
        "auto_create_schema": True,
        "create_metadata_index": True,
    }
    return OpenGaussVectorStore(
        dsn=Factory.require_param(config, "dsn", backend="opengauss vector"),
        dim=Factory.cfg_get(config, "dim", Factory.cfg_get(config, "embedder_dim", 0)),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
        **{key: Factory.cfg_get(config, key, default) for key, default in fields.items()},
    )
