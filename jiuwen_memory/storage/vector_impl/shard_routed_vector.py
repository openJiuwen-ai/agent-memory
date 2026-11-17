# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``shard_routed`` 向量存储（F09 D8）。

两种模式：

- ``split``（默认）：每个分片一个物理索引；写入经
  :class:`~storage.shard_routing.ShardLocator` 跟随 KV 归属；检索只访问选中分片，
  各分片取 top-k 后按分数合并截断。
- ``shared``：单索引，写入时给记录打保留字段 ``storage_shard``；检索把分片条件与
  调用方 filters 取 AND 后**在截断前**下推（要求内层声明
  ``StoreCapability.SHARD_PREFILTER``）。

**返回内容必须来自当前真源**（F09 D8 验收条件）：本包装层只路由与合并索引，不改变
检索链路回读真源的行为，也不把索引记录携带的元数据当作返回内容。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    RelocationInProgressError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import BaseStore, StoreType
from jiuwen_memory.storage.capability import StoreCapability, require_capability
from jiuwen_memory.storage.security import CompositeStoreSecurity
from jiuwen_memory.storage.shard_routing import ShardLocator, ShardResolver
from jiuwen_memory.storage.shard_search import (
    STORAGE_SHARD_FIELD,
    STORAGE_SHARD_NEXT_FIELD,
    LazyShardLocator,
    merge_scored,
    require_distinct_shard_resources,
    selected_shards,
    unit_id_from_metadata,
    with_shard_filter,
)
from jiuwen_memory.storage.types import ScoredHit, ScoredID, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)


class _ShardWriteError(Exception):
    """单条记录因定位失败或内层拒绝而失败（批量里按条收集）。"""

    def __init__(self, record_id: str, cause: Exception) -> None:
        self.record_id = record_id
        self.cause = cause
        super().__init__(f"record {record_id!r} failed: {cause}")


