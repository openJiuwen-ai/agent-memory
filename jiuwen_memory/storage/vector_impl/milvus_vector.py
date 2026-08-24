# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MilvusVectorStore — 基于 Milvus 的 :class:`~storage.vector.VectorStore` 实现。

``insert/update/delete/get`` 走主键 CRUD，``search`` 走 ANN 近邻检索。``scope`` 为
方法显式入参——写入时拆为五个标量字段落库，``search`` / 按 id 的 ``get`` /
``delete`` 通过布尔表达式约束在该 scope 内（对非空维度施加等值），实现原生隔离
（§5.2 / §7）；``id`` 是 scope 内逻辑主键，物理主键由 scope+id 生成，``metadata`` 经动态
字段承载，``filters`` 转为 scope 之外的标量谓词。

``pymilvus`` 惰性导入与连接（目标 API：``MilvusClient``，pymilvus 2.4+）。``score``
直接返回 Milvus 的 distance：``COSINE``/``IP`` 越大越相关、``L2`` 越小越相关，
由配置的 ``metric_type`` 决定语义。
"""

from __future__ import annotations

import json
from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterLogic,
    FilterOp,
    Scope,
    filter_field_metadata_key,
)
from jiuwen_memory.storage.vector import VectorProducer

from .._support import read_ssl_config, scope_dims, scope_segments, wrap_backend
from ..base import StoreType
from ..types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from ..vector import VectorStore

_SCOPE_FIELDS = (
    "scope_org",
    "scope_space",
    "scope_user",
    "scope_agent",
    "scope_session",
)

logger = get_logger(__name__)
_CMP_OPS = {
    FilterOp.EQ: "==",
    FilterOp.NE: "!=",
    FilterOp.GT: ">",
    FilterOp.GTE: ">=",
    FilterOp.LT: "<",
    FilterOp.LTE: "<=",
}


def _lit(value: Any) -> str:
    """把标量渲染为 Milvus 表达式字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    raise ValidationError(f"unsupported filter literal: {value!r}")


def _hit_id(hit: Any) -> str:
    if isinstance(hit, dict):
        entity = hit.get("entity") or {}
        if isinstance(entity, dict) and entity.get("logical_id"):
            return str(entity["logical_id"])
        if hit.get("logical_id"):
            return str(hit["logical_id"])
        return _logical_id(str(hit["id"]))
    try:
        entity = hit.get("entity") or {}
        if entity.get("logical_id"):
            return str(entity["logical_id"])
    except AttributeError:
        pass
    return _logical_id(str(hit["id"]))


def _logical_id(physical_id: str) -> str:
    parts = physical_id.split(":", 5)
    return parts[-1] if len(parts) == 6 else physical_id


