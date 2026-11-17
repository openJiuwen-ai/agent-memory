# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 D16：改路由字段后的自动搬迁（状态机、可见性与回滚）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    RelocationInProgressError,
    ValidationError,
)
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import (
    InMemoryFulltextStore,
)
from jiuwen_memory.storage.fulltext_impl.shard_routed_fulltext import (
    ShardRoutedFulltextStore,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.shard_placement import (
    RELOCATION_INDEX_PREFIX,
    PlacementRecord,
    PlacementStatus,
    placement_key,
    relocation_index_key,
)
from jiuwen_memory.storage.kv_impl.shard_relocation import content_digest
from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore
from jiuwen_memory.storage.shard_routing import (
    RuleBasedShardResolver,
)
from jiuwen_memory.storage.types import Document, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from jiuwen_memory.storage.vector_impl.shard_routed_vector import (
    ShardRoutedVectorStore,
)
from tests.support.whitebox import private, private_method

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile")


class ClockKV(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.t = 1_000.0

    def now(self) -> float:
        return self.t


def resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=list(SHARDS),
        default_shard="working",
        rules=[{"shard_from": "system_metadata.shard"}],
    )


def unit(uid: str = "u1", *, shard: str | None = "working", content: str = "alpha") -> MemoryUnit:
    metadata = {} if shard is None else {"shard": shard}
    return MemoryUnit(
        id=uid,
        scope=SCOPE,
        tier=MemoryTier.CORE,
        system_metadata=metadata,
        segments=[Segment(content=content)],
    )


def _bind(wrapper, kv) -> None:
    bind = getattr(private(wrapper, "_locator"), "bind", None)
    if callable(bind):
        bind(kv)


def build(*, relocate: bool = True, ports: tuple = (), placement=None, **kv_kwargs):
    placement = ClockKV() if placement is None else placement
    stores = {name: InMemoryKVStore() for name in SHARDS}
    kv = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores=stores,
        relocate_on_update=relocate,
        **kv_kwargs,
    )
    vector = ShardRoutedVectorStore(
        resolver=resolver(),
        locator=private(kv, "_locator"),
        mode="split",
        stores={name: InMemoryVectorStore() for name in SHARDS},
    )
    fulltext = ShardRoutedFulltextStore(
        resolver=resolver(),
        locator=private(kv, "_locator"),
        mode="split",
        stores={name: InMemoryFulltextStore(WhitespaceTokenizer()) for name in SHARDS},
    )
    _bind(vector, kv)
    _bind(fulltext, kv)
    kv.bind_relocatable_ports(ports or (vector, fulltext))
    return kv, stores, placement, vector, fulltext


def _write_unit(kv, stores, uid: str = "u1", *, shard: str | None = "working", ttl: float = 0.0):
    memory = unit(uid, shard=shard)
    kv.insert(SCOPE, memory_key(uid), dumps(memory), ttl=ttl)
    return memory


def _write_indexes(vector, fulltext, uid: str = "u1") -> None:
    vector.insert(
        SCOPE,
        [VectorRecord(id=f"{uid}-0", vector=[1.0, 0.0], metadata={"unit_id": uid})],
    )
    fulltext.insert(
        SCOPE, [Document(id=uid, text="alpha", metadata={"unit_id": uid})]
    )


def _record(placement, uid: str = "u1") -> PlacementRecord:
    raw = placement.read_many(SCOPE, [placement_key(uid)])[placement_key(uid)]
    return PlacementRecord.from_bytes(raw)


# ---------------------------------------------------------------------------
# 归属记录 / 触发规则
# ---------------------------------------------------------------------------


def test_placement_bytes_unchanged_when_relocation_fields_absent() -> None:
    legacy = PlacementRecord("working", PlacementStatus.ACTIVE, "t", 1.0)
    assert legacy.to_bytes() == (
        b'{"created_at":1.0,"shard":"working","status":"active","token":"t"}'
    )
    assert PlacementRecord.from_bytes(legacy.to_bytes()) == legacy


