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
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.construction.entity_schema import EntitySchemaCatalog
from jiuwen_memory.construction.evolver import EvolveMode, EvolverProducer
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import schema_entity_key
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaOrchestratingEvolver,
)
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import (
    EntitySchemaExtractor,
    SchemaExtractionNormalizer,
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


class _ConstantResponseLLM(LLM):
    def __init__(self, response: str) -> None:
        self.response = response

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del messages, options
        return self.response


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
    source = _source()
    response = json.dumps(
        {
            "message_mapping": {"0": ["Alice.occupation"]},
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "Alice became an engineer in 2023-08",
                            "time": "2023-08",
                            "operation": "set",
                            "source_unit_ids": [source.id],
                        }
                    ],
                }
            ],
            "edges": [],
        }
    )
    llm = _ConstantResponseLLM(response)
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = [
        unit
        for unit in extractor.extract([source])
        if unit.system_metadata.get("extraction_mode") == "schema"
    ]

    assert len(units) == 1
    assert units[0].temporal.t_event is None
    assert units[0].system_metadata["schema_event_time_precision"] == "month"
    assert units[0].system_metadata["schema_event_time_start"] == "2023-08-01T00:00:00+00:00"
    assert units[0].user_metadata == {"dataset": "test"}


def test_schema_derivations_do_not_inherit_prior_schema_internal_metadata() -> None:
    source = _source()
    source.system_metadata = {
        "pipeline": "schema-regression",
        "target": "old-target",
        "extracted_statement": "old statement",
        "extraction_mode": "schema",
        "schema_entity_id": "old-entity",
        "schema_event_time": "2020",
        "schema_event_time_precision": "year",
        "schema_event_time_start": "2020-01-01T00:00:00+00:00",
        "schema_event_time_end": "2021-01-01T00:00:00+00:00",
        "schema_property_time": "2020",
        "property_merge_action": "update",
    }
    response = json.dumps(
        {
            "message_mapping": {
                "0": ["Alice.occupation", "Bob.default_property"],
            },
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "Alice is an engineer",
                            "time": "",
                            "operation": "set",
                            "source_unit_ids": [source.id],
                        }
                    ],
                },
                {
                    "name": "Bob",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "default_property",
                            "value": "Bob knows Alice",
                            "time": "",
                            "operation": "set",
                            "source_unit_ids": [source.id],
                        }
                    ],
                },
                {
                    "name": "Carol",
                    "entity_type": "person",
                    "description": "Alice's colleague",
                    "properties": [],
                },
            ],
            "edges": [
                {
                    "link_entity1_name": "Alice",
                    "link_entity2_name": "Bob",
                    "link_description": "Alice knows Bob",
                    "time": "",
                }
            ],
        }
    )
    extractor = EntitySchemaExtractor(llm=_ConstantResponseLLM(response), schema=_catalog())

    units = extractor.extract([source])

    assert len(units) == 4
    assert {str(unit.system_metadata["extraction_mode"]) for unit in units} == {
        "schema",
        "schema_entity_observation",
        "schema_relation",
    }
    for unit in units:
        assert unit.system_metadata["pipeline"] == "schema-regression"
        assert "schema_entity_id" not in unit.system_metadata
        assert "schema_event_time" not in unit.system_metadata
        assert "schema_event_time_precision" not in unit.system_metadata
        assert "schema_event_time_start" not in unit.system_metadata
        assert "schema_event_time_end" not in unit.system_metadata
        assert "schema_property_time" not in unit.system_metadata
        assert "property_merge_action" not in unit.system_metadata


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
    llm = _ConstantResponseLLM(response)
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
        self.kv = SimpleNamespace()

    @staticmethod
    def has_graph() -> bool:
        return False

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


class _SchemaEvolverHarness(SchemaOrchestratingEvolver):
    def _persist_and_maintain_messages(self, _units):
        return []

    def _maybe_collect_extract_context(self, _units, _recent):
        return None

    @staticmethod
    def _is_procedural(_units):
        return False

    @staticmethod
    def _annotate_layers(_units):
        return None


def _evolver(
    extractor,
    *,
    resolver=None,
    registry=None,
) -> tuple[SchemaOrchestratingEvolver, _Storage, _Index]:
    storage = _Storage()
    index = _Index()
    evolver = _SchemaEvolverHarness(
        extractor=extractor,
        abstractor=SimpleNamespace(),
        associator=SimpleNamespace(),
        index_builder=index,
        storage=storage,
        dedup=SimpleNamespace(),
        llm=SimpleNamespace(),
        schema_entity_resolver=resolver,
        schema_entity_registry=registry,
    )
    return evolver, storage, index


def test_schema_failure_keeps_searchable_source_memory() -> None:
    class FailingExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            raise RuntimeError("three validation attempts exhausted")

    evolver, storage, index = _evolver(FailingExtractor())
    source = _source()
    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id]
    assert index.ids == [source.id]
    assert storage.units[source.id].system_metadata["schema_source_evidence"] is True
    assert storage.units[source.id].user_metadata == {"dataset": "test"}
    assert source.system_metadata == {}
    assert "RuntimeError" in evolver.last_schema_error


def test_entity_resolution_failure_keeps_only_source_and_skips_registry() -> None:
    source = _source()
    property_unit = MemoryUnit(
        id="property-unresolved",
        scope=source.scope,
        tier=MemoryTier.CORE,
        segments=[Segment(content="Alice is an engineer")],
        system_metadata={
            "extraction_mode": "schema",
            "schema_entity_key": "provisional-entity",
        },
    )
    observation = MemoryUnit(
        id="entity-observation:provisional-entity",
        scope=source.scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="Alice")],
        system_metadata={
            "extraction_mode": "schema_entity_observation",
            "schema_entity_key": "provisional-entity",
        },
    )

    class SuccessfulExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            return [property_unit, observation]

    class FailingResolver:
        @staticmethod
        def resolve(_units):
            raise RuntimeError("entity backend unavailable")

    class TrackingRegistry:
        def __init__(self) -> None:
            self.sync_calls = 0

        def sync(self, _units) -> None:
            self.sync_calls += 1

    registry = TrackingRegistry()
    evolver, storage, index = _evolver(
        SuccessfulExtractor(),
        resolver=FailingResolver(),
        registry=registry,
    )

    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id]
    assert set(storage.units) == {source.id}
    assert index.ids == [source.id]
    assert registry.sync_calls == 0
    assert "entity backend unavailable" in evolver.last_schema_error


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
    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id, property_unit.id]
    assert set(storage.units) == {source.id, property_unit.id}
    assert index.ids == [source.id, property_unit.id]


def test_schema_bootstrap_registers_isolated_evolver() -> None:
    register_schema_constructors()

    assert "schema_orchestrating" in EvolverProducer.known()
    assert "schema_dynamic" not in EvolverProducer.known()


def test_schema_targets_are_disabled_in_default_config() -> None:
    defaults = default_config_dict()

    assert defaults["extractor"]["default"]["target"] == "dynamic_llm"
    assert defaults["evolver"]["default"]["target"] == "orchestrating"
    assert defaults["storage"]["default"]["target"] == "composite"


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


def _build_static_schema_llm(config) -> _StaticSchemaLLM:
    return _StaticSchemaLLM(config.get("response"))


def test_schema_assembly_runs_source_identity_registry_and_property_end_to_end() -> None:
    target = "schema_extension_test_static"
    if target not in LlmProducer.known():
        LlmProducer.register(target)(_build_static_schema_llm)
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
    schema_path = Path(__file__).resolve().parents[3] / "examples" / "persona.json"
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
