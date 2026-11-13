# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""标准装配、插件替换和查询选择契约。"""

import asyncio
import inspect
from dataclasses import replace
from functools import wraps
from unittest.mock import Mock

import pytest
import yaml

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.pipeline import PipelineBinding
from jiuwen_memory.retrieval.query_parser import QueryParserProducer
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.retriever_impl.multimodal_retriever import MultimodalRetriever
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from tests.support.whitebox import private
from tests.unit.control.fixtures import selectable_retrieval_config

pytestmark = pytest.mark.unit


def capture_constructor(monkeypatch, cls, dependencies):
    original = cls.__init__

    @wraps(original)
    def capture(instance, *args, **kwargs):
        arguments = inspect.signature(original).bind(instance, *args, **kwargs)
        arguments.apply_defaults()
        dependencies[id(instance)] = arguments.arguments
        original(instance, *args, **kwargs)

    monkeypatch.setattr(cls, "__init__", capture)



@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud", "agent"])
def test_real_yaml_assembly_shares_parser_and_selects_pipeline(engine_kind, monkeypatch):
    dependencies = {}
    for cls in (VectorIndexBuilder, SimpleQueryParser, HashingEmbedder):
        capture_constructor(monkeypatch, cls, dependencies)
    built = []
    with Factory.observe_builds(built.append):
        kernel = _build_kernel(config=Config.from_yaml_str(
            yaml.safe_dump(selectable_retrieval_config(engine_kind)),
        ))
    try:
        engine = next(value for value in built if isinstance(value, MemoryEngine))
        assert engine.retrieval_pipeline_names() == ("default", "fast")
        retrievers = [value for value in built if isinstance(value, PipelineRetriever)]
        assert len(retrievers) == 2
        parsers = [value.query_parsers()[0] for value in retrievers]
        assert parsers[0] is parsers[1]
        vector_builders = [args for args in dependencies.values()
                           if isinstance(args["self"], VectorIndexBuilder)]
        assert vector_builders
        parser_args = dependencies.get(id(parsers[0]))
        assert parser_args is not None, "Parser constructor dependencies were not captured"
        assert all(value["embedder"] is parser_args["embedder"]
                   for value in vector_builders)
        embedder_args = dependencies.get(id(parser_args["embedder"]))
        assert embedder_args is not None, "Embedder constructor dependencies were not captured"
        assert embedder_args["tokenizer"] is parser_args["tokenizer"]
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
    built = []
    with Factory.observe_builds(built.append):
        kernel = _build_kernel(config=selectable_retrieval_config(engine_kind))
    engine = next(value for value in built if isinstance(value, MemoryEngine))
    profile_retriever = Mock(spec=Retriever)

    def empty_result(*_args):
        return RetrievalResult()

    profile_retriever.retrieve.side_effect = empty_result
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
        built = []
        with Factory.observe_builds(built.append):
            kernel = _build_kernel(config=config)
        try:
            engine = next(value for value in built if isinstance(value, MemoryEngine))
            result = asyncio.run(engine.recall(Scope(org="o", user="u"), RetrievalQuery(
                text="evidence", pipeline="fast", with_trajectory=True,
            )))
            assert any(step.stage == "custom_rerank" for step in result.trajectory)
            base = next(value for value in built
                        if isinstance(value, PipelineRetriever) and not isinstance(value, Variant))
            wrapper = MultimodalRetriever(base)
            assert wrapper.query_parsers() == base.query_parsers()
        finally:
            kernel.close()


def test_new_pipeline_health_failure_is_not_hidden(monkeypatch):
    built = []
    with Factory.observe_builds(built.append):
        kernel = _build_kernel(config=selectable_retrieval_config())
    try:
        engine = next(value for value in built if isinstance(value, MemoryEngine))
        fast = [value for value in built if isinstance(value, PipelineRetriever)][-1]
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
        built = []
        with Factory.observe_builds(built.append):
            kernel = _build_kernel(config=config)
        try:
            engine = next(value for value in built if isinstance(value, MemoryEngine))
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
    built = []
    with Factory.observe_builds(built.append):
        kernel = _build_kernel(config=config)
    try:
        engine = next(value for value in built if isinstance(value, MemoryEngine))
        result = asyncio.run(engine.recall(Scope(org="o", user="u"), RetrievalQuery(
            text="evidence", pipeline="combined", with_trajectory=True,
        )))
        assert result.trajectory[0].detail == {"name": "combined", "source": "query"}
        assert any(step.detail.get("strategy") == "rrf" for step in result.trajectory)
    finally:
        kernel.close()


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud", "agent"])
def test_named_binding_constrains_direct_engine_queries_without_mutating_input(engine_kind):
    from jiuwen_memory.common.type_def import MemoryUnit, Segment, matches_memory_unit

    config = selectable_retrieval_config(engine_kind)
    config["engine"]["default"]["params"]["retrieval_pipelines"] = {
        "ordinary": {"retriever": "default", "exclude_memory_types": ["experience", "skill"]},
        "lessons": {"retriever": "default", "memory_types": ["experience"]},
    }
    kernel = _build_kernel(config=config)
    try:
        engine = private(kernel.api, "_engine")
        scope = Scope(org="o", user="u")
        units = [MemoryUnit(
            id=kind, scope=scope, segments=[Segment(content="evidence")],
            system_metadata={"memory_type": kind},
        ) for kind in ["chat", "experience", "skill"]]
        private(engine, "_index").build(units)
        ordinary = asyncio.run(engine.recall(scope, RetrievalQuery(
            text="evidence", pipeline="ordinary",
        )))
        assert {item.unit_id for item in ordinary.items} == {"chat"}, "普通流程只查普通条目"
        query = RetrievalQuery(text="evidence", pipeline="lessons")
        lessons = asyncio.run(engine.recall(scope, query))
        assert {item.unit_id for item in lessons.items} == {"experience"}, "配置名无需等于类型名"
        assert query.filters is None, "不把固定条件写回调用方查询对象"
        predicate = engine.retrieval_pipeline_filter("lessons")
        assert matches_memory_unit(units[1], predicate), "配置边界使用标准FilterExpr"
        assert not matches_memory_unit(units[0], predicate), "普通条目在召回前被排除"
    finally:
        kernel.close()


@pytest.mark.parametrize("binding", [
    {"retriever": "default", "memory_types": "experience"},
    {"retriever": "default", "memory_types": []},
    {"retriever": "default", "memory_types": [""]},
    {"retriever": "default", "exclude_memory_types": [1]},
    {"retriever": "default", "memory_types": ["experience"],
     "exclude_memory_types": ["experience"]},
    {"retriever": "default", "memory_type": "experience"},
])
def test_invalid_type_bindings_are_rejected_during_assembly(binding):
    config = selectable_retrieval_config()
    config["engine"]["default"]["params"]["retrieval_pipelines"] = {"lessons": binding}
    with pytest.raises(ValidationError, match="retrieval_pipelines"):
        _build_kernel(config=config)