def test_resolve_on_update_rules() -> None:
    r = resolver()
    old = unit(shard="working")
    assert r.resolve_on_update(old, unit(shard="profile"), "working") == "profile"
    # 字段被删除：不搬
    assert r.resolve_on_update(old, unit(shard=None), "working") is None
    # 目标等于当前：不搬
    assert r.resolve_on_update(old, unit(shard="working"), "working") is None
    # 未知目标：on_unknown=reject
    with pytest.raises(ValidationError):
        r.resolve_on_update(old, unit(shard="ghost"), "working")


# ---------------------------------------------------------------------------
# update 打标记 / 搬迁中的可见性与写入
# ---------------------------------------------------------------------------


def test_update_marks_relocation_only_when_target_changes() -> None:
    kv, stores, placement, vector, fulltext = build()
    _write_unit(kv, stores)
    _write_indexes(vector, fulltext)

    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE
    assert record.relocate_to == "profile"

    # 再改回原分片：清除标记
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="working")))
    assert _record(placement).relocate_to is None


def test_field_removal_does_not_mark_relocation() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard=None)))
    assert _record(placement).relocate_to is None


def test_relocation_disabled_keeps_sticky_behaviour() -> None:
    kv, stores, placement, _, _ = build(relocate=False)
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    assert _record(placement).relocate_to is None
    assert kv.locate(SCOPE, ["u1"]).found == {"u1": "working"}


def test_moving_blocks_update_and_delete_but_reads_source() -> None:
    kv, stores, placement, _, _ = build()
    memory = _write_unit(kv, stores)
    active = _record(placement)
    moving = PlacementRecord(
        shard="working",
        status=PlacementStatus.MOVING,
        token="m",
        created_at=placement.now(),
        relocate_to="profile",
        step="copy_kv",
    )
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )

    assert kv.get(SCOPE, memory_key("u1")) == dumps(memory)
    location = kv.locate(SCOPE, ["u1"])
    assert location.found == {"u1": "working"}
    assert location.moving == frozenset({"u1"})
    with pytest.raises(RelocationInProgressError):
        kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    with pytest.raises(RelocationInProgressError):
        kv.delete(SCOPE, memory_key("u1"))
    with pytest.raises(ConflictError):
        kv.insert(SCOPE, memory_key("u1"), dumps(memory))


def test_derived_write_during_moving_is_rejected() -> None:
    kv, stores, placement, vector, _ = build()
    _write_unit(kv, stores)
    _write_indexes(vector, ShardRoutedFulltextStore(
        resolver=resolver(),
        locator=private(kv, "_locator"),
        mode="split",
        stores={name: InMemoryFulltextStore(WhitespaceTokenizer()) for name in SHARDS},
    ))
    active = _record(placement)
    moving = PlacementRecord(
        shard="working",
        status=PlacementStatus.MOVING,
        token="m",
        created_at=placement.now(),
        relocate_to="profile",
        step="copy_derived",
    )
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )
    with pytest.raises(RelocationInProgressError):
        vector.insert(
            SCOPE,
            [VectorRecord(id="u1-1", vector=[1.0, 0.0], metadata={"unit_id": "u1"})],
        )


# ---------------------------------------------------------------------------
# relocate_pending：正常搬迁 / 派生记录 / TTL
# ---------------------------------------------------------------------------


