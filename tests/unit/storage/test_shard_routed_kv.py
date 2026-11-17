# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S2：KV 分片路由与归属记录协议。"""

from __future__ import annotations

import threading

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
)
from jiuwen_memory.common.type_def.filter import normalize
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.shard_placement import (
    PlacementRecord,
    PlacementStatus,
    placement_key,
)
from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore
from jiuwen_memory.storage.shard_routing import (
    SHARD_PLACEMENT_KEY_PREFIX,
    RuleBasedShardResolver,
)

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile", "main")


class ClockKV(InMemoryKVStore):
    """可控时钟的内存 KV，便于构造超期 pending。"""

    def __init__(self) -> None:
        super().__init__()
        self.t = 1_000.0

    def now(self) -> float:
        return self.t


def unit(uid: str, *, tier: MemoryTier = MemoryTier.CORE, **meta) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=SCOPE,
        tier=tier,
        system_metadata=dict(meta),
        segments=[Segment(content=f"content-{uid}")],
    )


def make_store(*, clock: ClockKV | None = None, timeout: float = 900.0) -> tuple[
    ShardRoutedKVStore, dict[str, InMemoryKVStore], ClockKV
]:
    resolver = RuleBasedShardResolver(
        shards=list(SHARDS),
        default_shard="main",
        rules=[
            {"shard_from": "system_metadata.shard"},
            {"shard": "working", "match": {"tier": "working"}},
            {"shard": "profile", "match": {"tier": "core"}},
        ],
    )
    stores = {name: InMemoryKVStore() for name in SHARDS}
    placement = clock or ClockKV()
    passthrough = InMemoryKVStore()
    routed = ShardRoutedKVStore(
        resolver=resolver,
        passthrough=passthrough,
        placement=placement,
        shard_stores=stores,
        pending_timeout=timeout,
    )
    return routed, stores, placement


class InterleavedStore(ShardRoutedKVStore):
    """在协议步骤之间暂停，用来确定性复现交错。"""

    pause_after_pending = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reached_pending = threading.Event()
        self.resume = threading.Event()

    def _create_pending(self, scope, unit_id, record) -> None:
        super()._create_pending(scope, unit_id, record)
        if self.pause_after_pending:
            self.reached_pending.set()
            assert self.resume.wait(3.0), "test never resumed the paused insert"


def test_insert_routes_by_rule_and_keeps_data_in_shard() -> None:
    """TC-S2-1：按规则落表，归属 active。"""
    store, stores, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")), 0.0)
    assert stores["profile"].get(SCOPE, memory_key("u1"))
    assert store.get(SCOPE, memory_key("u1"))
    location = store.locate(SCOPE, ["u1"])
    assert location.found == {"u1": "profile"}


def test_non_memory_key_goes_to_passthrough() -> None:
    """非 /memory/ key 落 passthrough。"""
    store, stores, _ = make_store()
    store.insert(SCOPE, "/messages/1", b"raw")
    assert store.get(SCOPE, "/messages/1") == b"raw"
    for shard_store in stores.values():
        assert not shard_store.exists(SCOPE, "/messages/1")


def test_pending_and_cleaning_are_invisible() -> None:
    """TC-S2-3：pending / cleaning / 无归属均不可见。"""
    store, stores, placement = make_store()
    stores["profile"].insert(SCOPE, memory_key("u1"), b"data")
    placement.create_if_absent(
        SCOPE,
        placement_key("u1"),
        PlacementRecord("profile", PlacementStatus.PENDING, "t1", 1.0).to_bytes(),
    )
    with pytest.raises(NotFoundError):
        store.get(SCOPE, memory_key("u1"))
    assert store.exists(SCOPE, memory_key("u1")) is False
    with pytest.raises(NotFoundError):
        store.mget(SCOPE, [memory_key("u1")])

    placement.replace_if_value(
        SCOPE,
        placement_key("u1"),
        PlacementRecord("profile", PlacementStatus.PENDING, "t1", 1.0).to_bytes(),
        PlacementRecord("profile", PlacementStatus.CLEANING, "t2", 1.0).to_bytes(),
    )
    with pytest.raises(NotFoundError):
        store.get(SCOPE, memory_key("u1"))