class ShardRoutedVectorStore(VectorStore):
    """向量端口的 ``shard_routed`` 包装层。"""

    def __init__(
        self,
        *,
        resolver: ShardResolver,
        locator: ShardLocator,
        mode: str = "split",
        stores: dict[str, VectorStore] | None = None,
        inner: VectorStore | None = None,
        owned: tuple[BaseStore, ...] = (),
    ) -> None:
        if mode not in ("split", "shared"):
            raise ValidationError(f"vector shard mode must be split|shared, got {mode!r}")
        if mode == "shared":
            if inner is None or stores:
                raise ValidationError("shared vector shard mode requires 'inner' and no 'tables'")
            require_capability(
                inner, StoreCapability.SHARD_PREFILTER, role="vector shared inner"
            )
        else:
            if inner is not None or not stores:
                raise ValidationError("split vector shard mode requires per-shard stores")
            missing = [name for name in resolver.shards if name not in stores]
            if missing:
                raise ValidationError(f"vector split mode is missing shards: {missing}")
            unknown = [name for name in stores if name not in resolver.shards]
            if unknown:
                raise ValidationError(f"vector split mode has unconfigured shards: {unknown}")
            require_distinct_shard_resources(stores, kind="vector")
            directions = {store.score_higher_is_better() for store in stores.values()}
            if len(directions) != 1:
                raise ValidationError(
                    "vector split mode requires a consistent score direction across shards"
                )
        self._resolver = resolver
        self._locator = locator
        self._mode = mode
        self._stores = dict(stores or {})
        self._inner = inner
        self._owned = tuple(owned)

    # -- 自描述 --------------------------------------------------------------- #

    @property
    def security(self):
        return CompositeStoreSecurity(
            [self._inner] if self._inner is not None else list(self._stores.values())
        )

    def store_type(self) -> StoreType:
        return StoreType.VECTOR

    def capabilities(self) -> frozenset[StoreCapability]:
        caps = {StoreCapability.SHARD_ROUTED}
        if all(
            StoreCapability.UNIT_RECORD_LOOKUP in store.capabilities()
            for store in self._physical_stores()
        ):
            caps.add(StoreCapability.SHARD_RELOCATABLE)
        if self._mode == "shared" and self._inner is not None:
            # 只有共享索引的「截断前过滤」才构成前置过滤；split 靠选表，不声称该能力。
            if StoreCapability.SHARD_PREFILTER in self._inner.capabilities():
                caps.add(StoreCapability.SHARD_PREFILTER)
        return frozenset(caps)

    @property
    def shards(self) -> tuple[str, ...]:
        return tuple(self._stores) if self._mode == "split" else self._resolver.shards

    def shard_descriptors(self, kind: str = "vector") -> dict[str, dict[str, str]]:
        """每个分片的物理标识（shared 模式下每个分片共享同一实例身份）。

        ``kind`` 是登记键（``{capability}:{port_name}``），由装配器传入，保证向量
        L2/L0/L1 三层不会互相覆盖。
        """
        if self._inner is not None:
            descriptor = self._inner.resource_descriptor()
            if descriptor is None:
                return {}
            return {shard: {kind: descriptor} for shard in self.shards}
        out: dict[str, dict[str, str]] = {}
        for shard, store in self._stores.items():
            descriptor = store.resource_descriptor()
            if descriptor is not None:
                out[shard] = {kind: descriptor}
        return out

    def locator_name(self) -> str | None:
        """注入的定位端口名（装配期校验能力用）。"""
        provider = getattr(self._locator, "port_name", None)
        return provider() if callable(provider) else None

    def bind_shard_locator(self, locator: Any) -> None:
        """装配期绑定定位端口（S09 #3 / #4）；由 store manager 在端口建好后调用。"""
        bind = getattr(self._locator, "bind", None)
        if not callable(bind):
            raise ValidationError(
                f"{type(self).__name__} locator does not support assembly-time binding"
            )
        bind(locator)

    def health(self) -> None:
        if self._inner is not None:
            self._inner.health()
            return
        for store in self._stores.values():
            store.health()

    def connection_dependencies(self) -> tuple[BaseStore, ...]:
        """物理分片表 / 共享 inner 的连接预算来源（内联实例不在端口表里）。"""
        targets = [self._inner] if self._inner is not None else list(self._stores.values())
        return tuple(store for store in targets if isinstance(store, BaseStore))

    def close(self) -> None:
        for store in self._owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        self._owned = ()

    def score_higher_is_better(self) -> bool:
        if self._inner is not None:
            return self._inner.score_higher_is_better()
        return next(iter(self._stores.values())).score_higher_is_better()

    # -- 搬迁（F09 D16） ------------------------------------------------------ #

    def _physical_stores(self) -> list[VectorStore]:
        if self._inner is not None:
            return [self._inner]
        return list(self._stores.values())

    def _lookup_ids(self, store: VectorStore, scope: Scope, unit_id: str) -> list[str]:
        require_capability(
            store, StoreCapability.UNIT_RECORD_LOOKUP, role="vector relocation inner"
        )
        return list(store.record_ids_for_units(scope, [unit_id]).get(unit_id, []))

    @staticmethod
    def _read_exact(
        store: VectorStore, scope: Scope, ids: list[str]
    ) -> list[VectorRecord]:
        """读取搬迁需要的全部记录；少任何一条都拒绝，不能把部分读取当成完整复制。"""
        records = store.get_for_relocation(scope, ids)
        by_id = {record.id: record for record in records}
        missing = [record_id for record_id in ids if record_id not in by_id]
        if missing:
            raise BackendError(
                f"relocation read incomplete for unit records {missing[:5]}: "
                f"expected {len(ids)}, got {len(records)} (index may not be "
                "refreshed yet); refusing to relocate"
            )
        return [by_id[record_id] for record_id in ids]

    @staticmethod
    def _copy_records(store: VectorStore, scope: Scope, records: list[VectorRecord]) -> None:
        """逐条幂等复制：目标已有相同记录视为已完成，内容不同则报冲突。"""
        for record in records:
            try:
                store.insert_for_relocation(scope, [record])
            except ConflictError:
                existing = store.get_for_relocation(scope, [record.id])
                if existing and existing[0] == record:
                    continue
                raise

    def relocate_unit(
        self, scope: Scope, unit_id: str, source: str, target: str
    ) -> None:
        """把该记忆在源分片的全部记录原样复制到目标分片（不删源，幂等）。

        共享模式不改当前分片标签，只写过渡期标签 ``STORAGE_SHARD_NEXT_FIELD``：切换归属
        之前按源分片查询仍能命中，按目标分片查询也能在截断前命中（再由归属校验裁决），
        崩溃后标签也不会让记录两边都查不到。
        """
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            records = self._read_exact(
                self._inner, scope, self._lookup_ids(self._inner, scope, unit_id)
            )
            moved = []
            for record in records:
                if record.metadata.get(STORAGE_SHARD_NEXT_FIELD) == target:
                    continue
                metadata = dict(record.metadata)
                metadata[STORAGE_SHARD_NEXT_FIELD] = target
                moved.append(VectorRecord(id=record.id, vector=record.vector, metadata=metadata))
            if moved:
                self._inner.update(scope, moved)
            return
        source_store = self._stores[source]
        ids = self._lookup_ids(source_store, scope, unit_id)
        if ids:
            self._copy_records(
                self._stores[target],
                scope,
                self._read_exact(source_store, scope, ids),
            )

    def cleanup_unit(self, scope: Scope, unit_id: str, shard: str) -> None:
        """只清理指定分片中该记忆的记录（幂等）。

        共享模式：当前标签是 ``shard`` 且带过渡期标签 → 把过渡期标签转正（完成搬迁）；
        过渡期标签是 ``shard`` → 去掉它（回滚）；其余不动。不依赖定位结果。
        """
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            records = self._read_exact(
                self._inner, scope, self._lookup_ids(self._inner, scope, unit_id)
            )
            changed = []
            for record in records:
                metadata = dict(record.metadata)
                pending = metadata.get(STORAGE_SHARD_NEXT_FIELD)
                if pending is None:
                    continue
                if metadata.get(STORAGE_SHARD_FIELD) == shard:
                    metadata[STORAGE_SHARD_FIELD] = pending
                elif pending != shard:
                    continue
                metadata.pop(STORAGE_SHARD_NEXT_FIELD, None)
                changed.append(VectorRecord(id=record.id, vector=record.vector, metadata=metadata))
            if changed:
                self._inner.update(scope, changed)
            return
        store = self._stores[shard]
        ids = self._lookup_ids(store, scope, unit_id)
        if ids:
            store.delete(scope, ids)

    def record_ids_for_units(
        self, scope: Scope, unit_ids: list[str]
    ) -> dict[str, list[str]]:
        """聚合各分片后端的 ``metadata.unit_id`` 反查（供装配期与工具使用）。"""
        out: dict[str, list[str]] = {}
        for store in self._physical_stores():
            require_capability(
                store, StoreCapability.UNIT_RECORD_LOOKUP, role="vector relocation inner"
            )
            for unit_id, ids in store.record_ids_for_units(scope, unit_ids).items():
                out.setdefault(unit_id, []).extend(ids)
        return out

    # -- 写入 ---------------------------------------------------------------- #

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._write(scope, records, update=False)

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._write(scope, records, update=True)

    def _write(self, scope: Scope, records: list[VectorRecord], *, update: bool) -> None:
        if not records:
            return
        unit_ids: list[str] = []
        for record in records:
            unit_ids.append(unit_id_from_metadata(record.id, record.metadata))
        location = self._locator.locate(scope, list(dict.fromkeys(unit_ids)))
        grouped: dict[str, list[VectorRecord]] = {}
        failed: list[_ShardWriteError] = []
        for record, unit_id in zip(records, unit_ids, strict=True):
            if unit_id in location.moving:
                failed.append(
                    _ShardWriteError(record.id, RelocationInProgressError(unit_id))
                )
                continue
            shard = location.found.get(unit_id)
            if shard is None:
                failed.append(
                    _ShardWriteError(
                        record.id,
                        ValidationError(
                            f"no active shard placement for unit {unit_id!r}: "
                            "the memory was never written to the sharded KV or has been "
                            "deleted (check that the deployment's index_builder persists "
                            "the forward index)"
                        ),
                    )
                )
                continue
            target = record
            if self._mode == "shared":
                metadata = dict(record.metadata)
                metadata[STORAGE_SHARD_FIELD] = shard
                target = VectorRecord(id=record.id, vector=record.vector, metadata=metadata)
            grouped.setdefault(shard, []).append(target)
        for shard, group in grouped.items():
            store = self._inner if self._mode == "shared" else self._stores[shard]
            try:
                if update:
                    store.update(scope, group)
                else:
                    store.insert(scope, group)
            except Exception as exc:  # noqa: BLE001 - 按条/按组收集后统一报告
                failed.extend(_ShardWriteError(record.id, exc) for record in group)
        if failed:
            first = failed[0].cause
            first.failed_ids = [item.record_id for item in failed]  # type: ignore[attr-defined]
            raise first

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            self._inner.delete(scope, ids)
            return
        for store in self._stores.values():
            store.delete(scope, ids)

    # -- 读取 ---------------------------------------------------------------- #

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            return self._inner.get(scope, ids)
        found: dict[str, VectorRecord] = {}
        for store in self._stores.values():
            for record in store.get(scope, ids):
                found.setdefault(record.id, record)
        return [found[record_id] for record_id in ids if record_id in found]

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        shards = selected_shards(query.extensions, self.shards)
        narrowed = _narrowed_query(query, shards, mode=self._mode)
        results: list[ScoredID] = []
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            results = self._inner.search(scope, narrowed)
        else:
            for shard in shards:
                results.extend(self._stores[shard].search(scope, narrowed))
        return merge_scored(
            results,
            query.top_k,
            higher_is_better=self.score_higher_is_better(),
            key=lambda item: item.id,
            score=lambda item: item.score,
        )

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        shards = selected_shards(query.extensions, self.shards)
        narrowed = _narrowed_query(query, shards, mode=self._mode)
        targets = (
            [self._inner] if self._mode == "shared" else [self._stores[s] for s in shards]
        )
        hits: list[ScoredHit] = []
        for store in targets:
            if store is None:
                raise RuntimeError("shared mode requires a physical store")
            # 保持内层的 NotImplementedError 原样上抛：VectorRecaller 只捕获该异常
            # 才会回退到 search + get。包装层把它转成 UnsupportedCapabilityError
            # 会打断回退并把整条向量通道变成错误（F09 P1）。
            hits.extend(store.recall(scope, narrowed, output_fields))
        return merge_scored(
            hits,
            query.top_k,
            higher_is_better=self.score_higher_is_better(),
            key=lambda item: item.id,
            score=lambda item: item.score,
        )