class MilvusVectorStore(VectorStore):
    def __init__(
        self,
        *,
        uri: str | None = None,
        host: str = "localhost",
        port: int = 19530,
        token: str | None = None,
        collection: str = "agent_memory_vectors",
        dim: int = 0,
        metric_type: str = "COSINE",
        consistency_level: str = "Strong",
        scope_field_max_length: int = 256,
        id_max_length: int = 512,
        config_source=None,
        config_namespace: str = "vector_store",
        **options: Any,
    ) -> None:
        if dim <= 0:
            raise ValidationError("milvus vector store requires positive 'dim'")
        self._fallback_uri = uri or f"http://{host}:{port}"
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._token = token
        self._collection = collection
        self._dim = dim
        self._metric_type = metric_type
        # 记忆库需读己之写（read-after-write）：默认 Strong 一致性，让 get/search
        # 立刻看到刚写入/删除的结果，而非 Milvus 默认的 Bounded 陈旧读。
        self._consistency = consistency_level
        self._scope_len = scope_field_max_length
        self._id_len = id_max_length
        self._physical_id_len = id_max_length + 5 * (scope_field_max_length + 1)
        self._options = options
        self._client: Any = None
        self._client_uri: str | None = None

    @property
    def client(self) -> Any:
        uri = self._resolved_uri()
        if self._client is not None and self._client_uri == uri:
            return self._client
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise BackendError(
                "pymilvus client not installed (pip install pymilvus)"
            ) from exc
        with wrap_backend("milvus connect"):
            kwargs: dict[str, Any] = dict(self._options)
            if self._token:
                kwargs["token"] = self._token
            self._client = MilvusClient(uri=uri, **kwargs)
            self._client_uri = uri
            self._ensure_collection()
        return self._client

    # --------------------------------------------------------------- 序列化
    @staticmethod
    def _physical_id(scope: Scope, logical_id: str) -> str:
        return ":".join((*scope_segments(scope), logical_id))

    @staticmethod
    def _to_record(row: dict[str, Any]) -> VectorRecord:
        return VectorRecord(
            id=row.get("logical_id") or _logical_id(row["id"]),
            vector=list(row.get("vector") or []),
            metadata=row.get("metadata") or {},
        )

    @staticmethod
    def _filter_clause(fc: FilterClause) -> str:
        key = filter_field_metadata_key(fc.field)
        field = f"metadata[{_lit(key)}]"
        if fc.op in _CMP_OPS:
            return f"{field} {_CMP_OPS[fc.op]} {_lit(fc.value)}"
        if fc.op == FilterOp.IN:
            items = ", ".join(_lit(v) for v in fc.value)
            return f"{field} in [{items}]"
        if fc.op == FilterOp.NOT_IN:
            items = ", ".join(_lit(v) for v in fc.value)
            return f"{field} not in [{items}]"
        if fc.op == FilterOp.CONTAINS:  # metadata 为 JSON，数组包含用 json_contains
            return f"json_contains({field}, {_lit(fc.value)})"
        raise ValidationError(f"unsupported filter op for vector: {fc.op}")

    @classmethod
    def _row(cls, scope: Scope, rec: VectorRecord) -> dict[str, Any]:
        return {
            "id": cls._physical_id(scope, rec.id),
            "logical_id": rec.id,
            "vector": rec.vector,
            "scope_org": scope.org,
            "scope_space": scope.space,
            "scope_user": scope.user,
            "scope_agent": scope.agent,
            "scope_session": scope.session,
            "metadata": rec.metadata,  # JSON 字段
        }

    @classmethod
    def _compile_filter(cls, expr: FilterExpr | None) -> str:
        """把完整 FilterExpr 编译为 Milvus scalar filtering 表达式。"""
        if expr is None:
            return ""
        if isinstance(expr, FilterClause):
            return cls._filter_clause(expr)
        children = [cls._compile_filter(child) for child in expr.children]
        if expr.logic is FilterLogic.AND:
            return f"({' && '.join(children)})"
        if expr.logic is FilterLogic.OR:
            return f"({' || '.join(children)})"
        if expr.logic is FilterLogic.NOT:
            return f"(not ({children[0]}))"
        raise ValidationError(f"unsupported filter logic for vector: {expr.logic}")

    # --------------------------------------------------------------- CRUD
    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def health(self) -> None:
        try:
            self.client.list_collections()
        except Exception as exc:
            raise HealthCheckError(f"milvus health failed: {exc}") from exc

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        if not records:
            return
        existing = self._existing_ids(scope, [r.id for r in records])
        if existing:
            raise ConflictError(entity="vector", key=next(iter(existing)))
        with wrap_backend("milvus insert"):
            self.client.insert(self._collection, data=[self._row(scope, r) for r in records])

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        if not records:
            return
        ids = [r.id for r in records]
        missing = set(ids) - self._existing_ids(scope, ids)
        if missing:
            raise NotFoundError(entity="vector", key=next(iter(missing)))
        with wrap_backend("milvus update"):
            self.client.upsert(self._collection, data=[self._row(scope, r) for r in records])

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        items = ", ".join(_lit(self._physical_id(scope, i)) for i in ids)
        expr = self._expr(scope, None)
        filter_ = f"id in [{items}]" + (f" && {expr}" if expr else "")
        with wrap_backend("milvus delete"):
            self.client.delete(self._collection, filter=filter_)  # scope 内幂等删除

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if not ids:
            return []
        items = ", ".join(_lit(self._physical_id(scope, i)) for i in ids)
        expr = self._expr(scope, None)
        filter_ = f"id in [{items}]" + (f" && {expr}" if expr else "")
        with wrap_backend("milvus get"):
            rows = self.client.query(
                self._collection,
                filter=filter_,
                output_fields=["id", "logical_id", "vector", *_SCOPE_FIELDS, "metadata"],
                consistency_level=self._consistency,
            )
        return [self._to_record(row) for row in rows]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        expr = self._expr(scope, query.filters)
        with wrap_backend("milvus search"):
            results = self.client.search(
                self._collection,
                data=[query.vector],
                limit=query.top_k,
                filter=expr,
                output_fields=["logical_id"],
                search_params={"metric_type": self._metric_type},
                consistency_level=self._consistency,
            )
        hits = results[0] if results else []
        return [ScoredID(id=_hit_id(hit), score=float(hit["distance"])) for hit in hits]

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        # 把"召回 + 取 metadata"合并为一次 Milvus search 请求，省掉调用方再发
        # 一次 get 的网络 RTT 与服务端 id 匹配开销。output_fields 仅认 "metadata"
        # （归并所需的 unit_id 即在其中），其余值忽略并记日志。
        fetch_meta = bool(output_fields) and "metadata" in output_fields
        if output_fields:
            unknown = [f for f in output_fields if f != "metadata"]
            if unknown:
                logger.info("MilvusVectorStore.recall: output_fields only supports 'metadata', ignoring %s", unknown)
        expr = self._expr(scope, query.filters)
        milvus_out = ["logical_id", "metadata"] if fetch_meta else ["logical_id"]
        with wrap_backend("milvus recall"):
            results = self.client.search(
                self._collection,
                data=[query.vector],
                limit=query.top_k,
                filter=expr,
                output_fields=milvus_out,
                search_params={"metric_type": self._metric_type},
                consistency_level=self._consistency,
            )
        hits = results[0] if results else []
        out: list[ScoredHit] = []
        for hit in hits:
            meta: dict[str, Any] = {}
            if fetch_meta:
                entity = hit.get("entity") or {}
                raw = entity.get("metadata")
                if isinstance(raw, dict):
                    meta = raw
            out.append(
                ScoredHit(
                    id=_hit_id(hit),
                    score=float(hit["distance"]),
                    metadata=meta,
                )
            )
        return out

    def score_higher_is_better(self) -> bool:
        # 分数方向随 metric_type：COSINE/IP 越大越相关；L2 等距离型越小越相关。
        return str(self._metric_type).upper() in ("COSINE", "IP")

    def _resolved_uri(self) -> str:
        """当前 Milvus URI（ConfigSource ``vector_store.uri`` / ``fusion_store.uri``）。"""
        from jiuwen_memory.config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="uri",
            fallback=self._fallback_uri,
        )
        return live or self._fallback_uri

    def _ensure_collection(self) -> None:
        if self._client.has_collection(self._collection):
            self._client.load_collection(self._collection)
            return
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            "id", DataType.VARCHAR, is_primary=True, max_length=self._physical_id_len
        )
        schema.add_field("logical_id", DataType.VARCHAR, max_length=self._id_len)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        for fld in _SCOPE_FIELDS:
            schema.add_field(fld, DataType.VARCHAR, max_length=self._scope_len)
        schema.add_field("metadata", DataType.JSON)  # scope 之外的元数据整体存 JSON
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type=self._metric_type
        )
        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
            consistency_level=self._consistency,
        )
        self._client.load_collection(self._collection)

    def _scope_expr(self, scope: Scope) -> str:
        return " && ".join(f'scope_{dim} == {_lit(val)}' for dim, val in scope_dims(scope))

    def _expr(self, scope: Scope, filters: FilterExpr | None) -> str:
        parts = [self._scope_expr(scope)] if scope_dims(scope) else []
        compiled = self._compile_filter(filters)
        if compiled:
            parts.append(compiled)
        return " && ".join(p for p in parts if p)

    def _existing_ids(self, scope: Scope, ids: list[str]) -> set[str]:
        physical_ids = [self._physical_id(scope, rec_id) for rec_id in ids]
        items = ", ".join(_lit(i) for i in physical_ids)
        with wrap_backend("milvus query"):
            rows = self.client.query(
                self._collection,
                filter=f"id in [{items}]",
                output_fields=["logical_id"],
                consistency_level=self._consistency,
            )
        return {row["logical_id"] for row in rows}


