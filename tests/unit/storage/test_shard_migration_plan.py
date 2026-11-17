# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S7：迁移工具的纯规划函数（分区 / 重分区 / 退役校验）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_kv_by_shard.py"
_spec = importlib.util.spec_from_file_location("migrate_kv_by_shard", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migration
_spec.loader.exec_module(migration)

SCOPE = Scope(org="acme", user="u1")


def unit(uid: str, tier: MemoryTier) -> MemoryUnit:
    return MemoryUnit(id=uid, scope=SCOPE, tier=tier, segments=[Segment(content=uid)])


def resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=["working", "profile"],
        default_shard="working",
        rules=[{"shard": "profile", "match": {"tier": "core"}}],
    )


def test_reconcile_without_cleaning_needs_no_window(monkeypatch, tmp_path) -> None:
    """P1：在线对账（不含 cleaning）不要求维护窗口。"""
    monkeypatch.delenv("AGENT_MEMORY_MAINTENANCE_WINDOW", raising=False)
    path = _write_config(tmp_path)
    assert migration.main(["--config", path, "--reconcile"]) == 0
    # 维护窗口缺失属于工具输入错误：CLI 返回退出码 2，不抛 SystemExit。
    assert migration.main(["--config", path, "--reconcile", "--include-cleaning"]) == 2


def test_mutating_commands_require_maintenance_window(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MEMORY_MAINTENANCE_WINDOW", raising=False)
    with pytest.raises(migration.ToolError):
        getattr(migration, "_require_maintenance_window")("partition")
    monkeypatch.setenv("AGENT_MEMORY_MAINTENANCE_WINDOW", "1")
    getattr(migration, "_require_maintenance_window")("partition")


def test_plan_partition_counts_by_predicted_shard() -> None:
    source = InMemoryKVStore()
    for uid, tier in (("u1", MemoryTier.CORE), ("u2", MemoryTier.WORKING)):
        source.insert(SCOPE, memory_key(uid), dumps(unit(uid, tier)))
    plans, counts = migration.plan_partition(source, resolver())
    assert len(plans) == 2
    assert counts == {"profile": 1, "working": 1}
    assert {plan["key"] for plan in plans} == {memory_key("u1"), memory_key("u2")}


def test_plan_repartition_detects_sticky_mismatch() -> None:
    """粘性归属下改 tier 后内容与归属不一致，重分区规划应识别出来。"""
    from jiuwen_memory.storage.kv_impl.shard_placement import PlacementRecord
    from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore

    routed = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={"working": InMemoryKVStore(), "profile": InMemoryKVStore()},
    )
    routed.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.WORKING)))
    # 模拟「tier 被改成 core，但记忆仍留在 working」的粘性结果
    routed.update(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)))
    moves = migration.plan_repartition(routed, resolver())
    assert [(move.unit_id, move.source, move.target) for move in moves] == [
        ("u1", "working", "profile")
    ]
    assert PlacementRecord is not None  # 归属记录仍是唯一事实来源


def _write_config(tmp_path) -> str:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
globals: {store_manager: main}
shard_resolver:
  default:
    target: rule_based
    params:
      shards: [working, profile]
      default_shard: working
      rules: []
kv_store:
  default:
    target: shard_routed
    params:
      resolver: default
      passthrough: legacy
      placement_store: {target: memory}
      table_template: {target: memory, params: {table: "kv_{shard}"}}
  legacy: {target: memory}
store_manager:
  main: {target: composite}
"""
    )
    return str(config)


def _write_indexed_config(tmp_path, *, with_custom_builder: bool = False) -> str:
    config = tmp_path / "indexed.yaml"
    custom = (
        """
constructor:
  custom:
    target: hybrid
    params: {chunker: default, embedder: default}
"""
        if with_custom_builder
        else ""
    )
    config.write_text(
        """
globals: {store_manager: main, vector_enabled: true}
shard_resolver:
  default:
    target: rule_based
    params:
      shards: [working, profile]
      default_shard: working
      rules: [{shard: profile, match: {tier: core}}]
kv_store:
  default:
    target: shard_routed
    params:
      resolver: default
      passthrough: legacy
      placement_store: {target: memory}
      table_template: {target: memory, params: {table: "kv_{shard}"}}
  legacy: {target: memory}
vector_store:
  default:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "v_{shard}"}
  layers_l0:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "v_{shard}_l0"}
  layers_l1:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "v_{shard}_l1"}
fulltext_store:
  default:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "f_{shard}"}
  layers_l0:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "f_{shard}_l0"}
  layers_l1:
    target: shard_routed
    params:
      resolver: default
      mode: split
      table_template:
        target: memory
        params: {table: "f_{shard}_l1"}