def _narrowed_query(query: VectorQuery, shards: tuple[str, ...], *, mode: str) -> VectorQuery:
    """共享索引把分片条件并入 filters；拆分索引沿用原过滤条件。"""
    if mode == "shared":
        return VectorQuery(
            vector=query.vector,
            top_k=query.top_k,
            filters=with_shard_filter(query.filters, shards),
            extensions=dict(query.extensions),
            return_metadata=query.return_metadata,
        )
    return query


_EMBEDDING_KEYS = (
    "model",
    "embedding_model",
    "dim",
    "dimension",
    "metric",
    "distance",
    "normalize",
    "normalized",
)


def _embedding_signature(spec: dict, shard: str) -> tuple[tuple[str, str], ...]:
    params = _expand(spec.get("params") or {}, shard)
    return tuple(sorted((key, str(params[key])) for key in _EMBEDDING_KEYS if key in params))


def _validate_embedding_consistency(
    template: Any, overrides: dict, shards: tuple[str, ...]
) -> None:
    """各分片的 embedding 配置必须一致（模型 / 维度 / 度量 / 归一化）。

    后端没有统一的「embedding 描述」接口，能从配置读到的键必须一致；缺失时无法
    校验，写入部署文档作为约束（不在运行期静默接受不一致配置）。
    """
    signatures: list[tuple[tuple[str, str], ...]] = []
    for shard in shards:
        spec = overrides.get(shard) or template
        if isinstance(spec, dict):
            signatures.append(_embedding_signature(spec, shard))
    non_empty = [signature for signature in signatures if signature]
    if len(non_empty) > 1 and len(set(non_empty)) > 1:
        raise ValidationError(
            "vector shards declare inconsistent embedding configuration "
            f"(model/dim/metric/normalize): {non_empty}"
        )