def test_relocate_pending_moves_kv_and_derived_records() -> None:
    kv, stores, placement, vector, fulltext = build()
    _write_unit(kv, stores, ttl=60.0)
    _write_indexes(vector, fulltext)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")), ttl=60.0)

    assert kv.relocate_pending(limit=10).skipped == 1, "default min_age defers fresh marks"
    report = kv.relocate_pending(limit=10, min_age=0)

    assert report.moved == 1 and report.failed == 0
    assert kv.locate(SCOPE, ["u1"]).found == {"u1": "profile"}
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE
    assert record.relocate_to is None and record.cleanup_from is None
    assert stores["working"].exists(SCOPE, memory_key("u1")) is False
    assert stores["profile"].exists(SCOPE, memory_key("u1"))
    assert 0 < (stores["profile"].remaining_ttl(SCOPE, memory_key("u1")) or 0) <= 60.0

    # 派生记录：源分片清空、目标分片复制（embedding / 正文原样）
    assert private(vector, "_stores")["working"].record_ids_for_units(SCOPE, ["u1"]) == {}
    assert private(vector, "_stores")["profile"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1-0"]
    }
    assert private(fulltext, "_stores")["working"].record_ids_for_units(SCOPE, ["u1"]) == {}
    assert private(fulltext, "_stores")["profile"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1"]
    }


class _BrokenPort:
    """声明可搬迁但复制必失败：用于验证回滚。"""

    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    def relocate_unit(self, scope, unit_id, source, target) -> None:
        raise self._cause

    @staticmethod
    def cleanup_unit(scope, unit_id, shard) -> None:
        return None


def test_relocate_pending_rolls_back_on_derived_failure() -> None:
    kv, stores, placement, vector, fulltext = build(
        ports=(_BrokenPort(RuntimeError("boom")),)
    )
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")), ttl=60.0)

    report = kv.relocate_pending(limit=10, min_age=0)

    assert report.failed == 1 and report.moved == 0
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE
    assert record.shard == "working"
    assert record.relocate_to == "profile"
    assert not stores["profile"].exists(SCOPE, memory_key("u1"))
    assert stores["working"].exists(SCOPE, memory_key("u1"))


def test_stale_moving_is_only_recovered_in_maintenance_mode() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    active = _record(placement)
    moving = PlacementRecord(
        shard="working",
        status=PlacementStatus.MOVING,
        token="m",
        created_at=placement.now() - 10_000,
        relocate_to="profile",
        step="copy_kv",
    )
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )
    # 真实流程中打标记时已登记待搬迁索引；这里直接构造遗留 moving，同样补上索引。
    placement.create_if_absent(SCOPE, relocation_index_key("u1"), b"0:seed")

    online = kv.relocate_pending(limit=10)
    assert online.stale_moving == 1 and online.moved == 0
    assert _record(placement).status is PlacementStatus.MOVING

    recovered = kv.relocate_pending(limit=10, recover_stale=True)
    assert recovered.moved == 1
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE
    assert record.shard == "working" and record.relocate_to == "profile"


def test_relocate_pending_requires_enabled_switch() -> None:
    kv, _, _, _, _ = build(relocate=False)
    with pytest.raises(ValidationError):
        kv.relocate_pending()


def test_repartition_reuses_relocation_for_derived_records() -> None:
    kv, stores, placement, vector, fulltext = build()
    _write_unit(kv, stores)
    _write_indexes(vector, fulltext)

    final = kv.repartition_unit(SCOPE, "u1", "profile")

    assert final == "profile"
    assert _record(placement).shard == "profile"
    assert private(vector, "_stores")["profile"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1-0"]
    }
    assert private(fulltext, "_stores")["profile"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1"]
    }
    assert private(vector, "_stores")["working"].record_ids_for_units(SCOPE, ["u1"]) == {}


# ---------------------------------------------------------------------------
# 并发、待搬迁索引、失败上限与重新分区
# ---------------------------------------------------------------------------


def _start_moving(placement, uid: str = "u1") -> None:
    current = _record(placement, uid)
    moving = PlacementRecord(
        shard=current.shard,
        status=PlacementStatus.MOVING,
        token="relocator",
        created_at=placement.now(),
        relocate_to="profile",
        step="copy_kv",
    )
    placement.replace_if_value(
        SCOPE, placement_key(uid), current.to_bytes(), moving.to_bytes()
    )


