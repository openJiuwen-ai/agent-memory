# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Composer 专项测试：真实内存索引与仅用于记录/故障注入的公开包装器。"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
    HierarchyComposeRequest,
)
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.unit.construction.fixtures import create_test_plugins, create_test_stores

ORIGIN = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)


def make_leaf(uid: str, minute: int = 0, *, scope: Scope | None = None) -> MemoryUnit:
    """构造带原始事实字段的 snapshot。"""
    start = ORIGIN + timedelta(minutes=minute)
    return MemoryUnit(
        id=uid,
        scope=scope or Scope(
            org="org", space="space", user="alice", agent="assistant", session="s1"
        ),
        segments=[Segment(content=f"事实 {uid}：完成开发与验证", assets=[f"asset:{uid}"])],
        temporal=Temporal(t_event=start, t_message=start, t_ingest=start, t_valid=start),
        source_ref=f"raw:{uid}",
        provenance=[f"source:{uid}"],
        tags=["original"],
        system_metadata={"device_id": "laptop", "infer": "false"},
        user_metadata={"project": "memory"},
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME,
            role=HierarchyRole.SNAPSHOT,
            span_start=start,
            span_end=start,
        ),
    )


def make_request(
    leaves: list[MemoryUnit],
    *,
    home_scope: Scope | None = None,
    span: tuple[datetime, datetime] | None = None,
    parents: list[MemoryUnit] | None = None,
) -> HierarchyComposeRequest:
    """构造首次构建或显式区间替换请求。"""
    destination = deepcopy(home_scope if home_scope is not None else leaves[0].scope)
    if home_scope is None:
        destination.session = ""
    return HierarchyComposeRequest(
        leaves=leaves,
        existing_parents=parents or [],
        options=HierarchyComposeOptions(
            kind=HierarchyKind.TIME,
            leaf_role=HierarchyRole.SNAPSHOT,
            parent_roles=[HierarchyRole.TIME_SPAN],
            tree_home_scope=destination,
            span_start=span[0] if span else None,
            span_end=span[1] if span else None,
            metadata={"build_source": "manual"},
        ),
    )


@dataclass
class BuilderCall:
    method: str
    mode: IndexWriteMode | IndexRemoveMode
    units: list[MemoryUnit]


class RecordingIndexBuilder(IndexBuilder):
    """委托真实 builder，保留公开调用记录并可在指定调用之前抛出异常。"""

    def __init__(self, delegate: IndexBuilder | None = None) -> None:
        self.delegate = delegate
        self.calls: list[BuilderCall] = []
        self.fail_at: int | None = None

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.INDEX_BUILDER

    @staticmethod
    def health() -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        self._record("build", units, mode)
        if self.delegate is not None:
            self.delegate.build(units, mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """记录公开更新，并原样委托。"""
        self._record("update", units, mode)
        if self.delegate is not None:
            self.delegate.update(units, mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """记录公开删除，并原样委托。"""
        self._record("remove", units, mode)
        if self.delegate is not None:
            self.delegate.remove(units, mode=mode)

    def rebuild(self) -> None:
        if self.delegate is not None:
            self.delegate.rebuild()

    def _record(
        self, method: str, recorded_units: list[MemoryUnit], mode: IndexWriteMode | IndexRemoveMode
    ) -> None:
        self.calls.append(BuilderCall(method, mode, deepcopy(recorded_units)))
        if self.fail_at == len(self.calls):
            raise RuntimeError(f"injected {method} {mode.value} failure")


@dataclass
class CompositionHarness:
    composer: DefaultHierarchyComposer
    builder: RecordingIndexBuilder
    manager: CompositeStoreManager

    def read(self, scope: Scope, uid: str) -> MemoryUnit:
        return loads(self.manager.kv().get(scope, memory_key(uid)))


def make_harness(
    leaves: list[MemoryUnit],
    *,
    profiles: dict[HierarchyKind, HierarchyComposeProfile] | None = None,
    allow_cross_user: bool = False,
) -> CompositionHarness:
    """预先写入权威叶，Composer 后续用同一真实 Hybrid builder。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    manager = CompositeStoreManager(**stores)
    real_builder = HybridIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    real_builder.build(leaves)
    recorder = RecordingIndexBuilder(real_builder)
    composer = DefaultHierarchyComposer(recorder, profiles or {}, allow_cross_user)
    return CompositionHarness(composer, recorder, manager)


def replacement_request(
    harness: CompositionHarness, original: HierarchyComposeRequest, parent_ids: list[str]
) -> HierarchyComposeRequest:
    """通过公开存储读取最新叶和父，模拟调用方备齐重建输入。"""
    current_leaves = [harness.read(leaf.scope, leaf.id) for leaf in original.leaves]
    home = original.options.tree_home_scope
    current_parents = [harness.read(home, parent_id) for parent_id in parent_ids]
    return make_request(
        current_leaves,
        home_scope=home,
        span=(ORIGIN, ORIGIN + timedelta(hours=1)),
        parents=current_parents,
    )