def test_concurrent_insert_same_id_conflicts() -> None:
    """TC-S2-4：并发 insert 同一 id 只有一个成功。"""
    store, _, _ = make_store()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker() -> None:
        barrier.wait()
        try:
            store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
            outcomes.append("ok")
        except ConflictError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1, outcomes
    assert store.locate(SCOPE, ["u1"]).found == {"u1": "profile"}


def test_concurrent_insert_same_id_across_shards_conflicts() -> None:
    """TC-S2-4：同 id 并发落不同分片，也只能有一个成功。"""
    store, _, _ = make_store()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker(tier: MemoryTier) -> None:
        barrier.wait()
        try:
            store.insert(SCOPE, memory_key("u1"), dumps(unit("u1", tier=tier)))
            outcomes.append("ok")
        except ConflictError as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=worker, args=(MemoryTier.WORKING,)),
        threading.Thread(target=worker, args=(MemoryTier.CORE,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1, outcomes
    assert len(store.locate(SCOPE, ["u1"]).found) == 1


def test_insert_and_delete_interleaving() -> None:
    """TC-S2-5：delete 在 insert 提交前完成 → insert 失败且不可见。"""
    store, stores, _ = make_store()
    paused = InterleavedStore(
        resolver=getattr(store, "_resolver"),
        passthrough=InMemoryKVStore(),
        placement=getattr(store, "_placement"),
        shard_stores=stores,
    )
    paused.pause_after_pending = True
    result: list[object] = []

    def insert() -> None:
        try:
            paused.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
            result.append("ok")
        except ConflictError as exc:
            result.append(exc)

    thread = threading.Thread(target=insert)
    thread.start()
    assert paused.reached_pending.wait(3.0)
    paused.delete(SCOPE, memory_key("u1"))
    paused.resume.set()
    thread.join()
    assert result and isinstance(result[0], ConflictError), result
    with pytest.raises(NotFoundError):
        paused.get(SCOPE, memory_key("u1"))


def test_delete_then_reinsert_is_blocked_until_delete_completes() -> None:
    """TC-S2-7：cleaning 期间重新插入冲突；完成后可插入且旧 delete 不删新数据。"""
    store, stores, placement = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    cleaning = PlacementRecord("profile", PlacementStatus.CLEANING, "c1", 1.0)
    current = placement.read_many(SCOPE, [placement_key("u1")])[placement_key("u1")]
    placement.replace_if_value(SCOPE, placement_key("u1"), current, cleaning.to_bytes())
    with pytest.raises(ConflictError):
        store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    # 模拟 delete 完成
    stores["profile"].delete(SCOPE, memory_key("u1"))
    placement.delete_if_value(SCOPE, placement_key("u1"), cleaning.to_bytes())
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    assert stores["profile"].get(SCOPE, memory_key("u1"))


def test_update_keeps_sticky_shard() -> None:
    """TC-S2（粘性）：改 tier 后 update 仍写原分片。"""
    store, stores, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1", tier=MemoryTier.CORE)))
    store.update(
        SCOPE,
        memory_key("u1"),
        dumps(unit("u1", tier=MemoryTier.WORKING)),
    )
    assert stores["profile"].exists(SCOPE, memory_key("u1"))
    assert not stores["working"].exists(SCOPE, memory_key("u1"))
    assert store.locate(SCOPE, ["u1"]).found == {"u1": "profile"}


def test_update_missing_place_raises() -> None:
    store, _, _ = make_store()
    with pytest.raises(NotFoundError):
        store.update(SCOPE, memory_key("u1"), dumps(unit("u1")))


def test_retry_insert_after_success_conflicts() -> None:
    """TC-S2-13：响应丢失后重试得 ConflictError。"""
    store, _, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    with pytest.raises(ConflictError):
        store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))


def test_reconcile_expired_pending_and_reinsert() -> None:
    """TC-S2-6 / TC-S2-8：超期 pending 被对账清理，原插入提交失败。"""
    store, stores, placement = make_store(timeout=10.0)
    paused = InterleavedStore(
        resolver=getattr(store, "_resolver"),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores=stores,
        pending_timeout=10.0,
    )
    paused.pause_after_pending = True
    result: list[object] = []

    def insert() -> None:
        try:
            paused.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
            result.append("ok")
        except ConflictError as exc:
            result.append(exc)

    thread = threading.Thread(target=insert)
    thread.start()
    assert paused.reached_pending.wait(3.0)
    placement.t = 1_020.0  # 超过 pending_timeout
    report = paused.reconcile(scope=SCOPE)
    assert report.expired_pending == 1
    paused.resume.set()
    thread.join()
    assert result and isinstance(result[0], ConflictError), result
    with pytest.raises(NotFoundError):
        paused.get(SCOPE, memory_key("u1"))
    # 失效的慢 insert 晚到写入的数据成为无主数据，再次对账清理后可重新插入
    paused.reconcile(scope=SCOPE)
    paused.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    assert paused.get(SCOPE, memory_key("u1"))


