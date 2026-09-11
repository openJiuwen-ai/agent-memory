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
    MD_FILENAME_KEY,
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
from jiuwen_memory.config.document_flag import WRITE_DOCUMENT_KEY, should_write_document
from jiuwen_memory.storage.domain_store import DomainStore, DomainStoreProducer
from jiuwen_memory.storage.kv_impl.memory_list import list_memory_entries
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
from jiuwen_memory.storage.sync_gate import (
    close_write_window,
    open_write_window,
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
        write_document: bool = False,
    ) -> None:
        self._manager = manager
        self._preferred_pipeline = preferred_pipeline
        # 真源 KV 端口名：与其余消费方一致由装配期 resolve_name(config, "kv_store")
        # 指名（见 for_manager / builder），不在运行期硬编码 "default"。
        self._kv_name = kv_name
        # recallers 由 manager 装配期通过 bind_recallers 注入；默认空列表。
        self._recallers: list[Any] = []
        # write_document 装配期固化（与 _preferred_pipeline 同范式，F07 §2）：
        # true → 真源写影子索引 + md 人类视图，不写 KV；false → 仅写 KV。
        # markdown/shadow 端口是否装配与它绑定（manager 扫命名空间决定）。
        self._write_document = write_document

    @classmethod
    def for_manager(cls, manager: StoreManager, config: Any = None) -> CompositeDomainStore:
        """供 :class:`CompositeStoreManager` 在装配期**直接构造**（不经 Producer）。

        数据面持有 manager 引用，而 manager 又持有数据面——构造期天然循环。manager
        在自身就绪后调用本方法把 ``self`` 传进来即可闭环，无须再经
        ``DomainStoreProducer`` 按具名引用绕回去解析一次。

        ``config`` 是本套数据面的 profile 视图（``domain_stores.<name>`` entry，命名
        实例已 overlay 在 ``default`` entry 之上）：检索首选路径、真源 KV 端口名、召回
        路选择键与文档模式开关全部从它派生，组装完即 :meth:`bind_recallers` 绑定——这几
        件事本就同源，分开做只会给出「构造完但还没绑召回路」的半成品状态。

        ``config=None`` 是手工/测试接线口：全默认、不装召回路（手工接线的 recaller
        需要先有 manager 实例才能构造，仍走 :meth:`bind_recallers`）。
        """
        if config is None:
            return cls(manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK)
        domain_store = cls(
            manager=manager,
            preferred_pipeline=_parse_pipeline(config.get("preferred_retrieval_pipeline")),
            kv_name=resolve_name(config, "kv_store"),
            write_document=should_write_document(config.get(WRITE_DOCUMENT_KEY, False)),
        )
        domain_store.bind_recallers(_assemble_recallers(config, storage=manager))
        return domain_store

    @property
    def security(self) -> StorageSecurity:
        return self._manager.security

    @staticmethod
    def _sanitize_document_content(units: list[MemoryUnit]) -> None:
        """文档路径 content 单行清洗（F07 §12.4 的 enforcement point）。

        块格式契约（``<标题>\\n<正文单行>\\n\\n``、看门狗按行遍历、replace/remove 按
        ``\\n\\n`` 切块比对正文）建立在「一个 unit 一行正文」上，但上游（LLM 抽取/
        直写）不保证——content 含换行时：md 块被切碎、replace_content 比对失锚；
        看门狗按行遍历把第 2+ 行当独立幽灵 unit，且整段 content_hash 与任何单行
        hash 对不上 → diff 出「删真 unit + 建幽灵 unit（新 uuid，断版本链）」。

        故在 md.write / shadow.insert_units 分叉**之前**对 unit 本体原地折叠
        ``" ".join(content.split())``——md 视图、unit_json、content_hash、后续
        replace_content 锚点四方看到同一份单行 content。收口在文档路径入口
        而非 extractor：单行是文档记忆的**存储层约束**（F07 §12.4），非抽取层
        约束；KV 路径（结构化记忆）不受影响。
        """
        for unit in units:
            if not unit.segments:
                continue
            content = unit.segments[0].content
            if content and "\n" in content:
                unit.segments[0].content = " ".join(content.split())

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

    def should_write_document(self) -> bool:
        """运行期直接读实例属性，不查 config（装配期已固化，见 ``__init__``）。"""
        return self._write_document

    def _raw_markdown(self) -> Any:
        """取 markdown 端口；文档模式未装配时 manager 抛 UnsupportedStorageCapabilityError。"""
        return self._manager.markdown()

    def _raw_shadow_index(self) -> Any:
        """取 shadow 端口；文档模式未装配时 manager 抛 UnsupportedStorageCapabilityError。"""
        return self._manager.shadow_index()

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._preferred_pipeline

    def scopes(self) -> list[Scope]:
        return self._kv().scopes()

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.ADD, "memory_unit")
        # 本实现无独立投影能力（倒排/向量收进影子索引算子内部）：
        # 调用方只要检索索引时（RETRIEVAL_ONLY）无事可做。
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        if self.should_write_document():
            # 文档路径：写 md + 建影子索引，不碰 KV（F07 §3.1 互斥路径）。
            # md.write 内部按文件分组批量写、回填 unit.system_metadata["md_filename"]
            # + 兜底 memory_class（空落 team_memory，F08 §2）；shadow.insert_units
            # 从 system_metadata 读 md_filename 建 three-table 索引。
            # 写窗口：两步写期间 md 与索引短暂不一致，挡住看门狗对账（F07 §12.9 风险 6，
            # sync_gate 模块说明）——insert_units 含逐条 embed（完整模式远端 HTTP），
            # 窗口可达秒级，2s debounce 挡不住。
            self._sanitize_document_content(units)
            md = self._raw_markdown()
            shadow = self._raw_shadow_index()
            open_write_window()
            try:
                md.write(scope, units)
                shadow.insert_units(scope, units)
            finally:
                close_write_window()
        else:
            # 非文档路径：KV 真源（原样）。
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
    ) -> None:
        # 本实现落地范围仅记忆本体，FORWARD_ONLY 与 ALL 行为相同（无检索索引可跳过）。
        self._authorize(access, scope, StorageAction.UPDATE, "memory_unit")
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        if self.should_write_document():
            # 文档路径：影子索引 update_units 覆写 unit_json（content_hash 判定自动处理——
            # OVERWRITE content 变 → 重建 FTS5/vec0；SUPERSEDE 状态变 → 只覆写 unit_json），
            # content 变时同步 md.replace_content 改 md 文件（F07 §5.2.1 步骤③⑤）。
            # 写窗口（sync_gate，F07 §12.9 风险 6）：update 是「先索引后 md」反序——
            # update_units（含 OVERWRITE 重建的 re-embed）与 replace_content 之间，索引
            # 已变而 md 还是旧的，看门狗在此插入会「删真 unit + 建幽灵」。整个 for 循环
            # 共持一个窗口（批量 update 中途关窗会出现同类窗口）。
            self._sanitize_document_content(units)
            shadow = self._raw_shadow_index()
            md = self._raw_markdown()
            open_write_window()
            try:
                for unit in units:
                    # 先取旧 unit 拿 old content + md_filename（replace_content 的定位锚与路径）。
                    olds = shadow.get_units(scope, [unit.id])
                    old = olds[0] if olds else None
                    # 影子索引覆写（id 不存在内部报 NotFoundError，对齐 KVStore.update）。
                    shadow.update_units(scope, [unit])
                    # md 侧：仅 content 变才 replace（OVERWRITE 场景，§5.2.1）；SUPERSEDE 只改
                    # 状态字段 content 不变 → md 不动（§5.2.4 对照表「md 侧：无（保留）不适用」）。
                    # 对比口径用 segments[0].content（与 md/影子索引 _content_of 同源，§12.4 单段）。
                    if old is None:
                        continue
                    old_content = old.segments[0].content if old.segments else ""
                    new_content = unit.segments[0].content if unit.segments else ""
                    if old_content == new_content:
                        continue
                    md_filename = (unit.system_metadata or {}).get(MD_FILENAME_KEY, "")
                    if md_filename:
                        # 未命中（md 与索引漂移，如手改 md）返 False——不抛错，索引侧已更新，
                        # 漂移交看门狗（§12.3）后续对账，避免 update 因 md 异常而整体失败。
                        md.replace_content(scope, md_filename, old_content, new_content)
            finally:
                close_write_window()
        else:
            # 非文档路径：KV 真源（原样）。
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
    ) -> None:
        self._authorize(access, scope, StorageAction.DELETE, "memory_unit")
        # 同 add：无检索索引可单独移除，软删除保留本体即无事可做（KV 时代语义）。
        # 文档模式下本实现持有检索索引（FTS5/vec0 在 shadow 库里），但 SOFT 仍为
        # no-op——检索退出由调用方先 lifecycle.transition 改状态（update(FORWARD_ONLY)
        # 同步 lifecycle 投影列），检索侧靠谓词下推 + retriever 复核排除实现，
        # 不在 delete(SOFT) 里删投影（避免 content 变更重建投影使 unit 重回检索）。
        if mode is IndexRemoveMode.SOFT:
            return
        if self.should_write_document():
            # 文档路径：影子索引 delete_units 同事务删三表 + md.remove_content 删对应块
            # （F07 §5.4）。先 get_units 拿旧 unit（md_filename + content 定位 md 块）——
            # delete_units 幂等不返存在信息，md 块定位靠旧 unit 的 content（类比 update 的
            # replace_content 用 old_content 定位，§5.2.1 步骤⑤）。
            # 写窗口（sync_gate，F07 §12.9 风险 6）：delete 也是「先索引后 md」反序——
            # delete_units 与 remove_content 之间，索引已删而 md 还有行，看门狗在此插入
            # 会把刚删的 unit 以新 uuid 复活（双写）。
            shadow = self._raw_shadow_index()
            md = self._raw_markdown()
            open_write_window()
            try:
                olds = shadow.get_units(scope, unit_ids)
                # 影子索引删三表（幂等，缺失静默跳过，§12.7 显式删三表不级联）。
                shadow.delete_units(scope, unit_ids)
                # md 侧：对每个存在的旧 unit 删对应块。get_units 缺失 id 省略 → 已不存在的
                # unit 不删 md 块（md 与索引一致，本无块；若漂移交看门狗 §12.3 对账）。
                for old in olds:
                    content = old.segments[0].content if old.segments else ""
                    md_filename = (old.system_metadata or {}).get(MD_FILENAME_KEY, "")
                    if md_filename:
                        # 未命中（md 与索引漂移，如手改 md / 看门狗先删）返 False——不抛错，
                        # 索引侧已删，漂移交看门狗对账，避免 delete 因 md 异常而整体失败。
                        md.remove_content(scope, md_filename, content)
            finally:
                close_write_window()
        else:
            # 非文档路径：KV 真源（原样）。
            kv = self._kv()
            for unit_id in unit_ids:
                kv.delete(scope, memory_key(unit_id))

    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
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
    ) -> MemoryListResult:
        self._authorize(access, scope, StorageAction.LIST, "memory_unit")
        # 文档分流（F07 §5.3 方案A）：文档模式真源是 md+shadow，全量拉走 shadow.list_units
        # （对应 KV.scan 的角色），过滤/排序/分页原样复用 list_memory_entries——该函数入参是
        # list[tuple[str, bytes]]，shadow.list_units 产出 (unit_id, unit_json bytes) 正好对齐
        # （unit_id 当 key、unit_json 当 raw_bytes），无需区分来源。非文档维持原 KV.list 路径。
        if self.should_write_document():
            entries = self._raw_shadow_index().list_units(scope)
            result = list_memory_entries(
                entries,
                offset=offset,
                limit=limit,
                memory_types=memory_types,
                filters=filters,
                extensions=extensions,
            )
        else:
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

        文档模式（``write_document=true``）走影子索引 ``shadow.get_units``——
        真源已从 KV 切到 ``md``+``shadow``，KV 不再持有 MemoryUnit。``shadow.get_units``
        契约即「缺失省略、按输入顺序保序」，与 KV 路径的语义对齐（§5.6 S2）。

        非文档模式走 KV：``mget`` 不去重且任一 key 缺失即抛 ``NotFoundError``
        （见 :meth:`KVStore.mget`），故去重与「索引↔真源短暂不一致」的兜底
        都由本方法承担。影子索引路径无此问题——缺失 id 在 SQL 层自然省略。
        """
        if not unit_ids:
            return []
        if self.should_write_document():
            shadow = self._raw_shadow_index()
            by_id = {unit.id: unit for unit in shadow.get_units(scope, unit_ids)}
            # shadow.get_units 按 IN 查询返回唯一行，对重复 id 复用同一份 unit 对象，
            # 与 KV 路径「重复 id 各自返回」行为一致（召回物化侧已 seen 去重，实际无重复）。
            return [by_id[uid] for uid in unit_ids if uid in by_id]
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

    if should_write_document(config.get(WRITE_DOCUMENT_KEY, False)):
        # 文档模式：真源 md+shadow，召回统一走 ShadowRecaller（shadow.search_fulltext
        # + search_vector 复合算子），替代 KV 时代的 keyword+vector+layers 四路——
        # 那四路取 fulltext/vector 端口，文档模式不装配 → 全返空。graph 路独立于
        # fulltext/vector 端口，按端口就绪与否决定是否并存（GraphRecaller 构造期硬取
        # storage.graph，未配 graph store 时装配即抛，故需 has_graph_port 判定）。
        recallers = [_dep("shadow_recaller", "shadow")]
        if config.get("graph_enabled", True) and storage.has_graph():
            recallers.append(_dep("graph_recaller", "graph"))
        return recallers

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
