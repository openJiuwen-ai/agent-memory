# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``domain_stores`` 段：数据面装配参数归位后的声明形态、继承规则与校验。

F07 把数据面的全部装配参数（``preferred_retrieval_pipeline`` / ``kv_store`` /
``domain_store_target`` / 七个 ``*_recaller`` 选择键）从 ``store_manager.<inst>.params``
下移到 ``params.domain_stores.<name>``，并允许显式声明 ``default``。归位前这些键寄居在
manager 段，导致默认数据面与命名数据面对 params/globals 的读法相反、
``preferred_retrieval_pipeline`` 是唯一不继承的键——本文件是这段逻辑的首个覆盖
（归位前 ``domain_stores`` 段全仓零用例，缺陷因此从未暴露）。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, RetrievalPipeline, Scope, Segment
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManagerProducer

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="org", user="u")


def _unit(unit_id: str) -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=_SCOPE, segments=[Segment(content="c")])


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """具名实例缓存跨测试隔离：本文件全部用例走 producer 级 build_named。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


def _ctx(params: dict[str, Any], **extra_ns: Any) -> AssemblyContext:
    """最小装配上下文：内存 KV + 全文，关掉向量/图以压缩召回路到 keyword 一路。"""
    raw: dict[str, Any] = {
        "globals": {
            "store_manager": "main",
            "vector_enabled": False,
            "graph_enabled": False,
            "layers_index_enabled": False,
        },
        "kv_store": {"default": "memory"},
        "fulltext_store": {"default": {"target": "memory", "params": {"tokenizer": "default"}}},
        "tokenizer": {"default": "whitespace"},
        "recaller": {"keyword": {"target": "keyword"}},
        "store_manager": {"main": {"target": "composite", "params": params}},
    }
    raw.update(extra_ns)
    return AssemblyContext.from_dict(raw)


def _build(params: dict[str, Any], **extra_ns: Any) -> Any:
    register_plugins()
    register_backends()
    return StoreManagerProducer.build_named("main", _ctx(params, **extra_ns))


# ---------------------------------------------------------------------------
# default entry：数据面参数的新家
# ---------------------------------------------------------------------------


def test_default_entry_drives_pipeline_kv_and_recallers() -> None:
    """``domain_stores.default`` 三类键全部生效——manager 段不再承载数据面配置。"""
    manager = _build(
        {
            "domain_stores": {
                "default": {
                    "kv_store": "truth",
                    "preferred_retrieval_pipeline": "recall_and_get_rank",
                    "keyword_recaller": "keyword",
                }
            }
        },
        kv_store={"default": "memory", "truth": "memory"},
    )
    domain = manager.domain_store()

    assert domain.preferred_retrieval_pipeline() is RetrievalPipeline.RECALL_AND_GET_RANK
    assert [r.channel().value for r in domain.recallers] == ["keyword"]
    # kv_store 指名生效的判别式：写入只落 truth 端口，default 端口保持空
    domain.add(_SCOPE, [_unit("u1")])
    assert manager.kv("truth").list(_SCOPE).count == 1
    assert manager.kv().list(_SCOPE).count == 0


def test_default_domain_store_built_without_any_declaration() -> None:
    """``domain_stores`` 整段缺省仍建出 default 数据面（全默认 profile）。"""
    manager = _build({})
    domain = manager.domain_store()

    assert manager.has_domain_store()
    assert domain.preferred_retrieval_pipeline() is RetrievalPipeline.RECALL_GET_RANK
    assert StorageCapability.KV in manager.capabilities()


def test_default_key_is_accepted() -> None:
    """归位前 ``domain_stores`` 显式声明 ``default`` 会抛 ValidationError；现在是正常形态。"""
    manager = _build(
        {"domain_stores": {"default": {"preferred_retrieval_pipeline": "retrieve"}}}
    )

    assert manager.domain_store().preferred_retrieval_pipeline() is RetrievalPipeline.RETRIEVE


# ---------------------------------------------------------------------------
# 命名 entry：overlay 在 default 之上
# ---------------------------------------------------------------------------


def test_named_entry_overrides_only_declared_keys() -> None:
    """命名 entry 只覆盖自己声明的键，其余继承 default——一条规则对所有键统一生效。"""
    manager = _build(
        {
            "domain_stores": {
                "default": {
                    "preferred_retrieval_pipeline": "recall_get_rank",
                    "keyword_recaller": "keyword",
                },
                "fast": {"preferred_retrieval_pipeline": "retrieve"},
            }
        }
    )

    assert manager.domain_store().preferred_retrieval_pipeline() is (
        RetrievalPipeline.RECALL_GET_RANK
    )
    assert manager.domain_store("fast").preferred_retrieval_pipeline() is (
        RetrievalPipeline.RETRIEVE
    )


def test_named_entry_shares_recaller_instance_with_default() -> None:
    """命名 entry 未声明 ``*_recaller`` 时继承 default 的选择键，拿到**同一个** recaller 实例。

    这是继承规则的存在理由，不是便利：``RecallerProducer.dep`` 读的是
    ``config.params``（params 直读，**不回退 globals**）。命名 entry 若拿不到
    ``keyword_recaller`` 键，``dep`` 会落到 ``cls.build(default, {}, ctx)`` 匿名新建一套
    不共享、params 为空的 recaller——装配不报错，退化完全静默。故这里断言的是对象身份
    而非通道名：通道名在两种情形下都相同，只有 ``is`` 能分辨。
    """
    manager = _build(
        {
            "domain_stores": {
                "default": {"keyword_recaller": "keyword"},
                "fast": {"preferred_retrieval_pipeline": "retrieve"},
            }
        }
    )

    default_recallers = manager.domain_store().recallers
    fast_recallers = manager.domain_store("fast").recallers

    assert len(default_recallers) == len(fast_recallers) == 1
    assert fast_recallers[0] is default_recallers[0]


def test_named_entry_can_override_recaller_selection() -> None:
    """继承是缺省行为不是强制：entry 显式声明 ``*_recaller`` 时用自己的具名实例。"""
    manager = _build(
        {
            "domain_stores": {
                "default": {"keyword_recaller": "keyword"},
                "aux": {"keyword_recaller": "keyword_aux"},
            }
        },
        recaller={"keyword": {"target": "keyword"}, "keyword_aux": {"target": "keyword"}},
    )

    assert manager.domain_store("aux").recallers[0] is not manager.domain_store().recallers[0]


def test_named_entry_inherits_default_kv_port() -> None:
    """``kv_store`` 与检索 profile 各键同规则：default 声明的真源端口被命名实例继承。"""
    manager = _build(
        {
            "domain_stores": {
                "default": {"kv_store": "truth"},
                "fast": {"preferred_retrieval_pipeline": "retrieve"},
            }
        },
        kv_store={"default": "memory", "truth": "memory"},
    )

    manager.domain_store("fast").add(_SCOPE, [_unit("u1")])

    # 判别式：命名数据面的写入落在 default 声明的 truth 端口，而非各自退回 default
    assert manager.kv("truth").list(_SCOPE).count == 1
    assert manager.kv().list(_SCOPE).count == 0
    assert manager.domain_store("fast").preferred_retrieval_pipeline() is (
        RetrievalPipeline.RETRIEVE
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_domain_stores_section_must_be_mapping() -> None:
    with pytest.raises(ValidationError, match="domain_stores"):
        _build({"domain_stores": ["fast"]})


def test_domain_stores_entry_must_be_mapping() -> None:
    with pytest.raises(ValidationError, match="domain_stores.fast"):
        _build({"domain_stores": {"fast": "retrieve"}})


def test_unknown_pipeline_in_entry_fails_at_build_time() -> None:
    """非法 pipeline 在装配期 fail-fast，两条构造路径（直构/Producer）同一错误契约。"""
    with pytest.raises(ValidationError, match="preferred_retrieval_pipeline"):
        _build({"domain_stores": {"default": {"preferred_retrieval_pipeline": "nope"}}})


# ---------------------------------------------------------------------------
# manager 段不再承载数据面配置
# ---------------------------------------------------------------------------


def test_ports_come_from_namespaces_not_manager_params() -> None:
    """manager params 不含任何 ``<ns>_store`` 引用键时七类端口照样齐。

    F07 起端口由命名空间全量聚合（声明即端口），``vector_store`` / ``fulltext_store`` /
    ``graph_store`` 作为 manager params 键已无读者——本用例锁死这一点，防止有人
    "顺手补回"制造 manager 段承载数据面/端口配置的假象。
    """
    manager = _build(
        {},
        vector_store={"default": "memory"},
        graph_store={"default": "memory"},
    )

    assert {
        StorageCapability.KV,
        StorageCapability.VECTOR,
        StorageCapability.FULLTEXT,
        StorageCapability.GRAPH,
    } <= manager.capabilities()
