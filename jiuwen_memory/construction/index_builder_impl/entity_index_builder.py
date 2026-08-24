# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityIndexBuilder — 实体反向索引的 IndexBuilder 实现。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from uuid import uuid4

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.entity import (
    EntityLinkResult,
    EntityMention,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    hash_entity_text,
)
from jiuwen_memory.common.type_def.normalizer import EntityNormalizer
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)

_DEFAULT_LIST_LIMIT = 10000


# ---------------------------------------------------------------------------
# 准入策略（原 entity_linker/admission.py）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityIndexAdmission:
    """单条 MemoryUnit 是否进实体索引的判断结果。"""

    admitted: bool
    text: str = ""
    reason: str = ""


class EntityIndexAdmissionPolicy:
    """实体索引准入策略：保持聚焦于稳定检索锚点。

    原模块按 ``strategy_type`` 判定（只放 ``semantic:memory`` 和
    ``user_preference:memory``），吃 ``MemoryRecordVector``。当前工程改用
    ``tier``（``MemoryTier``），吃 ``MemoryUnit``：

    - **SEMANTIC / CORE / EPISODIC** 准入（含具体实体，值得建索引）
    - **WORKING**（易失态）/ **ARCHIVAL**（冷数据默认不召回）跳过

    所有准入 tier 全实体类型放行——当前 entity_type 生产路径只产 PROPER，
    无需按类型过滤；保留字段供未来 LLM 抽取细化分类时复用 schema。
    """

    # 准入 tier（含具体实体，值得建索引）
    _ADMITTED_TIERS = frozenset(
        {
            MemoryTier.SEMANTIC,  # 事实/概念
            MemoryTier.CORE,  # 高价值画像
            MemoryTier.EPISODIC,  # 情景（含具体事件实体）
        }
    )

    def decide(self, unit: MemoryUnit) -> EntityIndexAdmission:
        content = (unit.content or "").strip()
        if not content:
            return EntityIndexAdmission(admitted=False, reason="empty_content")

        if unit.tier in self._ADMITTED_TIERS:
            return EntityIndexAdmission(
                admitted=True,
                text=content,  # 直接用 unit.content（已是纯文本）
            )

        return EntityIndexAdmission(
            admitted=False,
            reason=f"tier_not_entity_indexed:{unit.tier.value}",
        )


# ---------------------------------------------------------------------------
# 写入侧编排（原 entity_linker/linker.py 的 EntityLinkService）
# ---------------------------------------------------------------------------


