"""StoreManager/DomainStore 基类默认行为：能力推导、命名端口异常与纯领域接口实现。

F07 拆分后原 ``UnitOnlyStorage`` fake 对应两个独立测试面：管理面无端口 fake
（端口异常与 ``has_*`` 默认推导）+ 数据面纯领域 fake（add/get/list/scopes 闭环）。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.type_def import (
    MemoryUnit,
    RankedStorageResult,
    RecallResult,
    RetrievalPipeline,
    Scope,
    Segment,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.security import AllowAllStorageSecurity, StorageSecurity
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManager
from jiuwen_memory.storage.types import MemoryListResult

pytestmark = pytest.mark.unit


class UnitOnlyStoreManager(StoreManager):
    """不暴露任何底层端口的管理面 fake：端口访问统一抛不支持异常。"""

    @property
    def security(self) -> StorageSecurity:
        return AllowAllStorageSecurity()

    def capabilities(self) -> frozenset[StorageCapability]:
        return frozenset()

    def domain_store(self, name: str = "default") -> DomainStore:
        raise UnsupportedStorageCapabilityError(
            f"domain_store is not available: {name!r}"
        )

    def has_domain_store(self, name: str = "default") -> bool:
        return False

    @staticmethod
    def _unsupported(name: str, port: str) -> Any:
        raise UnsupportedStorageCapabilityError(
            f"storage capability is not available: {port}.{name}"
        )

    def kv(self, name: str = "default") -> Any:
        self._unsupported(name, "kv")

    def vector(self, name: str = "default") -> Any:
        self._unsupported(name, "vector")

    def fulltext(self, name: str = "default") -> Any:
        self._unsupported(name, "fulltext")

    def graph(self, name: str = "default") -> Any:
        self._unsupported(name, "graph")

    def fusion(self, name: str = "default") -> Any:
        self._unsupported(name, "fusion")

    def fs(self, name: str = "default") -> Any:
        self._unsupported(name, "fs")

    def entity(self, name: str = "default") -> Any:
        self._unsupported(name, "entity")

    def health(self) -> None:
        return None


class UnitOnlyDomainStore(DomainStore):
    """只实现 MemoryUnit 领域接口的数据面 fake：不经任何底层 Store。"""

    def __init__(self) -> None:
        self._units: dict[str, MemoryUnit] = {}

    @property
    def security(self) -> StorageSecurity:
        return AllowAllStorageSecurity()

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
    manager = UnitOnlyStoreManager()

    assert manager.capabilities() == frozenset()
    assert not manager.has_kv()
    assert not manager.has_vector()
    assert not manager.has_vector("layers_l0")
    assert not manager.has_entity()
    assert not manager.has_domain_store("default")

    # 未声明端口统一抛 UnsupportedStorageCapabilityError，不抛 NotImplementedError。
    for port, name in (
        (manager.kv, "truth"),
        (manager.vector, "layers_l0"),
        (manager.fulltext, "layers_l1"),
        (manager.graph, "kg"),
        (manager.fusion, "hybrid"),
        (manager.fs, "assets"),
        (manager.entity, "entities"),
    ):
        with pytest.raises(UnsupportedStorageCapabilityError):
            port(name)

    with pytest.raises(UnsupportedStorageCapabilityError):
        manager.domain_store()


def test_domain_store_without_ports_serves_memory_units() -> None:
    scope = Scope(org="org", space="space")
    domain_store = UnitOnlyDomainStore()

    domain_store.add(
        scope, [MemoryUnit(id="u1", scope=scope, segments=[Segment(content="one")])]
    )
    assert [unit.id for unit in domain_store.get(scope, ["u1"])] == ["u1"]
    assert domain_store.list(scope).count == 1
    assert domain_store.scopes() == [scope]

    domain_store.delete(scope, ["u1"])
    assert domain_store.get(scope, ["u1"]) == []
