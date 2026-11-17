# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S6：分片装配、支持矩阵与物理资源命名校验。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.store_manager import StoreManagerProducer

pytestmark = pytest.mark.unit


def _base(extra: dict | None = None) -> dict:
    raw: dict = {
        "globals": {"store_manager": "main", "vector_enabled": False, "graph_enabled": False},
        "shard_resolver": {
            "default": {
                "target": "rule_based",
                "params": {
                    "shards": ["working", "profile"],
                    "default_shard": "working",
                    "rules": [{"shard": "profile", "match": {"tier": "core"}}],
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
                    "table_template": {
                        "target": "memory",
                        "params": {"table": "agent_memory_kv_{shard}"},
                    },
                },
            },
            "legacy": {"target": "memory"},
        },
        "store_manager": {"main": {"target": "composite"}},
    }
    if extra:
        for key, value in extra.items():
            raw[key] = value
    return raw


def _build(raw: dict):
    register_backends()
    ctx = AssemblyContext.from_dict(raw)
    return StoreManagerProducer.build_named("main", ctx)


def test_sharded_kv_assembles_from_templates() -> None:
    manager = _build(_base())
    kv = manager.kv()
    assert kv.shards == ("working", "profile")
    assert callable(kv.locate)


def _encrypted(raw: dict) -> dict:
    raw["globals"]["storage_encryption_enabled"] = True
    raw["cryptography"] = {
        "default": {"target": "local", "params": {"key_provider": "default"}}
    }
    raw["key_provider"] = {"default": {"target": "local", "params": {"key_hex": "11" * 32}}}
    return raw


def test_sharded_kv_assembles_with_global_encryption() -> None:
    """P0：全局加密 + 分片部署必须启动，且每层只加密一次。"""
    from jiuwen_memory.api.memory_api_impl.assembly import _register_all
    from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore

    _register_all()
    manager = _build(_encrypted(_base()))
    raw = getattr(manager, "_named_stores")[
        next(iter(getattr(manager, "_named_stores")))
    ]["default"]
    assert isinstance(raw, ShardRoutedKVStore)
    assert isinstance(getattr(raw, "_passthrough"), EncryptedKVStore)
    assert isinstance(getattr(raw, "_placement"), EncryptedKVStore)
    for store in getattr(raw, "_stores").values():
        assert isinstance(store, EncryptedKVStore)
        assert isinstance(getattr(store, "_raw"), InMemoryKVStore)  # 只包一层

    port = manager.kv()
    assert port.shards == ("working", "profile")
    assert callable(port.locate)
    assert callable(port.register_topology)
    from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment

    scope = Scope(org="acme", user="u1")
    unit = MemoryUnit(id="u1", scope=scope, segments=[Segment(content="x")])
    from jiuwen_memory.common.type_def.memory import memory_key
    from jiuwen_memory.common.type_def.memory_codec import dumps

    port.insert(scope, memory_key("u1"), dumps(unit))
    assert port.get(scope, memory_key("u1"))


def test_document_mode_with_sharding_rejected() -> None:
    """TC-S6-1：Document Mode + 分片启动失败。"""
    raw = _base({"globals": {"store_manager": "main", "write_document": True}})
    with pytest.raises(ValidationError):
        _build(raw)


def test_fusion_with_sharding_rejected() -> None:
    """TC-S6-1：FusionStore 消费者 + 分片启动失败。"""
    raw = _base({"fusion_store": {"default": {"target": "memory"}}})
    with pytest.raises(ValidationError):
        _build(raw)


def test_partial_template_failure_closes_new_instances() -> None:
    """S6-4：模板部分构建失败时只清理本次新建实例。"""
    from jiuwen_memory.storage.kv import KvProducer
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    closed: list[str] = []

    class _Closable(InMemoryKVStore):
        def close(self) -> None:
            closed.append("closed")

    with KvProducer.registration_scope():
        KvProducer.register("closable")(lambda config: _Closable())
        raw = _base(
            {
                "kv_store": {
                    "default": {
                        "target": "shard_routed",
                        "params": {
                            "resolver": "default",
                            "passthrough": "legacy",
                            "placement_store": {"target": "memory"},
                            "tables": {
                                "working": {"target": "closable"},
                                "profile": {"target": "unregistered"},
                            },
                        },
                    },
                    "legacy": {"target": "memory"},
                }
            }
        )
        with pytest.raises(ValidationError):
            _build(raw)
    assert closed == ["closed"]


def test_locator_must_declare_shard_locator_at_assembly() -> None:
    """P2：定位端口的能力在装配期校验，不拖到首次写入。"""
    raw = _base(
        {
            "vector_store": {
                name: {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "locator": "legacy",  # legacy 是普通 KV，不是定位端口
                        "mode": "split",
                        "table_template": {
                            "target": "memory",
                            "params": {"table": f"v_{name}_{{shard}}"},
                        },
                    },
                }
                for name in ("default", "layers_l0", "layers_l1")
            }
        }
    )
    with pytest.raises(ValidationError, match="SHARD_LOCATOR"):
        _build(raw)


