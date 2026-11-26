"""Failure recovery preserves the private binding of surviving truth."""

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", user="alice")
TEXT = "联系我：13800138000，api_key=private-demo-value"


def test_build_grouped_refreshes_only_receipted_units() -> None:
    """漏项回执时不能按位置把 COMMITTED 套给未确认的 unit 去刷新隐私修订号。"""
    from contextlib import nullcontext

    from jiuwen_memory.construction.index_builder import (
        IndexBuilder,
        UnitCommitState,
        UnitCommitStatus,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder

    class _SpyPrivacy:
        def __init__(self) -> None:
            self.refreshed: list[str] = []

        @staticmethod
        def staging_scope():
            return nullcontext()

        @staticmethod
        def prepare(unit) -> None:
            return None

        def refresh_revision(self, unit) -> None:
            self.refreshed.append(unit.id)

        @staticmethod
        def health() -> None:
            return None

    class _ShortReceiptBuilder(IndexBuilder):
        @staticmethod
        def operator_type():
            from jiuwen_memory.construction.base import OperatorType

            return OperatorType.INDEX_BUILDER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def supports_grouped_build() -> bool:
            return True

        def build_grouped(self, units):
            return [
                UnitCommitStatus(unit_id=units[1].id, state=UnitCommitState.COMMITTED)
            ]

        @staticmethod
        def build(units, *, mode=None) -> None:
            return None

        @staticmethod
        def update(units, *, mode=None) -> None:
            return None

        @staticmethod
        def remove(units, *, mode=None) -> None:
            return None

        @staticmethod
        def rebuild() -> None:
            return None

    spy = _SpyPrivacy()
    builder = PrivacyIndexBuilder(_ShortReceiptBuilder(), spy)
    first = MemoryUnit(id="a", scope=SCOPE, segments=[Segment("first")])
    second = MemoryUnit(id="b", scope=SCOPE, segments=[Segment("second")])

    statuses = builder.build_grouped([first, second])

    assert [status.state for status in statuses] == [
        UnitCommitState.UNKNOWN,
        UnitCommitState.COMMITTED,
    ]
    assert spy.refreshed == ["b"], "只有拿到 COMMITTED 回执的 unit 才刷新修订号"



def test_build_grouped_attaches_refresh_failure_and_keeps_later_units() -> None:
    """隐私修订刷新失败不能吞掉已确认回执，也不能跳过后续 unit。"""
    from contextlib import nullcontext

    from jiuwen_memory.construction.index_builder import (
        IndexBuilder,
        UnitCommitState,
        UnitCommitStatus,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder

    class _FlakyPrivacy:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        @staticmethod
        def staging_scope():
            return nullcontext()

        @staticmethod
        def prepare(unit) -> None:
            return None

        def refresh_revision(self, unit) -> None:
            self.attempts.append(unit.id)
            if unit.id == "a":
                raise RuntimeError("injected revision failure")

        @staticmethod
        def health() -> None:
            return None

    class _TwoCommitted(IndexBuilder):
        @staticmethod
        def operator_type():
            from jiuwen_memory.construction.base import OperatorType

            return OperatorType.INDEX_BUILDER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def supports_grouped_build() -> bool:
            return True

        def build_grouped(self, units):
            return [
                UnitCommitStatus(unit_id=unit.id, state=UnitCommitState.COMMITTED)
                for unit in units
            ]

        @staticmethod
        def build(units, *, mode=None) -> None:
            return None

        @staticmethod
        def update(units, *, mode=None) -> None:
            return None

        @staticmethod
        def remove(units, *, mode=None) -> None:
            return None

        @staticmethod
        def rebuild() -> None:
            return None

    privacy = _FlakyPrivacy()
    builder = PrivacyIndexBuilder(_TwoCommitted(), privacy)
    first = MemoryUnit(id="a", scope=SCOPE, segments=[Segment("first")])
    second = MemoryUnit(id="b", scope=SCOPE, segments=[Segment("second")])

    statuses = builder.build_grouped([first, second])

    assert statuses[0].state is UnitCommitState.COMMITTED
    assert statuses[0].error is not None, "刷新失败必须附到该条回执上"
    assert statuses[1].state is UnitCommitState.COMMITTED
    assert statuses[1].error is None, "后续 unit 不受前一条刷新失败影响"
    assert privacy.attempts == ["a", "b"], "刷新失败不能提前退出循环"



def test_build_grouped_keeps_index_error_when_refresh_also_fails() -> None:
    """刷新失败只归属单条回执：A 看到两类原因，B 只看到原索引错误。"""
    from contextlib import nullcontext

    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.construction.index_builder import (
        IndexBuilder,
        UnitCommitState,
        UnitCommitStatus,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder

    class _FlakyPrivacy:
        @staticmethod
        def staging_scope():
            return nullcontext()

        @staticmethod
        def prepare(unit) -> None:
            return None

        @staticmethod
        def refresh_revision(unit) -> None:
            if unit.id == "a":
                raise RuntimeError("injected revision failure")

        @staticmethod
        def health() -> None:
            return None

    index_error = BackendError("injected retrieval index failure")

    class _CommittedWithError(IndexBuilder):
        @staticmethod
        def operator_type():
            from jiuwen_memory.construction.base import OperatorType

            return OperatorType.INDEX_BUILDER

        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def supports_grouped_build() -> bool:
            return True

        def build_grouped(self, units):
            # 内置 Hybrid 的真实形态：同一个异常实例放进多条回执。
            return [
                UnitCommitStatus(
                    unit_id=unit.id,
                    state=UnitCommitState.COMMITTED,
                    error=index_error,
                )
                for unit in units
            ]

        @staticmethod
        def build(units, *, mode=None) -> None:
            return None

        @staticmethod
        def update(units, *, mode=None) -> None:
            return None

        @staticmethod
        def remove(units, *, mode=None) -> None:
            return None

        @staticmethod
        def rebuild() -> None:
            return None

    builder = PrivacyIndexBuilder(_CommittedWithError(), _FlakyPrivacy())
    first = MemoryUnit(id="a", scope=SCOPE, segments=[Segment("first")])
    second = MemoryUnit(id="b", scope=SCOPE, segments=[Segment("second")])

    statuses = builder.build_grouped([first, second])

    assert statuses[0].state is UnitCommitState.COMMITTED
    assert statuses[0].error is not index_error, "A 的回执需要单条复合错误"
    assert "injected retrieval index failure" in str(statuses[0].error)
    assert "privacy revision refresh also failed" in str(statuses[0].error)
    assert statuses[0].error.__cause__ is index_error
    assert statuses[1].error is index_error, "B 只能看到原索引错误"
    assert not getattr(statuses[1].error, "__notes__", ())


@pytest.mark.parametrize("operation", ["build", "update", "remove", "remove_with_scope"])
@pytest.mark.parametrize("truth_written", [False, True])
def test_builder_failure_keeps_each_surviving_truth_revision(
    privacy, monkeypatch, operation, truth_written
):
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

    raw = InMemoryKVStore()
    kv = PrivacyKVStore(raw, privacy)
    old = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment(TEXT)])
    kv.insert(SCOPE, "/memory/existing", dumps(old))
    forward = ForwardIndexBuilder(CompositeStoreManager(kv=kv))
    write_truth = forward.update
    builder = PrivacyIndexBuilder(PrivacyIndexBuilder(forward, privacy), privacy)
    assert builder.read_domain_store() is forward.read_domain_store()
    changed = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment("新电话13800138001")])

    def fail(units, *args, **kwargs):
        if truth_written and operation in {"build", "update"}:
            # Build may have delivered another unit of the batch before failing.
            write_truth(units)
        raise BackendError("injected index failure")

    if operation == "remove_with_scope":
        monkeypatch.setattr(forward, operation, fail, raising=False)
    else:
        monkeypatch.setattr(forward, operation, fail)
    with pytest.raises(BackendError):
        if operation == "remove_with_scope":
            builder.remove_with_scope([old.id], SCOPE)
        else:
            getattr(builder, operation)([changed])
    surviving = loads(raw.get(SCOPE, "/memory/existing"))
    expected = "新电话13800138001" if truth_written and operation in {"build", "update"} else TEXT
    assert privacy.render(surviving) == expected


def test_successful_projection_only_removal_keeps_truth_private_state(privacy, monkeypatch):
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

    old = MemoryUnit(id="u1", scope=SCOPE, segments=[Segment(TEXT)])
    privacy.persist(old)
    forward = ForwardIndexBuilder(CompositeStoreManager(kv=InMemoryKVStore()))
    monkeypatch.setattr(forward, "remove_with_scope", lambda ids, scope: None, raising=False)
    PrivacyIndexBuilder(forward, privacy).remove_with_scope([old.id], SCOPE)
    assert privacy.render(old) == TEXT


def test_index_only_builder_rejects_unbound_reference_before_delegate(privacy, monkeypatch):
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
    from jiuwen_memory.storage.types import IndexWriteMode

    forward = ForwardIndexBuilder(CompositeStoreManager(kv=InMemoryKVStore()))
    called = []
    monkeypatch.setattr(forward, "build", lambda *args, **kwargs: called.append(True))
    unit = MemoryUnit(
        id="foreign", scope=SCOPE, segments=[Segment("[JW-PRIVATE:" + "a" * 48 + "]")]
    )
    with pytest.raises(ValidationError, match="unbound"):
        PrivacyIndexBuilder(forward, privacy).build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
    assert not called
