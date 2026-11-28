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
import math
import threading
import time
from collections.abc import Callable
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
from jiuwen_memory.common.type_def.memory_filter import normalize_is_credential_clause_value
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


def _is_collection_not_loaded(exc: Exception) -> bool:
    """判断 Milvus 异常是否为集合尚未载入（code=101）。"""
    if getattr(exc, "code", None) == 101:
        return True
    msg = str(exc).lower()
    return "not loaded" in msg or "code=101" in msg


def _is_connection_lost(exc: Exception) -> bool:
    """判断 Milvus 异常是否为连接关闭/别名丢失（ConnectionNotExistException）。"""
    return "should create connection first" in str(exc).lower()


def _is_not_loaded(exc: Exception) -> bool:
    """Check if a Milvus exception indicates collection not loaded or connection lost.

    两类可自愈故障：code=101 (collection not loaded) 或
    ConnectionNotExistException (connection closed/lost, e.g. shared alias removed)。
    """
    return _is_collection_not_loaded(exc) or _is_connection_lost(exc)


_INDEX_NOT_EXIST_CODE = 700
_INDEX_FINISHED_STATES = frozenset({"Finished", "IndexState.Finished", "3"})
_INDEX_FAILED_STATES = frozenset({"Failed", "IndexState.Failed", "4"})


def _is_missing_index_error(exc: Exception) -> bool:
    """判断 Milvus 异常是否为集合缺少索引（如错误码 700 IndexNotExist）。"""
    code = getattr(exc, "code", None)
    if code == _INDEX_NOT_EXIST_CODE:
        return True
    msg = str(exc).lower()
    return (
        "index not found" in msg
        or "index doesn't exist" in msg
        or "index does not exist" in msg
        or "no index" in msg
        or "no vector index" in msg
        or f"code={_INDEX_NOT_EXIST_CODE}" in msg
    )


def _parse_describe_index(info: Any) -> tuple[str, str] | None:
    if not info:
        # 索引查不到时 pymilvus 返回 None（或空字典），属尚未可见，不能提前判完成。
        return "Pending", ""
    if not isinstance(info, dict):
        return (str(info).strip() or "Pending"), ""
    raw_state = info.get("state")
    state_text = "" if raw_state is None else str(raw_state).strip()
    if state_text:
        return state_text, str(info.get("fail_reason") or "")
    # pymilvus 2.4.0 的 describe_index 返回非空字典但无 state 键，交由调用方按需回退。
    return None


def _resolve_index_state_fallback(client: Any) -> Any:
    """获取 pymilvus 2.4.0 的底层 get_index_state 回退方法；不可用或异常时返回 None。

    为什么必须走私有 API：pymilvus 2.4.0 的 MilvusClient 没有公开的
    ``get_index_state`` 等价方法，且 ``describe_index`` 返回字典不含 ``state`` 键；
    需经由私有 ``_get_connection()`` 获取底层 handler 才能读取索引构建状态（并显式传
    ``timestamp=0`` 避开无超时的 ``alloc_timestamp``）。若连接别名尚未建立、已被移除或
    私有 API 变更，捕获异常降级为 ``None``，避免阻断 ``describe_index`` 轮询。
    """
    get_conn = getattr(client, "_get_connection", None)
    if not callable(get_conn):
        return None
    try:
        conn = get_conn()
    except Exception:
        return None
    get_index_state = getattr(conn, "get_index_state", None) if conn is not None else None
    return get_index_state if callable(get_index_state) else None


def require_positive_dim(dim: int, *, backend: str = "milvus vector store") -> None:
    """向量后端与融合工厂共用正维度规则，错误标明实际后端。"""
    if dim <= 0:
        raise ValidationError(f"{backend} requires positive 'dim'")