def test_update_racing_with_relocation_start_asks_caller_to_retry(monkeypatch) -> None:
    """读到 active 之后、写数据之前开始搬迁：写入可能落在已复制的源分片，必须报可重试。"""
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    original = stores["working"].update

    def racing_update(scope, key, value, ttl=0.0):
        _start_moving(placement)
        original(scope, key, value, ttl)

    monkeypatch.setattr(stores["working"], "update", racing_update)
    with pytest.raises(RelocationInProgressError):
        kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile", content="beta")))


class _SourceWritingPort:
    """复制派生记录时模拟一次并发 update 改写源分片数据。"""

    def __init__(self, stores) -> None:
        self._stores = stores

    def relocate_unit(self, scope, unit_id, source, target) -> None:
        self._stores[source].update(
            scope, memory_key(unit_id), dumps(unit(unit_id, shard="profile", content="late"))
        )

    @staticmethod
    def cleanup_unit(scope, unit_id, shard) -> None:
        return None


def test_source_changed_after_copy_rolls_back_without_losing_the_write() -> None:
    placement = ClockKV()
    stores = {name: InMemoryKVStore() for name in SHARDS}
    kv = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores=stores,
        relocate_on_update=True,
    )
    kv.bind_relocatable_ports((_SourceWritingPort(stores),))
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))

    report = kv.relocate_pending(min_age=0)

    assert report.moved == 0 and report.skipped == 1, str(report)
    record = _record(placement)
    assert record.shard == "working" and record.relocate_to == "profile"
    assert record.failures is None, "a concurrent write is not a relocation failure"
    assert not stores["profile"].exists(SCOPE, memory_key("u1")), "stale copy removed"
    assert b"late" in stores["working"].get(SCOPE, memory_key("u1")), "late write kept"


def test_relocate_pending_only_scans_the_relocation_index(monkeypatch) -> None:
    kv, stores, placement, _, _ = build()
    for index in range(20):
        _write_unit(kv, stores, uid=f"idle-{index}")
    _write_unit(kv, stores, uid="u1")
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    scanned: list[str] = []
    original = placement.scan_entries

    def spy(scope, prefix=""):
        scanned.append(prefix)
        return original(scope, prefix)

    monkeypatch.setattr(placement, "scan_entries", spy)
    report = kv.relocate_pending(min_age=0)

    assert report.moved == 1
    assert scanned and all(prefix == RELOCATION_INDEX_PREFIX for prefix in scanned), scanned
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]) == {}, "index dropped"


def test_stale_index_is_dropped_only_after_stale_after() -> None:
    kv, stores, placement, _, _ = build(stale_after=100.0)
    _write_unit(kv, stores)
    fresh = f"{placement.now():.6f}:fresh".encode()
    placement.create_if_absent(SCOPE, relocation_index_key("u1"), fresh)

    kv.relocate_pending(min_age=0)
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]), "fresh index kept"

    placement.t += 1_000
    kv.relocate_pending(min_age=0)
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]) == {}


def test_repeated_failures_abandon_the_mark() -> None:
    kv, stores, placement, _, _ = build(
        ports=(_BrokenPort(RuntimeError("boom")),), relocate_max_failures=2
    )
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))

    first = kv.relocate_pending(min_age=0)
    assert first.failed == 1 and _record(placement).failures == 1
    second = kv.relocate_pending(min_age=0)

    assert second.abandoned == 1
    record = _record(placement)
    assert record.shard == "working" and record.relocate_to is None
    assert record.failures is None
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]) == {}


def test_repartition_rolls_back_when_derived_copy_fails() -> None:
    kv, stores, placement, _, _ = build(ports=(_BrokenPort(RuntimeError("boom")),))
    _write_unit(kv, stores)

    with pytest.raises(ConflictError):
        kv.repartition_unit(SCOPE, "u1", "profile")

    record = _record(placement)
    assert record.shard == "working" and record.status is PlacementStatus.ACTIVE
    assert not stores["profile"].exists(SCOPE, memory_key("u1"))
    assert stores["working"].exists(SCOPE, memory_key("u1"))


