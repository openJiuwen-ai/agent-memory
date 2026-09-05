"""垂直切片：两级命名空间下 index_builder 与 recaller 的 VectorStore 共享语义。

F08 语义：共享的载体是**全局唯一 StoreManager 的命名端口**——具名 VectorStore
（如 ``vector_store.shared_vec``）经全量自动成为 manager 端口，消费者用
``params.vector_store: <端口名>`` 引用同一端口 → 同一实例。

- 显式具名 + 同名引用 → 两者拿到**同一个** VectorStore（写读一致的来源）。
- 零配置（各自默认端口）→ 仍共享 manager 的 default 端口实例；要独立须指名
  不同端口。已确认的取舍：要跨消费者共享/隔离须显式配置端口名。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.recaller import RecallerProducer
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.store_manager import StoreManagerProducer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _bootstrap():
    register_plugins()
    register_backends()
    register_operators()
    register_constructors()
    Factory.reset_all()
    yield
    Factory.reset_all()


def test_reference_shares_vector_port():
    """ib 与 rec 指名同一 vector 端口 → 同一实例（写读一致来源）。"""
    ctx = AssemblyContext.from_dict(
        {
            "vector_store": {
                "shared_vec": {"target": "memory"},
            },
            "store_manager": {
                "default": {
                    "target": "composite",
                    "params": {"kv_store": {"target": "memory"}, "vector_store": "shared_vec"},
                }
            },
            "constructor": {
                "ib": {
                    "target": "vector",
                    "params": {"vector_store": "shared_vec"},
                }
            },
            "recaller": {
                "rec": {
                    "target": "vector",
                    "params": {"vector_store": "shared_vec"},
                }
            },
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    # 同名端口 → 共享（与 manager.vector("shared_vec") 同一实例）
    assert ib._vector_store is manager.vector("shared_vec")  # pylint: disable=protected-access
    assert rec.vector_store is manager.vector("shared_vec")


def test_zero_config_uses_default_port_shared_by_manager():
    """零配置 → 两者都落 manager 的 default 端口（同一实例，F08 全局共享语义）。"""
    ctx = AssemblyContext.from_dict(
        {
            "store_manager": {
                "default": {
                    "target": "composite",
                    "params": {
                        "kv_store": {"target": "memory"},
                        "vector_store": {"target": "memory"},
                    }
                }
            },
            "constructor": {"ib": "vector"},
            "recaller": {"rec": "vector"},
        }
    )
    manager = StoreManagerProducer.build_named("default", ctx)
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    assert ib._vector_store is manager.vector()  # pylint: disable=protected-access
    assert rec.vector_store is manager.vector()
