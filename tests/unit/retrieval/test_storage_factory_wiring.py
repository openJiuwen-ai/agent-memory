"""Retriever 通过 StorageProducer 获取共享统一 Storage。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import RecallChannel
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.storage import StorageProducer
from jiuwen_memory.storage.storage_impl import CompositeStorage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_factories():
    Factory.reset_all()
    yield
    Factory.reset_all()


def test_pipeline_retriever_uses_named_storage_instance() -> None:
    register_plugins()
    register_backends()
    register_operators()
    context = default_context()

    storage = StorageProducer.build_named("default", context)
    retriever = RetrieverProducer.build_named("default", context)

    assert isinstance(storage, CompositeStorage)
    assert isinstance(retriever, PipelineRetriever)
    assert retriever.storage is storage
    assert storage.recallers


def test_retriever_override_without_storage_still_uses_default_storage() -> None:
    register_plugins()
    register_backends()
    register_operators()
    # 召回路开关已由 retriever params 移到 storage 层（globals 回退）；retriever
    # params 只保留本算子私有调参（rerank_enabled 等）。
    context = default_context().merged(
        AssemblyContext.from_dict(
            {
                "globals": {
                    "vector_enabled": False,
                    "graph_enabled": False,
                    "layers_index_enabled": False,
                },
                "retriever": {
                    "default": {
                        "target": "pipeline",
                        "params": {
                            "rerank_enabled": False,
                        },
                    }
                },
            }
        )
    )

    storage = StorageProducer.build_named("default", context)
    retriever = RetrieverProducer.build_named("default", context)

    assert isinstance(retriever, PipelineRetriever)
    assert retriever.storage is storage
    assert isinstance(storage, CompositeStorage)
    assert [recaller.channel() for recaller in storage.recallers] == [RecallChannel.KEYWORD]


def test_recaller_assembly_error_surfaces_at_build_time() -> None:
    """recaller 装配错误（如选择键指向未注册实现）必须在构建期暴露，不拖到首次召回。"""
    register_plugins()
    register_backends()
    register_operators()
    context = default_context().merged(
        AssemblyContext.from_dict(
            {
                "storage": {
                    "default": {
                        "target": "composite",
                        "params": {"vector_recaller": "nonexistent_impl"},
                    }
                }
            }
        )
    )

    with pytest.raises(ValidationError):
        StorageProducer.build_named("default", context)
