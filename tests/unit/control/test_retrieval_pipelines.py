# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""标准装配、插件替换和查询选择契约。"""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
import yaml

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.control.pipeline import PipelineBinding
from jiuwen_memory.retrieval.query_parser import QueryParserProducer
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.retriever_impl.multimodal_retriever import MultimodalRetriever
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from tests.unit.control.fixtures import selectable_retrieval_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud", "agent"])
def test_real_yaml_assembly_shares_parser_and_selects_pipeline(engine_kind):
    built = []
    with Factory.observe_builds(built.append):
        kernel = _build_kernel(config=Config.from_yaml_str(
            yaml.safe_dump(selectable_retrieval_config(engine_kind)),
        ))
    try:
        engine = kernel.api._engine
        assert engine.retrieval_pipeline_names() == ("default", "fast")
        retrievers = [value for value in built if isinstance(value, PipelineRetriever)]
        assert len(retrievers) == 2
        parsers = [value.query_parsers()[0] for value in retrievers]
        assert parsers[0] is parsers[1]
        vector_builders = [
            value._vector_builder for value in built if isinstance(value, HybridIndexBuilder)
        ]
        assert vector_builders
        assert all(value._embedder is parsers[0]._embedder for value in vector_builders)
        assert parsers[0]._embedder._tokenizer is parsers[0]._tokenizer
        query = RetrievalQuery(text="evidence", pipeline="fast", with_trajectory=True)
        result = asyncio.run(engine.recall(Scope(org="o", user="u"), query))
        assert result.trajectory[0].detail == {"name": "fast", "source": "query"}
        assert not any(step.stage == "rerank" for step in result.trajectory)
        with pytest.raises(ValidationError, match="available: default, fast"):
            asyncio.run(engine.recall(Scope(), replace(query, pipeline="unknown")))
    finally:
        kernel.close()


@pytest.mark.parametrize("bad_parser", [None, {"target": "simple"}, "different"])
def test_assembly_rejects_anonymous_or_different_parser(bad_parser):
    config = selectable_retrieval_config()
    config["query_parser"]["different"] = config["query_parser"]["default"]
    params = config["retriever"]["fast"]["params"]
    params.pop("query_parser")
    if bad_parser is not None:
        params["query_parser"] = bad_parser
    with pytest.raises(ValidationError, match="query_parser"):
        _build_kernel(config=config)


@pytest.mark.parametrize("references", [None, [], {"": "fast"}, {"fast": {}}, {"fast": "missing"}])
def test_invalid_whitelist_fails_at_assembly(references):
    config = selectable_retrieval_config()
    config["engine"]["default"]["params"]["retrieval_pipelines"] = references
    with pytest.raises(ValidationError):
        _build_kernel(config=config)


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud", "agent"])
def test_explicit_selection_bypasses_profile_without_mutating_default(engine_kind, monkeypatch):
    kernel = _build_kernel(config=selectable_retrieval_config(engine_kind))
    engine = kernel.api._engine
    profile_retriever = Mock(spec=Retriever)
    profile_retriever.retrieve.side_effect = lambda *_: RetrievalResult()
    binding = PipelineBinding("profile-name", Mock(), profile_retriever, Mock())
    profile = Mock(return_value=binding)
    monkeypatch.setattr(engine, "_recall_binding", profile)
    scope = Scope(org="o", user="u")
    try:
        explicit = asyncio.run(engine.recall(scope, RetrievalQuery(
            text="evidence", pipeline="fast", with_trajectory=True,
        )))
        profile.assert_not_called()
        assert explicit.trajectory[0].detail["source"] == "query"
        inherited = asyncio.run(engine.recall(scope, RetrievalQuery(with_trajectory=True)))
        assert inherited.trajectory[0].detail == {"name": "profile-name", "source": "profile"}
        profile_retriever.retrieve.assert_called_once()
        profile.return_value = None
        default = asyncio.run(engine.recall(scope, RetrievalQuery(with_trajectory=True)))
        assert default.trajectory[0].detail == {"name": "default", "source": "default"}
    finally:
        kernel.close()