def test_mixed_vector_ports_rejected() -> None:
    """S6：向量三层端口必须全部配置成分片，不能只分一层。"""
    raw = _base(
        {
            "vector_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "mode": "split",
                        "table_template": {"target": "memory", "params": {"table": "v_{shard}"}},
                    },
                },
                "layers_l0": {"target": "memory"},
            }
        }
    )
    with pytest.raises(ValidationError):
        _build(raw)


def test_graph_shard_target_assembles_shared() -> None:
    """S9：图端口支持 shared 分片；shared 内层必须声明前置过滤。"""
    raw = _base(
        {
            "globals": {
                "store_manager": "main",
                "vector_enabled": False,
                "graph_enabled": True,
            },
            "graph_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "inner": {"target": "memory"},
                    },
                }
            },
        }
    )
    from jiuwen_memory.storage.capability import StoreCapability, capabilities_of

    manager = _build(raw)
    graph = manager.graph()
    assert StoreCapability.SHARD_ROUTED in capabilities_of(graph)
    assert graph.shards == ("working", "profile")


def test_graph_shard_split_assembles_per_shard_stores() -> None:
    """S9：split 模式按分片持有独立图存储。"""
    raw = _base(
        {
            "globals": {
                "store_manager": "main",
                "vector_enabled": False,
                "graph_enabled": True,
            },
            "graph_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "mode": "split",
                        "stores": {
                            "working": {"target": "memory"},
                            "profile": {"target": "memory"},
                        },
                    },
                }
            },
        }
    )
    from jiuwen_memory.storage.capability import StoreCapability, capabilities_of

    manager = _build(raw)
    graph = manager.graph()
    assert StoreCapability.SHARD_ROUTED in capabilities_of(graph)


def test_evidence_graph_rejects_shard_routed_backend() -> None:
    """证据图只能用普通后端：分片图缺少有向邻接与条件投影能力。"""
    from jiuwen_memory.construction.index_builder_impl.evidence_graph import (
        EvidenceGraphBuilder,
    )

    raw = _base(
        {
            "globals": {
                "store_manager": "main",
                "vector_enabled": False,
                "graph_enabled": True,
            },
            "graph_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "inner": {"target": "memory"},
                    },
                }
            },
        }
    )
    with pytest.raises(ValidationError, match="incident edge capability"):
        EvidenceGraphBuilder(_build(raw).graph())


def test_template_without_shard_placeholder_rejected() -> None:
    """TC-S6-2：模板不给分片占位符会让所有分片指向同一资源。"""
    raw = _base(
        {
            "kv_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "passthrough": "legacy",
                        "placement_store": {"target": "memory"},
                        "table_template": {"target": "memory", "params": {"table": "kv"}},
                    },
                },
                "legacy": {"target": "memory"},
            }
        }
    )
    with pytest.raises(ValidationError):
        _build(raw)


def test_missing_table_for_shard_rejected() -> None:
    """TC-S6-2：某分片既无模板也无覆盖时启动失败。"""
    raw = _base(
        {
            "kv_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "passthrough": "legacy",
                        "placement_store": {"target": "memory"},
                        "tables": {
                            "working": {
                                "target": "memory",
                                "params": {"table": "kv_working"},
                            }
                        },
                    },
                },
                "legacy": {"target": "memory"},
            }
        }
    )
    with pytest.raises(ValidationError):
        _build(raw)


def test_placement_store_requires_conditional_capability() -> None:
    """TC-S6-3：归属存储未声明 ConditionalKV 时启动失败。"""
    register_backends()
    from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
    from jiuwen_memory.storage.kv import KvProducer
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    class _NoCapabilityKV(InMemoryKVStore):
        def capabilities(self):
            return frozenset()

    raw = _base()
    raw["kv_store"]["default"]["params"]["placement_store"] = "no_cap"
    ctx = AssemblyContext.from_dict(raw)
    KvProducer.put("no_cap", _NoCapabilityKV(), ctx=ctx)
    with pytest.raises(UnsupportedStorageCapabilityError):
        StoreManagerProducer.build_named("main", ctx)


def test_connection_budget_is_read_from_globals_and_validated() -> None:
    """预算键走 globals 回退；配错时即使没有可汇总的池也要报错。"""
    raw = _base()
    raw["globals"]["storage_connection_budget"] = "not-a-number"
    with pytest.raises(ValidationError):
        _build(raw)

    raw = _base()
    raw["globals"]["storage_connection_budget"] = 128
    assert _build(raw).kv() is not None
