# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``IndexBuilder.build_grouped`` 的逐条落盘结论（F10 决策 4）。"""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.type_def import memory_key
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.index_builder import (
    IndexBuilder,
    UnitCommitState,
    UnitCommitStatus,
)
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.conftest import make_storage
from tests.unit.construction.fixtures import (
    create_test_plugins,
    create_test_stores,
    create_test_unit,
)

pytestmark = pytest.mark.unit


class _FlakyKV:
    """第 N 次 insert 抛错的 KV 代理，其余调用原样透传。"""

    def __init__(self, inner, *, fail_on: int) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self.inserts = 0

    def insert(self, scope, key, value):
        self.inserts += 1
        if self.inserts == self._fail_on:
            raise RuntimeError("disk full")
        return self._inner.insert(scope, key, value)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RecordingPrivacy:
    """只记录调用的隐私替身；wrapper 只依赖这四个方法。"""

    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.refreshed: list[str] = []

    @staticmethod
    def staging_scope():
        return nullcontext()

    def prepare(self, unit) -> None:
        self.prepared.append(unit.id)

    def refresh_revision(self, unit) -> None:
        self.refreshed.append(unit.id)

    @staticmethod
    def health() -> None:
        return None


class _MinimalBuilder(IndexBuilder):
    """不声明任何批量能力的构建器，用于验证默认值。"""

    def operator_type(self):
        from jiuwen_memory.construction.base import OperatorType

        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units, *, mode=IndexWriteMode.ALL) -> None:
        return None

    def update(self, units, *, mode=IndexWriteMode.ALL) -> None:
        return None

    def remove(self, units, *, mode=IndexRemoveMode.HARD) -> None:
        return None

    def rebuild(self) -> None:
        return None


def test_grouped_build_partial_failure_reports_per_unit() -> None:
    stores = create_test_stores()
    plugins = create_test_plugins()
    flaky = _FlakyKV(stores["kv"], fail_on=2)
    builder = HybridIndexBuilder(
        make_storage(kv=flaky), plugins["chunker"], plugins["embedder"]
    )
    first = create_test_unit("u1", "第一条记忆")
    second = create_test_unit("u2", "第二条记忆")
    third = create_test_unit("u3", "第三条记忆")

    statuses = builder.build_grouped([first, second, third])

    assert [status.unit_id for status in statuses] == ["u1", "u2", "u3"], "结果必须与输入对齐"
    assert [status.state for status in statuses] == [
        UnitCommitState.COMMITTED,
        UnitCommitState.UNKNOWN,
        UnitCommitState.FAILED,
    ], "失败条目为 UNKNOWN，其后未尝试写入的条目为 FAILED"
    assert statuses[1].error is not None, "UNKNOWN 必须带上原始异常"
    restored = loads(stores["kv"].get(first.scope, memory_key(first.id)))
    assert restored is not None and restored.content == first.segments[0].content, (
        "已提交的条目必须能从真源读回"
    )
    from jiuwen_memory.common.errors import NotFoundError

    with pytest.raises(NotFoundError):
        stores["kv"].get(third.scope, memory_key(third.id))


def test_privacy_wrapper_delegates_grouped_capability() -> None:
    stores = create_test_stores()
    plugins = create_test_plugins()
    delegate = HybridIndexBuilder(
        make_storage(kv=stores["kv"]), plugins["chunker"], plugins["embedder"]
    )
    privacy = _RecordingPrivacy()
    wrapped = PrivacyIndexBuilder(delegate, privacy)
    units = [create_test_unit("p1", "隐私记忆一"), create_test_unit("p2", "隐私记忆二")]

    assert wrapped.supports_grouped_build() is True, "包装器不得吞掉被包装对象的能力声明"
    statuses = wrapped.build_grouped(units)

    assert [status.state for status in statuses] == [
        UnitCommitState.COMMITTED,
        UnitCommitState.COMMITTED,
    ], "全部成功时每条都应提交"
    assert privacy.prepared == ["p1", "p2"], "写入前必须对全部条目做隐私准备"
    assert privacy.refreshed == ["p1", "p2"], "只有已提交条目才刷新修订号"


def test_privacy_wrapper_skips_revision_for_uncommitted_units() -> None:
    class _FixedDelegate(_MinimalBuilder):
        def supports_grouped_build(self) -> bool:
            return True

        def build_grouped(self, units):
            return [
                UnitCommitStatus(unit_id=units[0].id, state=UnitCommitState.COMMITTED),
                UnitCommitStatus(
                    unit_id=units[1].id,
                    state=UnitCommitState.UNKNOWN,
                    error=RuntimeError("boom"),
                ),
            ]

    privacy = _RecordingPrivacy()
    wrapped = PrivacyIndexBuilder(_FixedDelegate(), privacy)
    units = [create_test_unit("q1", "一"), create_test_unit("q2", "二")]

    wrapped.build_grouped(units)

    assert privacy.refreshed == ["q1"], "未提交的条目不得刷新已持久化修订号"


def test_default_builder_does_not_support_grouped() -> None:
    builder = _MinimalBuilder()

    assert builder.supports_grouped_build() is False, "默认不支持批量建索引"
    with pytest.raises(UnsupportedCapabilityError):
        builder.build_grouped([create_test_unit()])


def test_unattempted_units_after_failure_carry_reason() -> None:
    """首个失败之后未尝试的 unit 必须带错误原因，不能是 FAILED + error=None。"""
    stores = create_test_stores()
    plugins = create_test_plugins()
    builder = HybridIndexBuilder(
        make_storage(kv=_FlakyKV(stores["kv"], fail_on=1)),
        plugins["chunker"],
        plugins["embedder"],
    )
    units = [create_test_unit(name, f"{name} 的记忆") for name in ("a", "b", "c")]

    statuses = builder.build_grouped(units)

    assert statuses[0].state is UnitCommitState.UNKNOWN
    for status in statuses[1:]:
        assert status.state is UnitCommitState.FAILED
        assert status.error is not None, "未尝试写入的条目也要能被上层诊断"
        assert "a" in str(status.error), "错误里要指出前一个失败者"