def test_registered_stage_variant_and_composite_need_no_engine_changes():
    class Variant(PipelineRetriever):
        def rerank(self, state):
            state.run.record("custom_rerank", 0.0)

    def build_variant(component):
        from jiuwen_memory.retrieval.discloser import DiscloserProducer
        from jiuwen_memory.retrieval.fuser import FuserProducer

        return Variant(
            QueryParserProducer.dep(component),
            FuserProducer.build_named("default", component.ctx),
            DiscloserProducer.build_named("default", component.ctx), None,
            domain_store=StoreManagerProducer.resolve(component).domain_store(),
        )

    config = selectable_retrieval_config()
    config["retriever"]["fast"] = {
        "target": "test_stage_variant", "params": {"query_parser": "default"},
    }
    with RetrieverProducer.registration_scope():
        RetrieverProducer.register("test_stage_variant")(build_variant)
        kernel = _build_kernel(config=config)
        try:
            engine = kernel.api._engine
            result = asyncio.run(engine.recall(Scope(org="o", user="u"), RetrievalQuery(
                text="evidence", pipeline="fast", with_trajectory=True,
            )))
            assert any(step.stage == "custom_rerank" for step in result.trajectory)
            wrapper = MultimodalRetriever(engine._retriever)
            assert wrapper.query_parsers() == engine._retriever.query_parsers()
        finally:
            kernel.close()


def test_new_pipeline_health_failure_is_not_hidden(monkeypatch):
    kernel = _build_kernel(config=selectable_retrieval_config())
    try:
        engine = kernel.api._engine
        fast = engine._retrieval_pipelines._pipelines["fast"]
        monkeypatch.setattr(fast, "health", Mock(side_effect=BackendError("unavailable")))
        with pytest.raises(BackendError, match="unavailable"):
            engine.health()
    finally:
        kernel.close()


def test_unconfigured_and_empty_whitelists_keep_default_routing():
    for references, expected in [(None, ("default",)), ({}, ())]:
        config = selectable_retrieval_config()
        params = config["engine"]["default"]["params"]
        if references is None:
            params.pop("retrieval_pipelines")
        else:
            params["retrieval_pipelines"] = references
        kernel = _build_kernel(config=config)
        try:
            engine = kernel.api._engine
            assert engine.retrieval_pipeline_names() == expected
            assert asyncio.run(engine.recall(Scope(), RetrievalQuery())).items == []
            if references is None:
                assert asyncio.run(engine.recall(Scope(), RetrievalQuery(pipeline="default")))
            else:
                with pytest.raises(ValidationError, match="unknown retrieval_pipeline"):
                    asyncio.run(engine.recall(Scope(), RetrievalQuery(pipeline="default")))
        finally:
            kernel.close()


def test_parser_new_instance_cannot_bypass_sharing_check():
    config = selectable_retrieval_config()
    config["query_parser"]["default"]["new_instance"] = True
    with pytest.raises(ValidationError, match="query_parser"):
        _build_kernel(config=config)


def test_unannounced_parser_capability_fails_closed(monkeypatch):
    monkeypatch.setattr(PipelineRetriever, "query_parsers", lambda _: None)
    with pytest.raises(ValidationError, match="query_parsers"):
        _build_kernel(config=selectable_retrieval_config())


def test_composite_pipeline_is_selectable_through_public_parser_declaration():
    config = selectable_retrieval_config()
    config["retriever"]["combined"] = {
        "target": "multimodal", "params": {"base_retriever": "default"},
    }
    config["engine"]["default"]["params"]["retrieval_pipelines"]["combined"] = "combined"
    kernel = _build_kernel(config=config)
    try:
        result = asyncio.run(kernel.api._engine.recall(Scope(org="o", user="u"), RetrievalQuery(
            text="evidence", pipeline="combined", with_trajectory=True,
        )))
        assert result.trajectory[0].detail == {"name": "combined", "source": "query"}
        assert any(step.detail.get("strategy") == "rrf" for step in result.trajectory)
    finally:
        kernel.close()
