# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""受统一 Storage 管控的原文数据端口。

原文记录在 X-01 的最终 Raw Data 模型确定前继续复用 ``MemoryUnit`` 作为
兼容载荷。端口本身不向上层暴露 ``/messages/``、codec 或底层 KV API；默认
适配器才把这些实现细节翻译到现有 KVStore。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def import MESSAGES_KEY_PREFIX, MemoryUnit, Scope, messages_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.security import (
    PASSTHROUGH_STORE_SECURITY,
    StorageAccessContext,
)


@dataclass(frozen=True)
class RawDataUsage:
    """Space-level raw-data usage returned by the management port."""

    message_count: int = 0
    storage_bytes: int = 0


class RawDataStore(ABC):
    """原文数据的业务端口。

    ``MemoryUnit`` 只是当前 X-01 决策前的兼容记录类型。上层只表达追加、列出
    最近记录和按 id 清理，不接触物理 key、序列化或保留策略实现。
    """

    @abstractmethod
    def append_raw(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        retain_limit: int = 0,
        access: StorageAccessContext | None = None,
    ) -> None:
        """追加原文；``retain_limit=0`` 表示不做数量淘汰。"""

    @abstractmethod
    def list_raw(
        self,
        scope: Scope,
        *,
        limit: int | None = 100,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        """按摄入时间倒序列出最近原文。"""

    @abstractmethod
    def delete_raw(
        self,
        scope: Scope,
        record_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        """按当前 Scope 删除原文记录（幂等）。"""

    def scopes(
        self,
        *,
        access: StorageAccessContext | None = None,
    ) -> list[Scope]:
        """列出包含原文的 Scope，供空间级治理使用。"""
        return []

    def usage(
        self,
        scope: Scope,
        *,
        access: StorageAccessContext | None = None,
    ) -> RawDataUsage:
        """统计一个 Scope 的原文条数与物理字节数。"""
        records = self.list_raw(scope, limit=None, access=access)
        return RawDataUsage(message_count=len(records))

    def purge(
        self,
        scope: Scope,
        *,
        access: StorageAccessContext | None = None,
    ) -> RawDataUsage:
        """清理一个 Scope 的全部原文并返回清理前的用量。"""
        records = self.list_raw(scope, limit=None, access=access)
        usage = RawDataUsage(message_count=len(records))
        self.delete_raw(scope, [record.id for record in records], access=access)
        return usage

    def shares_backend_with(self, backend: Any) -> bool:
        """Return whether this raw port stores bytes in ``backend`` as well.

        The default is deliberately conservative for custom raw stores: an
        implementation must opt in when it can prove that it shares the
        primary KV backend.  This is used only for accounting, not for data
        access or authorization.
        """
        return False


# 便于调用方使用更短的领域名称；``RawDataStore`` 是规范名称。
RawStore = RawDataStore


class KVRawDataStore(RawDataStore):
    """把现有 KV 原语适配为 RawDataStore。

    这里集中拥有 ``/messages/`` 前缀、``MemoryUnit`` codec 和保留策略；Evolver
    不再需要知道这些实现细节。``kv`` 采用鸭子类型以保留旧测试和自定义 KV
    实现的兼容性。
    """

    def __init__(self, kv: Any) -> None:
        self._kv = kv

    def shares_backend_with(self, backend: Any) -> bool:
        """KV-backed raw data shares storage only when the object is identical."""
        return self._kv is backend

    @property
    def security(self):
        return getattr(self._kv, "security", PASSTHROUGH_STORE_SECURITY)

    def health(self) -> None:
        health = getattr(self._kv, "health", None)
        if health is not None:
            health()

    def append_raw(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        retain_limit: int = 0,
        access: StorageAccessContext | None = None,
    ) -> None:
        if retain_limit < 0:
            raise ValueError("retain_limit must be non-negative")
        for unit in units:
            if unit.scope != scope:
                raise ValueError(f"Raw record scope differs from explicit scope: {unit.id}")
            key = messages_key(unit.id)
            value = dumps(unit)
            try:
                self._kv.insert(scope, key, value)
            except ConflictError:
                # A failed extraction may be retried with the same unit id.  Raw
                # persistence is intentionally idempotent for that retry path.
                self._kv.update(scope, key, value)
        if retain_limit <= 0:
            return
        records = self._scan(scope)
        records.sort(key=_ingest_time, reverse=True)
        for record in records[retain_limit:]:
            self._kv.delete(scope, messages_key(record.id))

    def list_raw(
        self,
        scope: Scope,
        *,
        limit: int | None = 100,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        records = self._scan(scope)
        records.sort(key=_ingest_time, reverse=True)
        return records if limit is None else records[:limit]

    def delete_raw(
        self,
        scope: Scope,
        record_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        for record_id in record_ids:
            self._kv.delete(scope, messages_key(record_id))

    def scopes(self, *, access: StorageAccessContext | None = None) -> list[Scope]:
        return self._kv.scopes()

    def usage(
        self,
        scope: Scope,
        *,
        access: StorageAccessContext | None = None,
    ) -> RawDataUsage:
        entries = self._kv.scan(scope, prefix=MESSAGES_KEY_PREFIX)
        return RawDataUsage(
            message_count=len(entries),
            storage_bytes=sum(len(value) for _, value in entries),
        )

    def purge(
        self,
        scope: Scope,
        *,
        access: StorageAccessContext | None = None,
    ) -> RawDataUsage:
        entries = self._kv.scan(scope, prefix=MESSAGES_KEY_PREFIX)
        usage = RawDataUsage(
            message_count=len(entries),
            storage_bytes=sum(len(value) for _, value in entries),
        )
        for key, _value in entries:
            self._kv.delete(scope, key)
        return usage

    def _scan(self, scope: Scope) -> list[MemoryUnit]:
        records: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, prefix=MESSAGES_KEY_PREFIX):
            unit = loads(raw)
            if unit is not None:
                records.append(unit)
        return records


def adapt_raw_data_store(candidate: Any) -> RawDataStore:
    """把旧的 KV 注入值转换成 RawDataStore，已适配端口则原样返回。"""
    if isinstance(candidate, RawDataStore) or all(
        callable(getattr(candidate, name, None))
        for name in ("append_raw", "list_raw", "delete_raw")
    ):
        return candidate
    return KVRawDataStore(candidate)


def _ingest_time(unit: MemoryUnit) -> datetime:
    return unit.temporal.t_ingest or datetime.min.replace(tzinfo=timezone.utc)


__all__ = [
    "RawDataStore",
    "RawStore",
    "RawDataUsage",
    "KVRawDataStore",
    "adapt_raw_data_store",
]
