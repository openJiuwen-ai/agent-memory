"""Focused contracts for the opt-in Schema extraction extension."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwen_memory.api.memory_api_impl.schema_assembly import build_schema_kernel
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.type_def import (
    ChatMessage,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.config import Config
from jiuwen_memory.construction.entity_schema import EntitySchemaCatalog
from jiuwen_memory.construction.evolver import EvolverProducer
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import schema_entity_key
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaOrchestratingEvolver,
)
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import (
    EntitySchemaExtractor,
    SchemaExtractionNormalizer,
    SchemaPropertyCandidate,
    _parse_json_object,
)
from jiuwen_memory.construction.schema_bootstrap import register_schema_constructors

pytestmark = pytest.mark.unit


def _catalog() -> EntitySchemaCatalog:
    return EntitySchemaCatalog.from_data(
        [
            {
                "entity_type": "person",
                "dynamic_property": {
                    "default_property": {"desc": "fallback"},
                    "occupation": {"desc": "job"},
                    "birthday": {"desc": "birth date"},
                },
            }
        ]
    )


def _source(unit_id: str = "source-1") -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content="speaker=Alice: I became an engineer in 2023-08")],
        temporal=Temporal(),
        user_metadata={"dataset": "test"},
    )


def test_selected_schema_is_the_validation_allowlist() -> None:
    normalizer = SchemaExtractionNormalizer(_catalog())
    selected = [
        {
            "entity_type": "person",
            "dynamic_property": {
                "default_property": {"desc": "fallback"},
                "occupation": {"desc": "job"},
            },
        }
    ]
    normalized, errors = normalizer.normalize_and_validate(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "birthday",
                            "value": "Alice was born on 1990-01-01",
                            "time": "1990-01-01",
                        }
                    ],
                }
            ],
            "edges": [],
        },
        "2026-08-19",
        entity_schema=selected,
    )

    assert normalized["entities"][0]["properties"] == []
    assert any("was not selected" in error for error in errors)


def test_schema_json_parser_rejects_prose_wrapped_inner_object() -> None:
    assert _parse_json_object('```json\n{"entities": [], "edges": []}\n```')["entities"] == []
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_json_object('example: {"entities": [], "edges": []}')


def test_partial_event_time_uses_scalar_metadata_without_changing_temporal() -> None:
    extractor = EntitySchemaExtractor(llm=SimpleNamespace(), schema=_catalog())
    source = _source()
    units = extractor._build_units(
        [
            SchemaPropertyCandidate(
                entity_name="Alice",
                normalized_entity_name="alice",
                entity_type="person",
                entity_description="",
                aliases=[],
                property_name="occupation",
                value="Alice became an engineer in 2023-08",
                property_time="2023-08",
                property_operation="set",
                source_unit_ids=[source.id],
            )
        ],
        [source],
    )

    assert len(units) == 1
    assert units[0].temporal.t_event is None
    assert units[0].system_metadata["schema_event_time_precision"] == "month"
    assert units[0].system_metadata["schema_event_time_start"] == "2023-08-01T00:00:00+00:00"
    assert units[0].user_metadata == {"dataset": "test"}


def test_extractor_success_path_builds_one_property_unit_and_binds_speaker() -> None:
    response = json.dumps(
        {
            "message_mapping": {"0": ["User.occupation"]},
            "entities": [
                {
                    "name": "User",
                    "entity_type": "person",
                    "description": "the speaker",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "User became an engineer in 2023-08",
                            "time": "2023-08",
                            "operation": "set",
                            "source_unit_ids": ["source-1"],
                        }
                    ],
                }
            ],
            "edges": [],
        }
    )
    llm = SimpleNamespace(chat=lambda _messages, **_options: response)
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = extractor.extract([_source()])

    assert len(units) == 1
    assert units[0].content == "User became an engineer in 2023-08"
    assert units[0].entities == ["Alice"]
    assert units[0].system_metadata["schema_entity_name"] == "Alice"
    assert units[0].system_metadata["schema_entity_extracted_name"] == "User"
    assert units[0].provenance == ["source-1"]


class _Storage:
    def __init__(self) -> None:
        self.units: dict[str, MemoryUnit] = {}

    def get(self, _scope, unit_ids):
        return [self.units[unit_id] for unit_id in unit_ids if unit_id in self.units]

    def add(self, _scope, units):
        for unit in units:
            self.units[unit.id] = unit


class _Index:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def build(self, units):
        self.ids.extend(unit.id for unit in units)


def _evolver(extractor) -> tuple[SchemaOrchestratingEvolver, _Storage, _Index]:
    evolver = object.__new__(SchemaOrchestratingEvolver)
    storage = _Storage()
    index = _Index()
    evolver._storage = storage
    evolver._index = index
    evolver._extractor = extractor
    evolver._persist_and_maintain_messages = lambda _units: []
    evolver._maybe_collect_extract_context = lambda _units, _recent: None
    evolver._is_procedural = lambda _units: False
    evolver._annotate_layers = lambda _units: None
    return evolver, storage, index


def test_schema_failure_keeps_searchable_source_memory() -> None:
    class FailingExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            raise RuntimeError("three validation attempts exhausted")

    evolver, storage, index = _evolver(FailingExtractor())
    source = _source()
    result = evolver._evolve_extract([source])

    assert result.created_ids == [source.id]
    assert index.ids == [source.id]
    assert storage.units[source.id].system_metadata["schema_source_evidence"] is True
    assert storage.units[source.id].user_metadata == {"dataset": "test"}
    assert source.system_metadata == {}
    assert "RuntimeError" in evolver.last_schema_error


def test_schema_property_is_added_without_ordinary_dedup() -> None:
    source = _source()
    property_unit = MemoryUnit(
        id="property-1",
        scope=source.scope,
        tier=MemoryTier.CORE,
        segments=[Segment(content="Alice is an engineer")],
        system_metadata={"extraction_mode": "schema"},
    )

    class SuccessfulExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            return [property_unit]

    evolver, storage, index = _evolver(SuccessfulExtractor())
    result = evolver._evolve_extract([source])

    assert result.created_ids == [source.id, property_unit.id]
    assert set(storage.units) == {source.id, property_unit.id}
    assert index.ids == [source.id, property_unit.id]


def test_schema_bootstrap_registers_both_isolated_evolvers() -> None:
    register_schema_constructors()

    assert "schema_orchestrating" in EvolverProducer.known()
    assert "schema_dynamic" in EvolverProducer.known()


class _StaticSchemaLLM(LLM):
    def __init__(self, response: str) -> None:
        self.response = response

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del options
        match = re.search(r"unit_id=([^\]\r\n]+)", messages[-1].content)
        source_id = match.group(1).strip() if match else ""
        return self.response.replace("__SOURCE_ID__", source_id)


def test_schema_assembly_runs_source_identity_registry_and_property_end_to_end() -> None:
    target = "schema_extension_test_static"
    if target not in LlmProducer.known():
        LlmProducer.register(target)(lambda config: _StaticSchemaLLM(config.get("response")))
    response = json.dumps(
        {
            "message_mapping": {"0": ["User.position_event"]},
            "entities": [
                {
                    "name": "User",
                    "entity_type": "user",
                    "description": "the speaker",
                    "properties": [
                        {
                            "property_name": "position_event",
                            "value": "On 2023-08-03, Alice became a software engineer",
                            "time": "2023-08-03",
                            "operation": "set",
                            "source_unit_ids": ["__SOURCE_ID__"],
                        }
                    ],
                }
            ],
            "edges": [],
        }
    )
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "persona.json"
    )
    kernel = build_schema_kernel(
        config=Config.from_dict(
            {
                "globals": {
                    "vector_enabled": True,
                    "graph_enabled": False,
                    "rerank_enabled": False,
                },
                "llm": {
                    "default": {
                        "target": target,
                        "params": {"response": response},
                    }
                },
                "vector_store": {"schema_entities": "memory"},
                "fulltext_store": {
                    "schema_entities": {
                        "target": "memory",
                        "params": {"tokenizer": "default"},
                    }
                },
                "storage": {
                    "default": {
                        "target": "schema_composite",
                        "params": {
                            "kv_store": "default",
                            "vector_store": "default",
                            "fulltext_store": "default",
                            "graph_store": "default",
                        },
                    }
                },
                "extractor": {
                    "default": {
                        "target": "entity_schema",
                        "params": {
                            "schema_path": str(schema_path),
                            "llm": "default",
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
                            "use_property_merge": False,
                        },
                    }
                },
            }
        )
    )
    scope = Scope(org="schema-e2e", user="alice")

    created = kernel.api.add(
        "speaker=Alice: On 2023-08-03, I became a software engineer.",
        scope,
        identity=scope,
        system_metadata={"infer": True},
    )

    assert len(created) == 2
    source = next(unit for unit in created if unit.system_metadata.get("memory_role"))
    property_unit = next(
        unit for unit in created if unit.system_metadata.get("extraction_mode") == "schema"
    )
    assert source.system_metadata["schema_source_evidence"] is True
    assert property_unit.content == "On 2023-08-03, Alice became a software engineer"
    entity_id = str(property_unit.system_metadata["schema_entity_id"])
    entity = loads(kernel.storage.kv.get(scope, schema_entity_key(entity_id)))
    assert entity is not None
    assert entity.system_metadata["schema_entity_name"] == "Alice"
    assert kernel.storage.vector_port("schema_entities").get(scope, [entity_id])
    assert kernel.storage.fulltext_port("schema_entities").get(scope, [entity_id])