# ---------------------------------------------------------------------------
# 评审问题回归：清理独占、清理保留新标记、索引版本、标记与数据版本
# ---------------------------------------------------------------------------


class _HookPort:
    """在 cleanup_unit(源分片) 时执行回调，用来在「清理已认领、尚未删除」时插入并发操作。"""

    def __init__(self, on_cleanup) -> None:
        self._on_cleanup = on_cleanup
        self.fired = False

    @staticmethod
    def relocate_unit(scope, unit_id, source, target) -> None:
        return None

    def cleanup_unit(self, scope, unit_id, shard) -> None:
        if not self.fired:
            self.fired = True
            self._on_cleanup(shard)


def test_claimed_cleanup_blocks_concurrent_cleanup_reuse_and_delete() -> None:
    """P1-1：清理持有 claim 期间，别的执行者不能清理、不能搬回源分片、不能删除。"""
    observations: dict[str, object] = {}

    def concurrent(shard: str) -> None:
        assert shard == "working"
        record = _record(placement)
        observations["claim"] = record.claim
        observations["other_round"] = kv.relocate_pending(min_age=0)
        with pytest.raises(ConflictError):
            kv.repartition_unit(SCOPE, "u1", "working")
        with pytest.raises(RelocationInProgressError):
            kv.delete(SCOPE, memory_key("u1"))

    hook = _HookPort(concurrent)
    kv, stores, placement, _, _ = build(ports=(hook,))
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))

    report = kv.relocate_pending(min_age=0)

    assert hook.fired and observations["claim"] is not None
    other = observations["other_round"]
    assert other.moved == 0, f"no second cleaner may proceed while claimed: {other}"
    assert report.moved == 1, str(report)
    record = _record(placement)
    assert record.shard == "profile" and record.claim is None and record.cleanup_from is None
    assert kv.exists(SCOPE, memory_key("u1")), "current data must survive"
    assert not stores["working"].exists(SCOPE, memory_key("u1"))


class _FlakyCleanupPort:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def relocate_unit(scope, unit_id, source, target) -> None:
        return None

    def cleanup_unit(self, scope, unit_id, shard) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient cleanup failure")


def test_cleanup_keeps_a_relocation_requested_while_cleanup_was_pending() -> None:
    """P2-4（无并发）：清理失败后又来的新搬迁请求，不能被下一轮清理抹掉。"""
    port = _FlakyCleanupPort()
    kv, stores, placement, _, _ = build(ports=(port,))
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))

    first = kv.relocate_pending(min_age=0)
    assert first.failed == 1
    pending = _record(placement)
    assert pending.shard == "profile" and pending.cleanup_from == "working"
    assert pending.claim is None, "a failed cleanup must release its claim for online retry"

    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="working", content="again")))
    assert _record(placement).relocate_to == "working"

    kv.relocate_pending(min_age=0)
    record = _record(placement)
    assert record.cleanup_from is None
    assert record.relocate_to == "working", "the newer request must survive cleanup"
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]), "its index too"


def test_cleanup_only_drops_the_index_version_it_read(monkeypatch) -> None:
    """P2-4（并发）：清理清除标记后、删除索引前到来的新标记，其索引不能被删掉。"""
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    original = placement.delete_if_value
    raced = {"done": False}

    def racing_delete(scope, key, expected):
        if key == relocation_index_key("u1") and not raced["done"]:
            raced["done"] = True
            kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="working", content="v3")))
        return original(scope, key, expected)

    monkeypatch.setattr(placement, "delete_if_value", racing_delete)
    kv.relocate_pending(min_age=0)

    assert raced["done"]
    assert _record(placement).relocate_to == "working"
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]), "new index kept"


