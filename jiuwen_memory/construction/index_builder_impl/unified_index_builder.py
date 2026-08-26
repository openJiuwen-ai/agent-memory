"""统一存储直写的 :class:`~construction.index_builder.IndexBuilder` 实现。

该实现不派生向量、全文等检索索引。它把 ``MemoryUnit`` 按 ``Scope`` 分组后直接交给
注入的 :class:`~storage.storage.Storage`，适用于由统一存储自行负责记忆持久化的装配。
"""

from __future__ import annotations

from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

_ScopeKey = tuple[str, str, str, str, str]


class UnifiedIndexBuilder(IndexBuilder):
    """将构建生命周期直接委托给统一 Storage 的记忆单元写接口。"""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        """转发给 Storage，由其一次性建立自身支持的全部索引形式。

        ``mode`` 原样下传——能否只补建检索索引由该 Storage 实现按自身能力
        决定，本类不代它判断。
        """
        for scope, scoped_units in _group_by_scope(units):
            self._storage.add(scope, scoped_units, mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """转发给 Storage；``mode`` 原样下传，同 :meth:`build`。"""
        for scope, scoped_units in _group_by_scope(units):
            self._storage.update(scope, scoped_units, mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """转发给 Storage；``mode`` 原样下传，同 :meth:`build`。"""
        for scope, scoped_units in _group_by_scope(units):
            self._storage.delete(scope, [unit.id for unit in scoped_units], mode=mode)

    def rebuild(self) -> None:
        # 最小实现：统一存储与真源同生命周期，无独立重建路径。
        return None


def _group_by_scope(units: list[MemoryUnit]) -> list[tuple[Scope, list[MemoryUnit]]]:
    """按 Scope 保持输入顺序分组，满足 Storage 的显式 scope 契约。"""
    groups: dict[_ScopeKey, list[MemoryUnit]] = {}
    scopes: dict[_ScopeKey, Scope] = {}
    for unit in units:
        key = (
            unit.scope.org,
            unit.scope.space,
            unit.scope.user,
            unit.scope.agent,
            unit.scope.session,
        )
        groups.setdefault(key, []).append(unit)
        scopes.setdefault(key, unit.scope)
    return [(scopes[key], scoped_units) for key, scoped_units in groups.items()]


@IndexBuilderProducer.register("unified")
def _build(config):
    return UnifiedIndexBuilder(StorageProducer.resolve(config))