def _parse_positive_timeout(value: Any, *, name: str) -> float:
    """校验超时参数为有限正数，拒绝布尔、NaN、无穷与非正数。"""
    if isinstance(value, bool):
        raise ValidationError(f"milvus vector store requires positive '{name}'")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"milvus vector store requires positive '{name}'") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValidationError(f"milvus vector store requires positive '{name}'")
    return parsed


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
        self._load_timeout = _parse_positive_timeout(load_timeout, name="load_timeout")
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
            except BaseException:
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
        value = normalize_is_credential_clause_value(key, fc.op, fc.value)
        field = f"metadata[{_lit(key)}]"
        if fc.op == FilterOp.NE:
            # Milvus 2.6+ 对缺失 JSON 键采用三值逻辑（not (UNKNOWN) 仍为 UNKNOWN）；
            # 显式并入 not exists 使缺键记录与 memory_filter.py 语义一致。
            return f"((not exists {field}) || ({field} != {_lit(value)}))"
        if fc.op in _CMP_OPS:
            return f"{field} {_CMP_OPS[fc.op]} {_lit(value)}"
        if fc.op == FilterOp.IN:
            if not value:
                return 'id == ""'
            items = ", ".join(_lit(v) for v in value)
            return f"{field} in [{items}]"
        if fc.op == FilterOp.NOT_IN:
            if not value:
                return 'id != ""'
            items = ", ".join(_lit(v) for v in value)
            # 与 NE 同理：缺键记录按 memory_filter.py 语义应满足 NOT_IN。
            return f"((not exists {field}) || ({field} not in [{items}]))"
        if fc.op == FilterOp.CONTAINS:  # metadata 为 JSON，数组包含用 json_contains
            return f"json_contains({field}, {_lit(value)})"
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

    def _wait_for_load_complete(self, client: Any, *, deadline: float) -> None:
        """Poll load state until the collection is fully loaded or the shared deadline elapses."""
        # load_collection 可能在集合真正载入 query node 之前就返回，紧随其后的
        # search 会撞 code=101 (collection not loaded)（如首次 mem2_add 后的
        # dedup search）；故轮询 get_load_state 直到 Loaded。
        if not callable(getattr(client, "get_load_state", None)):
            return
        last_error: Exception | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state = client.get_load_state(
                    collection_name=self._collection,
                    timeout=max(1, math.ceil(remaining)),
                )
                load_state = state.get("state") if isinstance(state, dict) else state
                if str(load_state) == "Loaded":
                    return
                # 查询成功但尚未 Loaded：清掉历史异常，继续轮询。
                last_error = None
            except Exception as exc:
                # 仅容忍集合尚未载入（code=101）的短暂窗口；权限/参数/断连/超时等直接上抛。
                if not _is_collection_not_loaded(exc):
                    raise
                last_error = exc
            sleep_for = min(0.5, deadline - time.monotonic())
            if sleep_for <= 0:
                break
            time.sleep(sleep_for)
        raise BackendError(
            f"MilvusVectorStore: collection {self._collection} not loaded "
            f"within {self._load_timeout:.1f}s"
        ) from last_error

    def _query_index_state(
        self,
        client: Any,
        *,
        deadline: float,
        index_name: str,
        use_describe: bool,
        resolve_fallback: Callable[[], Any],
    ) -> tuple[str, str, bool]:
        """查询向量索引构建状态，兼容 describe_index 返空及 pymilvus 2.4.0 无 state 键。

        返回 ``(state_text, fail_reason, next_use_describe)``：当 ``describe_index``
        返回非空字典但无 ``state`` 键且回退可用时，关闭后续轮次的 ``describe_index``，
        避免 pymilvus 2.4.0 每轮多发一次无效 RPC。
        """
        if use_describe:
            info = client.describe_index(
                collection_name=self._collection,
                index_name=index_name,
                timeout=self._remaining_rpc_timeout(deadline, stage="index state"),
            )
            parsed = _parse_describe_index(info)
            if parsed is not None:
                return parsed[0], parsed[1], True
        get_index_state = resolve_fallback()
        if callable(get_index_state):
            # 显式 timestamp=0：避免 pymilvus 2.4.x 内部触发无超时的 alloc_timestamp()。
            state, fail_reason = get_index_state(
                collection_name=self._collection,
                index_name=index_name,
                timeout=self._remaining_rpc_timeout(deadline, stage="index state"),
                timestamp=0,
            )
            return str(state).strip(), str(fail_reason or ""), False
        return "Finished", "", use_describe

    def _wait_for_index_complete(
        self,
        client: Any,
        *,
        deadline: float,
        index_name: str = "vector",
    ) -> None:
        """Poll vector index build status until finished or the shared deadline elapses."""
        use_describe = callable(getattr(client, "describe_index", None))
        fallback_resolved = False
        cached_get_index_state: Any = None

        def _get_fallback() -> Any:
            nonlocal fallback_resolved, cached_get_index_state
            if not fallback_resolved:
                cached_get_index_state = _resolve_index_state_fallback(client)
                fallback_resolved = True
            return cached_get_index_state

        if not use_describe and not callable(_get_fallback()):
            return
        last_error: Exception | None = None
        while deadline - time.monotonic() > 0:
            try:
                state_text, fail_reason, use_describe = self._query_index_state(
                    client,
                    deadline=deadline,
                    index_name=index_name,
                    use_describe=use_describe,
                    resolve_fallback=_get_fallback,
                )
            except Exception as exc:
                # 仅容忍刚异步提交建索引后元数据暂不可见；权限/参数/超时等直接上抛。
                if not _is_missing_index_error(exc):
                    raise
                last_error = exc
            else:
                if state_text in _INDEX_FINISHED_STATES:
                    return
                if state_text in _INDEX_FAILED_STATES:
                    raise BackendError(
                        f"MilvusVectorStore: vector index creation failed for "
                        f"{self._collection}: {fail_reason or state_text}"
                    )
                last_error = None
            sleep_for = min(0.5, deadline - time.monotonic())
            if sleep_for <= 0:
                break
            time.sleep(sleep_for)
        raise BackendError(
            f"MilvusVectorStore: collection {self._collection} index not ready "
            f"within {self._load_timeout:.1f}s"
        ) from last_error

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

    def _remaining_rpc_timeout(self, deadline: float, *, stage: str) -> int:
        # 返回正整数秒：pymilvus 2.4.15 的 retry_on_rpc_failure 仅在 isinstance(timeout, int)
        # 时将 timeout 当作重试总时限；传 float 遇到可重试 UNAVAILABLE 会退避数分钟。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError(
                f"MilvusVectorStore: collection {self._collection} {stage} timed out "
                f"within {self._load_timeout:.1f}s"
            )
        return max(1, math.ceil(remaining))

    def _vector_indexes(self, client: Any, deadline: float) -> list[str]:
        if not callable(getattr(client, "list_indexes", None)):
            return []
        indexes = client.list_indexes(
            collection_name=self._collection,
            field_name="vector",
            timeout=self._remaining_rpc_timeout(deadline, stage="index check"),
        )
        return [str(name) for name in (indexes or []) if name]

    def _create_vector_index(self, client: Any, deadline: float) -> None:
        timeout = self._remaining_rpc_timeout(deadline, stage="index creation")
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type=self._metric_type,
            index_name="vector",
        )
        # 显式 sync=False：规避 pymilvus 2.4.15 同步等待建索引时无超时调用
        # alloc_timestamp() 的挂死风险；随后由本层在同一 deadline 内轮询就绪。
        client.create_index(
            collection_name=self._collection,
            index_params=index_params,
            sync=False,
            timeout=timeout,
        )
        self._wait_for_index_complete(client, deadline=deadline)

    def _load_collection_async(self, client: Any, deadline: float) -> None:
        # 显式 _async=True：关闭 pymilvus 内部 wait_for_loading_collection，
        # 避免 LoadCollection 与 SDK 内部等待重复消费同一个 load_timeout；
        # 就绪等待统一交由 _wait_for_load_complete 按共享 deadline 控制。
        client.load_collection(
            self._collection,
            timeout=self._remaining_rpc_timeout(deadline, stage="load"),
            _async=True,
        )

    def _ensure_collection(self, client: Any) -> None:
        """Ensure collection exists, is indexed, and is loaded within load_timeout."""
        deadline = time.monotonic() + self._load_timeout
        exists = client.has_collection(
            self._collection,
            timeout=self._remaining_rpc_timeout(deadline, stage="existence check"),
        )
        if not exists:
            creation_timeout = self._remaining_rpc_timeout(deadline, stage="creation")
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
            # 拆开建集合（不传 index_params）、异步建索引与异步装载三步共用同一 deadline，
            # 避免 pymilvus 在 create_collection / load_collection 内重复消费同一 timeout。
            client.create_collection(
                collection_name=self._collection,
                schema=schema,
                consistency_level=self._consistency,
                timeout=creation_timeout,
            )
            self._create_vector_index(client, deadline)
            self._load_collection_async(client, deadline)
        else:
            # 已有集合先沿用直接装载路径，不额外要求 IndexDetail 权限；仅在收到明确
            # 缺索引错误时，再按 field_name="vector" 检查：无向量索引则补建，已有未完成
            # 向量索引（如首轮建索引超时后重试）则等待其构建就绪，再重试装载一次。
            try:
                self._load_collection_async(client, deadline)
            except Exception as exc:
                if not _is_missing_index_error(exc):
                    raise
                vector_indexes = self._vector_indexes(client, deadline)
                if vector_indexes:
                    self._wait_for_index_complete(
                        client,
                        deadline=deadline,
                        index_name=vector_indexes[0],
                    )
                elif (
                    callable(getattr(client, "prepare_index_params", None))
                    and callable(getattr(client, "create_index", None))
                ):
                    self._create_vector_index(client, deadline)
                else:
                    raise exc
                self._load_collection_async(client, deadline)
        self._wait_for_load_complete(client, deadline=deadline)

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