def test_commit_failure_leaves_pending_until_reconcile() -> None:
    """TC-S2-11：提交步骤失败后 pending 阻挡同 id，对账后才可重插。"""
    store, stores, placement = make_store(timeout=10.0)

    class _CommitFails(ShardRoutedKVStore):
        fail = True

        def _commit_active(self, scope, unit_id, record):
            if self.fail:
                raise ConflictError("memory", unit_id)
            return super()._commit_active(scope, unit_id, record)

    broken = _CommitFails(
        resolver=getattr(store, "_resolver"),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores=stores,
        pending_timeout=10.0,
    )
    with pytest.raises(ConflictError):
        broken.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    with pytest.raises(NotFoundError):
        broken.get(SCOPE, memory_key("u1"))
    with pytest.raises(ConflictError):
        broken.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    # 超期后对账清理 pending + 晚到的无主数据，之后可重新插入
    placement.t = 1_020.0
    broken.reconcile(scope=SCOPE)  # 清掉 pending 与提交失败后留下的无主数据
    broken.fail = False
    broken.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    assert broken.get(SCOPE, memory_key("u1"))


def test_reconcile_cleans_orphan_data() -> None:
    """无主数据由对账清理，清理前不可见。"""
    store, stores, _ = make_store()
    stores["profile"].insert(SCOPE, memory_key("ghost"), dumps(unit("ghost")))
    with pytest.raises(NotFoundError):
        store.get(SCOPE, memory_key("ghost"))
    report = store.reconcile(scope=SCOPE)
    assert report.orphan_data == 1
    assert not stores["profile"].exists(SCOPE, memory_key("ghost"))


def test_leftover_cleaning_not_taken_over_online() -> None:
    """TC-S2-9：遗留 cleaning 不在线接管，维护窗口才清理。"""
    store, stores, placement = make_store()
    stores["profile"].insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    placement.create_if_absent(
        SCOPE,
        placement_key("u1"),
        PlacementRecord("profile", PlacementStatus.CLEANING, "c1", 1_000.0).to_bytes(),
    )
    store.reconcile(scope=SCOPE)
    assert placement.read_many(SCOPE, [placement_key("u1")])
    assert stores["profile"].exists(SCOPE, memory_key("u1"))
    with pytest.raises(ConflictError):
        store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    report = store.reconcile(scope=SCOPE)
    assert report.leftover_cleaning == 1
    store.reconcile(scope=SCOPE, include_cleaning=True)
    assert not placement.read_many(SCOPE, [placement_key("u1")])
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    assert store.get(SCOPE, memory_key("u1"))


def test_operation_token_distinguishes_same_created_at() -> None:
    """TC-S2-10：时间相同也能靠 token 区分归属。"""
    store, _, placement = make_store()
    first = PlacementRecord("profile", PlacementStatus.PENDING, "t1", 1_000.0)
    second = PlacementRecord("profile", PlacementStatus.PENDING, "t2", 1_000.0)
    placement.create_if_absent(SCOPE, placement_key("u1"), first.to_bytes())
    with pytest.raises(ConflictError):
        placement.replace_if_value(
            SCOPE, placement_key("u1"), second.to_bytes(), first.to_bytes()
        )
    placement.replace_if_value(
        SCOPE, placement_key("u1"), first.to_bytes(), second.to_bytes()
    )
    stored = placement.read_many(SCOPE, [placement_key("u1")])[placement_key("u1")]
    assert stored == second.to_bytes()


def test_list_matches_single_table_semantics() -> None:
    """TC-S2-14：list 的过滤 / 计数 / 分页与单表一致。"""
    store, _, _ = make_store()
    for index, tier in enumerate([MemoryTier.CORE, MemoryTier.CORE, MemoryTier.WORKING]):
        store.insert(SCOPE, memory_key(f"u{index}"), dumps(unit(f"u{index}", tier=tier)))
    page = store.list(SCOPE, limit=1, offset=1)
    assert page.count == 3
    assert len(page.entries) == 1
    filtered = store.list(SCOPE, filters=normalize({"tier": "core"}))
    assert filtered.count == 2
    sharded = store.list(SCOPE, extensions={"shards": ["working"]})
    assert sharded.count == 1
    assert sharded.entries[0][0] == memory_key("u2")