def test_stale_mark_from_an_overwritten_write_is_redecided_from_current_data() -> None:
    """P2-5：A 的标记晚于 B 的写入落地时，按源分片当前数据（B）重新裁决。"""
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    a_content = dumps(unit(shard="profile", content="from A"))
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="working", content="from B")))
    record = _record(placement)
    assert record.relocate_to is None
    # A 的打标在 B 完成之后才落地（基于 A 自己那次写入的内容）。
    assert private(kv, "_relocator").mark(SCOPE, "u1", record, "profile", content_digest(a_content))

    report = kv.relocate_pending(min_age=0)

    assert report.moved == 0, str(report)
    final = _record(placement)
    assert final.shard == "working" and final.relocate_to is None
    assert b"from B" in stores["working"].get(SCOPE, memory_key("u1"))
    assert placement.read_many(SCOPE, [relocation_index_key("u1")]) == {}


def test_stale_mark_still_moves_when_current_data_agrees() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile", content="from B")))
    record = _record(placement)
    stale = replace(record, mark_digest="0" * 64)
    placement.replace_if_value(SCOPE, placement_key("u1"), record.to_bytes(), stale.to_bytes())

    report = kv.relocate_pending(min_age=0)

    assert report.moved == 1, str(report)
    assert _record(placement).shard == "profile"


# ---------------------------------------------------------------------------
# P1：回滚失败后的维护恢复不能切换到不存在的目标
# ---------------------------------------------------------------------------


def _stale_switch_record() -> PlacementRecord:
    return PlacementRecord(
        shard="working",
        status=PlacementStatus.MOVING,
        token="switch-leftover",
        created_at=1_000.0 - 10_000.0,
        relocate_to="profile",
        step="switch",
    )


def test_recover_stale_switch_with_missing_target_rolls_back() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    active = _record(placement)
    moving = _stale_switch_record()
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )
    placement.create_if_absent(SCOPE, relocation_index_key("u1"), b"0:seed")

    report = kv.relocate_pending(recover_stale=True, stale_after=1.0)

    assert report.moved == 1 and report.failed == 0
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE
    assert record.shard == "working"
    assert record.relocate_to == "profile"
    # 源数据仍是唯一有效副本，没有被切到空目标后删掉
    assert stores["working"].exists(SCOPE, memory_key("u1"))
    assert not stores["profile"].exists(SCOPE, memory_key("u1"))


def test_recover_stale_switch_with_diverged_target_rolls_back() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    stores["profile"].insert(SCOPE, memory_key("u1"), b"stale-copy")
    active = _record(placement)
    moving = _stale_switch_record()
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )
    placement.create_if_absent(SCOPE, relocation_index_key("u1"), b"0:seed")

    report = kv.relocate_pending(recover_stale=True, stale_after=1.0)

    assert report.moved == 1 and report.failed == 0
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE and record.shard == "working"
    assert stores["working"].exists(SCOPE, memory_key("u1"))
    assert not stores["profile"].exists(SCOPE, memory_key("u1"))


# ---------------------------------------------------------------------------
# P2：旧请求的「取消搬迁」不能覆盖新意图
# ---------------------------------------------------------------------------


def test_stale_cancel_keeps_newer_relocation_intent() -> None:
    kv, stores, placement, _, _ = build()
    _write_unit(kv, stores)
    kv.update(SCOPE, memory_key("u1"), dumps(unit(shard="profile")))
    record = _record(placement)
    assert record.relocate_to == "profile"

    # 模拟一个过期请求：它的正文不是当前源数据，CAS 重试时想清掉标记。
    stale_digest = content_digest(dumps(unit(shard="working", content="old")))
    assert private(kv, "_relocator").mark(SCOPE, "u1", record, None, stale_digest) is True
    assert _record(placement).relocate_to == "profile"


# ---------------------------------------------------------------------------
# P2：索引刷新失败要重试，不能假定别人已刷新
# ---------------------------------------------------------------------------


