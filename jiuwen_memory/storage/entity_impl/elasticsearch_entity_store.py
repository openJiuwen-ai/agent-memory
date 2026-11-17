# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ElasticsearchEntityStore — entity 反向索引的 Elasticsearch 实现。

实现 ``EntityStore`` 端口：维护"实体 → 关联 unit_id"反向索引，提供 hash 精确
查询、bulk 变更（INSERT/LINK/UNLINK_UPDATE/DELETE）、反查关联三类能力。client
实例自持、惰性建（``client`` property 首次访问时构造并触发 ``ensure_index``），
与 ``ElasticsearchFulltextStore`` 同构。

**2026-08-12 改造**：归并退化为 hash 精确 only，砍掉向量 kNN 检索与 embedding
字段。``search``（向量 kNN）方法删除，构造参数 ``vector_dimension/num_candidates/
ef_construction/m`` 删除。索引退化为纯 ``{entity_text_hash → linked_memory_ids}``
倒排表，不再依赖 Embedder。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common._support import read_ssl_config, require_tls_scheme, wrap_backend
from jiuwen_memory.common.errors import BackendError, UnsupportedCapabilityError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger, redact_for_log
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntitySearchResult,
    EntityStoreFilters,
)
from jiuwen_memory.common.type_def.scope import space_id_from_scope

from .._support import preload_backend
from ..base import StoreType
from ..capability import StoreCapability
from ..entity_store import EntityStore, EntityStoreProducer

logger = get_logger(__name__)