def test_scan_hides_placement_and_old_passthrough_memory_copies() -> None:
    """TC-S2-15：scan 不暴露归属记录与 passthrough 旧副本。"""
    store, _, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    store.insert(SCOPE, "/space/info", b"meta")
    passthrough = getattr(store, "_passthrough")
    passthrough.insert(SCOPE, memory_key("legacy"), dumps(unit("legacy")))
    keys = [key for key, _ in store.scan(SCOPE)]
    assert keys == [mem for mem in keys if not mem.startswith(SHARD_PLACEMENT_KEY_PREFIX)]
    assert memory_key("legacy") not in keys
    assert "/space/info" in keys
    assert memory_key("u1") in keys
    prefix_keys = [key for key, _ in store.scan(SCOPE, "/space/")]
    assert prefix_keys == ["/space/info"]


def test_scopes_merge_and_hide_control_only_scopes() -> None:
    """TC-S2-16：scopes 合并去重，隐藏仅有控制记录的 scope。"""
    store, _, placement = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    control_scope = Scope(org="acme", user="control-only")
    placement.create_if_absent(
        control_scope, placement_key("x"), PlacementRecord(
            "main", PlacementStatus.PENDING, "t", 1.0
        ).to_bytes()
    )
    scopes = store.scopes()
    assert SCOPE in scopes
    assert control_scope not in scopes


def test_reserved_prefix_rejected() -> None:
    """TC-S2-17：外部读写保留前缀被拒绝。"""
    store, _, _ = make_store()
    key = f"{SHARD_PLACEMENT_KEY_PREFIX}evil"
    for operation in (
        lambda: store.insert(SCOPE, key, b"x"),
        lambda: store.update(SCOPE, key, b"x"),
        lambda: store.delete(SCOPE, key),
        lambda: store.get(SCOPE, key),
        lambda: store.exists(SCOPE, key),
    ):
        with pytest.raises(ValidationError):
            operation()


def test_locator_partial_result() -> None:
    """TC-S1-14 / TC-S2：批量定位部分缺失不整批失败。"""
    store, _, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    location = store.locate(SCOPE, ["u1", "missing"])
    assert location.found == {"u1": "profile"}
    assert location.unlocated == frozenset({"missing"})


def test_shards_extension_validation() -> None:
    store, _, _ = make_store()
    with pytest.raises(ValidationError):
        store.list(SCOPE, extensions={"shards": []})
    with pytest.raises(ValidationError):
        store.list(SCOPE, extensions={"shards": "profile"})
    with pytest.raises(ValidationError):
        store.list(SCOPE, extensions={"shards": ["ghost"]})


def test_topology_registration_and_retiring() -> None:
    """TC-S2-19：拓扑登记比对与 retiring 拒写。"""
    store, _, _ = make_store()
    descriptors = {name: {"kv": f"pg:db/{name}"} for name in SHARDS}
    store.register_topology(descriptors)
    assert set(store.read_topology()) == set(SHARDS)
    store.retire_shard("working")
    with pytest.raises(ValidationError):
        store.insert(SCOPE, memory_key("w1"), dumps(unit("w1", tier=MemoryTier.WORKING)))
    # 读取照常
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    assert store.get(SCOPE, memory_key("u1"))

    changed = dict(descriptors)
    changed["profile"] = {"kv": "pg:db/moved"}
    with pytest.raises(ValidationError):
        store.register_topology(changed)

    # 登记中仍有 working（不在配置里）
    with pytest.raises(ValidationError):
        ShardRoutedKVStore(
            resolver=RuleBasedShardResolver(
                shards=["profile", "main"],
                default_shard="main",
                rules=[],
            ),
            passthrough=getattr(store, "_passthrough"),
            placement=getattr(store, "_placement"),
            shard_stores={"profile": InMemoryKVStore(), "main": InMemoryKVStore()},
        ).register_topology({"profile": {"kv": "pg:db/profile"}, "main": {"kv": "pg:db/main"}})


