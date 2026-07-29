"""MilvusVectorStore — 基于 Milvus 的 :class:`~storage.vector.VectorStore` 实现。

``insert/update/delete/get`` 走主键 CRUD，``search`` 走 ANN 近邻检索。``scope`` 为
方法显式入参——写入时拆为四个标量字段落库，``search`` / 按 id 的 ``get`` /
``delete`` 通过布尔表达式约束在该 scope 内（对非空维度施加等值），实现原生隔离
（§5.2 / §7）；``id`` 是全局唯一主键（冲突/缺失按 id 判定），``metadata`` 经动态
字段承载，``filters`` 转为 scope 之外的标量谓词。

``pymilvus`` 惰性导入与连接（目标 API：``MilvusClient``，pymilvus 2.4+）。``score``
直接返回 Milvus 的 distance：``COSINE``/``IP`` 越大越相关、``L2`` 越小越相关，
由配置的 ``metric_type`` 决定语义。
"""

from __future__ import annotations

import json
from typing import Any

from common.errors import (
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    ValidationError,
)
from common.factory.factory import Factory
from common.type_def import (
    FilterClause,
    FilterExpr,
    FilterLogic,
    FilterOp,
    Scope,
    filter_field_metadata_key,
)
from storage.vector import VectorProducer

from .._support import scope_dims, wrap_backend
from ..base import StoreType
from ..types import ScoredID, VectorQuery, VectorRecord
from ..vector import VectorStore

_SCOPE_FIELDS = ("scope_org", "scope_user", "scope_agent", "scope_session")
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
        **options: Any,
    ) -> None:
        if dim <= 0:
            raise ValidationError("milvus vector store requires positive 'dim'")
        self._uri = uri or f"http://{host}:{port}"
        self._token = token
        self._collection = collection
        self._dim = dim
        self._metric_type = metric_type
        # 记忆库需读己之写（read-after-write）：默认 Strong 一致性，让 get/search
        # 立刻看到刚写入/删除的结果，而非 Milvus 默认的 Bounded 陈旧读。
        self._consistency = consistency_level
        self._scope_len = scope_field_max_length
        self._id_len = id_max_length
        self._options = options
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
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
                self._client = MilvusClient(uri=self._uri, **kwargs)
                self._ensure_collection()
        return self._client

    def _ensure_collection(self) -> None:
        if self._client.has_collection(self._collection):
            self._client.load_collection(self._collection)
            return
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=self._id_len)
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

    # --------------------------------------------------------------- 序列化
    @staticmethod
    def _row(scope: Scope, rec: VectorRecord) -> dict[str, Any]:
        return {
            "id": rec.id,
            "vector": rec.vector,
            "scope_org": scope.org,
            "scope_user": scope.user,
            "scope_agent": scope.agent,
            "scope_session": scope.session,
            "metadata": rec.metadata,  # JSON 字段
        }

    @staticmethod
    def _to_record(row: dict[str, Any]) -> VectorRecord:
        return VectorRecord(
            id=row["id"],
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

    def _scope_expr(self, scope: Scope) -> str:
        return " && ".join(f'scope_{dim} == {_lit(val)}' for dim, val in scope_dims(scope))

    def _expr(self, scope: Scope, filters: FilterExpr | None) -> str:
        parts = [self._scope_expr(scope)] if scope_dims(scope) else []
        compiled = self._compile_filter(filters)
        if compiled:
            parts.append(compiled)
        return " && ".join(p for p in parts if p)

    def _existing_ids(self, ids: list[str]) -> set[str]:
        items = ", ".join(_lit(i) for i in ids)
        with wrap_backend("milvus query"):
            rows = self.client.query(
                self._collection,
                filter=f"id in [{items}]",
                output_fields=["id"],
                consistency_level=self._consistency,
            )
        return {row["id"] for row in rows}

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
        existing = self._existing_ids([r.id for r in records])
        if existing:
            raise ConflictError(entity="vector", key=next(iter(existing)))
        with wrap_backend("milvus insert"):
            self.client.insert(self._collection, data=[self._row(scope, r) for r in records])

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        if not records:
            return
        ids = [r.id for r in records]
        missing = set(ids) - self._existing_ids(ids)
        if missing:
            raise NotFoundError(entity="vector", key=next(iter(missing)))
        with wrap_backend("milvus update"):
            self.client.upsert(self._collection, data=[self._row(scope, r) for r in records])

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if not ids:
            return
        items = ", ".join(_lit(i) for i in ids)
        expr = self._expr(scope, None)
        filter_ = f"id in [{items}]" + (f" && {expr}" if expr else "")
        with wrap_backend("milvus delete"):
            self.client.delete(self._collection, filter=filter_)  # scope 内幂等删除

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if not ids:
            return []
        items = ", ".join(_lit(i) for i in ids)
        expr = self._expr(scope, None)
        filter_ = f"id in [{items}]" + (f" && {expr}" if expr else "")
        with wrap_backend("milvus get"):
            rows = self.client.query(
                self._collection,
                filter=filter_,
                output_fields=["id", "vector", *_SCOPE_FIELDS, "metadata"],
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
                search_params={"metric_type": self._metric_type},
                consistency_level=self._consistency,
            )
        hits = results[0] if results else []
        return [ScoredID(id=hit["id"], score=float(hit["distance"])) for hit in hits]

    def score_higher_is_better(self) -> bool:
        # 分数方向随 metric_type：COSINE/IP 越大越相关；L2 等距离型越小越相关。
        return str(self._metric_type).upper() in ("COSINE", "IP")


# -- 注册到 VectorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@VectorProducer.register("milvus")
def _build(config):
    # 三方库后端：uri 必填；dim 取本组件 params.dim，回退到内核共享的 embedder_dim。
    # 其余构造参数（host/port/一致性/字段长度等）均有默认值，可经 params 覆盖。
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
    )
