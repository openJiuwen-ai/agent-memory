# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic Schema update world with real API, engines and index builders."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import EntityStoreFilters, hash_entity_text
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.construction.entity_schema import EntitySchemaCatalog
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import EntitySchemaExtractor
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import (
    EntityIndexBuilder,
    EntityLinkService,
)
from tests.unit.construction.test_entity_linker import InMemoryEntityStore

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2025, 1, 1, tzinfo=timezone.utc)


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
        self.engine = self.api._engine
        self.index = self.engine._index
        self.evolver = self.engine._evolver
        self.llm = SchemaReplyLLM()
        self.evolver._extractor = EntitySchemaExtractor(
            self.llm,
            EntitySchemaCatalog.from_data(schema),
            validation_attempts=1,
            retry_max_retries=1,
            retry_backoff_ms=0,
        )
        self.entity_store = InMemoryEntityStore()
        self.index._entity_builder = EntityIndexBuilder(
            EntityLinkService(
                entity_store=self.entity_store,
            )
        )
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
        from jiuwen_memory.control.pipeline import PipelineBinding

        return PipelineBinding(
            name, index or self.index, self.engine._retriever, evolver or self.evolver
        )

    def route(self, bindings):
        def select(units):
            return bindings[units[0].system_metadata.get("message_type", "chat")]

        self.engine._pipeline = SimpleNamespace(select_for_write=select)