class ElasticsearchEntityStore(EntityStore):
    """entity 反向索引的 Elasticsearch 实现（与 ``ElasticsearchFulltextStore`` 同构）。

    client 实例自持（``self._client``），首次访问 ``client`` property 时惰性建并
    触发 ``ensure_index``。工厂在装配期预加载 ``elasticsearch``；未安装/未就绪
    不影响 ``import storage``。
    """

    def __init__(
        self,
        *,
        hosts: list[str] | str,
        index: str = "memory_entities",
        username: str | None = None,
        password: str | None = None,
        list_limit: int = 10000,
        number_of_shards: int = 32,
        number_of_replicas: int = 1,
        **options: Any,
    ) -> None:
        self._hosts = hosts or "http://localhost:9200"
        self._index = index
        self._auth = dict(username=username, password=password)
        # 索引参数（建 index 用）
        self._list_limit = list_limit
        self._number_of_shards = number_of_shards
        self._number_of_replicas = number_of_replicas
        self._options = options  # SSL 等额外构造参数
        # （ca_certs/verify_certs 由 _build 读 SSL 后传入）
        self._client: Any = None
        self._index_ready = False

    # ------------------------------------------------------------------
    # client 惰性建（对齐 ElasticsearchFulltextStore.client）
    # ------------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from elasticsearch import Elasticsearch
            except ImportError as exc:
                raise BackendError(
                    "elasticsearch client not installed (pip install elasticsearch) "
                    "——entity 索引需要 elasticsearch-py 8.x"
                ) from exc
            opts: dict[str, Any] = dict(self._options)
            if self._auth["username"]:
                opts["basic_auth"] = (self._auth["username"], self._auth["password"])
            with wrap_backend("elasticsearch (entity) connect"):
                self._client = Elasticsearch(self._hosts, **opts)
            logger.info(
                "EntityStore: Elasticsearch client initialized hosts=%s index=%s",
                redact_for_log(self._hosts),
                self._index,
            )
        return self._client

    @staticmethod
    def _parse_bulk_response(response: dict) -> EntityBatchResult:
        """Parse a bulk response into per-item successful/failed ids.

        Unlike the main CSSAdapter (which raises on any failure), this never raises
        — entity linking needs per-item granularity so one bad document does not
        fail the whole group.
        """
        successful_ids: list[str] = []
        failed_ids: list[str] = []
        for item in response.get("items", []):
            action_type = next(iter(item))
            result = item[action_type]
            doc_id = result.get("_id")
            if not doc_id:
                continue
            if result.get("status", 0) in (200, 201):
                successful_ids.append(doc_id)
            else:
                failed_ids.append(doc_id)
        return EntityBatchResult(successful_ids=successful_ids, failed_ids=failed_ids)

    @staticmethod
    def _parse_bulk_response_with_tolerance(
        response: dict, action_meta: list[tuple[str, bool]]
    ) -> EntityBatchResult:
        """404 对幂等操作（DELETE/UNLINK/DELETE_IF_EMPTY）视为成功。"""
        successful: list[str] = []
        failed: list[str] = []
        for index, item in enumerate(response.get("items", [])):
            action_type = next(iter(item))
            result = item[action_type]
            record_id, tolerant = (
                action_meta[index] if index < len(action_meta) else ("", False)
            )
            status = result.get("status", 0)
            if status in (200, 201) or (tolerant and status == 404):
                successful.append(record_id or str(result.get("_id", "")))
            else:
                failed.append(record_id or str(result.get("_id", "")))
        return EntityBatchResult(successful_ids=successful, failed_ids=failed)

    @staticmethod
    def _link_script_body(
        memory_ids: list[str],
        memory_shards: dict[str, str] | None = None,
        upsert_document: dict | None = None,
    ) -> dict:
        """Build the painless script body that atomically appends memory ids.

        Shared by LINK bulk operations. painless is ES native — ES/OS 语法零差异,
        脚本可直接复用。``ctx._source.linked_memory_ids`` 幂等去重追加。
        """
        return {
            "script": {
                "lang": "painless",
                "source": (
                    "if (ctx._source.linked_memory_ids == null) { "
                    "ctx._source.linked_memory_ids = new ArrayList(); "
                    "} "
                    "if (ctx._source.linked_by_shard == null) { "
                    "ctx._source.linked_by_shard = new HashMap(); "
                    "} "
                    "if (ctx._source.linked_shards == null) { "
                    "ctx._source.linked_shards = new ArrayList(); "
                    "} "
                    "for (def memory_id : params.memory_ids) { "
                    "if (!ctx._source.linked_memory_ids.contains(memory_id)) { "
                    "ctx._source.linked_memory_ids.add(memory_id); "
                    "} "
                    "def shard = params.shards.get(memory_id); "
                    "if (shard != null) { "
                    "if (!ctx._source.linked_shards.contains(shard)) { "
                    "ctx._source.linked_shards.add(shard); "
                    "} "
                    "if (ctx._source.linked_by_shard.get(shard) == null) { "
                    "ctx._source.linked_by_shard.put(shard, new ArrayList()); "
                    "} "
                    "def bucket = ctx._source.linked_by_shard.get(shard); "
                    "if (!bucket.contains(memory_id)) { bucket.add(memory_id); } "
                    "} "
                    "}"
                ),
                "params": {"memory_ids": memory_ids, "shards": dict(memory_shards or {})},
                **({"upsert": upsert_document} if upsert_document is not None else {}),
            }
        }

    @staticmethod
    def _unlink_script_body(
        memory_ids: list[str], memory_shards: dict[str, str] | None = None
    ) -> dict:
        """增量移除关联（单文档原子，不删除记录）。

        ``memory_shards`` 给出某个 id 的分组时只从该分组移除（GROUPED_LINKS 语义）；
        该 id 不再出现在任何分组里才从 ``linked_memory_ids`` 移除。未给分组的 id 从
        所有分组及 ``linked_memory_ids`` 移除。
        """
        return {
            "script": {
                "lang": "painless",
                "source": (
                    "if (ctx._source.linked_by_shard == null) { "
                    "ctx._source.linked_by_shard = new HashMap(); "
                    "} "
                    "for (def memory_id : params.memory_ids) { "
                    "def scoped = params.shards.get(memory_id); "
                    "for (def shard : ctx._source.linked_by_shard.keySet().toArray()) { "
                    "if (scoped != null && shard != scoped) { continue; } "
                    "def bucket = ctx._source.linked_by_shard.get(shard); "
                    "bucket.removeIf(x -> x.equals(memory_id)); "
                    "if (bucket.isEmpty()) { ctx._source.linked_by_shard.remove(shard); } "
                    "} "
                    "boolean still = false; "
                    "for (def bucket : ctx._source.linked_by_shard.values()) { "
                    "if (bucket.contains(memory_id)) { still = true; } "
                    "} "
                    "if (!still && ctx._source.linked_memory_ids != null) { "
                    "ctx._source.linked_memory_ids.removeIf(x -> x.equals(memory_id)); "
                    "} "
                    "} "
                    "ctx._source.linked_shards = "
                    "new ArrayList(ctx._source.linked_by_shard.keySet());"
                ),
                "params": {
                    "memory_ids": memory_ids,
                    "shards": dict(memory_shards or {}),
                },
            }
        }

    @staticmethod
    def _delete_if_empty_script_body() -> dict:
        """判空与删除在同一个 painless 脚本里原子完成。"""
        return {
            "script": {
                "lang": "painless",
                "source": (
                    "if (ctx._source.linked_memory_ids == null "
                    "|| ctx._source.linked_memory_ids.isEmpty()) { "
                    "ctx.op = 'delete'; "
                    "} else { ctx.op = 'noop'; }"
                ),
            }
        }

    # ------------------------------------------------------------------
    # 序列化与过滤构造
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filters(space_id: str, filters: EntityStoreFilters) -> list[dict]:
        query_filters = [{"term": {"space_id": space_id}}]
        for field in filters.__dataclass_fields__:
            value = getattr(filters, field)
            if value is not None:
                query_filters.append({"term": {field: value}})
        return query_filters

    @staticmethod
    def _to_document(
        record: EntityRecord, memory_shards: dict[str, str] | None = None
    ) -> dict:
        shards = dict(memory_shards or {})
        linked_by_shard: dict[str, list[str]] = {}
        for memory_id in record.linked_memory_ids:
            shard = shards.get(memory_id)
            if shard is not None:
                linked_by_shard.setdefault(shard, []).append(memory_id)
        doc = {
            "space_id": record.space_id,
            "entity_text_hash": record.entity_text_hash,
            "entity_type": record.entity_type,
            "linked_memory_ids": list(record.linked_memory_ids),
            "actor_id": record.filters.actor_id,
            "assistant_id": record.filters.assistant_id,
            "session_id": record.filters.session_id,
        }
        if linked_by_shard:
            doc["linked_by_shard"] = linked_by_shard
            doc["linked_shards"] = sorted(linked_by_shard)
        return doc

    @classmethod
    def _hit_to_entity_record(
        cls, hit: dict, shards: frozenset[str] | None = None
    ) -> EntityRecord:
        source = hit["_source"]
        linked: list[str]
        if shards:
            by_shard = source.get("linked_by_shard") or {}
            linked = []
            for shard, ids in by_shard.items():
                if shard in shards:
                    linked.extend(ids)
        else:
            linked = source.get("linked_memory_ids", [])
        return EntityRecord(
            id=hit["_id"],
            space_id=source["space_id"],
            # entity_text 明文不持久化（_to_document 只存 hash，见 hash_entity_text
            # 的隐私设计）——回读恒空。消费方不依赖回读的 entity_text，只读
            # linked_memory_ids / id。
            entity_text="",
            entity_text_hash=source.get("entity_text_hash", ""),
            entity_type=source["entity_type"],
            linked_memory_ids=tuple(dict.fromkeys(linked)),
            filters=EntityStoreFilters(
                actor_id=source.get("actor_id"),
                assistant_id=source.get("assistant_id"),
                session_id=source.get("session_id"),
            ),
        )

    # ------------------------------------------------------------------
    # 索引初始化与校验（lazy）
    # ------------------------------------------------------------------

    def capabilities(self) -> frozenset[StoreCapability]:
        # 增量 UNLINK / DELETE_IF_EMPTY 走单文档 painless 脚本；关联按分片分组；
        # hash 查询可下推 linked_shards 过滤。
        return frozenset(
            {
                StoreCapability.ATOMIC_UNLINK,
                StoreCapability.GROUPED_LINKS,
                StoreCapability.SHARD_PREFILTER,
                StoreCapability.RESOURCE_DESCRIPTOR,
            }
        )

    def resource_descriptor(self) -> str:
        from .._support import sanitize_resource_ids

        values = self._hosts if isinstance(self._hosts, list) else [self._hosts]
        return f"{sanitize_resource_ids(values)}/{self._index}"

    def ensure_index(self) -> None:
        """端口契约：确保 index 已创建并就绪。

        lazy 触发——首次查询/写入时由 ``_require_index_ready`` 调本方法，本方法
        首行访问 ``self.client``（property）惰性建 client，随后建/校验 index。
        """
        if self._index_ready:
            return

        client = self.client  # 触发 client 惰性建（首次）

        if client.indices.exists(index=self._index):
            self._validate_existing_index(client)
        else:
            try:
                client.indices.create(
                    index=self._index,
                    settings=self._build_index_settings(),
                    mappings=self._build_index_mappings(),
                )
                logger.info("entity_index_created index=%s", self._index)
            except Exception as exc:
                if "resource_already_exists_exception" in str(exc):
                    logger.info("entity_index_already_exists index=%s", self._index)
                    self._validate_existing_index(client)
                else:
                    raise BackendError(
                        f"Failed to create entity index {self._index}: {exc}"
                    ) from exc

        self._index_ready = True

    # ------------------------------------------------------------------
    # hash 精确查询（term/terms，ES/OS 通用语法，零改动）
    # ------------------------------------------------------------------

    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        limit: int = 500,
    ) -> list[EntityRecord]:
        """按 entity_text_hash keyword term 查询，返回命中的实体记录。"""
        self._require_index_ready()
        space_id = space_id_from_scope(scope)
        hashes = tuple(sorted({h for h in entity_text_hashes if h}))
        if not hashes:
            return []
        if limit <= 0:
            raise ValueError("limit must be positive")

        query_filters = self._build_filters(space_id, filters)
        query_filters.append({"terms": {"entity_text_hash": list(hashes)}})
        if shards:
            query_filters.append({"terms": {"linked_shards": sorted(shards)}})
        try:
            response = self._client.search(
                index=self._index,
                query={"bool": {"filter": query_filters}},
                size=limit,
                _source=True,
                routing=space_id,
            )
        except Exception as exc:
            raise BackendError(
                f"Entity hash lookup failed on index {self._index}: {exc}"
            ) from exc

        records = [
            self._hit_to_entity_record(hit, shards) for hit in response["hits"]["hits"]
        ]
        return [record for record in records if record.linked_memory_ids]

    @property
    def supports_complete_linked_enumeration(self) -> bool:
        return True

    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        complete: bool = False,
    ) -> list[EntityRecord]:
        """反查：哪些实体关联了该 memory_id（unlink 用）。

        filter 复用 ``_build_filters``，与写入侧 ``find_by_entity_text_hash`` 对称：
        space_id term + actor_id term（actor_id 来自调用方 scope）。把 actor 隔离
        下沉到反查，避免 space 内跨 user 的孤立误删。

        ``complete=True``（搬迁）必须返回全部关联：分页取完并核对 total；超过
        ``index.max_result_window`` 无法用 from/size 完整枚举时**拒绝**，不静默截断。
        """
        self._require_index_ready()
        space_id = space_id_from_scope(scope)
        query_filters = self._build_filters(space_id, filters)
        query_filters.append({"term": {"linked_memory_ids": memory_id}})
        if shards:
            query_filters.append({"terms": {"linked_shards": sorted(shards)}})
        query = {"bool": {"filter": query_filters}}
        try:
            if not complete:
                response = self._client.search(
                    index=self._index,
                    query=query,
                    size=self._list_limit,
                    _source=True,
                    routing=space_id,
                )
                records = [
                    self._hit_to_entity_record(hit, shards)
                    for hit in response["hits"]["hits"]
                ]
                return [record for record in records if record.linked_memory_ids]

            # 搬迁要求完整集合：分页读取并核对 total；ES 默认 max_result_window=10000，
            # 超过时 from/size 无法翻页，明确拒绝而不是返回前 N 条。
            page_size = min(self._list_limit, 1000)
            records = []
            offset = 0
            total = 0
            while True:
                response = self._client.search(
                    index=self._index,
                    query=query,
                    size=page_size,
                    from_=offset,
                    track_total_hits=True,
                    _source=True,
                    routing=space_id,
                )
                hits = response["hits"]["hits"]
                total_value = response["hits"]["total"]
                total = int(
                    total_value["value"]
                    if isinstance(total_value, dict)
                    else total_value
                )
                if total > 10000:
                    raise BackendError(
                        f"Entity find_by_linked_memory_id cannot enumerate {total} "
                        f"linked entities in {self._index} (index.max_result_window); "
                        "refusing an incomplete relocation"
                    )
                records.extend(
                    self._hit_to_entity_record(hit, shards) for hit in hits
                )
                offset += len(hits)
                if not hits or offset >= total:
                    break
            if len(records) < total:
                raise BackendError(
                    f"Entity find_by_linked_memory_id returned {len(records)} of "
                    f"{total} linked entities in {self._index}; refusing incomplete data"
                )
            return [record for record in records if record.linked_memory_ids]
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(
                f"Entity find_by_linked_memory_id failed on index {self._index}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # bulk 变更（INSERT/LINK/UNLINK_UPDATE/DELETE 混合，per-item 粒度返回）
    # ------------------------------------------------------------------

    def execute_operations(
        self,
        scope: Scope,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        """Apply a batch of INSERT/LINK/UNLINK_UPDATE/DELETE mutations via one bulk call.

        A group shares one space_id/routing, so the whole batch targets a single
        shard with one refresh. Returns per-item success/failure ids — partial
        failure does not raise, so the linker can count failed_count per entity.
        """
        self._require_index_ready()
        space_id = space_id_from_scope(scope)
        routing = space_id

        bulk_ops: list[dict] = []
        action_meta: list[tuple[str, bool]] = []
        for op in operations:
            if op.type is EntityOpType.INSERT:
                if op.record is None:
                    continue
                bulk_ops.append({"create": {
                    "_index": self._index,
                    "_id": op.record.id,
                    "routing": routing,
                }})
                action_meta.append((op.record.id, False))
                bulk_ops.append(self._to_document(op.record, op.memory_shards))
            elif op.type is EntityOpType.LINK:
                if op.record_id is None:
                    continue
                unique_memory_ids = sorted(set(op.link_memory_ids))
                if not unique_memory_ids:
                    continue
                bulk_ops.append({"update": {
                    "_index": self._index,
                    "_id": op.record_id,
                    "routing": routing,
                }})
                action_meta.append((op.record_id, False))
                bulk_ops.append(
                    self._link_script_body(
                        unique_memory_ids,
                        op.memory_shards,
                        self._to_document(op.record, op.memory_shards)
                        if op.record is not None
                        else None,
                    )
                )
            elif op.type is EntityOpType.UNLINK:
                if op.record_id is None or not op.unlink_memory_ids:
                    continue
                bulk_ops.append({"update": {
                    "_index": self._index,
                    "_id": op.record_id,
                    "routing": routing,
                }})
                action_meta.append((op.record_id, True))
                bulk_ops.append(
                    self._unlink_script_body(list(op.unlink_memory_ids), op.memory_shards)
                )
            elif op.type is EntityOpType.DELETE_IF_EMPTY:
                if op.record_id is None:
                    continue
                bulk_ops.append({"update": {
                    "_index": self._index,
                    "_id": op.record_id,
                    "routing": routing,
                }})
                action_meta.append((op.record_id, True))
                bulk_ops.append(self._delete_if_empty_script_body())
            elif op.type is EntityOpType.UNLINK_UPDATE:
                if op.record is None:
                    continue
                bulk_ops.append({"update": {
                    "_index": self._index,
                    "_id": op.record.id,
                    "routing": routing,
                }})
                bulk_ops.append({"doc": self._to_document(op.record)})
            elif op.type is EntityOpType.DELETE:
                if op.record_id is None:
                    continue
                bulk_ops.append({"delete": {
                    "_index": self._index,
                    "_id": op.record_id,
                    "routing": routing,
                }})
                action_meta.append((op.record_id, True))

        if not bulk_ops:
            return EntityBatchResult(successful_ids=[], failed_ids=[])

        try:
            response = self._client.bulk(operations=bulk_ops, refresh="wait_for", routing=routing)
        except Exception as exc:
            raise BackendError(
                f"Entity bulk write failed on index {self._index}: {exc}"
            ) from exc

        return self._parse_bulk_response_with_tolerance(response, action_meta)

    def search(self, scope: Scope, query_vector: list[float], *, top_k: int,
               filters: EntityStoreFilters, shards: frozenset[str] | None = None,
               ) -> list[EntitySearchResult]:
        raise UnsupportedCapabilityError(
            capability="vector_search",
            value="elasticsearch",
            component="ElasticsearchEntityStore",
            message="ElasticsearchEntityStore is hash-only",
        )

    # ------------------------------------------------------------------
    # BaseStore 契约
    # ------------------------------------------------------------------

    def store_type(self) -> StoreType:
        return StoreType.ENTITY

    def health(self) -> None:
        try:
            ok = self.client.ping()
        except Exception as exc:
            raise BackendError(f"elasticsearch (entity) ping failed: {exc}") from exc
        if not ok:
            raise BackendError("elasticsearch (entity) ping returned falsy")

    def _validate_existing_index(self, client) -> None:
        try:
            mapping_resp = client.indices.get_mapping(index=self._index)
        except Exception as exc:
            raise BackendError(f"Failed to validate entity index '{self._index}': {exc}") from exc

        mappings = mapping_resp[self._index]["mappings"]
        if not mappings.get("_routing", {}).get("required", False):
            raise BackendError(
                f"Entity index '{self._index}' exists but _routing.required is not set."
            )

        # entity_text_hash must exist; an index built before the hash change still
        # stores plaintext entity_text and must be rebuilt.
        if "entity_text_hash" not in mappings.get("properties", {}):
            raise BackendError(
                f"Entity index '{self._index}' is missing the entity_text_hash field. "
                "It was likely created before the entity_text hashing change and must be recreated."
            )

        logger.info("existing_entity_index_validated index=%s", self._index)

    def _build_index_settings(self) -> dict:
        return {
            "number_of_shards": self._number_of_shards,
            "number_of_replicas": self._number_of_replicas,
        }

    def _build_index_mappings(self) -> dict:
        return {
            "_routing": {"required": True},
            "properties": {
                "space_id": {"type": "keyword"},
                "entity_text_hash": {"type": "keyword"},
                "entity_type": {"type": "keyword"},
                "linked_memory_ids": {"type": "keyword"},
                "linked_shards": {"type": "keyword"},
                "linked_by_shard": {"type": "object"},
                "actor_id": {"type": "keyword"},
                "assistant_id": {"type": "keyword"},
                "session_id": {"type": "keyword"},
            },
        }

    def _require_index_ready(self) -> None:
        if not self._index_ready:
            self.ensure_index()  # lazy 触发（首次查询/写入时）


# -- 注册到 EntityStoreProducer（实现自注册，与 FulltextStore._build 同构） ------- #

def _at_rest_locations(config):
    if config.get("hosts"):
        return {"hosts": config.get("hosts")}
    if config.get("endpoint"):
        return {"endpoint": config.get("endpoint")}
    return {}  # 与 builder 一致：仅此实现的未配置状态明确关闭端口。


@EntityStoreProducer.register(
    "elasticsearch", config_inspectors={"at_rest_locations": _at_rest_locations}
)
def _build(config):
    # hosts 可选（entity 是增强层，未配即降级关闭 entity 链路，不报错——与
    # FulltextStore 的 require_param 不同，后者是主链路必需品，缺了必报错）。
    hosts = Factory.cfg_get(config, "hosts") or Factory.cfg_get(config, "endpoint")
    if not hosts:
        logger.warning("EntityStore: hosts not configured, entity chain disabled")
        return None  # → HybridIndexBuilder 跳过 entity 子 builder

    # SSL 与 FulltextStore 同风格：read_ssl_config + require_tls_scheme
    ssl = read_ssl_config(config, backend="elasticsearch entity")
    hosts_list = hosts if isinstance(hosts, (list, tuple)) else [hosts]
    options: dict[str, Any] = {"request_timeout": Factory.cfg_get(config, "timeout", 10) or 10}
    if ssl.verify:
        require_tls_scheme(
            hosts_list,
            expected="https",
            component="elasticsearch entity",
            param="params.hosts",
        )
        options["ca_certs"] = ssl.ca_cert
        options["verify_certs"] = True
    else:
        options["verify_certs"] = False

    store = ElasticsearchEntityStore(
        hosts=hosts_list,
        index=Factory.cfg_get(config, "index", "memory_entities"),
        username=Factory.cfg_get(config, "username"),
        password=Factory.cfg_get(config, "password"),
        list_limit=Factory.cfg_get(config, "list_limit", 10000) or 10000,
        number_of_shards=Factory.cfg_get(config, "number_of_shards", 32) or 32,
        number_of_replicas=Factory.cfg_get(config, "number_of_replicas", 1) or 1,
        **options,
    )
    preload_backend("elasticsearch")
    return store
