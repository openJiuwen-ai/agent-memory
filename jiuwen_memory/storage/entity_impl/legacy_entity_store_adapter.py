# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""旧签名 ``EntityStore`` 的兼容适配器（F09 D9，过渡一个发布周期）。

F09 把 ``EntityStore`` 首参从 ``space_id: str`` 改为 ``scope: Scope``，并新增
``UNLINK`` / ``DELETE_IF_EMPTY``。第三方实现需要一个发布周期的迁移窗口：

- 本适配器把新契约（Scope）翻译成旧签名（``space_id_from_scope``）；
- 收到非空 ``shards`` 时抛「不支持」错误；
- ``UNLINK`` / ``DELETE_IF_EMPTY`` 用「读出 → 修改 → 写回」模拟，存在与现状相同的
  竞态，因此**不声明** ``ATOMIC_UNLINK``（``shard_routed`` 包装层会拒绝用它）；
- 构造时发 :class:`DeprecationWarning`，下一个大版本移除。

实现只在未分片部署中使用；分片部署必须迁到新签名。

**删除条件**：所有受支持的 ``EntityStore`` 实现都继承当前 ABC（即
``CONTRACT_VERSION >= 2``）后，删除本文件、``legacy`` target 与 ``_contract_version``
分支；在此之前它只做旧契约 → 新契约的翻译，不承载新能力（S09 #14）。
"""

from __future__ import annotations

import warnings
from typing import Any

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
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
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.entity_store import EntityStore, EntityStoreProducer


def _contract_version(store: Any) -> int:
    """按 ``EntityStore.CONTRACT_VERSION`` 判定实现是否已是新契约。

    旧签名实现在当前 ABC 之前编写，不会继承这个类属性 → 视为 v1；新实现只要继承
    ``EntityStore`` 就自动是 v2。不读方法签名、不按 target 名判断（S09 #9）。
    """
    declared = getattr(type(store), "CONTRACT_VERSION", None)
    return int(declared) if isinstance(declared, int) else 1


class LegacyEntityStoreAdapter(EntityStore):
    """把旧签名实现包装成新 ``EntityStore`` 契约（非原子）。"""

    def __init__(self, legacy: Any, *, component: str = "") -> None:
        warnings.warn(
            f"{component or type(legacy).__name__} uses the deprecated EntityStore "
            "signature (space_id: str); migrate to scope: Scope within one release",
            DeprecationWarning,
            stacklevel=2,
        )
        self._legacy = legacy

    def store_type(self) -> StoreType:
        return self._legacy.store_type()

    def health(self) -> None:
        self._legacy.health()

    def ensure_index(self) -> None:
        self._legacy.ensure_index()

    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        limit: int = 500,
    ) -> list[EntityRecord]:
        self._reject_shards(shards)
        return self._legacy.find_by_entity_text_hash(
            space_id_from_scope(scope),
            entity_text_hashes,
            filters=filters,
            limit=limit,
        )

    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        complete: bool = False,
    ) -> list[EntityRecord]:
        self._reject_shards(shards)
        records = self._legacy.find_by_linked_memory_id(
            space_id_from_scope(scope), memory_id, filters=filters
        )
        if complete:
            # 旧后端没有「完整枚举」契约；不能把可能的截断结果当成完整集合。
            raise UnsupportedStorageCapabilityError(
                "legacy entity store cannot guarantee complete linked-memory "
                "enumeration required for relocation"
            )
        return records

    def execute_operations(
        self, scope: Scope, operations: list[EntityOperation]
    ) -> EntityBatchResult:
        """逐条翻译并提交；无法履约的操作必须**如实报失败**，不能静默丢弃。

        旧签名没有「按 id 读记录」的能力，``DELETE_IF_EMPTY`` 既不能原子判空，也无法
        用「读出为空 → 删除」模拟。把它当成成功返回会让调用方以为清理已完成（S09
        #12 禁止把未执行的操作报成成功）；这里把它计入 ``failed_ids``，空记录不被
        查询返回，由对账清理——与「执行阶段清理失败如实报告为部分完成」一致。
        """
        translated: list[EntityOperation] = []
        unsupported: list[str] = []
        for op in operations:
            if op.type is EntityOpType.UNLINK:
                translated.extend(self._simulate_unlink(scope, op))
            elif op.type is EntityOpType.DELETE_IF_EMPTY:
                unsupported.append(op.record_id or "")
            else:
                translated.append(op)
        successful: list[str] = []
        failed: list[str] = list(unsupported)
        if translated:
            result = self._legacy.execute_operations(
                space_id_from_scope(scope), translated
            )
            successful.extend(result.successful_ids)
            failed.extend(result.failed_ids)
        return EntityBatchResult(successful_ids=successful, failed_ids=failed)

    def search(
        self,
        scope: Scope,
        query_vector: list[float],
        *,
        top_k: int,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
    ) -> list[EntitySearchResult]:
        self._reject_shards(shards)
        return self._legacy.search(
            space_id_from_scope(scope), query_vector, top_k=top_k, filters=filters
        )

    def _simulate_unlink(
        self, scope: Scope, operation: EntityOperation
    ) -> list[EntityOperation]:
        """旧路径：读出完整关联 → 去掉一条 → 整体替换 / 为空则删。"""
        if operation.record_id is None or not operation.unlink_memory_ids:
            return []
        targets = set(operation.unlink_memory_ids)
        records = self._legacy.find_by_linked_memory_id(
            space_id_from_scope(scope),
            next(iter(targets)),
            filters=EntityStoreFilters(),
        )
        out: list[EntityOperation] = []
        for record in records:
            remaining = tuple(
                memory_id
                for memory_id in record.linked_memory_ids
                if memory_id not in targets
            )
            if remaining:
                out.append(
                    EntityOperation(
                        type=EntityOpType.UNLINK_UPDATE,
                        record=EntityRecord(
                            id=record.id,
                            space_id=record.space_id,
                            entity_text=record.entity_text,
                            entity_type=record.entity_type,
                            linked_memory_ids=remaining,
                            filters=record.filters,
                            entity_text_hash=record.entity_text_hash,
                            embedding=record.embedding,
                        ),
                    )
                )
            else:
                out.append(
                    EntityOperation(type=EntityOpType.DELETE, record_id=record.id)
                )
        return out

    @staticmethod
    def _reject_shards(shards: frozenset[str] | None) -> None:
        if shards:
            raise UnsupportedStorageCapabilityError(
                "legacy EntityStore adapter does not support shards filtering"
            )


@EntityStoreProducer.register("legacy")
def _build(config):
    raw = config.params.get("legacy_store")
    if raw is None:
        raise UnsupportedStorageCapabilityError(
            "legacy entity adapter requires params.legacy_store"
        )
    if isinstance(raw, (str, dict)):
        legacy = EntityStoreProducer.dep(config, "legacy_store")
    else:
        legacy = raw
    if _contract_version(legacy) >= EntityStore.CONTRACT_VERSION:
        return legacy
    return LegacyEntityStoreAdapter(legacy, component=str(config.get("component", "")))
