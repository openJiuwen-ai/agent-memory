"""文档记忆 IndexBuilder（``DocumentIndexBuilder``）——薄编排层的构造校验与委托。

文档模式下作为 IndexBuilder 装配方，全委托注入的 Storage（storage.add/update/delete
内部按文档模式分流到 md.write + shadow.insert_units）。失效方向：

- 构造期不校验「非文档模式 / 缺 markdown+shadow 端口」，会让首次写入以 AttributeError
  暴露而非清晰报错——真源写错地方。
- ``RETRIEVAL_ONLY`` 语义：文档场景影子索引即真源、无独立检索索引可补建，应整段跳过
  （连 storage.add 都不调）。
- build/update/remove 各委托 storage 一次，逐 unit 下传，mode 原样透传。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.construction.index_builder_impl.document_index_builder import (
    DocumentIndexBuilder,
)
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _RecordingStorage:
    """记录 add/update/delete 调用的假 Storage（文档模式、端口齐全）。"""

    def __init__(
        self,
        *,
        write_document: bool = True,
        has_markdown: bool = True,
        has_shadow: bool = True,
    ) -> None:
        self._write_document = write_document
        self._has_markdown = has_markdown
        self._has_shadow = has_shadow
        self.adds: list[tuple[Scope, list[MemoryUnit], IndexWriteMode]] = []
        self.updates: list[tuple[Scope, list[MemoryUnit], IndexWriteMode]] = []
        self.deletes: list[tuple[Scope, list[str], IndexRemoveMode]] = []

    def should_write_document(self) -> bool:
        return self._write_document

    def has_markdown_port(self, name: str = "default") -> bool:
        return self._has_markdown

    def has_shadow_port(self, name: str = "default") -> bool:
        return self._has_shadow

    def add(self, scope: Scope, units: list[MemoryUnit], *, mode: IndexWriteMode) -> None:
        self.adds.append((scope, units, mode))

    def update(self, scope: Scope, units: list[MemoryUnit], *, mode: IndexWriteMode) -> None:
        self.updates.append((scope, units, mode))

    def delete(self, scope: Scope, unit_ids: list[str], *, mode: IndexRemoveMode) -> None:
        self.deletes.append((scope, unit_ids, mode))


def _unit(uid: str) -> MemoryUnit:
    return MemoryUnit(id=uid, scope=SCOPE, segments=[Segment(content="content")])


# -- 构造校验 ---------------------------------------------------------------- #


def test_non_document_storage_is_rejected_at_construction() -> None:
    """非文档模式（write_document=False）装配即抛，不拖到首次写入。"""
    storage = _RecordingStorage(write_document=False)
    with pytest.raises(UnsupportedStorageCapabilityError, match="write_document"):
        DocumentIndexBuilder(storage)


def test_missing_markdown_or_shadow_port_is_rejected() -> None:
    """文档模式但缺 markdown / shadow 端口同样 fail-closed。"""
    with pytest.raises(UnsupportedStorageCapabilityError, match="markdown"):
        DocumentIndexBuilder(_RecordingStorage(has_markdown=False))
    with pytest.raises(UnsupportedStorageCapabilityError, match="shadow"):
        DocumentIndexBuilder(_RecordingStorage(has_shadow=False))


# -- 委托 -------------------------------------------------------------------- #


def test_build_delegates_each_unit_to_storage_add() -> None:
    storage = _RecordingStorage()
    builder = DocumentIndexBuilder(storage)
    units = [_unit("u1"), _unit("u2")]

    builder.build(units)

    assert len(storage.adds) == 2
    assert [call[1][0].id for call in storage.adds] == ["u1", "u2"]
    assert all(call[2] is IndexWriteMode.ALL for call in storage.adds)


def test_build_retrieval_only_is_a_noop() -> None:
    """RETRIEVAL_ONLY：文档场景无独立检索索引可补建，连 storage.add 都不调。"""
    storage = _RecordingStorage()
    builder = DocumentIndexBuilder(storage)

    builder.build([_unit("u1")], mode=IndexWriteMode.RETRIEVAL_ONLY)

    assert storage.adds == []


def test_update_delegates_each_unit_to_storage_update() -> None:
    storage = _RecordingStorage()
    builder = DocumentIndexBuilder(storage)
    unit = _unit("u1")

    builder.update([unit], mode=IndexWriteMode.FORWARD_ONLY)

    assert len(storage.updates) == 1
    assert storage.updates[0][1][0].id == "u1"
    assert storage.updates[0][2] is IndexWriteMode.FORWARD_ONLY


def test_remove_delegates_ids_to_storage_delete() -> None:
    storage = _RecordingStorage()
    builder = DocumentIndexBuilder(storage)

    builder.remove([_unit("u1"), _unit("u2")], mode=IndexRemoveMode.HARD)

    assert len(storage.deletes) == 2
    assert [call[1] for call in storage.deletes] == [["u1"], ["u2"]]
    assert all(call[2] is IndexRemoveMode.HARD for call in storage.deletes)


def test_rebuild_is_a_noop() -> None:
    """文档真源（md）与影子索引同生命周期，无独立重建路径。"""
    assert DocumentIndexBuilder(_RecordingStorage()).rebuild() is None
