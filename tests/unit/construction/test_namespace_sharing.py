"""垂直切片：两级命名空间下 index_builder 与 recaller 的 VectorStore 共享语义。

- 显式具名 + 同名引用 → 两者拿到**同一个** VectorStore（写读一致的来源）。
- 零配置（未声明 storage）→ 装配期直接失败。Storage 是有状态组件，不再匿名兜底构造：
  匿名实例不共享，两个算子会各拿一个，包装层状态（recallers/security/pipeline）随之分叉。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.recaller import RecallerProducer
from jiuwen_memory.storage.bootstrap import register_backends


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


def test_zero_config_assembly_fails_with_actionable_error():
    """未声明 storage 时装配期失败，并指出修复路径。

    早前此处会匿名兜底造一个 CompositeStorage，index_builder 与 recaller 因而各拿一个、
    互不共享。Storage 是有状态组件，匿名实例的 recallers/security/pipeline 会分叉——
    尤其 recallers 由 PipelineRetriever 单向 bind，未被绑定的那个实例 recall 恒返回空
    且不报错。故改为要求显式具名（见 test_reference_shares_vector_store）。
    """
    ctx = AssemblyContext.from_dict(
        {
            "constructor": {"ib": "vector"},
            "recaller": {"rec": "vector"},
        }
    )

    with pytest.raises(ValidationError, match="params.storage.*storage.default"):
        IndexBuilderProducer.build_named("ib", ctx)

    with pytest.raises(ValidationError, match="params.storage.*storage.default"):
        RecallerProducer.build_named("rec", ctx)
