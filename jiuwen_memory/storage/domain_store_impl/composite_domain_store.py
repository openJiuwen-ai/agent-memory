# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CompositeDomainStore — 默认数据面实现：MemoryUnit 领域 CRUD + 检索适配。

构造时注入 ``manager`` 引用，真源读写一律经 ``manager.kv()`` 端口（与 control 面
直连 KV 的路径同一条，授权代理在内）：领域方法先按 ``memory_unit`` 授权、端口再按
``kv`` 授权，两层 resource 不同，分层授权是预期语义而非冗余。

由 :meth:`CompositeDomainStore.for_manager` 供 ``CompositeStoreManager`` 在装配期
直接构造（manager 就绪后把自身传入，闭合二者的构造期循环）：检索 profile 派生、
召回路组装与绑定都在该方法内一次完成，manager 侧只是一次调用。

召回路（:class:`~storage.domain_store_impl.recaller.Recaller`）是本数据面的内部件，
契约与实现同处本包——生产链路里没有第二个消费方，``PipelineRetriever`` 只按首选路径
委托本类的 ``recall`` / ``recall_and_get`` / ``retrieve``。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from jiuwen_memory.common.errors import (
    NotFoundError,
    StorageRetrievalError,
    ValidationError,
    safe_error_message,
)
from jiuwen_memory.common.type_def import (
    CandidateFuser,
    ChannelError,
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    RankedStorageResult,
    RecallBatch,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredMemoryUnit,
    ScoredUnit,
    is_retrieval_candidate,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.domain_store import DomainStore, DomainStoreProducer
from jiuwen_memory.storage.security import (
    StorageAccessContext,
    StorageAction,
    StorageSecurity,
)
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode, MemoryListResult

from .recaller import RecallerProducer


def _parse_pipeline(value: RetrievalPipeline | str | None) -> RetrievalPipeline:
    """把配置值解析成 ``RetrievalPipeline``；缺省 ``RECALL_GET_RANK``，非法值抛
    ``ValidationError``。装配直构路径（:meth:`CompositeDomainStore.for_manager`）与
    Producer 路径（``_build``）共用本函数，两条路径的错误契约因此逐字一致。
    """
    if value is None:
        return RetrievalPipeline.RECALL_GET_RANK
    if isinstance(value, RetrievalPipeline):
        return value
    try:
        return RetrievalPipeline(value)
    except ValueError as exc:
        supported = [item.value for item in RetrievalPipeline]
        raise ValidationError(
            f"Unsupported preferred_retrieval_pipeline {value!r}; expected one of {supported}"
        ) from exc


class CompositeDomainStore(DomainStore):
    """默认数据面实现：MemoryUnit 领域 CRUD + 检索适配。"""

    def __init__(
        self,
        *,
        manager: StoreManager,
        preferred_pipeline: RetrievalPipeline,
        kv_name: str = "default",
    ) -> None:
        self._manager = manager
        self._preferred_pipeline = preferred_pipeline
        # 真源 KV 端口名：与其余消费方一致由装配期 resolve_name(config, "kv_store")
        # 指名（见 for_manager / builder），不在运行期硬编码 "default"。
        self._kv_name = kv_name
        # recallers 由 manager 装配期通过 bind_recallers 注入；默认空列表。
        self._recallers: list[Any] = []

    @classmethod
    def for_manager(cls, manager: StoreManager, config: Any = None) -> CompositeDomainStore:
        """供 :class:`CompositeStoreManager` 在装配期**直接构造**（不经 Producer）。

        数据面持有 manager 引用，而 manager 又持有数据面——构造期天然循环。manager
        在自身就绪后调用本方法把 ``self`` 传进来即可闭环，无须再经
        ``DomainStoreProducer`` 按具名引用绕回去解析一次。

        ``config`` 是本套数据面的 profile 视图（``domain_stores.<name>`` entry，命名
        实例已 overlay 在 ``default`` entry 之上）：检索首选路径、真源 KV 端口名与召回
        路选择键全部从它派生，组装完即 :meth:`bind_recallers` 绑定——这三件事本就同源，
        分开做只会给出「构造完但还没绑召回路」的半成品状态。

        ``config=None`` 是手工/测试接线口：全默认、不装召回路（手工接线的 recaller
        需要先有 manager 实例才能构造，仍走 :meth:`bind_recallers`）。
        """
        if config is None:
            return cls(manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK)
        domain_store = cls(
            manager=manager,
            preferred_pipeline=_parse_pipeline(config.get("preferred_retrieval_pipeline")),
            kv_name=resolve_name(config, "kv_store"),
        )
        domain_store.bind_recallers(_assemble_recallers(config, storage=manager))
        return domain_store

    @property
    def security(self) -> StorageSecurity:
        return self._manager.security

    @property
    def recallers(self) -> list[Any]:
        """已接入的 recaller 列表（只读视图；外部不应原地修改）。"""
        return self._recallers

    def bind_recallers(self, recallers: list[Any]) -> None:
        """手动绑定检索适配器（测试/手工装配用）；同一实例不允许绑定两套不同 recaller。"""
        bound = list(recallers)
        same_binding = len(self._recallers) == len(bound) and all(
            current is candidate for current, candidate in zip(self._recallers, bound)
        )
        if self._recallers and not same_binding:
            raise ValidationError("CompositeDomainStore cannot be rebound to different recallers")
        self._recallers = bound

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._preferred_pipeline

    def scopes(self, **kwargs: Any) -> list[Scope]:
        return self._kv().scopes()

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> None:
        self._authorize(access, scope, StorageAction.ADD, "memory_unit")
        # 本实现无投影能力，落地范围仅记忆本体：调用方只要检索索引时无事可做。
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        kv = self._kv()
        for unit in units:
            kv.insert(scope, memory_key(unit.id), dumps(unit))

    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> None:
        # 本实现落地范围仅记忆本体，FORWARD_ONLY 与 ALL 行为相同（无检索索引可跳过）。
        self._authorize(access, scope, StorageAction.UPDATE, "memory_unit")
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        kv = self._kv()
        for unit in units:
            kv.update(scope, memory_key(unit.id), dumps(unit))

    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> None:
        self._authorize(access, scope, StorageAction.DELETE, "memory_unit")
        # 同 add：无检索索引可单独移除，软删除保留本体即无事可做。
        if mode is IndexRemoveMode.SOFT:
            return
        kv = self._kv()
        for unit_id in unit_ids:
            kv.delete(scope, memory_key(unit_id))

    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> list[MemoryUnit]:
        self._authorize(access, scope, StorageAction.GET, "memory_unit")
        return self._get_units(scope, unit_ids)

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> MemoryListResult:
        self._authorize(access, scope, StorageAction.LIST, "memory_unit")
        result = self._kv().list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )
        items: list[MemoryUnit] = []
        for _, raw in result.entries:
            unit = loads(raw)
            if unit is not None:
                items.append(unit)
        return MemoryListResult(items=items, count=result.count)

    def recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> RecallResult[ScoredUnit]:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        return self._recall(scope, query, channels=channels, recall_limit=recall_limit)

    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> RecallResult[ScoredMemoryUnit]:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        return self._recall_and_get(
            scope, query, channels=channels, recall_limit=recall_limit
        )

    def retrieve(
        self,
        scope: Scope,
        query: ParsedQuery,
        fuser: CandidateFuser,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        rank_limit: int,
        access: StorageAccessContext | None = None,
        **kwargs: Any,
    ) -> RankedStorageResult:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        materialized = self._recall_and_get(
            scope, query, channels=channels, recall_limit=recall_limit
        )
        filtered: list[list[ScoredMemoryUnit]] = []
        for batch in materialized.batches:
            candidates = []
            for candidate in batch.candidates:
                if _passes(candidate.unit, query):
                    candidates.append(candidate)
            filtered.append(candidates)
        ranked = fuser.fuse(query, filtered)[:rank_limit]
        return RankedStorageResult(candidates=ranked, errors=materialized.errors)

    def health(self) -> None:
        # 数据面无独立资源（recallers 是被注入的，非健康检查对象）；委托 manager 聚合。
        self._manager.health()

    @staticmethod
    def _validate_units(scope: Scope, units: list[MemoryUnit]) -> None:
        invalid = [unit.id for unit in units if unit.scope != scope]
        if invalid:
            raise ValidationError(f"MemoryUnit scope differs from explicit scope: {invalid}")

    def _authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        self._manager.security.authorize(access, scope, action, resource)

    def _kv(self) -> Any:
        # 与 control 面一致，经 manager 的具名 KV 端口取用（授权代理在内）：领域方法先按
        # memory_unit 授权、端口再按 kv 授权，两层 resource 不同，分层授权是预期语义。
        # 端口缺失时 manager 抛 UnsupportedStorageCapabilityError 并指明缺失端口。
        return cast(Any, self._manager.kv(self._kv_name))

    def _get_units(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        """批量点读真源：按输入顺序返回，缺失 id 省略，重复 id 各自返回。

        ``mget`` 不去重且任一 key 缺失即抛 ``NotFoundError``（见 :meth:`KVStore.mget`），
        故去重与「索引↔真源短暂不一致」的兜底都由本方法承担。
        """
        if not unit_ids:
            return []
        kv = self._kv()
        unique = list(dict.fromkeys(unit_ids))
        try:
            loaded = list(zip(unique, kv.mget(scope, [memory_key(uid) for uid in unique])))
        except NotFoundError:
            loaded = []
            for unit_id in unique:
                try:
                    loaded.append((unit_id, kv.get(scope, memory_key(unit_id))))
                except NotFoundError:
                    continue
        by_id: dict[str, MemoryUnit] = {}
        for unit_id, raw in loaded:
            unit = loads(raw)
            if unit is not None:
                by_id[unit_id] = unit
        return [by_id[unit_id] for unit_id in unit_ids if unit_id in by_id]

    def _recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredUnit]:
        if channels == []:
            raise ValidationError("channels must be omitted or contain at least one channel")
        selected = [
            recaller
            for recaller in self._recallers
            if channels is None or recaller.channel() in channels
        ]
        if not selected:
            return RecallResult()
        batches: list[RecallBatch[ScoredUnit] | None] = [None] * len(selected)
        errors: list[ChannelError] = []
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            futures = {
                executor.submit(recaller.recall, scope, query, recall_limit): (index, recaller)
                for index, recaller in enumerate(selected)
            }
            for future in as_completed(futures):
                index, recaller = futures[future]
                source = _recaller_source(recaller)
                try:
                    candidates = future.result()
                except Exception as exc:
                    errors.append(
                        ChannelError(
                            channel=recaller.channel(),
                            source=source,
                            error_type=type(exc).__name__,
                            message=safe_error_message(exc),
                        )
                    )
                    continue
                batches[index] = RecallBatch(recaller.channel(), source, candidates)
        if errors and len(errors) == len(selected):
            raise StorageRetrievalError(errors)
        successful = [batch for batch in batches if batch is not None]
        return RecallResult(batches=successful, errors=errors)

    def _recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredMemoryUnit]:
        recalled = self._recall(
            scope, query, channels=channels, recall_limit=recall_limit
        )
        unit_ids: list[str] = []
        seen: set[str] = set()
        for batch in recalled.batches:
            for candidate in batch.candidates:
                if candidate.unit_id not in seen:
                    seen.add(candidate.unit_id)
                    unit_ids.append(candidate.unit_id)
        units = {unit.id: unit for unit in self._get_units(scope, unit_ids)}
        batches = []
        errors = list(recalled.errors)
        for batch in recalled.batches:
            candidates = []
            for candidate in batch.candidates:
                unit = units.get(candidate.unit_id)
                if unit is None:
                    errors.append(
                        ChannelError(
                            channel=batch.channel,
                            source=batch.source,
                            error_type="MissingMemoryUnit",
                            message=f"MemoryUnit not found: {candidate.unit_id}",
                        )
                    )
                    continue
                candidates.append(
                    ScoredMemoryUnit(unit, candidate.score, candidate.channel, candidate.evidence)
                )
            batches.append(RecallBatch(batch.channel, batch.source, candidates))
        return RecallResult(batches=batches, errors=errors)


