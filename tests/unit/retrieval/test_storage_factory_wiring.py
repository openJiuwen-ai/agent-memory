"""Retriever 通过 StorageProducer 获取共享统一 Storage。"""

from __future__ import annotations

import pytest

from common.bootstrap import register_plugins
from common.factory.factory import Factory
from config import AssemblyContext
from config.defaults import default_context
from retrieval.bootstrap import register_operators
from retrieval.retriever import RetrieverProducer
from retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from storage.bootstrap import register_backends
from storage.storage import StorageProducer
from storage.storage_impl import CompositeStorage

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
    assert retriever.recallers


def test_retriever_override_without_storage_still_uses_default_storage() -> None:
    register_plugins()
    register_backends()
    register_operators()
    context = default_context().merged(
        AssemblyContext.from_dict(
            {
                "retriever": {
                    "default": {
                        "target": "pipeline",
                        "params": {
                            "vector_enabled": False,
                            "graph_enabled": False,
                            "layers_index_enabled": False,
                            "rerank_enabled": False,
                        },
                    }
                }
            }
        )
    )

    storage = StorageProducer.build_named("default", context)
    retriever = RetrieverProducer.build_named("default", context)

    assert isinstance(retriever, PipelineRetriever)
    assert retriever.storage is storage