class EntityLinkService:
    """维护实体到 memory_id 的反向索引。

    迁移自原 ``core.application.entities.linker``，核心编排逻辑做工程化改造：

    1. **吃 MemoryUnit**：``link_memories(records: list[MemoryRecordVector])`` →
       ``link_memories(units: list[MemoryUnit])``，去 async（配合 §3.4 sync 决策）。
    2. **分组用 Scope**：``EntityStoreFilters.from_scope(unit.scope)`` +
       ``space_id_from_scope(unit.scope)``，不再 ``record.space_id``（UUID）。
    3. **str 化**：``memory_id`` 全程 ``unit.id``（str），
       ``linked_memory_ids`` 存 str，对齐召回侧 ``ScoredUnit.unit_id``。

    **2026-08-12 改造**：
    - 归并退化为 hash 精确 only。原三级匹配（hash 精确 → 向量语义归并 →
      INSERT/LINK）砍掉向量语义归并阶段——hash 精确命中 → LINK；未命中 → INSERT
      当新实体。不再算 entity embedding、不依赖 Embedder。同实体不同表述
      （"Python" vs "Python 语言"）hash 不同会被建成多条记录，召回质量依赖
      LLM 抽 entity 的表述稳定性。
    - **砍掉 spaCy 兜底抽取**：``link_memories`` 只消费 ``unit.entities`` 明文字段
      构造 ``EntityMention``；``unit.entities`` 为空的 unit 直接跳过（不入实体
      索引）。不再持有 ``EntityExtractor``，构造函数删除 ``extractor`` 参数。实体
      抽取职责完全前移到 LLM 写入侧（写入前抽好填进 ``unit.entities``）。

    recall 侧的 boost 逻辑迁到 ``EntityRecaller``，本类只管写入侧维护。
    """

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        admission_policy: EntityIndexAdmissionPolicy | None = None,
        list_limit: int = _DEFAULT_LIST_LIMIT,
    ) -> None:
        self._entity_store = entity_store
        self._admission_policy = admission_policy or EntityIndexAdmissionPolicy()
        self._list_limit = list_limit

    # ------------------------------------------------------------------
    # link_memories：吃 MemoryUnit（不再是 MemoryRecordVector），sync 调用
    # ------------------------------------------------------------------

    def link_memories(self, units: list[MemoryUnit]) -> EntityLinkResult:
        """一批 MemoryUnit 落盘后维护实体反向索引。sync 调用（配合 IndexBuilder 契约）。"""
        if not units:
            return EntityLinkResult()

        admitted: list[tuple[MemoryUnit, str]] = []
        skipped_count = 0
        for unit in units:
            admission = self._admission_policy.decide(unit)
            if not admission.admitted:
                skipped_count += 1
                logger.debug(
                    "entity_link_skipped_by_admission unit_id=%s tier=%s reason=%s",
                    unit.id, unit.tier.value, admission.reason,
                )
                continue
            admitted.append((unit, admission.text))

        if not admitted:
            if skipped_count:
                logger.info("entity_link_no_admitted_records skipped=%d", skipped_count)
            return EntityLinkResult()

        # 抽实体：只消费 unit.entities 明文（LLM 写入前抽好），无 spaCy 兜底。
        # unit.entities 非空时构造 EntityMention（display_name=实体文本，
        # normalized_name 走 normalizer 归一化 + 去重，entity_type 统一 PROPER——
        # LLM 抽的是专名级实体）；为空的 unit 跳过，不入实体索引。
        extracted_by_unit: list[list[EntityMention]] = [[] for _ in admitted]
        for index, (unit, _text) in enumerate(admitted):
            if not unit.entities:
                # 无 LLM 抽取的实体明文 → 跳过该 unit（已砍 spaCy 兜底）。
                logger.debug(
                    "entity_link_skipped_no_entities unit_id=%s", unit.id,
                )
                continue
            # LLM 抽取的实体：display_name=实体文本，normalized_name 走 normalizer
            # 归一化 + 去重，与 _to_mentions 同形态（entity_type 统一 PROPER——
            # LLM 抽的是专名级实体，spaCy 五路里的 NER/专名 span 也产 PROPER）
            seen: set[tuple[str, str]] = set()
            for ent_text in unit.entities:
                normalized = EntityNormalizer.normalize(ent_text)
                if not normalized:
                    continue
                key = ("PROPER", normalized)
                if key in seen:
                    continue
                seen.add(key)
                extracted_by_unit[index].append(
                    EntityMention(
                        entity_type="PROPER",
                        display_name=ent_text,
                        normalized_name=normalized,
                    )
                )

        # 分组：按 (space_id, 隔离三元组)，同组共享一次 bulk 查询/写入
        grouped: dict[tuple[str, tuple], list[tuple[MemoryUnit, int]]] = defaultdict(list)
        for index, (unit, _) in enumerate(admitted):
            filters = EntityStoreFilters.from_scope(unit.scope)  # ← 改吃 Scope
            space_id = space_id_from_scope(unit.scope)  # ← routing 算值
            grouped[(space_id, filters.key())].append((unit, index))

        result = EntityLinkResult()
        for (space_id, _), group in grouped.items():
            group_result = self._link_group(space_id, group, extracted_by_unit)
            result = EntityLinkResult(
                extracted_count=result.extracted_count + group_result.extracted_count,
                inserted_count=result.inserted_count + group_result.inserted_count,
                updated_count=result.updated_count + group_result.updated_count,
                deleted_count=result.deleted_count + group_result.deleted_count,
                failed_count=result.failed_count + group_result.failed_count,
            )
        logger.info(
            "entity_link_complete unit_count=%d extracted=%d inserted=%d updated=%d deleted=%d failed=%d",
            len(units), result.extracted_count, result.inserted_count,
            result.updated_count, result.deleted_count, result.failed_count,
        )
        return result

    def unlink_memory(self, *, scope: Scope, memory_id: str) -> EntityLinkResult:
        """删除记忆时清理 entity 链接。memory_id 是 str（unit.id）。

        scope 同时提供 space_id（routing）和 actor_id（隔离 term）：反查时带
        actor_id filter，只命中调用方 scope 所属的实体文档，避免 space 内
        跨 user 的孤立误删（纵深防御：当前 unit.id 是 UUID4 不会撞，但隔离
        下沉到存储层后，即便未来出现非 UUID 的 id 路径也安全）。
        """
        space_id = space_id_from_scope(scope)
        filters = EntityStoreFilters.from_scope(scope)
        try:
            entities = self._entity_store.find_by_linked_memory_id(
                space_id, memory_id, filters=filters,
            )
        except Exception:
            logger.warning("entity_unlink_lookup_failed space_id=%s memory_id=%s", space_id, memory_id, exc_info=True)
            return EntityLinkResult(failed_count=1)

        # Phase 1: 分类——剩余非空则 UNLINK_UPDATE，空则 DELETE
        pending_ops: list[EntityOperation] = []
        updated_count = 0
        deleted_count = 0
        for entity in entities:
            remaining = tuple(mid for mid in entity.linked_memory_ids if mid != memory_id)
            if remaining:
                pending_ops.append(EntityOperation(
                    type=EntityOpType.UNLINK_UPDATE,
                    record=replace(entity, linked_memory_ids=remaining),
                ))
                updated_count += 1
            else:
                pending_ops.append(EntityOperation(
                    type=EntityOpType.DELETE,
                    record_id=entity.id,
                ))
                deleted_count += 1

        # Phase 2: 一次 bulk 提交所有 update + delete
        failed_count = 0
        if pending_ops:
            try:
                batch_result = self._entity_store.execute_operations(space_id, pending_ops)
            except Exception:
                failed_count = len(pending_ops)
                logger.warning("entity_unlink_batch_failed space_id=%s memory_id=%s op_count=%d",
                               space_id, memory_id, len(pending_ops), exc_info=True)
            else:
                failed_count = len(batch_result.failed_ids)
                for failed_id in batch_result.failed_ids:
                    logger.warning("entity_unlink_failed entity_id=%s space_id=%s memory_id=%s",
                                   str(failed_id), space_id, memory_id)

        return EntityLinkResult(updated_count=updated_count, deleted_count=deleted_count, failed_count=failed_count)

    # ------------------------------------------------------------------
    # _link_group：两级匹配（hash 精确 → INSERT/LINK）
    # ------------------------------------------------------------------

    def _link_group(
        self,
        space_id: str,
        group: list[tuple[MemoryUnit, int]],
        extracted_by_unit: list,
    ) -> EntityLinkResult:
        first_unit = group[0][0]
        filters = EntityStoreFilters.from_scope(first_unit.scope)

        # 归一化 + hash 聚合：同 hash 的不同 unit_id 合并到一个 set
        entities_by_key: dict[str, tuple[str, str, str, set[str]]] = {}
        extracted_count = 0
        for unit, index in group:
            mentions = extracted_by_unit[index] if index < len(extracted_by_unit) else []
            for mention in mentions:
                normalized = EntityNormalizer.normalize(mention.display_name)
                if not normalized:
                    continue
                key = hash_entity_text(normalized)
                extracted_count += 1
                if key not in entities_by_key:
                    entities_by_key[key] = (mention.entity_type, mention.display_name, normalized, {unit.id})
                else:
                    entities_by_key[key][3].add(unit.id)  # ← unit.id（str）存进 set

        if not entities_by_key:
            return EntityLinkResult()

        # 阶段1: hash 精确匹配——命中即 LINK，未命中直接 INSERT（不做向量归并）
        try:
            existing = self._entity_store.find_by_entity_text_hash(
                space_id, tuple(entities_by_key.keys()),
                filters=filters, limit=self._list_limit,
            )
            existing_by_hash = {r.entity_text_hash: r for r in existing if r.entity_text_hash}
        except Exception:
            # 查询失败不能降级成"全 INSERT"——查不到不等于不存在。若置
            # existing_by_hash={} 继续走循环，每个实体会走 INSERT 分支，对已
            # 存在的实体新建重复文档（同 hash 多条 EntityRecord，召回侧
            # find_by_entity_text_hash 命中多条，raw_contrib 累加翻倍，打分失真）。
            # 整组 abort + 计 failed：不造重复副作用，失败可见，下次同实体写入
            # 时查询恢复→命中→LINK 自愈。
            logger.error("entity_exact_lookup_failed space_id=%s entity_count=%d abort group",
                         space_id, len(entities_by_key), exc_info=True)
            return EntityLinkResult(
                extracted_count=extracted_count,
                failed_count=len(entities_by_key),
            )

        # 读 + 分类（per-entity try/except，单条失败不中断全组）
        pending_ops: list[tuple[EntityOperation, str]] = []
        inserted_count = 0
        updated_count = 0
        failed_count = 0
        for key, (entity_type, entity_text, _normalized, memory_ids) in entities_by_key.items():
            try:
                match = existing_by_hash.get(key)
                if match is not None:
                    # LINK：追加新 unit_id（去重已有）
                    ids_to_add = tuple(sorted(set(memory_ids) - set(match.linked_memory_ids), key=str))
                    if ids_to_add:
                        pending_ops.append((
                            EntityOperation(type=EntityOpType.LINK, record_id=match.id, link_memory_ids=ids_to_add),
                            key,
                        ))
                        updated_count += 1
                    continue

                # INSERT：新建 entity 文档（hash 未命中即当新实体，不做向量归并）
                pending_ops.append((
                    EntityOperation(
                        type=EntityOpType.INSERT,
                        record=EntityRecord(
                            id=str(uuid4()),
                            space_id=space_id,  # str，不再 UUID
                            entity_text=entity_text,
                            entity_text_hash=key,
                            entity_type=entity_type,
                            linked_memory_ids=tuple(sorted(memory_ids, key=str)),  # tuple[str]（unit.id）
                            filters=filters,
                        ),
                    ),
                    key,
                ))
                inserted_count += 1
            except Exception:
                failed_count += 1
                logger.warning("entity_link_failed entity_text_hash=%s space_id=%s", key, space_id, exc_info=True)

        # 一次 bulk 提交整组
        if pending_ops:
            ops = [op for op, _ in pending_ops]
            hash_by_id: dict = {}
            for op, key in pending_ops:
                op_id = op.record_id if op.record_id is not None else op.record.id
                hash_by_id[op_id] = key
            try:
                batch_result = self._entity_store.execute_operations(space_id, ops)
            except Exception:
                failed_count += len(pending_ops)
                logger.warning(
                    "entity_link_batch_failed space_id=%s op_count=%d",
                    space_id, len(pending_ops), exc_info=True,
                )
            else:
                failed_count += len(batch_result.failed_ids)
                for failed_id in batch_result.failed_ids:
                    logger.warning("entity_link_failed entity_text_hash=%s space_id=%s record_id=%s",
                                   hash_by_id.get(failed_id), space_id, str(failed_id))

        return EntityLinkResult(
            extracted_count=extracted_count,
            inserted_count=inserted_count,
            updated_count=updated_count,
            failed_count=failed_count,
        )