class PausingDeleteStore(ShardRoutedKVStore):
    """在持有 ``cleaning`` 之后、删除数据之前暂停，用来复现并发 delete。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reached_cleaning = threading.Event()
        self.resume_delete = threading.Event()
        # 删除协议的分步实现在控制面 ShardAdmin 上，暂停点直接注入那里。
        original = self._admin.delete_data

        def paused_delete_data(scope, shard, key) -> None:
            self.reached_cleaning.set()
            assert self.resume_delete.wait(3.0), "test never resumed the paused delete"
            original(scope, shard, key)

        self._admin.delete_data = paused_delete_data


def make_pausing_store() -> tuple[PausingDeleteStore, dict[str, InMemoryKVStore], ClockKV]:
    resolver = RuleBasedShardResolver(
        shards=list(SHARDS),
        default_shard="main",
        rules=[{"shard": "working", "match": {"tier": "working"}}],
    )
    stores = {name: InMemoryKVStore() for name in SHARDS}
    placement = ClockKV()
    store = PausingDeleteStore(
        resolver=resolver,
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores=stores,
    )
    return store, stores, placement


def test_online_delete_does_not_take_over_cleaning() -> None:
    """P1 回归：第二个 delete 不得接管 cleaning，否则迟到的旧删除会删掉新数据。

    交错：A 占住 cleaning 并暂停 → B 发起删除（必须直接返回，不接管、不删数据）
    → 同 id 插入必须被 cleaning 挡住（ConflictError）→ A 恢复后数据与归属都消失。
    """
    store, stores, placement = make_pausing_store()
    key = memory_key("u1")
    store.insert(SCOPE, key, dumps(unit("u1")))
    shard = store.locate(SCOPE, ["u1"]).found["u1"]

    errors: list[BaseException] = []

    def delete_a() -> None:
        try:
            store.delete(SCOPE, key)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常转交主线程断言
            errors.append(exc)

    thread = threading.Thread(target=delete_a)
    thread.start()
    assert store.reached_cleaning.wait(3.0), "delete A never reached the cleaning step"

    # B 在线删除：看到 cleaning 直接返回，不得接管，也不得删数据。
    store.delete(SCOPE, key)
    assert stores[shard].exists(SCOPE, key), "second delete must not remove data"
    record = placement.read_many(SCOPE, [placement_key("u1")])[placement_key("u1")]
    assert PlacementRecord.from_bytes(record).status is PlacementStatus.CLEANING

    # cleaning 仍被 A 持有：同 id 插入必须被挡住，不能写入新数据。
    with pytest.raises(ConflictError):
        store.insert(SCOPE, key, dumps(unit("u1")))

    store.resume_delete.set()
    thread.join(timeout=5.0)
    assert not errors, errors
    assert not stores[shard].exists(SCOPE, key)
    assert store.locate(SCOPE, ["u1"]).found == {}


def test_mget_preserves_duplicate_keys_and_order() -> None:
    """S06 契约：mget 结果与输入下标一一对应，重复 key 各下标独立返回。"""
    store, _, _ = make_store()
    first = dumps(unit("u1"))
    second = dumps(unit("u2", tier=MemoryTier.WORKING))
    store.insert(SCOPE, memory_key("u1"), first)
    store.insert(SCOPE, memory_key("u2"), second)
    getattr(store, "_passthrough").insert(SCOPE, "/space/info", b"meta")

    keys = [
        memory_key("u2"),
        "/space/info",
        memory_key("u1"),
        memory_key("u2"),
        memory_key("u1"),
    ]
    assert store.mget(SCOPE, keys) == [second, b"meta", first, second, first]
    with pytest.raises(NotFoundError):
        store.mget(SCOPE, [memory_key("u1"), memory_key("missing")])
    assert store.mget(SCOPE, []) == []


def test_repartition_aborts_with_conflict_when_source_ttl_unknown(monkeypatch) -> None:
    """源记录读取后过期（remaining_ttl 为 None）→ ConflictError，旧数据与归属不变。"""
    store, stores, _ = make_store()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1")))
    monkeypatch.setattr(stores["profile"], "remaining_ttl", lambda scope, key: None)
    with pytest.raises(ConflictError):
        store.repartition_unit(SCOPE, "u1", "working")
    assert store.locate(SCOPE, ["u1"]).found == {"u1": "profile"}
    assert stores["profile"].exists(SCOPE, memory_key("u1"))
    assert not stores["working"].exists(SCOPE, memory_key("u1"))
