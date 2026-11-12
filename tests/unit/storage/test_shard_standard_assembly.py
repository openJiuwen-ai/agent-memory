# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09：标准配置装配下的拓扑登记（真实包装层 + 持久后端标识）。

内存后端没有 ``resource_descriptor()``，装配会直接跳过拓扑登记，因此「装配器按
``{capability}:{port_name}`` 调用各包装层 ``shard_descriptors``」这条路径只有在有
持久后端时才被执行。本用例用 SQLite 作为分片 KV（真持久标识、离线可跑）走完整
配置装配，避免只靠替身覆盖。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from tests.support.whitebox import private

pytestmark = pytest.mark.unit


def _raw(tmp_path, *, relocate: bool = False) -> dict:
    shard_params = {
        "resolver": "default",
        "locator": "default",
        "mode": "split",
        "table_template": {"target": "memory"},
    }
    return {
        "globals": {
            "store_manager": "main",
            "vector_enabled": False,
            "graph_enabled": False,
        },
        "shard_resolver": {
            "default": {
                "target": "rule_based",
                "params": {
                    "shards": ["working", "profile"],
                    "default_shard": "working",
                    "rules": [],
                },
            }
        },
        "kv_store": {
            "default": {
                "target": "shard_routed",
                "params": {
                    "resolver": "default",
                    "passthrough": "legacy",
                    "placement_store": {"target": "memory"},
                    "relocate_on_update": relocate,
                    "table_template": {
                        "target": "sqlite",
                        "params": {"db_path": str(tmp_path / "kv_{shard}.db")},
                    },
                },
            },
            "legacy": {"target": "memory"},
        },
        "vector_store": {
            name: {"target": "shard_routed", "params": dict(shard_params)}
            for name in ("default", "layers_l0", "layers_l1")
        },
        "fulltext_store": {
            name: {"target": "shard_routed", "params": dict(shard_params)}
            for name in ("default", "layers_l0", "layers_l1")
        },
        "store_manager": {"main": {"target": "composite"}},
    }


def test_standard_assembly_registers_topology_with_real_wrappers(tmp_path) -> None:
    register_backends()
    ctx = AssemblyContext.from_dict(_raw(tmp_path))
    manager = StoreManagerProducer.build_named("main", ctx)

    kv = manager.kv()
    assert kv.shards == ("working", "profile")
    topology = kv.read_topology()
    assert set(topology) == {"working", "profile"}
    for shard, name in (("working", "kv_working.db"), ("profile", "kv_profile.db")):
        # SQLite 的资源标识是 ``sqlite:///<绝对路径>``（不含凭据的规范化身份）。
        assert topology[shard].ports["kv:default"].endswith(name)

    # 向量 / 全文包装层同样走标准装配与定位器绑定（没有描述符则不登记，但不能报错）。
    assert manager.vector().shards == ("working", "profile")
    assert manager.fulltext().shards == ("working", "profile")


def test_standard_assembly_binds_relocatable_ports(tmp_path) -> None:
    register_backends()
    ctx = AssemblyContext.from_dict(_raw(tmp_path, relocate=True))
    manager = StoreManagerProducer.build_named("main", ctx)
    kv = manager.kv()
    assert kv.relocation_enabled() is True
    # 装配期已把声明 SHARD_RELOCATABLE 的派生端口绑定进搬迁组件。
    assert private(kv, "_relocatable_ports")


def test_relocation_ports_are_bound_to_the_kv_they_follow(tmp_path) -> None:
    """P1-3：两个独立分片 KV 各自只绑定 locator 指向自己的派生端口。"""
    raw = _raw(tmp_path, relocate=True)
    other = dict(raw["kv_store"]["default"])
    other["params"] = dict(other["params"])
    other["params"]["table_template"] = {
        "target": "sqlite",
        "params": {"db_path": str(tmp_path / "other_{shard}.db")},
    }
    other["params"]["passthrough"] = "legacy_other"
    raw["kv_store"]["other"] = other
    raw["kv_store"]["legacy_other"] = {"target": "memory"}
    raw["vector_store"]["layers_l0"]["params"]["locator"] = "other"
    register_backends()
    manager = StoreManagerProducer.build_named(
        "main", AssemblyContext.from_dict(raw)
    )

    default_bound = private(manager.kv(), "_relocatable_ports")
    other_bound = private(manager.kv("other"), "_relocatable_ports")
    assert {port.locator_name() for port in default_bound} == {"default"}
    assert {port.locator_name() for port in other_bound} == {"other"}
    assert len(default_bound) == 5 and len(other_bound) == 1
    assert not {id(p) for p in default_bound} & {id(p) for p in other_bound}


def test_relocation_rejects_non_relocatable_follower_only_for_enabled_kv(tmp_path) -> None:
    raw = _raw(tmp_path, relocate=True)
    raw["kv_store"]["default"]["params"]["relocate_on_update"] = False
    register_backends()
    manager = StoreManagerProducer.build_named("main", AssemblyContext.from_dict(raw))
    assert private(manager.kv(), "_relocatable_ports") == ()