def _build_shard_stores(
    config: Any, resolver: ShardResolver
) -> tuple[dict[str, VectorStore], tuple[BaseStore, ...]]:
    template = config.get("table_template")
    overrides = config.get("tables") or {}
    if not isinstance(overrides, dict):
        raise ValidationError("params.tables must be a mapping of shard → store")
    stores: dict[str, VectorStore] = {}
    owned: list[BaseStore] = []
    try:
        for shard in resolver.shards:
            override = overrides.get(shard)
            if override is None:
                if not isinstance(template, dict):
                    raise ValidationError(
                        f"shard {shard!r} has neither table_template nor a tables override"
                    )
                store = _build_inline(template, shard, config.ctx)
                owned.append(store)
            elif isinstance(override, str):
                store = VectorProducer.build_named(override, config.ctx)
            elif isinstance(override, dict):
                store = _build_inline(override, shard, config.ctx)
                owned.append(store)
            else:
                raise ValidationError(
                    f"params.tables[{shard!r}] must be a name or inline config"
                )
            stores[shard] = store
    except Exception:
        for store in owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        raise
    return stores, tuple(owned)


def _build_inline(spec: dict, shard: str, ctx: Any) -> VectorStore:
    target = spec.get("target")
    if not target:
        raise ValidationError("shard table template requires a 'target'")
    params = _expand(spec.get("params") or {}, shard)
    return VectorProducer.build(str(target), params, ctx, name=f"shard_{shard}")


def _expand(value: Any, shard: str) -> Any:
    if isinstance(value, str):
        return value.replace("{shard}", shard)
    if isinstance(value, dict):
        return {key: _expand(item, shard) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, shard) for item in value]
    return value


@VectorProducer.register("shard_routed")
def _build(config):
    from jiuwen_memory.storage.shard_routing import ShardResolverProducer

    resolver = ShardResolverProducer.dep(config, "resolver")
    if not isinstance(resolver, ShardResolver):
        raise ValidationError("params.resolver must resolve to a ShardResolver")
    locator = LazyShardLocator(str(config.get("locator", "default")))
    mode = str(config.get("mode", "split"))
    if mode == "shared":
        inner = VectorProducer.dep(config, "inner")
        return ShardRoutedVectorStore(
            resolver=resolver, locator=locator, mode=mode, inner=inner
        )
    _validate_embedding_consistency(
        config.get("table_template"), config.get("tables") or {}, resolver.shards
    )
    stores, owned = _build_shard_stores(config, resolver)
    return ShardRoutedVectorStore(
        resolver=resolver,
        locator=locator,
        mode="split",
        stores=stores,
        owned=owned,
    )