def _passes(unit: MemoryUnit, query: ParsedQuery) -> bool:
    return is_retrieval_candidate(
        unit,
        as_of=query.as_of,
        time_from=query.time_from,
        time_to=query.time_to,
        filters=query.recheck_filters,
        include_archived=query.include_archived,
    )


def _recaller_source(recaller: Any) -> str:
    layer = getattr(recaller, "layer", None)
    if layer:
        return f"{recaller.channel().value}_{layer}"
    return type(recaller).__name__


def _assemble_recallers(config: Any, *, storage: StoreManager) -> list[Any]:
    """按能力开关组装召回路；每路 recaller 自取其 Store，可被 config 各自覆盖。

    构建期同步执行，装配错误 fail-fast（F06 内收设计保留，调用时机在
    :meth:`CompositeDomainStore.for_manager` 内）。具名构建（``config.name`` 非空）由
    manager ``from_config`` 预注册进具名缓存，``RecallerProducer.dep`` 走具名引用路径，
    recaller builder 内 ``StoreManagerProducer.resolve`` 命中缓存打破循环。匿名构建无
    缓存键，此处用合成名（``id(storage)`` 保证唯一）预注册本实例，改走
    ``RecallerProducer.build`` 直接把 manager 引用注入 params，让 builder 内的
    ``resolve`` 走第一分支（``cls.dep``）命中合成名缓存——避免落到第三分支再建一个
    匿名 manager 触发递归。

    ``config`` 是某套数据面的 profile 视图。``RecallerProducer.dep`` 读的是
    ``config.params``（**直读不回退 globals**），故 ``domain_stores`` 的命名 entry 必须
    先 overlay 在 ``default`` entry 之上再传进来：漏掉 ``*_recaller`` 选择键会让 ``dep``
    落到 ``cls.build(default, {}, ctx)`` **匿名新建**一套不共享的 recaller，静默退化。
    """
    if config.name:
        # 具名构建：recaller 命名空间下声明的具名实例带 ``store_manager: <name>``
        # 引用，``dep`` 走 ``build_named`` 命中缓存即可，无需注入。
        def _dep(key: str, default_target: str) -> Any:
            return RecallerProducer.dep(config, key, default=default_target)
    else:
        # 匿名构建：无 recaller 命名空间，用合成名注册 + 直接 build 注入 manager
        # 引用，让 builder 内 ``StoreManagerProducer.resolve`` 走 ``cls.dep`` 第一
        # 分支命中缓存。
        synthetic_name = f"__anon_store_manager_{id(storage)}__"
        StoreManagerProducer.put(synthetic_name, storage)

        def _dep(key: str, default_target: str) -> Any:
            target = config.get(key, default_target)
            return RecallerProducer.build(
                target, {"store_manager": synthetic_name}, config.ctx
            )

    recallers = [_dep("keyword_recaller", "keyword")]
    if config.get("vector_enabled", True):
        recallers.append(_dep("vector_recaller", "vector"))
    if config.get("graph_enabled", True):
        recallers.append(_dep("graph_recaller", "graph"))
    # L0/L1 分层召回：layers_index_enabled 默认 true（与构建侧对齐：默认建默认查）。
    # recaller 内部 store 为 None 时 recall 返空，不破坏其他路（向后兼容）。
    if config.get("layers_index_enabled", True):
        recallers.append(_dep("keyword_l0_recaller", "keyword_l0"))
        recallers.append(_dep("keyword_l1_recaller", "keyword_l1"))
        if config.get("vector_enabled", True):
            recallers.append(_dep("vector_l0_recaller", "vector_l0"))
            recallers.append(_dep("vector_l1_recaller", "vector_l1"))
    return recallers


@DomainStoreProducer.register("composite")
def _build(config):
    # 回取 manager：params["store_manager"] 为字符串引用（manager from_config 已预注册
    # 进缓存，dep 走 build_named 命中）。必填、无 default——独立构建会触发 manager
    # 匿名重建的无限递归，且违背「所有存储类从 StoreManager 获取」原则。
    manager = StoreManagerProducer.dep(config)
    if not isinstance(manager, StoreManager):
        raise TypeError(
            f"DomainStore builder assembled {type(manager).__name__}, expected StoreManager"
        )
    # 本路径不装召回路：非 composite target 自带检索路径（F06 决策 3），走到这里的
    # composite 实例是「以别名注册的可换实现」，其召回路由注册方自行负责。
    return CompositeDomainStore(
        manager=manager,
        preferred_pipeline=_parse_pipeline(config.get("preferred_retrieval_pipeline")),
        kv_name=resolve_name(config, "kv_store"),
    )
