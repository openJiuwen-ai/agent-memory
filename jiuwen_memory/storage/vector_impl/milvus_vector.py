# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MilvusVectorStore — 基于 Milvus 的 :class:`~storage.vector.VectorStore` 实现。

``insert/update/delete/get`` 走主键 CRUD，``search`` 走 ANN 近邻检索。``scope`` 为
方法显式入参——写入时拆为五个标量字段落库，``search`` / 按 id 的 ``get`` /
``delete`` 通过布尔表达式约束在该 scope 内（对非空维度施加等值），实现原生隔离
（§5.2 / §7）；``id`` 是 scope 内逻辑主键，物理主键由 scope+id 生成，``metadata`` 经动态
字段承载，``filters`` 转为 scope 之外的标量谓词。

工厂在装配期预加载 ``pymilvus``，连接仍惰性执行（目标 API：``MilvusClient``，
pymilvus 2.4+）。``score``
直接返回 Milvus 的 distance：``COSINE``/``IP`` 越大越相关、``L2`` 越小越相关，
由配置的 ``metric_type`` 决定语义。
"""

from __future__ import annotations

import json
import threading
import time
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

from .._support import preload_backend, read_ssl_config, scope_dims, scope_segments, wrap_backend
from ..base import StoreType
from ..capability import StoreCapability
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


def _is_not_loaded(exc: Exception) -> bool:
    """Check if a Milvus exception indicates collection not loaded or connection lost.

    两类可自愈故障：code=101 (collection not loaded) 或
    ConnectionNotExistException (connection closed/lost, e.g. shared alias removed)。
    """
    code = getattr(exc, "code", None)
    if code == 101:
        return True
    msg = str(exc).lower()
    return (
        "not loaded" in msg
        or "code=101" in msg
        or "should create connection first" in msg
    )


def require_positive_dim(dim: int, *, backend: str = "milvus vector store") -> None:
    """向量后端与融合工厂共用正维度规则，错误标明实际后端。"""
    if dim <= 0:
        raise ValidationError(f"{backend} requires positive 'dim'")


#: ``record_ids_for_units`` 单次查询上限（Milvus 默认 query 结果窗口）；达到即报错，不静默截断。
_RECORD_LOOKUP_LIMIT = 16_384


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
        load_timeout: float = 30.0,
        config_source=None,
        config_namespace: str = "vector_store",
        **options: Any,
    ) -> None:
        require_positive_dim(dim)
        if load_timeout <= 0:
            raise ValidationError("milvus vector store requires positive 'load_timeout'")
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
        self._load_timeout = load_timeout
        self._options = options
        self._client: Any = None
        self._client_uri: str | None = None
        # 建连互斥：并发首访会各自 MilvusClient(...) 建连，后者的连接被前者
        # 覆盖后未关闭即泄漏 gRPC channel；同时 ensure 竞态。
        self._connect_lock = threading.Lock()

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
        with self._connect_lock:
            # 双检：等锁期间另一线程可能已完成建连 + ensure。
            if self._client is not None and self._client_uri == uri:
                return self._client
            with wrap_backend("milvus connect"):
                kwargs: dict[str, Any] = dict(self._options)
                if self._token:
                    kwargs["token"] = self._token
                client = MilvusClient(uri=uri, **kwargs)
            # ensure 全程走局部 client，成功后才发布缓存：发布前快路径不会把
            # 「尚未 ensure」的连接发给其他线程，失败也不产生共享状态回滚，
            # 与 _recover_connection 的缓存清空互不踩踏。
            try:
                with wrap_backend("milvus connect"):
                    self._ensure_collection(client)
            except Exception:
                closer = getattr(client, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        logger.debug(
                            "milvus client close after failed ensure raised", exc_info=True
                        )
                raise
            self._client, self._client_uri = client, uri
        # 返回本线程已 ensure 成功的局部 client：锁外复查共享缓存存在窄竞态，
        # 窗口内被 _recover_connection 清空会返回 None，调用方 client.search(...)
        # 将抛裸 AttributeError 而非归一化的 BackendError。
        return client

    def _wait_for_load_complete(self, client: Any, timeout: float | None = None) -> None:
        """Poll load state until the collection is fully loaded or timeout elapses."""
        # load_collection 可能在集合真正载入 query node 之前就返回，紧随其后的
        # search 会撞 code=101 (collection not loaded)（如首次 mem2_add 后的
        # dedup search）；故轮询 get_load_state 直到 Loaded。
        # timeout 缺省取 load_timeout——慢集群调大、fail-fast 部署调小。
        if not callable(getattr(client, "get_load_state", None)):
            return
        if timeout is None:
            timeout = self._load_timeout
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                state = client.get_load_state(collection_name=self._collection)
                load_state = state.get("state") if isinstance(state, dict) else state
                if str(load_state) == "Loaded":
                    return
                # 查询成功但尚未 Loaded：清掉历史异常，继续轮询。
                last_error = None
            except Exception as exc:
                last_error = exc
            time.sleep(0.5)
        raise BackendError(
            f"MilvusVectorStore: collection {self._collection} not loaded within {timeout:.1f}s"
        ) from last_error

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
    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset(
            {
                StoreCapability.RESOURCE_DESCRIPTOR,
                StoreCapability.SHARD_PREFILTER,
                StoreCapability.UNIT_RECORD_LOOKUP,
            }
        )

    def record_ids_for_units(
        self, scope: Scope, unit_ids: list[str]
    ) -> dict[str, list[str]]:
        """按 ``metadata.unit_id`` 找出该记忆的全部记录 id（F09 D16）。

        依赖 Milvus 对 ``metadata`` JSON 字段的标量索引；未建索引时按表达式扫描。
        """
        if not unit_ids:
            return {}
        literals = ", ".join(_lit(unit_id) for unit_id in unit_ids)
        expr = f'metadata["unit_id"] in [{literals}]'
        scope_expr = self._expr(scope, None)
        if scope_expr:
            expr = f"({expr}) && ({scope_expr})"
        with wrap_backend(f"milvus record lookup {len(unit_ids)} units"):
            rows = self.client.query(
                self._collection,
                filter=expr,
                output_fields=["logical_id", "metadata"],
                # 搬迁随后会清理源分片：查漏即丢记录，这里不跟随部署配置的较弱一致性。
                consistency_level="Strong",
                limit=_RECORD_LOOKUP_LIMIT,
            )
        if len(rows) >= _RECORD_LOOKUP_LIMIT:
            raise BackendError(
                f"record lookup for {len(unit_ids)} unit(s) reached the Milvus query limit "
                f"{_RECORD_LOOKUP_LIMIT}; split the request"
            )
        out: dict[str, list[str]] = {}
        for row in rows:
            metadata = row.get("metadata") or {}
            unit_id = metadata.get("unit_id") if isinstance(metadata, dict) else None
            if isinstance(unit_id, str):
                out.setdefault(unit_id, []).append(str(row["logical_id"]))
        return out

    def resource_descriptor(self) -> str | None:
        from .._support import sanitize_resource_id

        uri = self._resolved_uri()
        if not uri:
            return None
        # 分片 split 的隔离单位是 collection，不是实例；不同分片共用实例时
        # 必须靠 collection 名区分，不能把整个实例误判成同一物理资源。
        return f"milvus://{sanitize_resource_id(uri).rstrip('/')}/{self._collection}"

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

    def insert_for_relocation(
        self, scope: Scope, records: list[VectorRecord]
    ) -> None:
        """搬迁复制用强一致预检：弱一致下重复调用不会产生同主键重复行。"""
        if not records:
            return
        existing = self._existing_ids(
            scope, [r.id for r in records], consistency="Strong"
        )
        if existing:
            raise ConflictError(entity="vector", key=next(iter(existing)))
        with wrap_backend("milvus insert"):
            self.client.insert(
                self._collection, data=[self._row(scope, r) for r in records]
            )

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
        return self._get(scope, ids, self._consistency)

    def get_for_relocation(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        """搬迁读取固定强一致：ID 由强一致枚举得到，读记录不能再用弱一致。

        部署配置为 Eventually / Bounded 时，枚举后立即 ``get`` 可能读不到刚写入的记录，
        若包装层把缺记录当成成功并清理源分片，就会丢索引。这里与枚举用同一一致性级别，
        包装层还会再校验覆盖全部 ID。
        """
        return self._get(scope, ids, "Strong")

    def _get(
        self, scope: Scope, ids: list[str], consistency: str
    ) -> list[VectorRecord]:
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
                consistency_level=consistency,
            )
        return [self._to_record(row) for row in rows]

    def _recover_connection(self, op: str, failed: Any) -> None:
        """统一恢复：仅当共享缓存仍指向调用方确认失效的连接时才清空重建。"""
        logger.warning(
            "MilvusVectorStore: %s failed with not-loaded/connection-lost, "
            "reconnecting and ensuring collection %s",
            op,
            self._collection,
        )
        # 缓存写入统一收敛到 _connect_lock 内：property 的建连与这里的清空
        # 是仅有的两个写入口，避免恢复路径与在建线程互踩共享状态。
        with self._connect_lock:
            if self._client is not failed:
                # 缓存已不是本次失败的那个连接：并发恢复时他人刚发布了新
                # client（或正在重建），让位复用其成果——无条件清空会丢弃
                # 新 client 且无 close，泄漏 gRPC channel。
                return
            self._client = None
            self._client_uri = None
        # 清空与重建不必同临界区：清空后缓存为 None，其余恢复者经 is 比较
        # 自然让位；重建走 property 双检，并发下只建一个连接。
        # 不可把 self.client 放进上面的锁内——Lock 不可重入，会死锁。
        _ = self.client  # 触发重建 + ensure_collection

    def _search_with_not_loaded_retry(
        self,
        *,
        op: str,
        query: VectorQuery,
        filter_expr: str,
        output_fields: list[str],
    ) -> Any:
        """执行一次 milvus search；撞 code=101 / 连接丢失则重建后重试一次。"""
        client = self.client
        try:
            with wrap_backend(op):
                return client.search(
                    self._collection,
                    data=[query.vector],
                    limit=query.top_k,
                    filter=filter_expr,
                    output_fields=output_fields,
                    search_params={"metric_type": self._metric_type},
                    consistency_level=self._consistency,
                )
        except Exception as exc:
            if not _is_not_loaded(exc):
                raise
            self._recover_connection(op, client)
            client = self.client
            with wrap_backend(op):
                return client.search(
                    self._collection,
                    data=[query.vector],
                    limit=query.top_k,
                    filter=filter_expr,
                    output_fields=output_fields,
                    search_params={"metric_type": self._metric_type},
                    consistency_level=self._consistency,
                )

    def _query_with_not_loaded_retry(
        self,
        *,
        filter_: str,
        output_fields: list[str] | None = None,
        consistency: str | None = None,
    ) -> Any:
        """执行一次 milvus query；撞 code=101 / 连接丢失则重建后重试一次。"""
        client = self.client
        kwargs: dict[str, Any] = {
            "filter": filter_,
            "consistency_level": consistency or self._consistency,
        }
        if output_fields is not None:
            kwargs["output_fields"] = output_fields
        try:
            with wrap_backend("milvus query"):
                return client.query(self._collection, **kwargs)
        except Exception as exc:
            if not _is_not_loaded(exc):
                raise
            self._recover_connection("milvus query", client)
            client = self.client
            with wrap_backend("milvus query"):
                return client.query(self._collection, **kwargs)

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        expr = self._expr(scope, query.filters)
        results = self._search_with_not_loaded_retry(
            op="milvus search",
            query=query,
            filter_expr=expr,
            output_fields=["logical_id"],
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
                logger.info(
                    "MilvusVectorStore.recall: output_fields only supports 'metadata', ignoring %s",
                    unknown,
                )
        expr = self._expr(scope, query.filters)
        milvus_out = ["logical_id", "metadata"] if fetch_meta else ["logical_id"]
        results = self._search_with_not_loaded_retry(
            op="milvus recall",
            query=query,
            filter_expr=expr,
            output_fields=milvus_out,
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

    def _ensure_collection(self, client: Any) -> None:
        """Ensure collection exists and is loaded, on the given (possibly unpublished) client."""
        if client.has_collection(self._collection):
            client.load_collection(self._collection)
            self._wait_for_load_complete(client)
            return
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            "id", DataType.VARCHAR, is_primary=True, max_length=self._physical_id_len
        )
        schema.add_field("logical_id", DataType.VARCHAR, max_length=self._id_len)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        for fld in _SCOPE_FIELDS:
            schema.add_field(fld, DataType.VARCHAR, max_length=self._scope_len)
        schema.add_field("metadata", DataType.JSON)  # scope 之外的元数据整体存 JSON
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type=self._metric_type
        )
        client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
            consistency_level=self._consistency,
        )
        client.load_collection(self._collection)
        self._wait_for_load_complete(client)

    def _scope_expr(self, scope: Scope) -> str:
        return " && ".join(f'scope_{dim} == {_lit(val)}' for dim, val in scope_dims(scope))

    def _expr(self, scope: Scope, filters: FilterExpr | None) -> str:
        parts = [self._scope_expr(scope)] if scope_dims(scope) else []
        compiled = self._compile_filter(filters)
        if compiled:
            parts.append(compiled)
        return " && ".join(p for p in parts if p)

    def _existing_ids(
        self, scope: Scope, ids: list[str], *, consistency: str | None = None
    ) -> set[str]:
        physical_ids = [self._physical_id(scope, rec_id) for rec_id in ids]
        items = ", ".join(_lit(i) for i in physical_ids)
        filter_ = f"id in [{items}]"
        rows = self._query_with_not_loaded_retry(
            filter_=filter_, consistency=consistency
        )
        return {row["logical_id"] for row in rows}


# -- 注册到 VectorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@VectorProducer.register(
    "milvus", config_inspectors={"at_rest_locations": lambda config: {"uri": config.get("uri")}}
)
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
    store = MilvusVectorStore(
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
        load_timeout=Factory.cfg_get(config, "load_timeout", 30.0),
        config_source=ConfigSourceProducer.get_cached("default"),
        **options,
    )
    preload_backend("pymilvus")
    return store
