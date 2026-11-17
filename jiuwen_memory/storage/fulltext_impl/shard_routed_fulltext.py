# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``shard_routed`` 全文存储（F09 D8）。

与向量包装层同构：``split``（默认）每个分片一个物理索引，写入经 ``ShardLocator``
跟随 KV 归属、检索只访问选中分片并合并各分片 top-k；``shared`` 单索引，写入打
``storage_shard`` 标签，检索把分片条件与 filters 取 AND 后**在截断前**下推。

跨分片 BM25 合并是**产品语义上的近似**（各索引 IDF 不同），不承诺与单索引排序
完全等价；验收门槛见 F09「性能基线」。
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
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
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
from jiuwen_memory.storage.types import Document, ScoredID, TextQuery

logger = get_logger(__name__)


class ShardRoutedFulltextStore(FulltextStore):
    """全文端口的 ``shard_routed`` 包装层。"""

    def __init__(
        self,
        *,
        resolver: ShardResolver,
        locator: ShardLocator,
        mode: str = "split",
        stores: dict[str, FulltextStore] | None = None,
        inner: FulltextStore | None = None,
        owned: tuple[BaseStore, ...] = (),
    ) -> None:
        if mode not in ("split", "shared"):
            raise ValidationError(f"fulltext shard mode must be split|shared, got {mode!r}")
        if mode == "shared":
            if inner is None or stores:
                raise ValidationError(
                    "shared fulltext shard mode requires 'inner' and no 'tables'"
                )
            require_capability(
                inner, StoreCapability.SHARD_PREFILTER, role="fulltext shared inner"
            )
        else:
            if inner is not None or not stores:
                raise ValidationError("split fulltext shard mode requires per-shard stores")
            missing = [name for name in resolver.shards if name not in stores]
            if missing:
                raise ValidationError(f"fulltext split mode is missing shards: {missing}")
            unknown = [name for name in stores if name not in resolver.shards]
            if unknown:
                raise ValidationError(f"fulltext split mode has unconfigured shards: {unknown}")
            require_distinct_shard_resources(stores, kind="fulltext")
        self._resolver = resolver
        self._locator = locator
        self._mode = mode
        self._stores = dict(stores or {})
        self._inner = inner
        self._owned = tuple(owned)

    @property
    def security(self):
        return CompositeStoreSecurity(
            [self._inner] if self._inner is not None else list(self._stores.values())
        )

    def store_type(self) -> StoreType:
        return StoreType.FULLTEXT

    def capabilities(self) -> frozenset[StoreCapability]:
        caps = {StoreCapability.SHARD_ROUTED}
        if all(
            StoreCapability.UNIT_RECORD_LOOKUP in store.capabilities()
            for store in self._physical_stores()
        ):
            caps.add(StoreCapability.SHARD_RELOCATABLE)
        if self._mode == "shared" and self._inner is not None:
            if StoreCapability.SHARD_PREFILTER in self._inner.capabilities():
                caps.add(StoreCapability.SHARD_PREFILTER)
        return frozenset(caps)

    @property
    def shards(self) -> tuple[str, ...]:
        return self._resolver.shards

    def shard_descriptors(self, kind: str = "fulltext") -> dict[str, dict[str, str]]:
        """每个分片的物理标识（shared 模式下每个分片共享同一实例身份）。

        ``kind`` 是登记键（``{capability}:{port_name}``），由装配器传入。
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
        """物理分片索引 / 共享 inner 的连接预算来源（内联实例不在端口表里）。"""
        targets = [self._inner] if self._inner is not None else list(self._stores.values())
        return tuple(store for store in targets if isinstance(store, BaseStore))

    def close(self) -> None:
        for store in self._owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        self._owned = ()

    # -- 搬迁（F09 D16） ------------------------------------------------------ #

    def _physical_stores(self) -> list[FulltextStore]:
        if self._inner is not None:
            return [self._inner]
        return list(self._stores.values())

    def _lookup_ids(self, store: FulltextStore, scope: Scope, unit_id: str) -> list[str]:
        require_capability(
            store, StoreCapability.UNIT_RECORD_LOOKUP, role="fulltext relocation inner"
        )
        return list(store.record_ids_for_units(scope, [unit_id]).get(unit_id, []))

    @staticmethod
    def _read_exact(
        store: FulltextStore, scope: Scope, ids: list[str]
    ) -> list[Document]:
        """读取搬迁需要的全部文档；少任何一条都拒绝，不能把部分读取当成完整复制。"""
        docs = store.get(scope, ids)
        by_id = {doc.id: doc for doc in docs}
        missing = [doc_id for doc_id in ids if doc_id not in by_id]
        if missing:
            raise BackendError(
                f"relocation read incomplete for unit documents {missing[:5]}: "
                f"expected {len(ids)}, got {len(docs)}; refusing to relocate"
            )
        return [by_id[doc_id] for doc_id in ids]

    @staticmethod
    def _copy_docs(store: FulltextStore, scope: Scope, docs: list[Document]) -> None:
        for doc in docs:
            try:
                store.insert(scope, [doc])
            except ConflictError:
                existing = store.get(scope, [doc.id])
                if existing and existing[0] == doc:
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
            docs = self._read_exact(
                self._inner, scope, self._lookup_ids(self._inner, scope, unit_id)
            )
            moved = []
            for doc in docs:
                if doc.metadata.get(STORAGE_SHARD_NEXT_FIELD) == target:
                    continue
                metadata = dict(doc.metadata)
                metadata[STORAGE_SHARD_NEXT_FIELD] = target
                moved.append(Document(id=doc.id, text=doc.text, metadata=metadata))
            if moved:
                self._inner.update(scope, moved)
            return
        source_store = self._stores[source]
        ids = self._lookup_ids(source_store, scope, unit_id)
        if ids:
            self._copy_docs(
                self._stores[target], scope, self._read_exact(source_store, scope, ids)
            )

    def cleanup_unit(self, scope: Scope, unit_id: str, shard: str) -> None:
        """只清理指定分片中该记忆的记录（幂等）。

        共享模式：当前标签是 ``shard`` 且带过渡期标签 → 把过渡期标签转正（完成搬迁）；
        过渡期标签是 ``shard`` → 去掉它（回滚）；其余不动。不依赖定位结果。
        """
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            docs = self._read_exact(
                self._inner, scope, self._lookup_ids(self._inner, scope, unit_id)
            )
            changed = []
            for doc in docs:
                metadata = dict(doc.metadata)
                pending = metadata.get(STORAGE_SHARD_NEXT_FIELD)
                if pending is None:
                    continue
                if metadata.get(STORAGE_SHARD_FIELD) == shard:
                    metadata[STORAGE_SHARD_FIELD] = pending
                elif pending != shard:
                    continue
                metadata.pop(STORAGE_SHARD_NEXT_FIELD, None)
                changed.append(Document(id=doc.id, text=doc.text, metadata=metadata))
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
        out: dict[str, list[str]] = {}
        for store in self._physical_stores():
            require_capability(
                store, StoreCapability.UNIT_RECORD_LOOKUP, role="fulltext relocation inner"
            )
            for unit_id, ids in store.record_ids_for_units(scope, unit_ids).items():
                out.setdefault(unit_id, []).extend(ids)
        return out

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        self._write(scope, docs, update=False)

    def update(self, scope: Scope, docs: list[Document]) -> None:
        self._write(scope, docs, update=True)

    def _write(self, scope: Scope, docs: list[Document], *, update: bool) -> None:
        if not docs:
            return
        unit_ids = [unit_id_from_metadata(doc.id, doc.metadata) for doc in docs]
        location = self._locator.locate(scope, list(dict.fromkeys(unit_ids)))
        grouped: dict[str, list[Document]] = {}
        failed: list[tuple[str, Exception]] = []
        for doc, unit_id in zip(docs, unit_ids, strict=True):
            if unit_id in location.moving:
                failed.append((doc.id, RelocationInProgressError(unit_id)))
                continue
            shard = location.found.get(unit_id)
            if shard is None:
                failed.append(
                    (
                        doc.id,
                        ValidationError(
                            f"no active shard placement for unit {unit_id!r}: the memory was "
                            "never written to the sharded KV or has been deleted (check that "
                            "the deployment's index_builder persists the forward index)"
                        ),
                    )
                )
                continue
            target = doc
            if self._mode == "shared":
                metadata = dict(doc.metadata)
                metadata[STORAGE_SHARD_FIELD] = shard
                target = Document(id=doc.id, text=doc.text, metadata=metadata)
            grouped.setdefault(shard, []).append(target)
        for shard, group in grouped.items():
            store = self._inner if self._mode == "shared" else self._stores[shard]
            try:
                if update:
                    store.update(scope, group)
                else:
                    store.insert(scope, group)
            except Exception as exc:  # noqa: BLE001 - 收集后统一报告
                failed.extend((doc.id, exc) for doc in group)
        if failed:
            first = failed[0][1]
            first.failed_ids = [record_id for record_id, _ in failed]  # type: ignore[attr-defined]
            raise first

    def delete(self, scope: Scope, ids: list[str]) -> None:
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            self._inner.delete(scope, ids)
            return
        for store in self._stores.values():
            store.delete(scope, ids)

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            return self._inner.get(scope, ids)
        found: dict[str, Document] = {}
        for store in self._stores.values():
            for doc in store.get(scope, ids):
                found.setdefault(doc.id, doc)
        return [found[doc_id] for doc_id in ids if doc_id in found]

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        shards = selected_shards(query.extensions, self.shards)
        narrowed = query
        if self._mode == "shared":
            narrowed = TextQuery(
                text=query.text,
                top_k=query.top_k,
                filters=with_shard_filter(query.filters, shards),
                extensions=dict(query.extensions),
            )
        hits: list[ScoredID] = []
        if self._mode == "shared":
            if self._inner is None:
                raise RuntimeError("shared mode requires an inner store")
            hits = self._inner.search(scope, narrowed)
        else:
            for shard in shards:
                hits.extend(self._stores[shard].search(scope, narrowed))
        return merge_scored(
            hits,
            query.top_k,
            higher_is_better=True,
            key=lambda item: item.id,
            score=lambda item: item.score,
        )


def _expand(value: Any, shard: str) -> Any:
    if isinstance(value, str):
        return value.replace("{shard}", shard)
    if isinstance(value, dict):
        return {key: _expand(item, shard) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, shard) for item in value]
    return value


def _build_inline(spec: dict, shard: str, ctx: Any) -> FulltextStore:
    target = spec.get("target")
    if not target:
        raise ValidationError("shard table template requires a 'target'")
    params = _expand(spec.get("params") or {}, shard)
    return FulltextProducer.build(str(target), params, ctx, name=f"shard_{shard}")


@FulltextProducer.register("shard_routed")
def _build(config):
    from jiuwen_memory.storage.shard_routing import ShardResolverProducer

    resolver = ShardResolverProducer.dep(config, "resolver")
    if not isinstance(resolver, ShardResolver):
        raise ValidationError("params.resolver must resolve to a ShardResolver")
    locator = LazyShardLocator(str(config.get("locator", "default")))
    mode = str(config.get("mode", "split"))
    if mode == "shared":
        inner = FulltextProducer.dep(config, "inner")
        return ShardRoutedFulltextStore(
            resolver=resolver, locator=locator, mode=mode, inner=inner
        )
    template = config.get("table_template")
    overrides = config.get("tables") or {}
    if not isinstance(overrides, dict):
        raise ValidationError("params.tables must be a mapping of shard → store")
    stores: dict[str, FulltextStore] = {}
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
                store = FulltextProducer.build_named(override, config.ctx)
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
    return ShardRoutedFulltextStore(
        resolver=resolver,
        locator=locator,
        mode="split",
        stores=stores,
        owned=tuple(owned),
    )
