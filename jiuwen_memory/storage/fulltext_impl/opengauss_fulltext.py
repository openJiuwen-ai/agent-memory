# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""openGauss native BM25 FulltextStore with guarded index-backed scoring."""

from __future__ import annotations

import json
import math
from typing import Any

from jiuwen_memory.common.errors import BackendError, ValidationError
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
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.types import Document, ScoredHit, ScoredID, TextQuery

logger = get_logger(__name__)


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{name} must be a finite number in [{minimum}, {maximum}]") from exc
    if isinstance(value, bool) or not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValidationError(f"{name} must be a finite number in [{minimum}, {maximum}]")
    return result


class OpenGaussFulltextStore(OpenGaussStoreBase, FulltextStore):
    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "public",
        table: str = "agent_memory_fulltext",
        bm25_k1: float = 1.2,
        bm25_b: float = 0.75,
        max_score_ratio: float = 1.05,
        **options: Any,
    ) -> None:
        self._k1 = _bounded_float(bm25_k1, "bm25_k1", 0, 3)
        self._b = _bounded_float(bm25_b, "bm25_b", 0, 1)
        self._max_score_ratio = _bounded_float(max_score_ratio, "max_score_ratio", 0.5, 1.3)
        self._bm25_index_names: frozenset[str] = frozenset()
        options.setdefault("config_namespace", "fulltext_store")
        super().__init__(dsn=dsn, schema=schema, table=table, **options)

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset(
            {
                StoreCapability.RESOURCE_DESCRIPTOR,
                StoreCapability.SHARD_PREFILTER,
                StoreCapability.UNIT_RECORD_LOOKUP,
            }
        )

    def _ensure_schema(self, cursor) -> None:
        self._ensure_table(cursor, "content text NOT NULL")
        if self._auto_create_schema:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {self._index_name('bm25')} "
                f"ON {self._table_ref} USING bm25 (content)"
            )
        if not self._refresh_bm25_indexes(cursor):
            raise ValidationError(
                "openGauss fulltext requires a valid, non-partial, "
                "single-column BM25(content) index"
            )

    def _refresh_bm25_indexes(self, cursor) -> frozenset[str]:
        # Pre-created tables may name the index freely, so it is discovered, not derived.
        cursor.execute(
            "SELECT ci.relname FROM pg_index i "
            "JOIN pg_class ci ON ci.oid = i.indexrelid "
            "JOIN pg_am am ON am.oid = ci.relam "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            "WHERE i.indrelid = %s::regclass AND am.amname = 'bm25' AND a.attname = 'content' "
            "AND i.indisvalid AND i.indisready AND i.indnatts = 1 "
            "AND i.indpred IS NULL AND i.indexprs IS NULL",
            (self._table_ref,),
        )
        self._bm25_index_names = frozenset(str(row[0]) for row in cursor.fetchall())
        return self._bm25_index_names

    @staticmethod
    def _text(value: str) -> str:
        if not isinstance(value, str) or "\0" in value:
            raise ValidationError("openGauss fulltext requires text without NUL")
        return value

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        self._write_documents(scope, docs, update=False)

    def update(self, scope: Scope, docs: list[Document]) -> None:
        self._write_documents(scope, docs, update=True)

    def _write_documents(self, scope: Scope, docs: list[Document], *, update: bool) -> None:
        rows = [(self._text(doc.text), json_text(doc.metadata)) for doc in docs]
        self._write_rows(
            scope,
            [doc.id for doc in docs],
            rows,
            columns=("content", "metadata"),
            placeholders=("%s", "%s::jsonb"),
            entity="document",
            update=update,
        )

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        if not ids:
            return []
        sql, params = scope_clause(scope, exact=True)
        with self.transaction() as cursor:
            cursor.execute(
                f"SELECT id, content, metadata::text FROM {self._table_ref} "
                f"WHERE {sql} AND id = ANY(%s::text[])",
                (*params, ids),
            )
            return [
                Document(id=str(id_), text=content, metadata=json.loads(metadata))
                for id_, content, metadata in cursor.fetchall()
            ]

    @staticmethod
    def _plan_index(plan: Any) -> str | None:
        """Index name of an ordered index scan under Limit/Result wrappers, else None."""
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except ValueError as exc:
                raise BackendError("openGauss returned an invalid JSON EXPLAIN plan") from exc
        node = plan[0].get("Plan", {}) if isinstance(plan, list) and plan else {}
        while str(node.get("Node Type", "")) in {"Limit", "Result"}:
            children = node.get("Plans", [])
            if len(children) != 1:
                return None
            node = children[0]
        if node.get("Node Type") not in {"Index Scan", "Index Only Scan", "Index Scan Backward"}:
            return None
        if not node.get("Order By"):
            return None
        return node.get("Index Name")

    def _search_settings(self, top_k: int) -> tuple[str, tuple[str, ...]]:
        # set_config(..., true) is SET LOCAL; one statement keeps it to a single round trip.
        settings = (
            ("enable_seqscan", "off"),
            ("enable_bitmapscan", "off"),
            ("enable_indexscan", "on"),
            ("enable_bm25_taat", "off"),
            ("bm25_topk", str(min(200, max(5, top_k)))),
            ("bm25_k1", str(self._k1)),
            ("bm25_b", str(self._b)),
            ("max_score_ratio", str(self._max_score_ratio)),
        )
        sql = "SELECT " + ", ".join("set_config(%s, %s, true)" for _ in settings)
        return sql, tuple(value for pair in settings for value in pair)

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        return [ScoredID(id=hit.id, score=hit.score) for hit in self._search(scope, query)]

    def recall(
        self,
        scope: Scope,
        query: TextQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        if any(field != "metadata" for field in output_fields or []):
            logger.info("OpenGaussFulltextStore.recall supports only the metadata output field")
        return self._search(scope, query, metadata="metadata" in (output_fields or []))

    def _search(self, scope: Scope, query: TextQuery, *, metadata: bool = False) -> list[ScoredHit]:
        top_k = positive_int(query.top_k, "top_k")
        text = self._text(query.text)
        if not text.strip():
            return []
        where, params = self._search_where(scope, query.filters)
        payload = ", metadata::text" if metadata else ""
        statement = (
            f"SELECT id, content <&> %s AS score{payload} FROM {self._table_ref} "
            f"WHERE {where} ORDER BY content <&> %s DESC LIMIT %s"
        )
        arguments = (text, *params, text, top_k)
        with self.transaction() as cursor:
            cursor.execute(*self._search_settings(top_k))
            # BM25 scores are only correct inside a BM25 index scan; costs alone do not
            # guarantee that path. EXPLAIN's AccessShareLock on the table and its indexes
            # lasts until commit, so the verified plan cannot be invalidated by DDL.
            cursor.execute("EXPLAIN (FORMAT JSON) " + statement, arguments)
            row = cursor.fetchone()
            if row is None:
                raise BackendError("openGauss EXPLAIN returned no plan")
            index = self._plan_index(row[0])
            if index is not None and index not in self._bm25_index_names:
                self._refresh_bm25_indexes(cursor)
            if index is None or index not in self._bm25_index_names:
                raise BackendError(
                    "openGauss planner did not select an ordered BM25 index scan; "
                    "refusing standalone BM25 scoring. "
                    "Check table statistics, filters and index validity"
                )
            cursor.execute(statement, arguments)
            # WHERE stays inside the scan query, never outside a globally truncated candidate CTE.
            return [
                ScoredHit(
                    id=str(row[0]),
                    score=float(row[1]),
                    metadata=json.loads(row[2]) if metadata else {},
                )
                for row in cursor.fetchall()
            ]


@FulltextProducer.register(
    "opengauss",
    config_inspectors={"at_rest_locations": lambda config: {"dsn": config.get("dsn")}},
)
def _build(config):
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    ssl = read_ssl_config(config, backend="opengauss fulltext")
    fields = {
        "schema": "public",
        "table": "agent_memory_fulltext",
        "bm25_k1": 1.2,
        "bm25_b": 0.75,
        "max_score_ratio": 1.05,
        "pool_min_size": 1,
        "pool_max_size": 8,
        "pool_timeout": 30,
        "connect_timeout": 10,
        "application_name": "agent_memory",
        "auto_create_schema": True,
        "create_metadata_index": True,
    }
    return OpenGaussFulltextStore(
        dsn=Factory.require_param(config, "dsn", backend="opengauss fulltext"),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
        **{key: Factory.cfg_get(config, key, default) for key, default in fields.items()},
    )