# -- 注册到 VectorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@VectorProducer.register("milvus")
def _build(config):
    # 三方库后端：uri 必填；dim 取本组件 params.dim，回退到内核共享的 embedder_dim。
    # 其余构造参数（host/port/一致性/字段长度等）均有默认值，可经 params 覆盖。
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    ssl = read_ssl_config(config, backend="milvus vector")
    options: dict[str, Any] = {}
    if ssl.verify:
        # secure 是真开关，无须校验 uri scheme（https:// 亦会置位，两者等价）。
        # 单向 TLS 用 server_pem_path：ca_pem_path 属双向认证分支，须与客户端证书
        # 和私钥同时提供才生效，单独配置会静默失效。
        options["secure"] = True
        options["server_pem_path"] = ssl.ca_cert
    return MilvusVectorStore(
        uri=Factory.require_param(config, "uri", backend="milvus vector"),
        host=Factory.cfg_get(config, "host", "localhost"),
        port=Factory.cfg_get(config, "port", 19530),
        token=Factory.cfg_get(config, "token"),
        collection=Factory.cfg_get(config, "collection", "agent_memory_vectors"),
        dim=Factory.cfg_get(config, "dim", Factory.cfg_get(config, "embedder_dim", 0)),
        metric_type=Factory.cfg_get(config, "metric_type", "COSINE"),
        consistency_level=Factory.cfg_get(config, "consistency_level", "Strong"),
        scope_field_max_length=Factory.cfg_get(config, "scope_field_max_length", 256),
        id_max_length=Factory.cfg_get(config, "id_max_length", 512),
        config_source=ConfigSourceProducer.get_cached("default"),
        **options,
    )
