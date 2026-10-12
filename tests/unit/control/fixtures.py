# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic Schema update world with real API, engines and index builders."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from inspect import signature
from types import SimpleNamespace

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import EntityStoreFilters, hash_entity_text
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import EntitySchemaExtractor
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import EntityLinkService
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.pipeline import PipelineBinding
from tests.unit.construction.test_entity_linker import InMemoryEntityStore

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def capture_constructor(monkeypatch, component, **overrides):
    """Retain real instances and injected dependencies at their constructor boundary."""
    constructor = component.__init__
    parameters = signature(constructor)
    captured = SimpleNamespace()

    def initialize(instance, *args, **kwargs):
        bound = parameters.bind(instance, *args, **kwargs)
        bound.arguments.update(overrides)
        constructor(*bound.args, **bound.kwargs)
        captured.instance = instance
        captured.dependencies = SimpleNamespace(**bound.arguments)

    monkeypatch.setattr(component, "__init__", initialize)
    return captured


def capture_engine(monkeypatch, engine_kind, pipeline):
    """Capture each engine through its current dependency-injection boundary."""
    if engine_kind != "cloud":
        return capture_constructor(monkeypatch, InMemoryEngine, pipeline=pipeline)

    constructor = CloudEngine.__init__
    captured = SimpleNamespace()

    def initialize(instance, dependencies, options=None):
        injected = replace(dependencies, pipeline=pipeline)
        constructor(instance, injected, options)
        captured.instance = instance
        captured.dependencies = injected

    monkeypatch.setattr(CloudEngine, "__init__", initialize)
    return captured


class SchemaReplyLLM(LLM):
    """Script properties only; actual normalization and all persistence remain real."""

    def __init__(self):
        self.facts = [("陈静", "occupation", "陈静负责推荐算法迭代", "")]
        self.response = None
        self.calls = 0

    def plugin_type(self):
        return PluginType.LLM

    def health(self):
        pass

    def chat(self, messages, **options):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        ids = re.findall(r"unit_id=([^\]\r\n]+)", messages[-1].content)
        if self.response is not None:
            return self.response.replace("__ID__", ids[0])
        entities = {}
        for name, prop, value, event in self.facts:
            entity = entities.setdefault(
                name, {"name": name, "entity_type": "person", "properties": []}
            )
            entity["properties"].append(
                {"property_name": prop, "value": value, "time": event, "source_unit_ids": ids}
            )
        return json.dumps(
            {
                "entities": list(entities.values()),
                "update_complete": True,
                "unresolved_retractions": [],
            },
            ensure_ascii=False,
        )


class SchemaWorld:
    def __init__(self, tmp_path, monkeypatch, engine_kind="in_memory"):
        monkeypatch.setattr(
            "jiuwen_memory.api.memory_api_impl.assembly.setup_logging", lambda config: None
        )
        schema = [
            {
                "entity_type": "person",
                "dynamic_property": {
                    "occupation": {"desc": "work"},
                    "review": {"desc": "reviewed document"},
                },
            }
        ]
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        self.llm = SchemaReplyLLM()
        self.entity_store = InMemoryEntityStore()
        self.pipeline = SimpleNamespace(
            select_for_write=lambda units: None, select_for_recall=lambda query: None
        )
        with monkeypatch.context() as assembly_patch:
            wiring = capture_engine(assembly_patch, engine_kind, self.pipeline)
            capture_constructor(assembly_patch, EntitySchemaExtractor, llm=self.llm)
            capture_constructor(
                assembly_patch, HybridIndexBuilder,
                entity_linker=EntityLinkService(entity_store=self.entity_store),
            )
            self.kernel = _build_kernel(
                config={
                    "globals": {
                        "schema_enabled": True,
                        "graph_enabled": False,
                        "rerank_enabled": False,
                        "layers_index_enabled": False,
                    },
                    "engine": {
                        "default": {
                            "target": engine_kind,
                            "params": {
                                "ingestor": "default",
                                "index_builder": "default",
                                "retriever": "default",
                                "scheduler": "default",
                                "evolver": "default",
                                "lifecycle": "default",
                            },
                        }
                    },
                    "extractor": {
                        "default": {
                            "target": "entity_schema",
                            "params": {
                                "schema_path": str(schema_path),
                                "schema_validation_attempts": 1,
                                "extractor_retry_max": 1,
                                "extractor_retry_backoff": 0,
                            },
                        }
                    },
                    "evolver": {
                        "default": {
                            "target": "schema_orchestrating",
                            "params": {
                                "extractor": "default",
                                "llm": "default",
                            },
                        }
                    },
                }
            )
        self.api = self.kernel.api
        self.engine = wiring.instance
        self.index = wiring.dependencies.index_builder
        self.evolver = wiring.dependencies.evolver
        self.retriever = wiring.dependencies.retriever
        self.scope = Scope(org="issue208", user="alice")
        self.security = legacy_request_context(self.scope)

    def add(self, text):
        created = self.api.add(
            text, self.scope, security=self.security, system_metadata={"infer": True}
        )
        source = next(u for u in created if u.system_metadata.get("schema_source_evidence"))
        properties = [u for u in created if u.system_metadata.get("extraction_mode") == "schema"]
        # Stable historical intervals, independent of wall-clock execution date.
        for unit in [source, *properties]:
            unit.temporal.t_valid = T0
        self.index.update([source, *properties])
        return source, properties

    def update(self, source, patch):
        return self.api.update(source.id, self.scope, patch, security=self.security)

    def units(self):
        return [loads(raw) for _, raw in self.kernel.kv.scan(self.scope, "/memory/")]

    def get(self, uid, as_of=None):
        return self.api.get(uid, self.scope, as_of=as_of, security=self.security)

    def linked(self, name):
        records = self.entity_store.find_by_entity_text_hash(
            space_id_from_scope(self.scope),
            (hash_entity_text(name),),
            filters=EntityStoreFilters.from_scope(self.scope),
        )
        return {uid for record in records for uid in record.linked_memory_ids}

    def snapshot(self):
        return dict(self.kernel.kv.scan(self.scope, "/memory/"))

    def binding(self, name, index=None, evolver=None):
        return PipelineBinding(
            name, index or self.index, self.retriever, evolver or self.evolver
        )

    def route(self, bindings):
        def select(units):
            return bindings[units[0].system_metadata.get("message_type", "chat")]

        self.pipeline.select_for_write = select