# ---------------------------------------------------------------------------
# IndexBuilder 实现
# ---------------------------------------------------------------------------


class EntityIndexBuilder(IndexBuilder):
    """实体反向索引构建：MemoryUnit → EntityLinkService 落 ES。

    SUPERSEDE/UPDATE 场景下 unit 内容可能变化，update 走"先 unlink 旧实体链
    接，再 link 新内容实体"的刷新语义。但 SUPERSEDE 里旧 unit 仅 lifecycle 变
    SUPERSEDED（内容未变）时不 unlink——召回侧 UnitReader 的 lifecycle 过滤会
    把失效 unit 滤掉，entity 索引保留链接以支持 as_of 回溯查询；累积的失效
    链接靠定期清理。
    """

    def __init__(self, entity_link_service: EntityLinkService) -> None:
        self._linker = entity_link_service

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        # 本实现只建检索索引，不交付记忆本体：FORWARD_ONLY 即整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        if not units:
            return
        logger.info("EntityIndexBuilder: building entity index for %d units", len(units))
        try:
            result = self._linker.link_memories(units)
        except Exception as exc:
            # entity 是增强层（fulltext/vector 已落盘、真源 KV 已在前置 write
            # 落盘），失败不回滚、不阻断 write——与 update 路径同语义（update 的
            # unlink/link 失败也只 log 不抛）。但失败必须可见：用 error 级别 +
            # 带回 EntityLinkResult 的 failed_count，便于告警与对账。下次同实体
            # 写入时 hash 精确匹配会重新命中并 LINK，有机会自愈。
            logger.error(
                "EntityIndexBuilder: link_memories failed for %d units (entity index "
                "stale, will self-heal on next write): %s", len(units), exc,
                exc_info=True,
            )
            return
        if result.failed_count:
            # 部分失败（如查询超时整组 abort）：不阻断 write，但 error 级别可见。
            logger.error(
                "EntityIndexBuilder: link_memories partial failure for %d units: "
                "extracted=%d inserted=%d updated=%d deleted=%d failed=%d",
                len(units), result.extracted_count, result.inserted_count,
                result.updated_count, result.deleted_count, result.failed_count,
            )

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """增量更新：先 unlink 旧实体链接，再按新内容 link。

        SUPERSEDE 场景下若旧 unit 已是 SUPERSEDED 状态（仅 lifecycle 变化），
        跳过 unlink，靠召回侧 lifecycle 过滤处理（保留链接支持 as_of 回溯）。
        """
        # 本实现只建实体反向索引（检索）：调用方要求只动正排时整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        if not units:
            return
        logger.info("EntityIndexBuilder: updating entity index for %d units", len(units))
        for unit in units:
            if unit.lifecycle == LifecycleState.SUPERSEDED:
                continue
            try:
                self._linker.unlink_memory(
                    scope=unit.scope,
                    memory_id=unit.id,
                )
            except Exception as exc:
                logger.warning("EntityIndexBuilder: unlink_memory failed for unit %s: %s", unit.id[:8], exc)
        try:
            self._linker.link_memories(units)
        except Exception as exc:
            logger.warning("EntityIndexBuilder: link_memories failed in update for %d units: %s", len(units), exc)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        # 同 build：不持有记忆本体，SOFT/HARD 都要移出检索，行为相同。
        if not units:
            return
        logger.info("EntityIndexBuilder: removing entity index for %d units", len(units))
        for unit in units:
            try:
                self._linker.unlink_memory(
                    scope=unit.scope,
                    memory_id=unit.id,
                )
            except Exception as exc:
                logger.warning("EntityIndexBuilder: unlink_memory failed for unit %s: %s", unit.id[:8], exc)

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        """已知 scope 时直接清理 entity 反向索引，避免 lookup。

        与 fulltext/vector 子 builder 同名的便捷方法。unlink_memory 收
        scope + memory_id（scope 内含 space_id routing 和 actor_id 隔离 term），
        因此这里拿 unit_ids + scope 即可逐条清理。修复：原先
        HybridIndexBuilder.remove_with_scope 只委托 fulltext/vector，漏掉 entity，
        会留孤立链接（entity 文档的 linked_memory_ids 仍指向已删 unit_id，
        召回侧会拉回已删记忆）。
        """
        if not unit_ids:
            return
        logger.info("EntityIndexBuilder: removing entity index for %d unit_ids (by scope)", len(unit_ids))
        for unit_id in unit_ids:
            try:
                self._linker.unlink_memory(scope=scope, memory_id=unit_id)
            except Exception as exc:
                logger.warning("EntityIndexBuilder: unlink_memory failed for unit_id %s: %s", unit_id[:8], exc)

    def rebuild(self) -> None:
        return None