store_manager: {main: {target: composite}}
"""
        + custom
    )
    return str(config)


def test_rebuild_indexes_rebuilds_split_indexes(tmp_path) -> None:
    """P1：重建入口把向量/全文索引写到当前分片。"""
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.config import Config
    from jiuwen_memory.config.defaults import default_context
    from jiuwen_memory.storage.store_manager import StoreManagerProducer
    from jiuwen_memory.storage.types import TextQuery

    path = _write_indexed_config(tmp_path)
    getattr(migration, "_register_runtime")()
    user = Config.from_yaml(path).context(known_top_names=Factory.known_top_names())
    ctx = default_context().merged(user)
    manager = StoreManagerProducer.build_named("main", ctx)
    manager.kv().insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)))

    assert migration.rebuild_indexes(ctx, manager) == 1
    assert [record.id for record in manager.vector().get(SCOPE, ["u1-0"])] == ["u1-0"]
    hits = manager.fulltext().search(SCOPE, TextQuery(text="u1", top_k=5))
    assert [hit.id for hit in hits] == ["u1"]


def test_rebuild_indexes_smoke(tmp_path) -> None:
    """P1：重建入口可运行；KV-only 配置下不抛错。"""
    from jiuwen_memory.config import Config
    from jiuwen_memory.storage.store_manager import StoreManagerProducer

    path = _write_config(tmp_path)
    getattr(migration, "_register_runtime")()
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.config.defaults import default_context

    user = Config.from_yaml(path).context(known_top_names=Factory.known_top_names())
    ctx = default_context().merged(user)
    manager = StoreManagerProducer.build_named("main", ctx)
    manager.kv().insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)))
    rebuilt = migration.rebuild_indexes(ctx, manager)
    assert rebuilt == 1


def test_cli_dry_run_and_print_ddl(tmp_path, capsys) -> None:
    """S7：dry-run / print-ddl 不写数据且能正常退出。"""
    path = _write_config(tmp_path)
    assert migration.main(["--config", path, "--dry-run"]) == 0
    assert migration.main(["--config", path, "--print-ddl"]) == 0
    output = capsys.readouterr().out
    assert "agent_memory_kv_working" in output
    assert "agent_memory_shard_placement" in output


def test_repartition_unit_is_data_first() -> None:
    """P1：重新分区目标写入失败时旧数据仍在；成功后才删旧分片数据。"""
    from jiuwen_memory.common.errors import ConflictError
    from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore

    routed = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={"working": InMemoryKVStore(), "profile": InMemoryKVStore()},
    )
    routed.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.WORKING)))
    # 当前在 working；目标 profile 预置同 key 数据 → 迁移必须中止且旧数据仍在
    getattr(routed, "_stores")["profile"].insert(SCOPE, memory_key("u1"), b"occupied")
    with pytest.raises(ConflictError):
        routed.repartition_unit(SCOPE, "u1", "profile")
    assert getattr(routed, "_stores")["working"].get(SCOPE, memory_key("u1"))
    assert routed.locate(SCOPE, ["u1"]).found == {"u1": "working"}

    # 清掉占位后可成功迁移
    getattr(routed, "_stores")["profile"].delete(SCOPE, memory_key("u1"))
    routed.repartition_unit(SCOPE, "u1", "profile")
    assert routed.locate(SCOPE, ["u1"]).found == {"u1": "profile"}
    assert not getattr(routed, "_stores")["working"].exists(SCOPE, memory_key("u1"))

    # TTL 保留
    routed.insert(SCOPE, memory_key("u2"), dumps(unit("u2", MemoryTier.WORKING)), 100.0)
    routed.repartition_unit(SCOPE, "u2", "profile")
    remaining = getattr(routed, "_stores")["profile"].remaining_ttl(SCOPE, memory_key("u2"))
    assert remaining is not None and 0 < remaining <= 100

    # 退役分片不能作为迁移目标（走真实登记 + 退役路径，不直接改内部状态）
    from jiuwen_memory.common.errors import ValidationError

    routed.register_topology(
        {"working": {"kv": "mem:working"}, "profile": {"kv": "mem:profile"}}
    )
    routed.retire_shard("profile")
    routed.insert(SCOPE, memory_key("u3"), dumps(unit("u3", MemoryTier.WORKING)))
    with pytest.raises(ValidationError):
        routed.repartition_unit(SCOPE, "u3", "profile")


def test_apply_moves_moves_data() -> None:
    class _Target:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def get(self, scope, key):
            return self.values[key]

        def delete(self, scope, key):
            self.values.pop(key, None)

        def insert(self, scope, key, raw):
            self.values[key] = raw

        def repartition_unit(self, scope, unit_id, target_shard):
            self.values[memory_key(unit_id)] = self.values[memory_key(unit_id)]
            return target_shard

    target = _Target()
    target.values[memory_key("u1")] = b"raw"
    move = migration.Move(
        "acme", "", "u1", "", "", "u1", "working", "profile"
    )
    moved, failures = migration.apply_moves(target, [move])
    assert moved == 1
    assert failures == []
    assert target.values[memory_key("u1")] == b"raw"


def test_rebuild_indexes_moves_derived_indexes_after_repartition(
    monkeypatch, tmp_path
) -> None:
    """P1：重新分区后派生索引仍在旧分片，``--rebuild-indexes`` 把它们迁到新归属。"""
    from jiuwen_memory.construction.index_builder import IndexBuilderProducer
    from jiuwen_memory.storage.types import IndexWriteMode, TextQuery

    path = _write_indexed_config(tmp_path)
    ctx, manager, _ = getattr(migration, "_load")(path)
    builder = IndexBuilderProducer.build_named("default", ctx)
    memory = MemoryUnit(
        id="u1", scope=SCOPE, tier=MemoryTier.WORKING, segments=[Segment(content="alpha beta")]
    )
    builder.build([memory], mode=IndexWriteMode.ALL)

    fulltext = manager.fulltext()

    def hits(shard: str) -> set[str]:
        results = fulltext.search(
            SCOPE, TextQuery(text="alpha", top_k=5, extensions={"shards": [shard]})
        )
        return {hit.id for hit in results}

    assert hits("working") == {"u1"}
    assert hits("profile") == set()

    kv = manager.kv()
    kv.repartition_unit(SCOPE, "u1", "profile")
    assert kv.locate(SCOPE, ["u1"]).found == {"u1": "profile"}

    # 只搬了 KV：按新归属收窄召回仍然漏掉它——这正是需要重建索引的窗口期。
    assert hits("profile") == set()
    assert hits("working") == {"u1"}

    # 重建（工具要求维护窗口）后派生索引跟随归属。
    monkeypatch.setenv("AGENT_MEMORY_MAINTENANCE_WINDOW", "1")
    assert migration.rebuild_indexes(ctx, manager) == 1
    assert hits("profile") == {"u1"}
    assert hits("working") == set()


def test_rebuild_indexes_dry_run_counts_without_writing(monkeypatch, tmp_path) -> None:
    """``--rebuild-indexes --dry-run`` 只统计：不需要维护窗口，也不写索引。"""
    from jiuwen_memory.construction.index_builder import IndexBuilderProducer
    from jiuwen_memory.storage.types import IndexWriteMode

    path = _write_indexed_config(tmp_path)
    ctx, manager, _ = getattr(migration, "_load")(path)
    builder = IndexBuilderProducer.build_named("default", ctx)
    memory = MemoryUnit(
        id="u1", scope=SCOPE, tier=MemoryTier.WORKING, segments=[Segment(content="alpha")]
    )
    builder.build([memory], mode=IndexWriteMode.ALL)

    monkeypatch.delenv("AGENT_MEMORY_MAINTENANCE_WINDOW", raising=False)
    assert migration.main(["--config", path, "--rebuild-indexes", "--dry-run"]) == 0
    # dry-run 不清旧索引：按 working 收窄仍能召回。
    from jiuwen_memory.storage.types import TextQuery

    hits = manager.fulltext().search(
        SCOPE, TextQuery(text="alpha", top_k=5, extensions={"shards": ["working"]})
    )
    assert {hit.id for hit in hits} == {"u1"}


def test_rebuild_indexes_accepts_custom_builder_name(monkeypatch, tmp_path) -> None:
    from jiuwen_memory.storage.types import IndexWriteMode

    path = _write_indexed_config(tmp_path, with_custom_builder=True)
    ctx, manager, _ = getattr(migration, "_load")(path)
    from jiuwen_memory.construction.index_builder import IndexBuilderProducer

    IndexBuilderProducer.build_named("default", ctx).build(
        [
            MemoryUnit(
                id="u1",
                scope=SCOPE,
                tier=MemoryTier.WORKING,
                segments=[Segment(content="alpha")],
            )
        ],
        mode=IndexWriteMode.ALL,
    )
    monkeypatch.setenv("AGENT_MEMORY_MAINTENANCE_WINDOW", "1")
    assert migration.rebuild_indexes(ctx, manager, builder_name="custom") == 1


class _NoTtlKV(InMemoryKVStore):
    """模拟不暴露剩余 TTL 的后端。"""

    @staticmethod
    def remaining_ttl(scope, key):
        return None


def _memory_with_ttl(seconds: float) -> InMemoryKVStore:
    store = InMemoryKVStore()
    store.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)), ttl=seconds)
    return store


def test_apply_partition_preserves_remaining_ttl() -> None:
    """P1：首次分区必须保留剩余 TTL，不能把会过期的记忆迁成永久记录。"""
    source = _memory_with_ttl(60.0)
    target = InMemoryKVStore()
    plans, _counts = migration.plan_partition(source, resolver())

    written, failures = migration.apply_partition(source, target, plans)

    assert (written, failures) == (1, [])
    ttl = target.remaining_ttl(SCOPE, memory_key("u1"))
    assert ttl is not None and 0 < ttl <= 60.0


def test_apply_partition_refuses_when_ttl_is_unknown() -> None:
    """无法得知剩余 TTL 时拒绝迁移，而不是按 ttl=0 写入永久记录。"""
    source = _NoTtlKV()
    source.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)), ttl=60.0)
    target = InMemoryKVStore()
    plans, _counts = migration.plan_partition(source, resolver())

    written, failures = migration.apply_partition(source, target, plans)

    assert written == 0
    assert failures and "TTLUnavailable" in failures[0]
    assert not target.exists(SCOPE, memory_key("u1"))


def test_relocate_shard_preserves_ttl(monkeypatch) -> None:
    """P1：``--relocate-shard`` 拷贝数据同样要保留剩余 TTL。"""
    import argparse

    from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore

    monkeypatch.setenv("AGENT_MEMORY_MAINTENANCE_WINDOW", "1")
    routed = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={"working": InMemoryKVStore(), "profile": InMemoryKVStore()},
    )
    routed.insert(
        SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.WORKING)), ttl=60.0
    )
    destination = InMemoryKVStore()

    class _Manager:
        @staticmethod
        def kv(name: str):
            return destination

    # 拓扑更新与本用例无关（没有持久标识），测试内替换为 no-op。
    def _noop_update_topology(shard, ports):
        return None

    routed.update_topology = _noop_update_topology  # type: ignore[method-assign]
    args = argparse.Namespace(
        relocate_shard="working",
        destination_kv="dest",
        dry_run=False,
        descriptor=["kv=mem:dest"],
    )

    assert getattr(migration, "_relocate")(args, _Manager(), routed) == 0
    assert destination.get(SCOPE, memory_key("u1"))
    ttl = destination.remaining_ttl(SCOPE, memory_key("u1"))
    assert ttl is not None and 0 < ttl <= 60.0


def _expire_sqlite_row(db_path: str, key: str) -> None:
    import sqlite3
    import time

    conn = sqlite3.connect(db_path)
    try:
        conn.execute('UPDATE kv SET expires_at = ? WHERE key = ?', (time.time() - 1, key))
        conn.commit()
    finally:
        conn.close()


def test_apply_partition_refuses_source_expired_after_planning(tmp_path) -> None:
    """P1 回归：计划时有效、执行时已过期——不得写成永久记录。"""
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

    db_path = str(tmp_path / "source.db")
    source = SQLiteKVStore(db_path=db_path)
    source.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.CORE)), ttl=60.0)
    plans, _counts = migration.plan_partition(source, resolver())
    assert len(plans) == 1

    _expire_sqlite_row(db_path, memory_key("u1"))
    target = InMemoryKVStore()
    written, failures = migration.apply_partition(source, target, plans)

    assert written == 0
    assert failures and "TTLUnavailable" in failures[0]
    assert not target.exists(SCOPE, memory_key("u1"))


def test_relocate_refuses_when_ttl_no_longer_reportable(monkeypatch) -> None:
    """搬迁时源记录已过期（``remaining_ttl`` 返回 None）→ 中止，不写永久副本。"""
    import argparse

    from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore

    class _ExpiredShard(InMemoryKVStore):
        @staticmethod
        def remaining_ttl(scope, key):
            return None

    monkeypatch.setenv("AGENT_MEMORY_MAINTENANCE_WINDOW", "1")
    routed = ShardRoutedKVStore(
        resolver=resolver(),
        passthrough=InMemoryKVStore(),
        placement=InMemoryKVStore(),
        shard_stores={"working": _ExpiredShard(), "profile": InMemoryKVStore()},
    )
    routed.insert(SCOPE, memory_key("u1"), dumps(unit("u1", MemoryTier.WORKING)), ttl=60.0)
    destination = InMemoryKVStore()

    class _Manager:
        @staticmethod
        def kv(name: str):
            return destination

    def _noop_update_topology(shard, ports):
        return None

    routed.update_topology = _noop_update_topology  # type: ignore[method-assign]
    args = argparse.Namespace(
        relocate_shard="working",
        destination_kv="dest",
        dry_run=False,
        descriptor=["kv=mem:dest"],
    )

    with pytest.raises(migration.ToolError):
        getattr(migration, "_relocate")(args, _Manager(), routed)
    assert not destination.exists(SCOPE, memory_key("u1"))


def test_admin_commands_reject_port_without_shard_placement() -> None:
    """管理命令先按能力校验目标端口，不按方法名探测（配错端口给出明确原因）。"""
    with pytest.raises(migration.ToolError, match="SHARD_PLACEMENT"):
        getattr(migration, "_require_shard_admin")(InMemoryKVStore(), "plain")
