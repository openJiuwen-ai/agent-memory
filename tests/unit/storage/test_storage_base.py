"""Storage 基类默认行为：能力推导、命名端口异常类型与纯领域接口实现。"""

from __future__ import annotations

from typing import Any

import pytest

from common.errors import UnsupportedStorageCapabilityError
from common.type_def import (
    MemoryUnit,
    RankedStorageResult,
    RecallResult,
    RetrievalPipeline,
    Scope,
    Segment,
)
from storage.security import AllowAllStorageSecurity, StorageSecurity
from storage.storage import Storage, StorageCapability
from storage.types import MemoryListResult

pytestmark = pytest.mark.unit


class UnitOnlyStorage(Storage):
    """不暴露任何底层端口的一体化 Storage：只实现 MemoryUnit 领域接口。"""

    def __init__(self) -> None:
        self._units: dict[str, MemoryUnit] = {}

    @property
    def security(self) -> StorageSecurity:
        return AllowAllStorageSecurity()

    def capabilities(self) -> frozenset[StorageCapability]:
        return frozenset()

    @staticmethod
    def _unsupported(name: str) -> Any:
        raise UnsupportedStorageCapabilityError(f"storage capability is not available: {name}")

    @property
    def kv(self) -> Any:
        return self._unsupported("kv")

    @property
    def vector(self) -> Any:
        return self._unsupported("vector")

    @property
    def fulltext(self) -> Any:
        return self._unsupported("fulltext")

    @property
    def graph(self) -> Any:
        return self._unsupported("graph")

    @property
    def fusion(self) -> Any:
        return self._unsupported("fusion")

    @property
    def fs(self) -> Any:
        return self._unsupported("fs")

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return RetrievalPipeline.RECALL_GET_RANK

    def scopes(self) -> list[Scope]:
        found: list[Scope] = []
        for unit in self._units.values():
            if unit.scope not in found:
                found.append(unit.scope)
        return found

    def add(self, scope: Scope, units: list[MemoryUnit], **kwargs: Any) -> None:
        for unit in units:
            self._units[unit.id] = unit

    def update(self, scope: Scope, units: list[MemoryUnit], **kwargs: Any) -> None:
        self.add(scope, units)

    def delete(self, scope: Scope, unit_ids: list[str], **kwargs: Any) -> None:
        for unit_id in unit_ids:
            self._units.pop(unit_id, None)

    def get(self, scope: Scope, unit_ids: list[str], **kwargs: Any) -> list[MemoryUnit]:
        return [self._units[unit_id] for unit_id in unit_ids if unit_id in self._units]

    def list(self, scope: Scope, **kwargs: Any) -> MemoryListResult:
        items = [unit for unit in self._units.values() if unit.scope == scope]
        return MemoryListResult(items=items, count=len(items))

    def recall(self, scope: Scope, query: Any, **kwargs: Any) -> RecallResult:
        return RecallResult()

    def recall_and_get(self, scope: Scope, query: Any, **kwargs: Any) -> RecallResult:
        return RecallResult()

    def retrieve(
        self, scope: Scope, query: Any, fuser: Any, **kwargs: Any
    ) -> RankedStorageResult:
        return RankedStorageResult(candidates=[], errors=[])

    def health(self) -> None:
        return None


def test_base_named_ports_raise_unsupported_capability() -> None:
    storage = UnitOnlyStorage()

    assert storage.capabilities() == frozenset()
    assert not storage.has_kv()
    assert not storage.has_vector()
    assert not storage.has_vector_port("layers_l0")

    # 未声明端口统一抛 UnsupportedStorageCapabilityError，不抛 NotImplementedError。
    for port, name in (
        (storage.kv_port, "truth"),
        (storage.vector_port, "layers_l0"),
        (storage.fulltext_port, "layers_l1"),
        (storage.graph_port, "kg"),
        (storage.fusion_port, "hybrid"),
        (storage.fs_port, "assets"),
    ):
        with pytest.raises(UnsupportedStorageCapabilityError):
            port(name)


def test_storage_without_ports_serves_memory_units_through_domain_api() -> None:
    scope = Scope(org="org", space="space")
    storage = UnitOnlyStorage()

    storage.add(scope, [MemoryUnit(id="u1", scope=scope, segments=[Segment(content="one")])])
    assert [unit.id for unit in storage.get(scope, ["u1"])] == ["u1"]
    assert storage.list(scope).count == 1
    assert storage.scopes() == [scope]

    storage.delete(scope, ["u1"])
    assert storage.get(scope, ["u1"]) == []
