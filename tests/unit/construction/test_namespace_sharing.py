"""垂直切片：两级命名空间下 index_builder 与 recaller 的 VectorStore 共享语义。

- 显式具名 + 同名引用 → 两者拿到**同一个** VectorStore（写读一致的来源）。
- 零配置（各自匿名默认）→ 两者各拿一个、**互相独立**。
  已确认的取舍：要共享须显式配置。
"""

from __future__ import annotations

import pytest

from common.bootstrap import register_plugins
from common.factory.factory import Factory
from config.context import AssemblyContext
from construction.bootstrap import register_constructors
from construction.index_builder import IndexBuilderProducer
from retrieval.bootstrap import register_operators
from retrieval.recaller import RecallerProducer
from storage.bootstrap import register_backends


@pytest.fixture(autouse=True)
def _bootstrap():
    register_plugins()
    register_backends()
    register_operators()
    register_constructors()
    Factory.reset_all()
    yield
    Factory.reset_all()


def test_reference_shares_vector_store():
    ctx = AssemblyContext.from_dict(
            {
                "vector_store": {"shared_vec": {"target": "memory"}},
                "storage": {
                    "shared": {
                        "target": "composite",
                        "params": {"vector_store": "shared_vec"},
                    }
                },
                "constructor": {"ib": {"target": "vector", "params": {"storage": "shared"}}},
                "recaller": {"rec": {"target": "vector", "params": {"storage": "shared"}}},
            }
    )
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    assert getattr(ib, "_vector_store") is getattr(rec, "_vector")  # 同名引用 → 共享


def test_zero_config_vector_store_is_independent():
    ctx = AssemblyContext.from_dict(
        {
            "constructor": {"ib": "vector"},
            "recaller": {"rec": "vector"},
        }
    )
    ib = IndexBuilderProducer.build_named("ib", ctx)
    rec = RecallerProducer.build_named("rec", ctx)
    assert getattr(ib, "_vector_store") is not getattr(rec, "_vector")  # 各自匿名默认