class _RacingPlacement(ClockKV):
    """第一次替换索引时模拟旧清理任务删掉索引，然后返回 CAS 冲突。"""

    def __init__(self) -> None:
        super().__init__()
        self.race = False

    def replace_if_value(self, scope, key, expected, value) -> bool:
        if self.race and key.startswith(RELOCATION_INDEX_PREFIX):
            self.race = False
            self.delete_if_value(scope, key, expected)
            raise ConflictError("index deleted by a stale cleanup")
        return super().replace_if_value(scope, key, expected, value)


def test_touch_index_recreates_after_cas_conflict() -> None:
    placement = _RacingPlacement()
    kv, _, _, _, _ = build(placement=placement)
    assert private_method(private(kv, "_relocator"), "_touch_index")(SCOPE, "u1") is True
    first = private_method(private(kv, "_relocator"), "_read_index")(SCOPE, "u1")
    assert first is not None

    placement.race = True
    assert private_method(private(kv, "_relocator"), "_touch_index")(SCOPE, "u1") is True
    second = private_method(private(kv, "_relocator"), "_read_index")(SCOPE, "u1")
    assert second is not None and second != first


# ---------------------------------------------------------------------------
# P2：搬迁读取必须覆盖全部记录（Milvus 弱一致 / 部分读取）
# ---------------------------------------------------------------------------


class _DropOneVectorStore(InMemoryVectorStore):
    def get_for_relocation(self, scope, ids) -> list[VectorRecord]:
        return super().get_for_relocation(scope, ids)[:-1]


def test_relocation_refuses_partial_vector_read() -> None:
    source = _DropOneVectorStore()
    source.insert(
        SCOPE,
        [
            VectorRecord(id="u1-0", vector=[1.0, 0.0], metadata={"unit_id": "u1"}),
            VectorRecord(id="u1-1", vector=[0.0, 1.0], metadata={"unit_id": "u1"}),
        ],
    )
    routed = ShardRoutedVectorStore(
        resolver=resolver(),
        locator=InMemoryKVStore(),  # relocate_unit 的 split 路径不依赖定位
        mode="split",
        stores={"working": source, "profile": InMemoryVectorStore()},
    )

    with pytest.raises(BackendError, match="incomplete"):
        routed.relocate_unit(SCOPE, "u1", "working", "profile")


def test_recover_stale_rollback_step_never_switches() -> None:
    """回滚已清掉目标派生记录、但目标 KV 仍在时，恢复必须继续回滚而不是前滚。"""
    kv, stores, placement, vector, fulltext = build()
    _write_unit(kv, stores)
    _write_indexes(vector, fulltext)
    active = _record(placement)
    moving = PlacementRecord(
        shard="working",
        status=PlacementStatus.MOVING,
        token="rollback-leftover",
        created_at=1_000.0 - 10_000.0,
        relocate_to="profile",
        step="rollback",
    )
    # 目标 KV 副本还在（与源一致），但目标派生记录已被回滚清理。
    stores["profile"].insert(
        SCOPE, memory_key("u1"), stores["working"].get(SCOPE, memory_key("u1"))
    )
    placement.replace_if_value(
        SCOPE, placement_key("u1"), active.to_bytes(), moving.to_bytes()
    )
    placement.create_if_absent(SCOPE, relocation_index_key("u1"), b"0:seed")

    report = kv.relocate_pending(recover_stale=True, stale_after=1.0)

    assert report.moved == 1 and report.failed == 0
    record = _record(placement)
    assert record.status is PlacementStatus.ACTIVE and record.shard == "working"
    # 源分片的 KV 与派生记录都还在；没有被前滚清理掉。
    assert stores["working"].exists(SCOPE, memory_key("u1"))
    assert private(vector, "_stores")["working"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1-0"]
    }
    assert private(fulltext, "_stores")["working"].record_ids_for_units(SCOPE, ["u1"]) == {
        "u1": ["u1"]
    }
    assert not stores["profile"].exists(SCOPE, memory_key("u1"))
